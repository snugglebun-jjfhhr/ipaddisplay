import Foundation
import Combine   // ObservableObject / @Published live here
import Darwin
import CoreMedia // CMTime (DecodedFrame.pts) bridging for the renderer hand-off

// M0: the iPad link listener. A BSD-socket TCP server on 0.0.0.0:7000 that the
// Windows host drives over usbmux. It speaks the canonical ipaddisplay wire
// protocol (see docs/protocol.md / host/protocol.h): a 24-byte LITTLE-ENDIAN
// MsgHeader followed by `length` payload bytes, dispatched by MessageType.
//
// This milestone implements the handshake + keepalive fully:
//   HELLO          (H->D) -> reply DEVICE_INFO
//   STREAM_CONFIG  (H->D) -> store + reply STREAM_CONFIG_ACK{ok=1}
//   PING           (both) -> reply PONG echoing the payload
//   TEARDOWN       (both) -> close the connection
//
// M5 adds live video: VIDEO_PARAM_SETS (0x10) and VIDEO_FRAME (0x11) are parsed
// and driven into a VideoDecoder, whose decoded NV12 CVPixelBuffers are handed to
// the renderer (a VideoFrameSink, the MetalDisplayView) for M6 display.
// SET_LAYER / STILL_* (M7/M8) remain recognised, counted, and drained.

// MARK: - Wire protocol constants

enum MessageType: UInt8 {
    case hello           = 0x01 // H->D
    case deviceInfo      = 0x02 // D->H
    case streamConfig    = 0x03 // H->D
    case streamConfigAck = 0x04 // D->H
    case teardown        = 0x05 // both
    case ping            = 0x06 // both
    case pong            = 0x07 // both
    case framePresented  = 0x08 // D->H
    case error           = 0x09 // both
    case videoParamSets  = 0x10 // H->D
    case videoFrame      = 0x11 // H->D
    case setLayer        = 0x20 // H->D
    case stillBegin      = 0x21 // H->D
    case stillTile       = 0x22 // H->D
    case stillCommit     = 0x23 // H->D
}

private enum Wire {
    static let headerSize = 24
    static let magic: [UInt8] = Array("IPDP".utf8) // 0x49,0x50,0x44,0x50
    static let protoVersion: UInt16 = 1
    static let codecH264: UInt32 = 1 << 0
    static let codecHEVC: UInt32 = 1 << 1
    // Defensive cap so a bogus length can't make us allocate unbounded memory.
    // The largest M0-M2 payload is 16 bytes; PING payloads are tiny. 16 MiB is
    // generous headroom for future video frames without being a DoS vector.
    static let maxPayload = 16 * 1024 * 1024
}

// MARK: - Decoded header

private struct MsgHeader {
    var type: UInt8
    var flags: UInt8
    var seq: UInt32
    var length: UInt32
    var timestampUs: UInt64
}

// MARK: - LinkServer

final class LinkServer: ObservableObject {
    // Handshake / connection state surfaced to the UI.
    enum Phase: String {
        case starting      = "starting…"
        case listening     = "listening on :7000"
        case connected     = "client connected"
        case helloReceived = "HELLO received"
        case configured    = "stream configured"
        case disconnected  = "client disconnected"
    }

    struct NegotiatedConfig: Equatable {
        var width: UInt16
        var height: UInt16
        var fpsCap: UInt8
        var codec: UInt8
        var chroma: UInt8
        var bitDepth: UInt8
        var fullRange: UInt8
        var colorPrimaries: UInt8
        var transfer: UInt8
        var matrix: UInt8

        var codecName: String { codec == 0 ? "H.264" : "HEVC" }
        var chromaName: String { chroma == 0 ? "4:2:0" : "\(chroma)" }
        var rangeName: String { fullRange == 1 ? "full" : "video" }
        // Reconciled M5 contract: 1=BT.709 primaries, 13=sRGB transfer, 1=BT.709 matrix.
        var colorName: String { "prim=\(colorPrimaries) transfer=\(transfer) matrix=\(matrix)" }
        var resolution: String { "\(width)×\(height)" }
    }

    @Published var phase: Phase = .starting
    @Published var detail: String = ""
    @Published var config: NegotiatedConfig? = nil
    @Published var pings: UInt64 = 0
    @Published var stubbed: UInt64 = 0   // count of set-layer/still messages dropped (video is live in M5)
    @Published var isStreaming: Bool = false   // true once decoded frames are flowing
    @Published var framesDecoded: UInt64 = 0
    @Published var framesPresented: UInt64 = 0  // present() calls that actually hit a non-nil sink
    @Published var drew: UInt64 = 0             // render committed a draw (code 1)
    @Published var noDrawable: UInt64 = 0       // nextDrawable() returned nil (code 4)
    @Published var texFail: UInt64 = 0          // makeTexture returned nil (code 3)

    /// The renderer sink (a MetalDisplayView). Set by the UI before `start()`.
    /// Held weakly: SwiftUI owns the view's lifetime.
    weak var frameSink: VideoFrameSink?

    private let decoder = VideoDecoder()
    private let port: UInt16
    private let queue = DispatchQueue(label: "linkserver", qos: .userInitiated)

    init(port: UInt16 = 7000) {
        self.port = port
        // Bridge decoded NV12 frames to the renderer. Fires on a VideoToolbox queue;
        // MetalDisplayView.present(_:) is thread-safe (it latches under a lock).
        decoder.onDecodedFrame = { [weak self] frame in
            guard let self else { return }
            // DecodedFrame.pts was built as CMTime(value: Int64(bitPattern: timestampUs),
            // timescale: 1_000_000); recover the original microsecond stamp exactly.
            let ts = UInt64(bitPattern: frame.pts.value)
            if let sink = self.frameSink {
                sink.present(VideoFrame(pixelBuffer: frame.pixelBuffer, timestampUs: ts))
                self.bumpPresented()
            }
            self.markFrameDecoded()
        }
    }

    func start() { queue.async { [weak self] in self?.runServer() } }

    // MARK: UI publishing

    private func set(_ p: Phase, _ d: String = "") {
        DispatchQueue.main.async { self.phase = p; self.detail = d }
    }
    private func publishConfig(_ c: NegotiatedConfig) {
        DispatchQueue.main.async { self.config = c }
    }
    private func bumpPings() {
        DispatchQueue.main.async { self.pings &+= 1 }
    }
    private func bumpStubbed() {
        DispatchQueue.main.async { self.stubbed &+= 1 }
    }
    private func markFrameDecoded() {
        DispatchQueue.main.async {
            self.framesDecoded &+= 1
            if !self.isStreaming { self.isStreaming = true }
        }
    }
    private func bumpPresented() {
        DispatchQueue.main.async { self.framesPresented &+= 1 }
    }
    /// Called by the view from its render tick (main thread) with the render code.
    func reportRender(_ code: Int) {
        switch code {
        case 1: drew &+= 1
        case 3: texFail &+= 1
        case 4: noDrawable &+= 1
        default: break
        }
    }
    private func endStreaming() {
        DispatchQueue.main.async { self.isStreaming = false }
    }

    // MARK: Listener

    private func runServer() {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { set(.starting, "socket() failed"); return }
        var yes: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout<Int32>.size))

        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_addr.s_addr = INADDR_ANY            // 0.0.0.0; usbmux delivers via loopback
        addr.sin_port = port.bigEndian               // network byte order for the port field

        let bindRes = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bindRes == 0 else { set(.starting, "bind() failed errno=\(errno)"); close(fd); return }
        guard listen(fd, 1) == 0 else { set(.starting, "listen() failed errno=\(errno)"); close(fd); return }
        set(.listening, "run host/handshake_host.py")

        while true {
            let cfd = accept(fd, nil, nil)
            if cfd < 0 { continue }
            var one: Int32 = 1
            setsockopt(cfd, Int32(IPPROTO_TCP), TCP_NODELAY, &one, socklen_t(MemoryLayout<Int32>.size))
            set(.connected)
            handleClient(cfd)
            decoder.reset()      // drop session; host resends param sets + IDR on reconnect
            endStreaming()
            close(cfd)
            set(.disconnected, "listening on :\(port)")
        }
    }

    // MARK: Blocking IO (length-bounded, mirrors PerfServer)

    private func recvFully(_ fd: Int32, _ buf: UnsafeMutableRawPointer, _ count: Int) -> Bool {
        var got = 0
        while got < count {
            let n = recv(fd, buf + got, count - got, 0)
            if n <= 0 { return false }
            got += n
        }
        return true
    }

    private func sendFully(_ fd: Int32, _ buf: UnsafeRawPointer, _ count: Int) -> Bool {
        var sent = 0
        while sent < count {
            let n = send(fd, buf + sent, count - sent, 0)
            if n <= 0 { return false }
            sent += n
        }
        return true
    }

    // Drain and discard `count` bytes (for stubbed message payloads).
    private func drain(_ fd: Int32, _ count: Int) -> Bool {
        guard count > 0 else { return true }
        let chunkSize = min(count, 64 * 1024)
        let scratch = UnsafeMutableRawPointer.allocate(byteCount: chunkSize, alignment: 16)
        defer { scratch.deallocate() }
        var remaining = count
        while remaining > 0 {
            let n = recv(fd, scratch, min(chunkSize, remaining), 0)
            if n <= 0 { return false }
            remaining -= n
        }
        return true
    }

    // MARK: Little-endian readers

    // Reads `length` payload bytes into a fresh [UInt8]. Length is bounded.
    private func readPayload(_ fd: Int32, _ length: Int) -> [UInt8]? {
        if length == 0 { return [] }
        if length < 0 || length > Wire.maxPayload { return nil }
        var bytes = [UInt8](repeating: 0, count: length)
        let ok = bytes.withUnsafeMutableBytes { recvFully(fd, $0.baseAddress!, length) }
        return ok ? bytes : nil
    }

    // Reads + decodes the 24-byte little-endian MsgHeader.
    private func readHeader(_ fd: Int32) -> MsgHeader? {
        var raw = [UInt8](repeating: 0, count: Wire.headerSize)
        let ok = raw.withUnsafeMutableBytes { recvFully(fd, $0.baseAddress!, Wire.headerSize) }
        guard ok else { return nil }
        // offset 0 u8 type | 1 u8 flags | 2 u16 _pad | 4 u32 seq |
        // 8 u32 length | 12 u32 _pad2 | 16 u64 timestampUs  (all little-endian)
        return MsgHeader(
            type: raw[0],
            flags: raw[1],
            seq: leU32(raw, 4),
            length: leU32(raw, 8),
            timestampUs: leU64(raw, 16)
        )
    }

    private func leU16(_ b: [UInt8], _ o: Int) -> UInt16 {
        UInt16(b[o]) | (UInt16(b[o + 1]) << 8)
    }
    private func leU32(_ b: [UInt8], _ o: Int) -> UInt32 {
        UInt32(b[o]) | (UInt32(b[o + 1]) << 8) | (UInt32(b[o + 2]) << 16) | (UInt32(b[o + 3]) << 24)
    }
    private func leU64(_ b: [UInt8], _ o: Int) -> UInt64 {
        var v: UInt64 = 0
        for i in 0..<8 { v |= UInt64(b[o + i]) << (8 * i) }
        return v
    }

    // MARK: Little-endian writers

    private func putU16(_ buf: inout [UInt8], _ v: UInt16) {
        buf.append(UInt8(v & 0xFF))
        buf.append(UInt8((v >> 8) & 0xFF))
    }
    private func putU32(_ buf: inout [UInt8], _ v: UInt32) {
        for i in 0..<4 { buf.append(UInt8((v >> (8 * UInt32(i))) & 0xFF)) }
    }
    private func putU64(_ buf: inout [UInt8], _ v: UInt64) {
        for i in 0..<8 { buf.append(UInt8((v >> (8 * UInt64(i))) & 0xFF)) }
    }

    // Build a 24-byte little-endian header. Asserts the size for self-check.
    private func makeHeader(type: MessageType, seq: UInt32, length: UInt32) -> [UInt8] {
        var h = [UInt8]()
        h.reserveCapacity(Wire.headerSize)
        h.append(type.rawValue)          // offset 0  : type
        h.append(0)                       // offset 1  : flags
        putU16(&h, 0)                     // offset 2  : _pad = 0
        putU32(&h, seq)                   // offset 4  : seq
        putU32(&h, length)                // offset 8  : length
        putU32(&h, 0)                     // offset 12 : _pad2 = 0
        putU64(&h, nowMicros())           // offset 16 : timestampUs
        assert(h.count == Wire.headerSize, "MsgHeader must be 24 bytes")
        return h
    }

    private func nowMicros() -> UInt64 {
        UInt64(Date().timeIntervalSince1970 * 1_000_000)
    }

    // Sends a full message: header + payload, in one shot.
    @discardableResult
    private func sendMessage(_ fd: Int32, type: MessageType, seq: UInt32, payload: [UInt8]) -> Bool {
        var frame = makeHeader(type: type, seq: seq, length: UInt32(payload.count))
        frame.append(contentsOf: payload)
        return frame.withUnsafeBytes { sendFully(fd, $0.baseAddress!, frame.count) }
    }

    // MARK: Payload builders

    // DEVICE_INFO (16 bytes): nativeW=2732,nativeH=2048,maxDecode=2732x2048,
    // decoderMask=H264|HEVC, maxRefreshHz=120, supportsP3=1, hwHEVC=1, _pad=0.
    private func deviceInfoPayload() -> [UInt8] {
        var p = [UInt8]()
        p.reserveCapacity(16)
        putU16(&p, 2732)                              // offset 0  : nativeW
        putU16(&p, 2048)                              // offset 2  : nativeH
        putU16(&p, 2732)                              // offset 4  : maxDecodeW
        putU16(&p, 2048)                              // offset 6  : maxDecodeH
        putU32(&p, Wire.codecH264 | Wire.codecHEVC)  // offset 8  : decoderMask
        p.append(120)                                 // offset 12 : maxRefreshHz
        p.append(1)                                   // offset 13 : supportsP3
        p.append(1)                                   // offset 14 : hwHEVC
        p.append(0)                                   // offset 15 : _pad
        assert(p.count == 16, "DEVICE_INFO must be 16 bytes")
        return p
    }

    // STREAM_CONFIG_ACK (2 bytes): ok, reason.
    private func streamConfigAckPayload(ok: UInt8, reason: UInt8) -> [UInt8] {
        let p: [UInt8] = [ok, reason]
        assert(p.count == 2, "STREAM_CONFIG_ACK must be 2 bytes")
        return p
    }

    // Decode STREAM_CONFIG (12 bytes) into the published config.
    private func decodeStreamConfig(_ b: [UInt8]) -> NegotiatedConfig? {
        guard b.count == 12 else { return nil }
        return NegotiatedConfig(
            width: leU16(b, 0),           // offset 0  : W
            height: leU16(b, 2),          // offset 2  : H
            fpsCap: b[4],                 // offset 4  : fpsCap
            codec: b[5],                  // offset 5  : codec
            chroma: b[6],                 // offset 6  : chroma
            bitDepth: b[7],               // offset 7  : bitDepth
            fullRange: b[8],              // offset 8  : fullRange
            colorPrimaries: b[9],         // offset 9  : colorPrimaries
            transfer: b[10],              // offset 10 : transfer
            matrix: b[11]                 // offset 11 : matrix
        )
    }

    // MARK: VIDEO_PARAM_SETS parsing

    // VIDEO_PARAM_SETS payload: u8 count, then `count` records of
    // [u16 BIG-ENDIAN nalLen][nalLen raw NAL bytes] (no start code). The u16
    // length prefixes are big-endian (AVCC/network order) — the one deliberate
    // exception to the otherwise little-endian wire (see protocol.md). Returns the
    // raw SPS/PPS NALs (SPS first) for VideoDecoder.setParameterSets, or nil on a
    // truncated/inconsistent payload.
    private func parseParamSets(_ p: [UInt8]) -> [[UInt8]]? {
        guard p.count >= 1 else { return nil }
        let count = Int(p[0])
        var idx = 1
        var sets: [[UInt8]] = []
        sets.reserveCapacity(count)
        for _ in 0..<count {
            guard idx + 2 <= p.count else { return nil }
            let len = (Int(p[idx]) << 8) | Int(p[idx + 1])   // BIG-ENDIAN
            idx += 2
            guard len > 0, idx + len <= p.count else { return nil }
            sets.append(Array(p[idx..<idx + len]))
            idx += len
        }
        return sets.isEmpty ? nil : sets
    }

    // MARK: Dispatch loop

    private func handleClient(_ fd: Int32) {
        var seqOut: UInt32 = 0

        while true {
            guard let hdr = readHeader(fd) else { return }
            let length = Int(hdr.length)
            if length < 0 || length > Wire.maxPayload { return }

            guard let mtype = MessageType(rawValue: hdr.type) else {
                // Unknown type: drain its payload and keep going.
                if !drain(fd, length) { return }
                continue
            }

            switch mtype {
            case .hello:
                guard let payload = readPayload(fd, length), payload.count >= 16 else { return }
                // Validate magic 'IPDP' at offset 0.
                let magicOK = Array(payload[0..<4]) == Wire.magic
                set(.helloReceived, magicOK ? "" : "bad HELLO magic")
                guard magicOK else { return }
                seqOut &+= 1
                if !sendMessage(fd, type: .deviceInfo, seq: seqOut, payload: deviceInfoPayload()) { return }

            case .streamConfig:
                guard let payload = readPayload(fd, length),
                      let cfg = decodeStreamConfig(payload) else { return }
                publishConfig(cfg)
                set(.configured, "\(cfg.resolution) \(cfg.codecName)")
                seqOut &+= 1
                if !sendMessage(fd, type: .streamConfigAck, seq: seqOut,
                                payload: streamConfigAckPayload(ok: 1, reason: 0)) { return }

            case .ping:
                // Echo the opaque payload back as PONG.
                guard let payload = readPayload(fd, length) else { return }
                seqOut &+= 1
                if !sendMessage(fd, type: .pong, seq: seqOut, payload: payload) { return }
                bumpPings()

            case .teardown:
                _ = readPayload(fd, length)
                return

            case .videoParamSets:
                // u8 count, then count * (u16 BE nalLen + nalLen raw bytes); SPS then PPS.
                guard let payload = readPayload(fd, length) else { return }
                if let sets = parseParamSets(payload) {
                    decoder.setParameterSets(sets)
                } else {
                    NSLog("[LinkServer] malformed VIDEO_PARAM_SETS (\(payload.count) bytes)")
                }

            case .videoFrame:
                // Payload IS a ready-to-decode AVCC access unit (u32 BE length prefixes);
                // hand it to the decoder verbatim. flags bit0=IDR; pts from the header.
                guard let payload = readPayload(fd, length) else { return }
                decoder.decode(au: payload, flags: hdr.flags, timestampUs: hdr.timestampUs)
                // M5 cannot ask the host for a forced IDR (it force-sends one on connect);
                // only log if we are still keyframe-starved after feeding a non-IDR AU.
                if (hdr.flags & 0x01) == 0 && decoder.needsKeyframe {
                    NSLog("[LinkServer] decoder awaiting IDR; dropping until keyframe")
                }

            case .setLayer, .stillBegin, .stillTile, .stillCommit:
                // M7/M8 types: drain payload, count it, keep the link alive.
                if !drain(fd, length) { return }
                bumpStubbed()

            case .deviceInfo, .streamConfigAck, .pong, .framePresented, .error:
                // Device-originated or host-side replies; not expected inbound. Drain.
                if !drain(fd, length) { return }
            }
        }
    }
}

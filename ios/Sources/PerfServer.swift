import Foundation
import Darwin

// Stage B: a tiny TCP perf server the Windows host drives over usbmux to measure
// REAL socket throughput + round-trip latency (vs the AFC proxy in spike #1).
//
// Wire protocol (host drives; all ints big-endian):
//   'D' + uint64 L          host->device throughput: device reads L bytes, replies 1 byte 0x06 (ACK)
//   'U' + uint64 L          device->host throughput: device sends L zero bytes
//   'P' + uint32 P + Pbytes ping: device echoes the P bytes back immediately
//   'Q'                     quit (close connection)
final class PerfServer: ObservableObject {
    @Published var status: String = "starting…"
    @Published var bytesReceived: UInt64 = 0
    @Published var bytesSent: UInt64 = 0
    @Published var pings: UInt64 = 0

    private let port: UInt16
    private let queue = DispatchQueue(label: "perfserver", qos: .userInitiated)

    init(port: UInt16 = 7000) { self.port = port }

    func start() { queue.async { [weak self] in self?.runServer() } }

    private func set(_ s: String) { DispatchQueue.main.async { self.status = s } }

    private func publish(recv: UInt64, sent: UInt64, pings: UInt64) {
        DispatchQueue.main.async {
            self.bytesReceived = recv
            self.bytesSent = sent
            self.pings = pings
        }
    }

    private func runServer() {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { set("socket() failed"); return }
        var yes: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout<Int32>.size))

        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_addr.s_addr = INADDR_ANY            // 0.0.0.0; usbmux delivers via loopback
        addr.sin_port = port.bigEndian

        let bindRes = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bindRes == 0 else { set("bind() failed errno=\(errno)"); close(fd); return }
        guard listen(fd, 1) == 0 else { set("listen() failed errno=\(errno)"); close(fd); return }
        set("listening on :\(port) — run perf_host.py on Windows")

        while true {
            let cfd = accept(fd, nil, nil)
            if cfd < 0 { continue }
            var one: Int32 = 1
            setsockopt(cfd, Int32(IPPROTO_TCP), TCP_NODELAY, &one, socklen_t(MemoryLayout<Int32>.size))
            set("client connected")
            handleClient(cfd)
            close(cfd)
            set("client disconnected — listening on :\(port)")
        }
    }

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

    private func handleClient(_ fd: Int32) {
        let bufSize = 256 * 1024
        let buf = UnsafeMutableRawPointer.allocate(byteCount: bufSize, alignment: 16)
        defer { buf.deallocate() }
        memset(buf, 0, bufSize)

        var recvTotal: UInt64 = 0
        var sentTotal: UInt64 = 0
        var pingCount: UInt64 = 0

        while true {
            var cmd: UInt8 = 0
            if !recvFully(fd, &cmd, 1) { return }

            switch cmd {
            case 0x44: // 'D' host->device throughput
                var lenBE: UInt64 = 0
                if !recvFully(fd, &lenBE, 8) { return }
                var remaining = UInt64(bigEndian: lenBE)
                while remaining > 0 {
                    let chunk = Int(min(UInt64(bufSize), remaining))
                    if !recvFully(fd, buf, chunk) { return }
                    remaining -= UInt64(chunk)
                    recvTotal += UInt64(chunk)
                }
                var ack: UInt8 = 0x06
                if !sendFully(fd, &ack, 1) { return }
                publish(recv: recvTotal, sent: sentTotal, pings: pingCount)

            case 0x55: // 'U' device->host throughput
                var lenBE: UInt64 = 0
                if !recvFully(fd, &lenBE, 8) { return }
                var remaining = UInt64(bigEndian: lenBE)
                while remaining > 0 {
                    let chunk = Int(min(UInt64(bufSize), remaining))
                    if !sendFully(fd, buf, chunk) { return }
                    remaining -= UInt64(chunk)
                    sentTotal += UInt64(chunk)
                }
                publish(recv: recvTotal, sent: sentTotal, pings: pingCount)

            case 0x50: // 'P' ping echo
                var plenBE: UInt32 = 0
                if !recvFully(fd, &plenBE, 4) { return }
                let plen = Int(UInt32(bigEndian: plenBE))
                if plen <= 0 || plen > bufSize { return }
                if !recvFully(fd, buf, plen) { return }
                if !sendFully(fd, buf, plen) { return }
                pingCount += 1
                if pingCount % 50 == 0 { publish(recv: recvTotal, sent: sentTotal, pings: pingCount) }

            case 0x51: // 'Q' quit
                return
            default:
                return
            }
        }
    }
}

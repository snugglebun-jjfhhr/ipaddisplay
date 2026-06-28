import Foundation
import VideoToolbox
import CoreMedia
import CoreVideo

// M5: the iPad H.264 hardware decoder.
//
// Consumes the M5 video wire messages already parsed by LinkServer:
//   VIDEO_PARAM_SETS (0x10) -> setParameterSets([SPS, PPS])   (raw NALs, no start code)
//   VIDEO_FRAME      (0x11) -> decode(au:flags:timestampUs:)  (one AVCC access unit)
//
// The VIDEO_FRAME payload is ALREADY in AVCC form: a back-to-back sequence of
// [u32 BE nalLen][nalBytes] records (the host did the Annex-B -> AVCC conversion).
// That is precisely the layout VideoToolbox wants, so we wrap the payload bytes
// straight into a CMBlockBuffer and decode with nalUnitHeaderLength == 4 -- no
// byte rewriting on device. The u32 length prefixes are big-endian (network /
// AVCC order); that is the single deliberate exception to the otherwise
// little-endian wire and is exactly what CoreMedia expects, so we never touch them.
//
// Pipeline: SPS/PPS -> CMVideoFormatDescription -> VTDecompressionSession
// (hardware-required, real-time) -> CVPixelBuffer (NV12 4:2:0 8-bit FULL RANGE,
// IOSurface- and Metal-compatible) -> DecodedFrame -> onDecodedFrame closure.
//
// This class does NO rendering. It only produces DecodedFrame. The renderer reads
// the YCbCr matrix / primaries / transfer / CGColorSpace off the CVPixelBuffer's
// color attachments (VideoToolbox propagates them from the SPS VUI).
//
// Threading contract: decode(au:...), setParameterSets(...), reset() and
// invalidate() are NOT internally serialized against each other -- LinkServer
// drives them from its single serial `linkserver` dispatch queue, so they are
// already serialized by the caller. onDecodedFrame fires asynchronously on a
// VideoToolbox-owned queue (NOT the caller's thread); the renderer must hop to
// its own queue / main as needed. `needsKeyframe` is lock-guarded because it is a
// cross-component signal LinkServer may read to ask the host for an IDR.

/// One decoded picture handed to the renderer. Color is described by the
/// CVPixelBuffer's own attachments (kCVImageBufferYCbCrMatrix / ColorPrimaries /
/// TransferFunction); the documented fallback if absent is BT.709 full-range.
struct DecodedFrame {
    /// IOSurface-backed, Metal-compatible NV12 (420YpCbCr8BiPlanarFullRange) buffer.
    let pixelBuffer: CVPixelBuffer
    /// Presentation timestamp echoed from the access unit (host capture clock).
    let pts: CMTime
}

final class VideoDecoder {

    // MARK: Public interface

    /// Called for every successfully decoded frame, on a VideoToolbox-internal
    /// queue. Set this before feeding frames.
    var onDecodedFrame: ((DecodedFrame) -> Void)?

    /// True when the decoder is waiting for an IDR (initial state, after a
    /// param-set change, or after a recoverable session error). LinkServer can
    /// poll this to ask the host to force a keyframe. Non-IDR frames are dropped
    /// while this is set.
    var needsKeyframe: Bool {
        stateLock.lock(); defer { stateLock.unlock() }
        return awaitingIDR
    }

    init() {}

    deinit { invalidate() }

    /// Install SPS/PPS (raw NALs, no start code, no length prefix). Order is the
    /// host's: SPS (type 7) then PPS (type 8); any count is accepted. Rebuilds the
    /// format description and (re)creates the decompression session only when the
    /// parameter set bytes actually change, then arms the IDR wait.
    func setParameterSets(_ paramSets: [[UInt8]]) {
        guard !paramSets.isEmpty else { return }
        if paramSets == parameterSets, session != nil {
            // Identical to what we already run -- nothing to rebuild.
            return
        }
        parameterSets = paramSets

        guard let fmt = makeFormatDescription(from: paramSets) else {
            NSLog("[VideoDecoder] CMVideoFormatDescriptionCreateFromH264ParameterSets failed")
            tearDownSession()
            setAwaitingIDR(true)
            return
        }
        formatDesc = fmt
        recreateSession(with: fmt)
    }

    /// Decode one access unit. `au` is the raw VIDEO_FRAME payload (AVCC,
    /// big-endian u32 length prefixes). `flags` is MsgHeader.flags
    /// (bit0 = IDR). `timestampUs` is MsgHeader.timestampUs.
    func decode(au: [UInt8], flags: UInt8, timestampUs: UInt64) {
        guard !au.isEmpty else { return }
        let isIDR = (flags & VideoDecoder.flagIDR) != 0

        // Drop everything until we can start on a clean IDR.
        if awaitingIDRUnlocked() && !isIDR { return }

        guard let session = session, formatDesc != nil else {
            // No session yet (param sets not installed) -> stay hungry for a keyframe.
            setAwaitingIDR(true)
            return
        }

        let pts = CMTime(value: Int64(bitPattern: timestampUs), timescale: 1_000_000)

        guard let sampleBuffer = makeSampleBuffer(au: au, pts: pts) else {
            NSLog("[VideoDecoder] failed to build CMSampleBuffer (\(au.count) bytes)")
            return
        }

        let decodeFlags: VTDecodeFrameFlags = [._EnableAsynchronousDecompression]
        // Capture whether THIS submitted AU was the IDR so the output handler can
        // clear the IDR wait only after a real decoded picture comes back.
        let submittedIDR = isIDR
        let handler: VTDecompressionOutputHandler = { [weak self] status, _, imageBuffer, framePTS, _ in
            guard let self = self else { return }
            if status != noErr {
                NSLog("[VideoDecoder] decode callback status=\(status)")
                return
            }
            guard let pixelBuffer = imageBuffer else { return }
            // The IDR is only truly decoded once it returns noErr with a non-nil
            // image buffer here -- not at submit time (which only means queued).
            if submittedIDR { self.setAwaitingIDR(false) }
            self.onDecodedFrame?(DecodedFrame(pixelBuffer: pixelBuffer, pts: framePTS))
        }

        let status = VTDecompressionSessionDecodeFrame(
            session,
            sampleBuffer: sampleBuffer,
            flags: decodeFlags,
            infoFlagsOut: nil,
            outputHandler: handler
        )

        switch status {
        case noErr:
            // Submission succeeded (frame queued). awaitingIDR is cleared in the
            // output handler once the IDR actually produces a decoded picture.
            break
        case kVTInvalidSessionErr, kVTVideoDecoderMalfunctionErr:
            // Recoverable: rebuild the session from the retained format description
            // and wait for the host to resend an IDR.
            NSLog("[VideoDecoder] session error \(status) -> recreating, awaiting IDR")
            if let fmt = formatDesc { recreateSession(with: fmt) } else { setAwaitingIDR(true) }
        default:
            NSLog("[VideoDecoder] VTDecompressionSessionDecodeFrame status=\(status)")
        }
    }

    /// Drop the session/format and re-arm the IDR wait. The host must resend
    /// VIDEO_PARAM_SETS + an IDR (it always does on (re)connect).
    func reset() {
        tearDownSession()
        formatDesc = nil
        parameterSets = []
        setAwaitingIDR(true)
    }

    /// Fully tear down (call on teardown / dealloc).
    func invalidate() {
        tearDownSession()
    }

    // MARK: Private state

    private var session: VTDecompressionSession?
    private var formatDesc: CMVideoFormatDescription?
    private var parameterSets: [[UInt8]] = []

    private let stateLock = NSLock()
    private var awaitingIDR = true

    private static let flagIDR: UInt8 = 0x01

    private func setAwaitingIDR(_ v: Bool) {
        stateLock.lock(); awaitingIDR = v; stateLock.unlock()
    }
    private func awaitingIDRUnlocked() -> Bool {
        stateLock.lock(); defer { stateLock.unlock() }
        return awaitingIDR
    }

    // MARK: Format description

    private func makeFormatDescription(from paramSets: [[UInt8]]) -> CMVideoFormatDescription? {
        let sizes = paramSets.map { $0.count }
        var out: CMFormatDescription?

        // Build the parallel array of base pointers while keeping every element's
        // withUnsafeBufferPointer scope open (recursion), so the pointers stay
        // valid for the single create call at the bottom.
        func build(_ index: Int, _ ptrs: [UnsafePointer<UInt8>]) -> OSStatus {
            if index == paramSets.count {
                return ptrs.withUnsafeBufferPointer { ptrBuf in
                    sizes.withUnsafeBufferPointer { sizeBuf in
                        CMVideoFormatDescriptionCreateFromH264ParameterSets(
                            allocator: kCFAllocatorDefault,
                            parameterSetCount: paramSets.count,
                            parameterSetPointers: ptrBuf.baseAddress!,
                            parameterSetSizes: sizeBuf.baseAddress!,
                            nalUnitHeaderLength: 4,
                            formatDescriptionOut: &out
                        )
                    }
                }
            }
            return paramSets[index].withUnsafeBufferPointer { buf in
                build(index + 1, ptrs + [buf.baseAddress!])
            }
        }

        let status = build(0, [])
        guard status == noErr else {
            NSLog("[VideoDecoder] format desc create status=\(status)")
            return nil
        }
        return out
    }

    // MARK: Session lifecycle

    private func recreateSession(with fmt: CMVideoFormatDescription) {
        tearDownSession()

        // No decoderSpecification: on iOS, H.264 is always hardware-decoded, so the
        // VTVideoDecoderSpecification hardware-accel keys are unnecessary (and they
        // are iOS 17+ in the Xcode 16 SDK -- omitting them keeps the iOS 16 target).

        // NV12, full range; IOSurface-backed + Metal-compatible for zero-copy render.
        let imageAttrs: [CFString: Any] = [
            kCVPixelBufferPixelFormatTypeKey: Int(kCVPixelFormatType_420YpCbCr8BiPlanarFullRange),
            kCVPixelBufferMetalCompatibilityKey: true,
            kCVPixelBufferIOSurfacePropertiesKey: [CFString: Any]()
        ]

        var newSession: VTDecompressionSession?
        let status = VTDecompressionSessionCreate(
            allocator: kCFAllocatorDefault,
            formatDescription: fmt,
            decoderSpecification: nil,
            imageBufferAttributes: imageAttrs as CFDictionary,
            outputCallback: nil,                 // nil -> use the per-decode output handler block
            decompressionSessionOut: &newSession
        )

        guard status == noErr, let created = newSession else {
            NSLog("[VideoDecoder] VTDecompressionSessionCreate status=\(status)")
            session = nil
            setAwaitingIDR(true)
            return
        }

        // Real-time, no power-efficiency throttling -- this is a live mirror.
        VTSessionSetProperty(created, key: kVTDecompressionPropertyKey_RealTime, value: kCFBooleanTrue)

        session = created
        // Fresh session: it must start on an IDR.
        setAwaitingIDR(true)
    }

    private func tearDownSession() {
        if let s = session {
            VTDecompressionSessionWaitForAsynchronousFrames(s)
            VTDecompressionSessionInvalidate(s)
        }
        session = nil
    }

    // MARK: Sample buffer

    private func makeSampleBuffer(au: [UInt8], pts: CMTime) -> CMSampleBuffer? {
        guard let fmt = formatDesc else { return nil }

        // Allocate a block buffer and copy the AVCC AU into it. (A copy here keeps
        // ownership simple; the AU is ~100 KB and the decode is async.)
        var blockBuffer: CMBlockBuffer?
        var status = CMBlockBufferCreateWithMemoryBlock(
            allocator: kCFAllocatorDefault,
            memoryBlock: nil,
            blockLength: au.count,
            blockAllocator: kCFAllocatorDefault,
            customBlockSource: nil,
            offsetToData: 0,
            dataLength: au.count,
            flags: kCMBlockBufferAssureMemoryNowFlag,
            blockBufferOut: &blockBuffer
        )
        guard status == kCMBlockBufferNoErr, let bb = blockBuffer else {
            NSLog("[VideoDecoder] CMBlockBufferCreateWithMemoryBlock status=\(status)")
            return nil
        }

        status = au.withUnsafeBytes { raw -> OSStatus in
            CMBlockBufferReplaceDataBytes(
                with: raw.baseAddress!,
                blockBuffer: bb,
                offsetIntoDestination: 0,
                dataLength: au.count
            )
        }
        guard status == kCMBlockBufferNoErr else {
            NSLog("[VideoDecoder] CMBlockBufferReplaceDataBytes status=\(status)")
            return nil
        }

        var sampleBuffer: CMSampleBuffer?
        var timing = CMSampleTimingInfo(
            duration: .invalid,
            presentationTimeStamp: pts,
            decodeTimeStamp: .invalid
        )
        var sampleSize = au.count
        status = CMSampleBufferCreateReady(
            allocator: kCFAllocatorDefault,
            dataBuffer: bb,
            formatDescription: fmt,
            sampleCount: 1,
            sampleTimingEntryCount: 1,
            sampleTimingArray: &timing,
            sampleSizeEntryCount: 1,
            sampleSizeArray: &sampleSize,
            sampleBufferOut: &sampleBuffer
        )
        guard status == noErr, let sb = sampleBuffer else {
            NSLog("[VideoDecoder] CMSampleBufferCreateReady status=\(status)")
            return nil
        }
        return sb
    }
}

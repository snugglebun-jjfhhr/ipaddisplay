//  DisplayRenderer.swift — M6 Metal render core.
//
//  Owns the Metal device/queue/pipeline/texture-cache and turns a decoded
//  NV12 CVPixelBuffer into pixels in a CAMetalLayer's drawable. The motion path
//  uploads the two planes (luma .r8Unorm, chroma .rg8Unorm) zero-copy via
//  CVMetalTextureCache and draws a letterboxed fullscreen triangle that runs the
//  YCbCr->RGB shader (see Shaders.metal). The still path (M8) is a 1:1 blit.
//
//  Color is driven from the CVPixelBuffer's color attachments (YCbCrMatrix +
//  pixel-format range) with BT.709 full-range as the documented fallback — see
//  `colorConversion(for:)`. We never linearize; the shader emits sRGB-encoded
//  R'G'B' that the layer (tagged sRGB) color-manages to Display-P3.

import Foundation
import Metal
import CoreVideo
import simd
import QuartzCore

// MARK: - Public interface (consumed by the decoder / still receiver)

/// A decoded motion frame ready to render. `pixelBuffer` is an IOSurface-backed
/// NV12 (4:2:0 8-bit, full-range) buffer straight from VTDecompressionSession.
struct VideoFrame {
    let pixelBuffer: CVPixelBuffer
    let timestampUs: UInt64

    init(pixelBuffer: CVPixelBuffer, timestampUs: UInt64 = 0) {
        self.pixelBuffer = pixelBuffer
        self.timestampUs = timestampUs
    }
}

/// A lossless still frame (M8). A private RGBA texture blitted 1:1 into the
/// drawable. Defined now so the render API is stable across milestones.
struct StillFrame {
    let texture: MTLTexture
    let timestampUs: UInt64

    init(texture: MTLTexture, timestampUs: UInt64 = 0) {
        self.texture = texture
        self.timestampUs = timestampUs
    }
}

/// Authoritative layer mode (mirrors the wire SET_LAYER message).
enum RenderMode {
    case motion
    case still
}

/// The sink a frame producer (decoder) calls. `MetalDisplayView` conforms.
protocol VideoFrameSink: AnyObject {
    func present(_ frame: VideoFrame)
    func present(still: StillFrame)
}

// MARK: - Color conversion (matches `ColorConversion` in Shaders.metal)

/// rgb = matrix * (ycbcr - offset). Layout-compatible with the Metal struct:
/// simd_float3x3 (48 B) then simd_float3 (16 B, 16-aligned) => 64 B stride.
struct ColorConversion {
    var matrix: simd_float3x3
    var offset: simd_float3
}

// MARK: - Clean aperture (matches `CleanAperture` in Shaders.metal)

/// Maps the displayed [0,1] UV onto the coded texture's clean sub-rect:
/// `uv = uv * scale + offset`. Layout-compatible with the Metal struct
/// (two simd_float2 / float2 => 16 B). Identity is scale=(1,1), offset=(0,0).
struct CleanAperture {
    var scale: simd_float2
    var offset: simd_float2
}

// MARK: - DisplayRenderer

final class DisplayRenderer {
    let device: MTLDevice
    private let queue: MTLCommandQueue
    private let motionPipeline: MTLRenderPipelineState
    private let textureCache: CVMetalTextureCache

    /// The drawable pixel format the layer must use (no _srgb — see color notes).
    static let drawablePixelFormat: MTLPixelFormat = .bgra8Unorm

    init?() {
        guard let device = MTLCreateSystemDefaultDevice(),
              let queue = device.makeCommandQueue() else { return nil }

        // The .metal file is compiled into the default library by the build.
        guard let library = device.makeDefaultLibrary(),
              let vfn = library.makeFunction(name: "fullscreenVertex"),
              let ffn = library.makeFunction(name: "yuvFragment") else { return nil }

        let desc = MTLRenderPipelineDescriptor()
        desc.label = "ipaddisplay.motion"
        desc.vertexFunction = vfn
        desc.fragmentFunction = ffn
        desc.colorAttachments[0].pixelFormat = DisplayRenderer.drawablePixelFormat

        guard let pipeline = try? device.makeRenderPipelineState(descriptor: desc) else { return nil }

        var cache: CVMetalTextureCache?
        guard CVMetalTextureCacheCreate(kCFAllocatorDefault, nil, device, nil, &cache) == kCVReturnSuccess,
              let cache else { return nil }

        self.device = device
        self.queue = queue
        self.motionPipeline = pipeline
        self.textureCache = cache
    }

    // MARK: Motion

    /// Render result codes (diagnostic): 1=drew, 2=no size, 3=texture nil, 4=no drawable.
    @discardableResult
    func render(_ frame: VideoFrame, to layer: CAMetalLayer) -> Int {
      return autoreleasepool { () -> Int in
        let drawableSize = layer.drawableSize
        guard drawableSize.width >= 1, drawableSize.height >= 1 else { return 2 }

        let pb = frame.pixelBuffer

        // Two planes -> two Metal textures (zero-copy via IOSurface).
        guard let lumaCV = makeTexture(from: pb, plane: 0, format: .r8Unorm),
              let chromaCV = makeTexture(from: pb, plane: 1, format: .rg8Unorm),
              let luma = CVMetalTextureGetTexture(lumaCV),
              let chroma = CVMetalTextureGetTexture(chromaCV) else { return 3 }

        guard let drawable = layer.nextDrawable() else { return 4 }

        let pass = MTLRenderPassDescriptor()
        pass.colorAttachments[0].texture = drawable.texture
        pass.colorAttachments[0].loadAction = .clear
        pass.colorAttachments[0].clearColor = MTLClearColorMake(0, 0, 0, 1) // black bars
        pass.colorAttachments[0].storeAction = .store

        guard let cmd = queue.makeCommandBuffer(),
              let enc = cmd.makeRenderCommandEncoder(descriptor: pass) else { return 5 }

        var scale = letterboxScale(pixelBuffer: pb, drawableSize: drawableSize)
        var cc = colorConversion(for: pb)
        var aperture = cleanAperture(for: pb)

        enc.setRenderPipelineState(motionPipeline)
        enc.setVertexBytes(&scale, length: MemoryLayout<simd_float2>.stride, index: 0)
        enc.setFragmentTexture(luma, index: 0)
        enc.setFragmentTexture(chroma, index: 1)
        enc.setFragmentBytes(&cc, length: MemoryLayout<ColorConversion>.stride, index: 0)
        enc.setFragmentBytes(&aperture, length: MemoryLayout<CleanAperture>.stride, index: 1)
        enc.drawPrimitives(type: .triangle, vertexStart: 0, vertexCount: 3)
        enc.endEncoding()

        // Retain the CVMetalTexture wrappers + source buffer (its IOSurface backs
        // the textures) until the GPU is done reading them.
        cmd.addCompletedHandler { _ in
            _ = lumaCV
            _ = chromaCV
            _ = pb
        }
        cmd.present(drawable)
        cmd.commit()

        // Release cached CVMetalTexture wrappers for older buffers so the texture
        // cache stops pinning their IOSurfaces and starving VideoToolbox's pool.
        // The current frame's wrappers (lumaCV/chromaCV) and source buffer stay
        // alive via the completion handler above until the GPU finishes reading.
        CVMetalTextureCacheFlush(textureCache, 0)
        return 1
      }
    }

    // MARK: Still (M8) — bit-exact 1:1 blit into the drawable

    func render(still: StillFrame, to layer: CAMetalLayer) {
        let drawableSize = layer.drawableSize
        guard drawableSize.width >= 1, drawableSize.height >= 1,
              let drawable = layer.nextDrawable(),
              let cmd = queue.makeCommandBuffer(),
              let blit = cmd.makeBlitCommandEncoder() else { return }

        let w = min(still.texture.width, drawable.texture.width)
        let h = min(still.texture.height, drawable.texture.height)
        blit.copy(from: still.texture,
                  sourceSlice: 0, sourceLevel: 0,
                  sourceOrigin: MTLOrigin(x: 0, y: 0, z: 0),
                  sourceSize: MTLSize(width: w, height: h, depth: 1),
                  to: drawable.texture,
                  destinationSlice: 0, destinationLevel: 0,
                  destinationOrigin: MTLOrigin(x: 0, y: 0, z: 0))
        blit.endEncoding()
        cmd.present(drawable)
        cmd.commit()
    }

    // MARK: Texture upload

    private func makeTexture(from pb: CVPixelBuffer, plane: Int,
                             format: MTLPixelFormat) -> CVMetalTexture? {
        let w = CVPixelBufferGetWidthOfPlane(pb, plane)
        let h = CVPixelBufferGetHeightOfPlane(pb, plane)
        guard w > 0, h > 0 else { return nil }
        var out: CVMetalTexture?
        let status = CVMetalTextureCacheCreateTextureFromImage(
            kCFAllocatorDefault, textureCache, pb, nil,
            format, w, h, plane, &out)
        return status == kCVReturnSuccess ? out : nil
    }

    // MARK: Aspect / letterbox

    /// Per-axis shrink factor that fits the source display aspect inside the
    /// drawable (the unscaled axis stays 1.0). Drives the vertex `scale` uniform.
    private func letterboxScale(pixelBuffer pb: CVPixelBuffer,
                                drawableSize: CGSize) -> simd_float2 {
        var display = CVImageBufferGetDisplaySize(pb)   // honors clean aperture / PAR
        if display.width < 1 || display.height < 1 {
            display = CGSize(width: CVPixelBufferGetWidth(pb),
                             height: CVPixelBufferGetHeight(pb))
        }
        guard display.width >= 1, display.height >= 1 else { return simd_float2(1, 1) }

        let videoAspect = display.width / display.height
        let drawAspect = drawableSize.width / drawableSize.height
        if videoAspect > drawAspect {
            // Source is wider: fill width, bars top/bottom.
            return simd_float2(1, Float(drawAspect / videoAspect))
        } else {
            // Source is taller/narrower: fill height, bars left/right.
            return simd_float2(Float(videoAspect / drawAspect), 1)
        }
    }

    // MARK: Clean aperture (coded padding -> displayed sub-rect)

    /// UV scale+offset that maps the displayed [0,1] range onto the texture's
    /// clean sub-rect, so the coded padding (e.g. 1088 coded rows vs 1080 shown)
    /// is never sampled. CVPixelBufferGetWidth/Height give the CODED size; the
    /// clean rect gives the display sub-rect. Falls back to identity if the clean
    /// rect is empty/zero.
    private func cleanAperture(for pb: CVPixelBuffer) -> CleanAperture {
        let identity = CleanAperture(scale: simd_float2(1, 1), offset: simd_float2(0, 0))
        let codedW = CGFloat(CVPixelBufferGetWidth(pb))
        let codedH = CGFloat(CVPixelBufferGetHeight(pb))
        guard codedW >= 1, codedH >= 1 else { return identity }

        let clean = CVImageBufferGetCleanRect(pb)
        guard clean.width >= 1, clean.height >= 1 else { return identity }

        return CleanAperture(
            scale: simd_float2(Float(clean.width / codedW), Float(clean.height / codedH)),
            offset: simd_float2(Float(clean.origin.x / codedW), Float(clean.origin.y / codedH))
        )
    }

    // MARK: Color attachments -> conversion matrix

    /// Reads the buffer's YCbCrMatrix attachment and range (from the pixel
    /// format) and returns the matching YCbCr->R'G'B' conversion. Fallback is
    /// BT.709 full-range (the pinned stream contract).
    private func colorConversion(for pb: CVPixelBuffer) -> ColorConversion {
        let pf = CVPixelBufferGetPixelFormatType(pb)
        let fullRange = (pf == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange)

        var is601 = false
        if let raw = CVBufferCopyAttachment(pb, kCVImageBufferYCbCrMatrixKey, nil) {
            // CVBufferCopyAttachment returns a managed CFTypeRef? (not Unmanaged).
            if let s = raw as? String {
                is601 = (s == (kCVImageBufferYCbCrMatrix_ITU_R_601_4 as String))
            }
        }
        return DisplayRenderer.conversion(is601: is601, fullRange: fullRange)
    }

    /// BT.709 (default) or BT.601, full- or video-range. Matrices fold the range
    /// scaling into the coefficients; `offset` carries the black level + 0.5
    /// chroma centre.
    static func conversion(is601: Bool, fullRange: Bool) -> ColorConversion {
        let chromaOffset: Float = 0.5
        if fullRange {
            let offset = simd_float3(0, chromaOffset, chromaOffset)
            if is601 {
                // BT.601 full-range.
                return ColorConversion(matrix: simd_float3x3(rows: [
                    simd_float3(1, 0,         1.402),
                    simd_float3(1, -0.344136, -0.714136),
                    simd_float3(1, 1.772,     0),
                ]), offset: offset)
            }
            // BT.709 full-range (pinned default).
            return ColorConversion(matrix: simd_float3x3(rows: [
                simd_float3(1, 0,         1.5748),
                simd_float3(1, -0.187324, -0.468124),
                simd_float3(1, 1.8556,    0),
            ]), offset: offset)
        } else {
            // Video (limited) range: Y' has black level 16/255.
            let offset = simd_float3(16.0 / 255.0, chromaOffset, chromaOffset)
            if is601 {
                // BT.601 video-range.
                return ColorConversion(matrix: simd_float3x3(rows: [
                    simd_float3(1.164384, 0,         1.596027),
                    simd_float3(1.164384, -0.391762, -0.812968),
                    simd_float3(1.164384, 2.017232,  0),
                ]), offset: offset)
            }
            // BT.709 video-range.
            return ColorConversion(matrix: simd_float3x3(rows: [
                simd_float3(1.164384, 0,         1.792741),
                simd_float3(1.164384, -0.213249, -0.532909),
                simd_float3(1.164384, 2.112402,  0),
            ]), offset: offset)
        }
    }
}

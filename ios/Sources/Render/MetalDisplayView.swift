//  MetalDisplayView.swift — M6 CAMetalLayer host + SwiftUI bridge.
//
//  A UIView whose backing layer is a CAMetalLayer, configured for the color
//  contract: pixelFormat .bgra8Unorm (NOT _srgb) and colorspace tagged sRGB so
//  Core Animation color-manages the sRGB-encoded R'G'B' the shader emits up to
//  the Display-P3 panel. (Tagging the layer Display-P3 while writing sRGB values
//  is the oversaturation bug from the M3/M4 review — see CLAUDE.md.)
//
//  Pacing: a CADisplayLink pinned to 60/60/60 (refresh-last priority) draws the
//  most-recent pending frame each vsync. Decode cadence is decoupled from
//  display; drawable back-pressure in DisplayRenderer paces the GPU.
//
//  Frame producers (the VideoDecoder, the still receiver) call `present(_:)` /
//  `present(still:)` via the `VideoFrameSink` protocol from any thread.

import UIKit
import QuartzCore
import Metal
import SwiftUI

final class MetalDisplayView: UIView, VideoFrameSink {

    override class var layerClass: AnyClass { CAMetalLayer.self }
    private var metalLayer: CAMetalLayer { layer as! CAMetalLayer }

    private let renderer: DisplayRenderer?
    private var displayLink: CADisplayLink?

    // Latest frame state, guarded by `lock`. The display link consumes the
    // pending frame on the main thread; producers set it from decode threads.
    private let lock = NSLock()
    private var pendingMotion: VideoFrame?
    private var pendingStill: StillFrame?
    private var mode: RenderMode = .motion
    private var needsDisplay = false

    // MARK: Init

    override init(frame: CGRect) {
        self.renderer = DisplayRenderer()
        super.init(frame: frame)
        configureLayer()
    }

    required init?(coder: NSCoder) {
        self.renderer = DisplayRenderer()
        super.init(coder: coder)
        configureLayer()
    }

    private func configureLayer() {
        metalLayer.device = renderer?.device ?? MTLCreateSystemDefaultDevice()
        metalLayer.pixelFormat = DisplayRenderer.drawablePixelFormat   // .bgra8Unorm
        // sRGB working space; CA converts sRGB -> P3 for the panel. Do NOT use displayP3.
        metalLayer.colorspace = CGColorSpace(name: CGColorSpace.sRGB)
        metalLayer.framebufferOnly = false        // allow the still-path blit into the drawable
        metalLayer.isOpaque = true
        metalLayer.maximumDrawableCount = 3
        metalLayer.presentsWithTransaction = false
        backgroundColor = .black
    }

    // MARK: Layout — drive drawable size from the view (panel) bounds, not a
    // hardcoded 2732x2048. Native scale gives panel-resolution pixels.

    override func layoutSubviews() {
        super.layoutSubviews()
        let scale = window?.screen.nativeScale ?? UIScreen.main.nativeScale
        metalLayer.contentsScale = scale
        let px = CGSize(width: bounds.width * scale, height: bounds.height * scale)
        if px.width >= 1, px.height >= 1 {
            metalLayer.drawableSize = px
        }
    }

    // MARK: Display link lifecycle

    override func didMoveToWindow() {
        super.didMoveToWindow()
        if window != nil {
            startDisplayLink()
        } else {
            stopDisplayLink()
        }
    }

    private func startDisplayLink() {
        guard displayLink == nil else { return }
        let proxy = DisplayLinkProxy(self)
        let link = CADisplayLink(target: proxy, selector: #selector(DisplayLinkProxy.tick(_:)))
        link.preferredFrameRateRange = CAFrameRateRange(minimum: 60, maximum: 60, preferred: 60)
        link.add(to: .main, forMode: .common)
        displayLink = link
    }

    private func stopDisplayLink() {
        displayLink?.invalidate()
        displayLink = nil
    }

    deinit { stopDisplayLink() }

    // MARK: VideoFrameSink (thread-safe producers)

    func present(_ frame: VideoFrame) {
        lock.lock()
        pendingMotion = frame
        mode = .motion
        needsDisplay = true
        lock.unlock()
    }

    func present(still: StillFrame) {
        lock.lock()
        pendingStill = still
        mode = .still
        needsDisplay = true
        lock.unlock()
    }

    // MARK: Per-vsync draw (main thread)

    fileprivate func renderTick() {
        lock.lock()
        guard needsDisplay else { lock.unlock(); return }
        let mode = self.mode
        let motion = pendingMotion
        let still = pendingStill
        needsDisplay = false
        lock.unlock()

        guard let renderer else { return }
        switch mode {
        case .motion:
            if let motion { renderer.render(motion, to: metalLayer) }
        case .still:
            if let still { renderer.render(still: still, to: metalLayer) }
        }
    }
}

// Breaks the CADisplayLink -> target retain cycle (the link retains its target).
private final class DisplayLinkProxy {
    weak var view: MetalDisplayView?
    init(_ view: MetalDisplayView) { self.view = view }
    @objc func tick(_ link: CADisplayLink) { view?.renderTick() }
}

// MARK: - SwiftUI bridge

/// Wraps a caller-owned `MetalDisplayView` so the same instance can be handed to
/// the decoder as a `VideoFrameSink`. Create the view once, pass it here, and
/// wire `view` into the frame producer.
struct MetalDisplayRepresentable: UIViewRepresentable {
    let view: MetalDisplayView

    func makeUIView(context: Context) -> MetalDisplayView { view }
    func updateUIView(_ uiView: MetalDisplayView, context: Context) {}
}

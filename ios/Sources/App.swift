import SwiftUI
import UIKit

// The iPad link endpoint. It listens on TCP :7000 and speaks the canonical
// ipaddisplay wire protocol (24-byte little-endian MsgHeader + payload). The
// Windows host (host/handshake_host.py, then host/stream_host.py) drives the
// handshake + the live H.264 stream over usbmux.
//
// M5/M6: once decoded frames start flowing the UI swaps the handshake status
// screen for a full-screen MetalDisplayView (the decoder's VideoFrameSink). When
// the client disconnects it falls back to the status screen.
@main
struct IpadDisplayApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}

/// Owns the single MetalDisplayView instance for the app's lifetime. As a
/// @StateObject this is created exactly once (SwiftUI never re-runs the
/// autoclosure), so we never spin up a second Metal device / display link.
final class DisplayHolder: ObservableObject {
    let view = MetalDisplayView(frame: .zero)
}

struct ContentView: View {
    @StateObject private var server = LinkServer()
    @StateObject private var display = DisplayHolder()

    var body: some View {
        ZStack {
            Color.black
            if server.isStreaming {
                ZStack(alignment: .topLeading) {
                    MetalDisplayRepresentable(view: display.view)
                    // DIAGNOSTIC overlay: live decoded-frame count. If this climbs
                    // while the picture is frozen -> decoder OK, renderer stuck.
                    // If it stays at 1 -> decoder stuck. (Temporary, M5 debug.)
                    Text("dec \(server.framesDecoded)  pres \(server.framesPresented)  rend \(server.framesRendered)")
                        .font(.system(size: 22, weight: .bold, design: .monospaced))
                        .foregroundStyle(.green)
                        .padding(8)
                        .background(Color.black.opacity(0.6))
                        .padding()
                }
            } else {
                handshakeStatus
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .ignoresSafeArea()
        .statusBarHidden(true)
        .onAppear {
            UIApplication.shared.isIdleTimerDisabled = true   // don't sleep mid-session
            server.frameSink = display.view                   // decoder -> renderer hand-off
            display.view.onRendered = { [weak server] in server?.bumpRendered() }  // DIAGNOSTIC
            server.start()
        }
    }

    // The pre-stream handshake / status screen (unchanged from M0 aside from label).
    private var handshakeStatus: some View {
        VStack(spacing: 18) {
            Text("ipaddisplay · M5")
                .font(.system(size: 44, weight: .bold, design: .rounded))

            Text(server.phase.rawValue)
                .font(.title3)
                .foregroundStyle(.green)
                .multilineTextAlignment(.center)
            if !server.detail.isEmpty {
                Text(server.detail)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }

            Grid(horizontalSpacing: 24, verticalSpacing: 8) {
                if let c = server.config {
                    GridRow { Text("resolution").gridColumnAlignment(.trailing); Text(c.resolution) }
                    GridRow { Text("codec").gridColumnAlignment(.trailing); Text(c.codecName) }
                    GridRow { Text("chroma").gridColumnAlignment(.trailing); Text("\(c.chromaName) \(c.bitDepth)-bit") }
                    GridRow { Text("range").gridColumnAlignment(.trailing); Text(c.rangeName) }
                    GridRow { Text("color").gridColumnAlignment(.trailing); Text(c.colorName) }
                } else {
                    GridRow { Text("config").gridColumnAlignment(.trailing); Text("— not negotiated —") }
                }
                GridRow { Text("pings").gridColumnAlignment(.trailing); Text("\(server.pings)") }
                if server.framesDecoded > 0 {
                    GridRow { Text("frames").gridColumnAlignment(.trailing); Text("\(server.framesDecoded)") }
                }
                if server.stubbed > 0 {
                    GridRow { Text("stubbed msgs").gridColumnAlignment(.trailing); Text("\(server.stubbed)") }
                }
            }
            .font(.title3.monospaced())
            .padding(.top, 8)

            Text("Keep this screen on. Run host/stream_host.py on the host.")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .padding(.top, 8)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(white: 0.07))
        .foregroundStyle(.white)
        .ignoresSafeArea()
    }
}

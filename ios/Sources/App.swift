import SwiftUI
import UIKit

// M0: the app is the iPad link endpoint. It listens on TCP :7000 and speaks the
// canonical ipaddisplay wire protocol (24-byte little-endian MsgHeader + payload).
// The Windows host (host/handshake_host.py) drives the handshake over usbmux.
// This screen shows the handshake state + the negotiated stream config.
// Next step after this: VideoToolbox/Metal frame receiver (M3+).
@main
struct IpadDisplayApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}

struct ContentView: View {
    @StateObject private var server = LinkServer()

    var body: some View {
        VStack(spacing: 18) {
            Text("ipaddisplay · M0")
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
                if server.stubbed > 0 {
                    GridRow { Text("stubbed msgs").gridColumnAlignment(.trailing); Text("\(server.stubbed)") }
                }
            }
            .font(.title3.monospaced())
            .padding(.top, 8)

            Text("Keep this screen on. Run host/handshake_host.py on the host.")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .padding(.top, 8)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(white: 0.07))
        .foregroundStyle(.white)
        .ignoresSafeArea()
        .onAppear {
            UIApplication.shared.isIdleTimerDisabled = true   // don't sleep mid-session
            server.start()
        }
    }
}

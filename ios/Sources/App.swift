import SwiftUI
import UIKit

// Stage B: the app is now a usbmux perf server. It listens on TCP :7000; the
// Windows host (spikes/stage-b/perf_host.py) drives throughput + latency tests
// over the cable. Next step after this: VideoToolbox/Metal frame receiver.
@main
struct IpadDisplayApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}

struct ContentView: View {
    @StateObject private var server = PerfServer()

    var body: some View {
        VStack(spacing: 18) {
            Text("ipaddisplay · Stage B")
                .font(.system(size: 44, weight: .bold, design: .rounded))
            Text(server.status)
                .font(.title3)
                .foregroundStyle(.green)
                .multilineTextAlignment(.center)

            Grid(horizontalSpacing: 24, verticalSpacing: 8) {
                GridRow { Text("received").gridColumnAlignment(.trailing); Text(fmtBytes(server.bytesReceived)) }
                GridRow { Text("sent").gridColumnAlignment(.trailing); Text(fmtBytes(server.bytesSent)) }
                GridRow { Text("pings echoed").gridColumnAlignment(.trailing); Text("\(server.pings)") }
            }
            .font(.title3.monospaced())
            .padding(.top, 8)

            Text("Keep this screen on. Run perf_host.py on Windows.")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .padding(.top, 8)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(white: 0.07))
        .foregroundStyle(.white)
        .ignoresSafeArea()
        .onAppear {
            UIApplication.shared.isIdleTimerDisabled = true   // don't sleep mid-test
            server.start()
        }
    }

    private func fmtBytes(_ b: UInt64) -> String {
        let mb = Double(b) / (1024 * 1024)
        if mb >= 1024 { return String(format: "%.2f GiB", mb / 1024) }
        return String(format: "%.1f MiB", mb)
    }
}

import SwiftUI

// Toolchain smoke test: the smallest app that proves CI build -> .ipa ->
// Sideloadly -> runs on the iPad. Once this launches on-device, we grow it into
// the Stage B usbmux socket client, then the VideoToolbox/Metal frame receiver.
@main
struct IpadDisplayApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}

struct ContentView: View {
    var body: some View {
        VStack(spacing: 20) {
            Text("ipaddisplay")
                .font(.system(size: 56, weight: .bold, design: .rounded))
            Text("build + sideload OK")
                .font(.title2)
                .foregroundStyle(.green)
            Text("Next: usbmux socket client (Stage B)")
                .font(.headline)
                .foregroundStyle(.secondary)
            Text(deviceLine)
                .font(.footnote)
                .foregroundStyle(.tertiary)
                .padding(.top, 8)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(white: 0.07))
        .foregroundStyle(.white)
        .ignoresSafeArea()
    }

    private var deviceLine: String {
        let d = UIDevice.current
        return "\(d.model) · \(d.systemName) \(d.systemVersion)"
    }
}

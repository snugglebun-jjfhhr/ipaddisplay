# iPad app — cloud-Mac build + Windows sideload

The iPad app is built on a **GitHub Actions macOS runner** (the "cloud Mac") and
installed onto the iPad from **Windows** with **Sideloadly**, which signs it with
your Apple ID at install time. No Mac to own, no Apple certs in CI.

## Current state

A trivial SwiftUI smoke-test app: it shows "build + sideload OK" and the device
info. Its only job right now is to prove the whole toolchain works end to end
before we invest in the real socket + Metal code.

Roadmap (each grows this same app):
1. **Toolchain smoke test** (now) — CI builds, Sideloadly installs, it launches.
2. **Stage B** — usbmux socket client: connect to the Windows host over the
   cable, run a sustained blast + ping-pong, show real throughput + round-trip
   latency on screen (the precise version of spike #1).
3. **Mirror receiver** — receive H.264 over the socket, VideoToolbox decode,
   render to a Metal layer. First real wired image.

## One-time setup

1. **Push this repo to GitHub as a PUBLIC repo** (macOS runner minutes are free
   for public repos; private bills at 10x).
   ```bash
   gh repo create ipaddisplay --public --source . --remote origin --push
   ```
2. The `ios-build` workflow runs on every push touching `ios/**`, or manually via
   the Actions tab ("Run workflow"). It uploads `IpadDisplay-unsigned-ipa`.

## Each build → install loop

1. Let CI build (Actions tab) and **download the `IpadDisplay-unsigned-ipa`
   artifact** to Windows; unzip to get `IpadDisplay-unsigned.ipa`.
2. Install **Sideloadly** (https://sideloadly.io) on Windows. It needs the Apple
   Devices app / iTunes (already installed — provides the usbmux driver).
3. Plug in the iPad (unlocked, trusted). In Sideloadly: select the device, drop
   in the `.ipa`, enter your Apple ID, click **Start**. It signs and installs
   over USB (the usbmux transport we validated in spike #1).
4. On the iPad: **Settings → General → VPN & Device Management** → trust your
   developer (Apple ID) profile. Then launch the app.

## Free vs paid Apple ID

- **Free Apple ID:** works, but the signed app **expires after 7 days** (re-run
  Sideloadly to refresh) and you're limited to 3 sideloaded apps. Fine for now.
- **$99/yr Apple Developer:** 1-year signing, no weekly refresh. Worth it once
  the app is something you use daily.

## Local generate (optional, if you ever get Mac access)

```bash
brew install xcodegen
cd ios && xcodegen generate && open IpadDisplay.xcodeproj
```

## Why XcodeGen

`project.yml` is the source of truth; the `.xcodeproj` is generated and
git-ignored. This keeps the project as plain text (editable from Windows/Linux)
and avoids the unmergeable `.xcodeproj` churn. CI runs `xcodegen generate` before
building.

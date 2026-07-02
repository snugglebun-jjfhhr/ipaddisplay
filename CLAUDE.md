# ipaddisplay — project memory

A wired, color-accurate display that turns a 12.9" M1 iPad Pro into a second
monitor for a Windows PC. Windows captures the desktop → H.264-encodes (NVENC) →
ships it over a **usbmux TCP socket** (the USB cable, no WiFi) → iPad
VideoToolbox-decodes → renders to a Metal layer, color-managed to the P3 panel.
Built for photo/video/color editing, so **fidelity beats framerate** (native
resolution #1, color accuracy #2, refresh rate last — 60 Hz is fine).

> **RESUMING? READ ["CURRENT STATE"](#current-state) FIRST.** The mirror WORKS:
> live 1080p desktop on the iPad at a steady 60 fps (M5+M6 verified 2026-07-02).
> Next: M7 color pass. Cleanup owed: on-device debug overlay.

---

## Hardware & environment (all facts are real, verified)

- **Windows host:** Ryzen 9 5900X + **NVIDIA GTX 970 (GM204, 2nd-gen Maxwell)**.
  NVENC is **H.264 8-bit only** (HEVC-encode disputed/absent on GM204; no 10-bit,
  no 4:4:4, no AV1). ~500 MP/s NVENC throughput.
- **iPad:** 12.9" iPad Pro **M1 (iPad13,8)**, iPadOS **27.0**, UDID
  `00008103-00126151117B001E`. Native panel **2732×2048**, ProMotion 120 Hz,
  mini-LED Liquid Retina XDR, Display P3, Reference Mode, USB4/Thunderbolt,
  hardware HEVC+H.264 **decode**.
- **⚠️ The physical Windows monitor is currently 1920×1080**, so the mirror shows
  1080p. Native 2732×2048 only applies once the virtual-display milestone (post-M6)
  creates a 2732×2048 IddCx virtual monitor.
- **This shell is WSL2 Linux on the Windows box.** The iPad is NOT visible from
  WSL2 (no USB passthrough). Drive Windows via
  `powershell.exe -NoProfile -Command '...'`. **Use SINGLE quotes** around the
  PowerShell command — backticks inside bash double-quotes break repeatedly.
- **Windows tooling (installed):** `ffmpeg` at `C:\ffmpeg\bin\` (gyan.dev
  2026-01-22, has `ddagrab` + `h264_nvenc`); **Python 3.12 via `py -3.12`** with
  `pymobiledevice3 9.30.1`; Apple Devices app (usbmux service running);
  **Sideloadly** for install. `ddagrab` works through the WSL→PowerShell bridge.
- **Working dirs on Windows:** host scripts staged at
  `C:\Users\aidan\ipaddisplay-spike\host\`; the `.ipa` lands at
  `C:\Users\aidan\Downloads\ipaddisplay-ipa\IpadDisplay-unsigned.ipa`.

---

## Build & deploy loop (no Mac owned — this is the whole workflow)

The iPad app **runs against Windows**; a Mac is only needed to *compile/sign*. We
have none, so:

1. **Edit** Swift under `ios/Sources/` (XcodeGen; `ios/project.yml` globs the whole
   `Sources/` dir, subdirs + `.metal` auto-included — no project edits needed to add files).
2. **Push to GitHub** → `.github/workflows/ios-build.yml` runs on a **`macos-15`**
   runner (Xcode 16), `xcodegen generate` → `xcodebuild` → uploads an **unsigned
   `.ipa`** artifact `IpadDisplay-unsigned-ipa`. **CI is the compile gate.**
3. **Download** the artifact to Windows (`gh run download <id> -n IpadDisplay-unsigned-ipa`).
4. **Sideloadly** on Windows signs (free Apple ID) + installs over USB.
5. **User re-sideloads + force-relaunches** the app; then run a host script.

Repo: **`github.com/snugglebun-jjfhhr/ipaddisplay` (public** — public = free macOS
CI minutes). Free Apple ID signing **expires after 7 days**; $99/yr dev account
removes that (deferred).

> **⚠️ Git trap:** `/home/aidan/projects` is a DIFFERENT git repo containing ALL
> the user's projects. This project's repo is the **standalone** one at
> `/home/aidan/projects/ipaddisplay` (its own `.git`). Never `gh repo create` or
> commit from the parent. Local git identity: Aidan Orsino / aidan.orsino@gmail.com.
> Commit trailer: `Co-Authored-By:` the current Claude model (e.g.
> `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`).

Common commands (from the WSL repo root):
```bash
git push -q origin main
RID=$(gh run list --limit 1 --json databaseId -q '.[0].databaseId')
gh run watch "$RID" --exit-status --interval 12
gh run download "$RID" -n IpadDisplay-unsigned-ipa -D /mnt/c/Users/aidan/Downloads/ipaddisplay-ipa
```

---

## Architecture

```
WINDOWS                                              iPad (SwiftUI app)
ddagrab capture ─ libswscale CSC ─ h264_nvenc        LinkServer :7000 (BSD socket)
   (BGRA)         (BT.709 full)     (Annex-B)          │ reads 24B LE MsgHeader + payload
        │                                              ├ VIDEO_PARAM_SETS → VideoDecoder.setParameterSets
        └─ NAL pump (stream_host.py) ── usbmux TCP ──▶ ├ VIDEO_FRAME → VideoDecoder.decode (VTDecompressionSession)
           Annex-B→AVCC, splits AUs   :7000 tunnel     │        → CVPixelBuffer (NV12 full-range)
                                                        └ onDecodedFrame → MetalDisplayView.present
                                                                 → DisplayRenderer (NV12→RGB BT.709, CADisplayLink)
                                                                 → CAMetalLayer (bgra8Unorm, colorspace sRGB → CA maps to P3)
```

- **Two-mode design (target):** *motion* = 4:2:0 H.264 (built, M5/M6); *lossless
  reference-still* = full-RGB frames bypass the codec for exact color when static
  (M8, not built). v1 is a whole-layer mode swap, not a per-tile compositor.
- **Transport = usbmux** over USB-C. Dev/test uses `pymobiledevice3 usbmux forward
  7000 7000` (Python relay); production should use C libusbmuxd `usbmuxd_connect`.

### Wire protocol (see `docs/protocol.md`; codecs in `host/protocol.py` + `host/protocol.h`)
- **24-byte little-endian `MsgHeader`**: `u8 type, u8 flags, u16 _pad, u32 seq,
  u32 length, u32 _pad2, u64 timestampUs`.
- Handshake: `HELLO(0x01) → DEVICE_INFO(0x02) → STREAM_CONFIG(0x03) →
  STREAM_CONFIG_ACK(0x04)`, plus `PING/PONG(0x06/0x07)`, `TEARDOWN(0x05)`.
- Video: `VIDEO_PARAM_SETS(0x10)` = `u8 count` then per-NAL `u16 len + NAL` (SPS,PPS,
  raw, no start code). `VIDEO_FRAME(0x11)` = one AVCC access unit `[u32 len][NAL]…`;
  `flags` bit0=IDR, bit1=paramSetsPrecede, bit2=discardable.
- **⚠️ The NAL length prefixes inside video messages are BIG-ENDIAN** (AVCC /
  network order, so VideoToolbox ingests them with zero rewriting) — the *only*
  exception to the otherwise all-little-endian wire. Everything else is LE.
- `STREAM_CONFIG.colorPrimaries = 1` (BT.709), transfer=13 (sRGB), matrix=1,
  fullRange=1 (see color gotcha below).

---

## Repo layout

| Path | Role |
|------|------|
| `docs/mirror-milestone-plan.md` | Full M0–M9 implementation plan (+ adversarial verification results) |
| `docs/protocol.md` | Byte-exact wire spec |
| `docs/milestone-status.md` | Milestone tracker + acceptance evidence |
| `docs/m3-m4-capture-encode.md` | FFmpeg capture/encode command, flag rationale, color note |
| `spikes/usbmux-throughput/` | Spike #1 (AFC) throughput+latency probes + `RESULTS.md` |
| `spikes/stage-b/` | Live-socket perf (`perf_host*.py`) + `RESULTS.md` (transport asymmetry) |
| `spikes/m5-freeze/` | M5 frozen-image root-cause probes + `RESULTS.md` (bisection ladder) |
| `host/protocol.py` / `protocol.h` | Canonical wire codec (Python + C header) |
| `host/handshake_host.py` | M2 handshake-only harness |
| `host/stream_host.py` | **M5 live streamer**: ffmpeg → NAL pump → usbmux. Has `--test` (testsrc2) |
| `host/capture_encode.ps1` | M3/M4 standalone capture→encode→.h264 (verify recipe inside) |
| `ios/Sources/LinkServer.swift` | Socket listener, MsgHeader dispatch, handshake, drives decoder→renderer |
| `ios/Sources/Decode/VideoDecoder.swift` | VTDecompressionSession, SPS/PPS→CMFormatDesc, AVCC AU → CVPixelBuffer |
| `ios/Sources/Render/DisplayRenderer.swift` | Metal NV12→RGB, letterbox, clean-aperture, color |
| `ios/Sources/Render/MetalDisplayView.swift` | CAMetalLayer UIView + CADisplayLink + SwiftUI bridge |
| `ios/Sources/Render/Shaders.metal` | fullscreen-triangle vertex + YCbCr→RGB fragment |
| `ios/Sources/App.swift` | SwiftUI; handshake screen ↔ live MetalDisplayView; **debug overlay** |
| `ios/project.yml` | XcodeGen spec | `.github/workflows/ios-build.yml` | CI |

---

## Milestone status

| # | What | Status |
|---|------|--------|
| M0 | iPad LinkServer (framing + handshake) | ✅ done, verified |
| M1 | Transport re-test (iproxy host→device throughput) | ⏸ deferred (optimization gate, not a blocker) |
| M2 | Host handshake harness | ✅ done (Python) |
| M3 | Capture (FFmpeg `ddagrab`) | ✅ done, verified live |
| M4 | Encode (FFmpeg `h264_nvenc`) | ✅ done, verified live |
| M5 | Host NAL pump + iPad VideoToolbox decode | ✅ done, verified live (60 fps end-to-end) |
| M6 | Metal render, color-managed | ✅ done, verified live (native-res/120Hz awaits virtual display) |
| M7 | Color-correctness pass (deltaE test pattern) | ⬜ |
| M8 | Lossless still path + mode swap | ⬜ |
| M9 | Pacing (INFLIGHT≤2, FRAME_PRESENTED) + robustness | ⬜ |

**Key measurements:** usbmux ~1.25 Gbps (AFC) / device→host 1381 Mbps + RTT
~0.74 ms (socket); **host→device measured only 272 Mbps via the Python relay** —
suspected Python-forwarder artifact, unresolved; design conservatively for 272
Mbps host→device (see `spikes/stage-b/RESULTS.md`). M3/M4 stream verified: High
profile, 4:2:0 8-bit full-range, VUI 1/13/1, decodes clean, I/P only. M5 live:
the socket path sustains 60 msg/s with ease (an early ~18 fps ceiling was the
pump's Python parser, since fixed — never the transport).

---

## Hard-won gotchas (do not relearn these)

- **COLOR — tag BT.709/sRGB, NOT P3.** The Windows desktop is sRGB-primary. Tag the
  stream `primaries=1 (BT.709), transfer=13 (sRGB), matrix=1, full-range` and let
  the iPad **color-manage sRGB→P3** (CAMetalLayer `.bgra8Unorm` + `colorspace sRGB`,
  never `displayP3`). Tagging P3 while feeding sRGB values = **oversaturation**.
- **FFmpeg color:** do the RGB→NV12 conversion in **libswscale**
  (`scale=out_color_matrix=bt709:out_range=full,format=nv12`) and tag the VUI
  **separately** via the `setparams` filter. Never let NVENC do the CSC (it may use
  BT.601 regardless of the tag). The `-color_primaries`/`-color_trc` *output* flags
  are **silently dropped** in this ffmpeg build — `setparams` is mandatory.
- **FFmpeg misc:** `-level auto` (GM204 rejects hard `5.2`); `-aud 1` so the pump
  splits access units on AUD; strip AUD + SPS/PPS from the per-frame AVCC (VT wants
  clean VCL; param sets go via VIDEO_PARAM_SETS).
- **`ddagrab` needs an interactive desktop session** (fails under service/SSH; works
  via our powershell bridge). Captures at the monitor's native size.
- **iOS 16 target:** do NOT use `kVTVideoDecoderSpecification_*HardwareAccelerated`
  (iOS 17+ symbols; H.264 is always HW-decoded anyway — pass `nil`).
  `CVBufferCopyAttachment` returns a **managed** `CFTypeRef?` (no `.takeRetainedValue()`).
- **XcodeGen/CI:** use `xcodebuild -scheme` (not `-target`) with `-derivedDataPath`;
  build on `macos-15` (Xcode 16 reads the project format XcodeGen emits).
- **Process hygiene between runs:** kill stale `ffmpeg.exe` + `python.exe` (leftover
  `usbmux forward` relays fight over port 7000), and **force-quit + relaunch the iPad
  app** if a connection sticks ("Connection closed by device after 0 of 24 bytes"
  = stale/half-open connection).
- **THE M5 FREEZE (a week lost — see `spikes/m5-freeze/RESULTS.md`):** a slow pump
  reader (per-byte Python start-code scan + quadratic buffer re-scan) backpressured
  ffmpeg's stdout pipe; a stalled graph makes **`ddagrab` emit dups of a stale
  cached frame** (dup_frames CFR catch-up) — the wire itself carries a frozen
  desktop while every iPad stage "succeeds". Morals: (1) hot-path byte scanning in
  Python must use C-speed primitives (`bytes.find`) and scan incrementally; (2) a
  too-slow CONSUMER shows up as stale CONTENT, not as low fps alone; (3) testsrc2
  (lavfi) masks stalls — no wall clock, frames just come out late-but-unique;
  (4) NVENC CBR pads with filler NALs (stripped by `avcc_from_au_for_decode`), so
  framemd5 "all frames unique" does NOT prove live content — extract PNGs and look.

---

## CURRENT STATE

**The mirror works end-to-end (2026-07-02):** live 1920×1080 desktop on the iPad,
steady 60 fps on the wire, visually smooth with only slight lag, colors correct.
The week-long "frozen image" bug was **host-side**: the pump's Python AU parser was
too slow, ffmpeg stalled on its stdout pipe, and `ddagrab` emitted dups of a stale
frame (full story + bisection ladder in `spikes/m5-freeze/RESULTS.md`; fix landed
in `host/protocol.py` `_find_start_codes` + `host/stream_host.py`
`AccessUnitParser`, fuzz-tested against the old scanner as oracle). The iPad code
needed **no changes** — both on-device hypotheses (SwiftUI churn, stale IOSurface
textures) were disproven by the `--test` bisection.

### Next work, in order
1. **Cleanup owed:** remove the debug overlay + counters from `App.swift`/
   `LinkServer.swift`/`MetalDisplayView.swift`/`DisplayRenderer.swift` (the
   `dec/pres/drew/texFail/noDrw` plumbing and `onRenderResult`; the render `Int`
   return codes can stay). Requires a CI build + re-sideload.
2. **M7 color pass:** deltaE test pattern end-to-end.
3. **M8 lossless still path**, **M9 pacing** (INFLIGHT≤2 / FRAME_PRESENTED — note
   the pump currently has NO frame dropping: any consumer slower than capture
   recreates the stall→dup freeze; M9 should add latest-wins dropping).
4. Then the virtual-display milestone (2732×2048 IddCx) for native res.

### How to run a live stream (once app is listening on :7000)
```bash
# from WSL; kill stale procs first if a prior run stuck:
powershell.exe -NoProfile -Command 'taskkill /F /IM ffmpeg.exe; taskkill /F /IM python.exe'
cp host/*.py /mnt/c/Users/aidan/ipaddisplay-spike/host/
powershell.exe -NoProfile -Command 'cd C:\Users\aidan\ipaddisplay-spike\host; py -3.12 -u stream_host.py'        # desktop
powershell.exe -NoProfile -Command 'cd C:\Users\aidan\ipaddisplay-spike\host; py -3.12 -u stream_host.py --test' # moving pattern
```
The user must have force-relaunched the app so it shows "listening on :7000".

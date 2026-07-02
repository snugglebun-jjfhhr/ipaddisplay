# Mirror Milestone — status

Tracks docs/mirror-milestone-plan.md §2. Updated as milestones land.

| # | Milestone | Status | Evidence |
|---|-----------|--------|----------|
| M0 | iPad LinkServer (24B LE MsgHeader framing + dispatch) | ✅ done | builds on CI; handshake verified |
| M1 | Transport re-test (iproxy host→device) | ⏸ deferred | optimization gate, not a blocker; design assumes ~272 Mbps host→device. Needs libimobiledevice on Windows. |
| M2 | Host skeleton + handshake | ✅ done (Python harness) | acceptance below. Production C++ host deferred to M3 (reuses host/protocol.h). |
| M3 | Capture (DXGI Desktop Duplication) | ✅ done (FFmpeg ddagrab) | verified live, see below |
| M4 | Encode (NVENC H.264) | ✅ done (FFmpeg h264_nvenc) | verified live, see below |
| M5 | Motion path wired end-to-end | ✅ done | live desktop on iPad at 60 fps; acceptance below |
| M6 | Metal render (P3-managed CAMetalLayer) | ✅ done | renders the live stream; native-res + 120Hz deferred to the virtual-display milestone |
| M7 | Color-correctness pass (VUI vs CSC) | ⬜ | top risk |
| M8 | Lossless still path + mode swap | ⬜ | |
| M9 | Pacing + robustness | ⬜ | |

## M0 + M2 acceptance (2026-06-28)

Ran `host/handshake_host.py` over `pymobiledevice3 usbmux forward` against the
sideloaded M0 app on iPad13,8. Full FSM completed first try:

```
-> HELLO            codecMask=H264|HEVC maxBitrate=80000kbps
<- DEVICE_INFO      native=2732x2048 maxDecode=2732x2048 decoders=H264|HEVC refresh=120Hz P3=1 hwHEVC=1
-> STREAM_CONFIG    2732x2048 codec=0 chroma=0 bitDepth=8 fullRange=1 prim=12 transfer=13 matrix=1
<- STREAM_CONFIG_ACK ok=1 reason=0
   20x PING/PONG    RTT median 0.57 ms (min 0.46, max 0.83)
PASS
```

Byte-for-byte conformance across docs/protocol.md, host/protocol.py,
host/protocol.h, host/handshake_host.py, ios/Sources/LinkServer.swift verified by
adversarial cross-check (workflow Verify phase). The little-endian 24-byte
MsgHeader + handshake payloads interoperate on the wire.

## Next decision: M3/M4 need a Windows native toolchain

M3 (DXGI capture) and M4 (NVENC encode) are C++ and require MSVC Build Tools (or
Visual Studio) on the Windows host — not yet installed. Options when ready:
install VS Build Tools + Video Codec SDK, or shortcut M3+M4 with FFmpeg
(`ddagrab` + `h264_nvenc`) to get a first moving image faster, then replace with
the hand-rolled shared-ID3D11Device path for zero-copy + lower latency.

## M3 + M4 acceptance (2026-06-28) — FFmpeg shortcut

`host/capture_encode.ps1` (ddagrab GPU capture -> libswscale CPU CSC -> h264_nvenc)
ran live on the GTX 970. Captured the actual primary monitor (1920x1080 — the
2732x2048 target awaits the virtual-display milestone). 5s / 300 frames, real-time
(speed 0.99x), 80 Mbps CBR, 0 drops.

Verification (all pass):
- profile=High, yuvj420p (4:2:0 8-bit full range), has_b_frames=0, level=50.
- SPS VUI: video_full_range_flag=1, colour_primaries=1 (BT.709), transfer=13
  (sRGB), matrix_coefficients=1 (BT.709). Color matrix applied == matrix tagged
  (CSC in libswscale, VUI via setparams) — 601/709 trap structurally avoided.
- frame_cropping correct (1088 coded -> 1080 shown; no garbage edge).
- decodes clean (ffmpeg -xerror exit 0); frame types I/P only, IDR every 60.

Deviations from plan, flagged: FFmpeg shortcut instead of hand-rolled C++ (no MSVC
toolchain); color tagged BT.709/sRGB (true source) not P3 (review fix — iPad
color-manages); -level auto (GM204 max) not hard 5.2. Hand-rolled zero-copy C++
DXGI+NVENC remains the deferred latency optimization.

Note for M5: STREAM_CONFIG must send colorPrimaries=1 (not 12) to match the stream.

## M5 + M6 acceptance (2026-07-02)

`host/stream_host.py` (ffmpeg ddagrab/NVENC -> Python NAL pump -> usbmux :7000)
against the sideloaded app on iPad13,8: live 1920x1080 desktop mirrored on the
iPad at a steady 60 fps wire rate (~3 Mbit/s of stripped-AVCC payload for light
desktop motion), decode -> Metal render confirmed visually smooth ("50-60 fps,
small lag") with correct colors. On-device counters: dec == pres == drew,
texFail = 0, noDrw = 0.

**The week-long "frozen/stale image" bug was HOST-side, not iPad-side.** Root
cause: `protocol._find_start_codes` was a per-byte pure-Python loop and
`AccessUnitParser.feed` re-scanned (and re-copied) the whole buffer on every
64 KiB read — quadratic per AU. CBR-padded AUs (~660 KB at fps=15) cost ~0.5 s
each, throttling the reader below the capture rate; pipe backpressure stalled
ffmpeg's filter graph, and ddagrab (behind its CFR schedule, dup_frames=true)
emitted duplicates of a stale cached frame. The wire really carried a frozen
desktop; the iPad displayed it faithfully. Fixed by making start-code search
C-speed (`bytes.find`) and the AU parser incremental (scan only new bytes).
Diagnostics + probes preserved in `spikes/m5-freeze/`.

Deferred out of M5/M6: native 2732x2048 + 120 Hz (needs the virtual display,
post-M6 milestone); debug overlay removal (kept while M7-M9 land).

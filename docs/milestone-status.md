# Mirror Milestone — status

Tracks docs/mirror-milestone-plan.md §2. Updated as milestones land.

| # | Milestone | Status | Evidence |
|---|-----------|--------|----------|
| M0 | iPad LinkServer (24B LE MsgHeader framing + dispatch) | ✅ done | builds on CI; handshake verified |
| M1 | Transport re-test (iproxy host→device) | ⏸ deferred | optimization gate, not a blocker; design assumes ~272 Mbps host→device. Needs libimobiledevice on Windows. |
| M2 | Host skeleton + handshake | ✅ done (Python harness) | acceptance below. Production C++ host deferred to M3 (reuses host/protocol.h). |
| M3 | Capture (DXGI Desktop Duplication, C++) | ⬜ next | needs MSVC toolchain on Windows |
| M4 | Encode (NVENC H.264, C++) | ⬜ | |
| M5 | Motion path wired end-to-end | ⬜ | first moving image |
| M6 | Metal render (native 2732×2048, P3, 60Hz) | ⬜ | |
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

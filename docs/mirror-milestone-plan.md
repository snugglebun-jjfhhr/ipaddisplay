# Mirror Milestone — Implementation Plan

**Goal:** First real wired image. Windows captures a monitor → H.264-encodes (NVENC, GTX 970) → ships whole access units over a usbmux TCP socket → iPad VideoToolbox-decodes → renders to a native 2732×2048 CAMetalLayer. A second, lossless reference-still path bypasses the codec for exact color on static frames. v1 is a **whole-layer mode swap**, not a per-tile compositor (that is post-v1). Priorities, in order: (1) native-resolution fidelity, (2) color fidelity, (3) refresh last (60 Hz pinned, fine over 120).

---

## 1. Overview + End-to-End Dataflow

```
 WINDOWS HOST (Ryzen 9 5900X + GTX 970 / GM204)                         iPad Pro 12.9" M1 (iPadOS 27)
 ┌──────────────────────────────────────────────┐                      ┌────────────────────────────────────────┐
 │ capture (C++)            encode (C++/NVENC)   │                      │ transport (LinkServer)   decode (VT)     │
 │ ┌────────────┐  ID3D11   ┌──────────────────┐ │   usbmux TCP :7000   │ ┌──────────────┐  AU+pts ┌────────────┐ │
 │ │ DXGI Dup   │  Device   │ NvEncEncodePicture│ │  (host = client,     │ │ recv framed  │───────► │ VTDecompr. │ │
 │ │ AcquireNext│──tex────► │ H.264 4:2:0 8-bit │ │   iPad = listener)   │ │ MsgHeader    │         │ Session    │ │
 │ │ Frame      │  (GPU)    │ ARGB→NV12 (ASIC)  │ │  ◄──── one socket ───►│ │ split AU/    │         └─────┬──────┘ │
 │ └─────┬──────┘           └────────┬─────────┘ │     (libusbmuxd       │ │ still tiles  │   CVPixelBuffer │       │
 │       │ WAIT_TIMEOUT             Annex-B AU   │      usbmuxd_connect) │ └──────┬───────┘   (NV12 420f)  ▼       │
 │       │ = STATIC                   │           │                      │        │          render (Metal)        │
 │       ▼                            ▼           │   ◄── FRAME_PRESENTED │        │ STILL    ┌────────────────┐    │
 │ still (zstd/QOI BGRA8 full frame)──┴──────────┼──── PING/PONG ───────►│        └────────► │ CAMetalLayer    │   │
 │                                               │                      │  motion: YCbCr→RGB │ 2732x2048 P3    │   │
 │                                               │                      │  still:  blit 1:1  │ 60Hz, bgra8     │   │
 └──────────────────────────────────────────────┘                      └────────────────────┴────────────────────┘
   capture+encode share ONE ID3D11Device (zero CPU readback)             decode→render zero-copy via IOSurface
```

End-to-end target: **~30–50 ms**. Capture/transport are not the bottleneck; **NVENC encode (~10–12 ms)** plus pacing dominate.

---

## 2. Build Order (numbered milestones, starting from the existing PerfServer app)

1. **M0 — Rename + grow the iPad listener.** `ios/Sources/PerfServer.swift` → `LinkServer.swift`. Keep the BSD-socket listener on `0.0.0.0:7000`, TCP_NODELAY. Switch wire byte order **big-endian → little-endian** (the perf spike was disposable). Grow recv buffers beyond 256 KB and read length-bounded, not fixed-chunk. Implement the 24-byte `MsgHeader` reader + dispatch switch (stubs for each type). App stays foreground, `isIdleTimerDisabled = true`.
2. **M1 — Transport re-test (no code commitment).** Prove the 272 Mbps cap is the Python forwarder, not usbmux. Run the C `iproxy` relay + `spikes/stage-b/perf_host_iproxy.py` (commands in §5). Gate: host→device should jump to ~900–1250 Mbps. If it does, commit to in-process `libusbmuxd usbmuxd_connect()` for production; if iproxy *also* caps near 272, the cause is AMDS's per-channel window and we accept it / investigate further.
3. **M2 — Host skeleton + handshake.** Standalone C++ host. libusbmuxd `usbmuxd_connect(handle, 7000)` → raw fd. Implement `HELLO` → `DEVICE_INFO` → `STREAM_CONFIG` → `STREAM_CONFIG_ACK` FSM. `PING`/`PONG` keepalive. No video yet.
4. **M3 — Capture.** Greenfield C++ DXGI Desktop Duplication: one `ID3D11Device` on the GTX 970 (`D3D11_CREATE_DEVICE_BGRA_SUPPORT`), `IDXGIOutput1::DuplicateOutput` (BGRA8 SDR), `AcquireNextFrame`/`ReleaseFrame` loop, copy into a 3-deep ring of own textures, `ReleaseFrame` immediately after the GPU→GPU `CopyResource`. `DXGI_ERROR_WAIT_TIMEOUT` = static signal; `ACCESS_LOST`/`ACCESS_DENIED` recovery state machine.
5. **M4 — Encode.** NVENC (Video Codec SDK 12.x) sharing the capture `ID3D11Device` (`NV_ENC_DEVICE_TYPE_DIRECTX`). Verify caps at runtime (expect H.264-only, no HEVC). H.264 High, 4:2:0 8-bit, P4 + `ULTRA_LOW_LATENCY`, CBR ~80 Mbps, no B-frames, infinite GOP, intra-refresh, `repeatSPSPPS=1`, full VUI. Emit Annex-B. Verify a local `.h264` dump decodes.
6. **M5 — Motion path wired end-to-end.** Host: split NALs, send `VIDEO_PARAM_SETS` on change, `VIDEO_FRAME` (AVCC, scatter-gather `writev`/`WSASend`). iPad: greenfield `VideoDecoder` (VTDecompressionSession, NV12 full-range, IOSurface/Metal-compatible) → `DecodedFrame`. **First moving image.**
7. **M6 — Render.** Greenfield `MetalDisplayView` (layerClass = CAMetalLayer) + `DisplayRenderer` + `Shaders.metal`. `drawableSize = 2732×2048`, `pixelFormat = .bgra8Unorm` (NOT _srgb), `colorspace = displayP3`, `framebufferOnly = false`. Motion fullscreen-triangle YCbCr→RGB shader; pacing via drawable back-pressure + `CADisplayLink` pinned 60/60/60. `UIRequiresFullScreen=true`.
8. **M7 — Color correctness pass.** Validate VUI vs actual NVENC CSC with a known test pattern (the BT.601-vs-BT.709 risk). Read CVPixelBuffer color attachments; if NVENC's internal CSC is wrong, switch to a D3D11 compute-shader RGB→NV12 with an exact BT.709 full-range matrix.
9. **M8 — Lossless still path + mode swap.** On `WAIT_TIMEOUT` ≥ ~80 ms: `STILL_BEGIN` → `STILL_TILE` (zstd/QOI BGRA8, tileCount=1 full-frame for v1) → `SET_LAYER{STILL}` → `STILL_COMMIT`. iPad uploads to a private texture, `MTLBlitCommandEncoder.copy` bit-exact into drawable, present-and-idle. First dirty rect → `SET_LAYER{MOTION}` + forced IDR. **Two-mode pipeline complete.**
10. **M9 — Pacing + robustness.** `FRAME_PRESENTED` ACK feedback, INFLIGHT≤2 with host-side frame-drop, IDR-on-(re)connect, session-reset recovery (`kVTInvalidSessionErr` → `decoderNeedsKeyframe`). Latency measurement against the 30–50 ms target.

---

## 3. Wire Protocol Spec

One TCP connection. iPad listens `:7000`, host connects via `usbmuxd_connect`. **Little-endian** on the wire (x86-64 + ARM64, zero byteswaps). Whole access units, never fragmented to MTU. Header + payload via a single scatter-gather write. Reserve device port `:7001` for a future bulk/still channel (post-v1 compositor).

### Header — 24 bytes, packed
```c
struct MsgHeader {
  uint8_t  type;        // MessageType
  uint8_t  flags;       // type-specific bitfield
  uint16_t _pad;        // 0
  uint32_t seq;         // monotonic per-message sequence
  uint32_t length;      // payload bytes following (0..~24M)
  uint32_t _pad2;       // 0 (keeps timestampUs 8-byte aligned)
  uint64_t timestampUs; // host monotonic capture clock, microseconds
};
```

### Message types
| Code | Name | Dir | Payload |
|------|------|-----|---------|
| 0x01 | HELLO | H→D | magic 'IPDP'(4), u16 protoVer=1, u16 hostFlags, u32 codecMask(bit0 H264,bit1 HEVC), u32 maxBitrateKbps |
| 0x02 | DEVICE_INFO | D→H | u16 nativeW=2732, nativeH=2048, maxDecodeW, maxDecodeH, u32 decoderMask, u8 maxRefreshHz, supportsP3, hwHEVC |
| 0x03 | STREAM_CONFIG | H→D | u16 W,H; u8 fpsCap, codec(0=H264), chroma(0=420), bitDepth(8), fullRange(1), colorPrimaries(VUI), transfer, matrix |
| 0x04 | STREAM_CONFIG_ACK | D→H | u8 ok, u8 reason |
| 0x05 | TEARDOWN | both | — |
| 0x06 / 0x07 | PING / PONG | both | opaque echo (keepalive + live RTT) |
| 0x08 | FRAME_PRESENTED | D→H | u32 ackSeq, u64 presentTimeUs, u16 decodeQueueDepth, u8 layerNow, u8 dropped |
| 0x09 | ERROR | both | u16 code, utf8 msg |
| 0x10 | VIDEO_PARAM_SETS | H→D | u8 count; per NAL: u16 nalLen, bytes (H264 SPS+PPS; AVCC param-set form) |
| 0x11 | VIDEO_FRAME | H→D | one AU AVCC: [u32 nalLen][nal]…  flags bit0=IDR, bit1=paramSetsPrecede, bit2=discardable |
| 0x20 | SET_LAYER | H→D | u8 layer (0=MOTION,1=STILL) — authoritative |
| 0x21 | STILL_BEGIN | H→D | u32 stillId, u16 fullW,fullH, u8 pixfmt(0=BGRA8), comp(0=raw,1=QOI,2=zstd), u16 tileCount, u8 primaries, transfer |
| 0x22 | STILL_TILE | H→D | u32 stillId, u16 x,y,w,h, u32 rawBytes, compBytes; then compBytes |
| 0x23 | STILL_COMMIT | H→D | u32 stillId → decompress+upload all tiles, atomic swap to STILL, ack via FRAME_PRESENTED |

### Session FSM
connect → host `HELLO` → dev `DEVICE_INFO` → host picks config (2732×2048, H264 High, 4:2:0 8-bit, fullRange=1, P3 primaries) → host `STREAM_CONFIG` → dev `STREAM_CONFIG_ACK{ok}` → host **MUST** send `VIDEO_PARAM_SETS` then `VIDEO_FRAME(IDR)` first. On **every (re)connect** the host always resends param sets + IDR (session-scoped, not persisted).

---

## 4. Module Maps

### Host (Windows, C++)
| Module | Responsibility | Key types |
|--------|----------------|-----------|
| `capture/` | DXGI Desktop Duplication, GPU texture ring, static detect, device-loss recovery | `GpuContext`, `CapturedFrame`, `IFrameSink` |
| `encode/` | NVENC H.264, zero-copy register/map, VUI, forceIDR, intra-refresh | `IEncoder`, `EncodedUnit` |
| `transport/` | libusbmuxd connect, hotplug state machine, scatter-gather send | `ipad_transport_t`, `transport_send/recv` |
| `still/` | static-frame capture → zstd/QOI tiles | STILL_* emitters |
| `protocol/` + `session/` | MsgHeader codec, handshake FSM, pacing (INFLIGHT≤2, drop) | `MsgHeader`, `MessageType` |

capture and encode **share one `ID3D11Device`** (a CopyResource lands directly in an NVENC-registered input texture). This is the reason the host is C++ end-to-end (no FFI seam at the hottest boundary).

### iPad (Swift)
| Module | Responsibility | Key types |
|--------|----------------|-----------|
| `LinkServer.swift` (was PerfServer) | listener :7000, MsgHeader read, dispatch, FRAME_PRESENTED/PONG | `MsgHeader`, dispatch switch |
| `Decode/VideoDecoder.swift` | Annex-B→AVCC, SPS/PPS→CMVideoFormatDescription, VTDecompressionSession, IDR-wait recovery | `DecodedFrame`, `VideoDecoderDelegate` |
| `Render/DisplayRenderer.swift` + `MetalDisplayView.swift` + `Shaders.metal` | CAMetalLayer setup, motion YCbCr→RGB, still blit, mode swap, 60Hz pacing | `DisplayRenderer`, `RenderMode`, `VideoFrame`, `StillFrame` |
| `StillReceiver` | tile assembly → Metal texture | STILL_* handlers |

decode→render is **zero-copy** (IOSurface-backed CVPixelBuffer → `CVMetalTextureCacheCreateTextureFromImage`).

---

## 5. Transport Recommendation (resolved) + Re-test Commands

**Recommendation:** Eliminate the Python relay. Production host uses **in-process `libusbmuxd usbmuxd_connect(handle, 7000)`** → raw OS socket fd (one fewer copy than any relay, event-based hotplug via `usbmuxd_subscribe`). Fallback / test harness = `iproxy` (C relay, sustains ~gigabit). **Rejected for production:** pymobiledevice3 `usbmux forward` — the 272 Mbps culprit.

**Why the asymmetry:** `pymobiledevice3 usbmux forward` is a per-chunk Python select/recv/sendall pump under the GIL on the host→device leg → pinned ~272 Mbps. The same physical path moved 1252 Mbps under AFC push (large contiguous buffers, no re-chunk). device→host hit 1381 Mbps because the Python copy is masked there. Conclusion: bottleneck is the Python forwarder, not usbmux/USB/device.

**Honest open question:** This is a *hypothesis* until M1 proves it. If `iproxy` (C) *also* caps near 272, the cause is AMDS's per-channel usbmux window, independent of relay language, and in-process connect won't help — we'd accept the cap (still fine for 80 Mbps motion; only the 16.8 MB lossless still suffers, ~134 ms vs ~500 ms).

**Host tuning:** `TCP_NODELAY` on the connect fd, `SO_SNDBUF ≥ 4 MiB`, write 256 KiB–1 MiB contiguous blocks, single connection.

**Re-test commands (runnable now, existing hardware):**
```
# verify the device is visible to Apple's usbmuxd first
idevice_id -l

# window 1: C relay (NOT the Python forwarder)
iproxy 7000 7000

# window 2: speak PerfServer protocol against the local relay port
py -3.12 spikes/stage-b/perf_host_iproxy.py --mb 256 --reps 5

# PASS: host->device ~900-1250 Mbps (was 271.9 via python forward); RTT ~0.7ms median
```

---

## 6. Per-Component Key APIs / Flags

**Capture:** `D3D11CreateDevice(D3D11_CREATE_DEVICE_BGRA_SUPPORT, FL 11_0)`; `IDXGIOutput1::DuplicateOutput` (SDR BGRA8) / `IDXGIOutput5::DuplicateOutput1` (FP16 scRGB, HDR later); `AcquireNextFrame`/`ReleaseFrame` (release immediately after copy); `DXGI_OUTDUPL_FRAME_INFO` (LastPresentTime, AccumulatedFrames); `GetFrameDirtyRects`/`GetFrameMoveRects`; `CopyResource`. `WAIT_TIMEOUT`=static, `ACCESS_LOST`/`ACCESS_DENIED`=recover.

**Encode:** `NvEncodeAPICreateInstance`; `NvEncOpenEncodeSessionEx(NV_ENC_DEVICE_TYPE_DIRECTX, ID3D11Device*)`; `NvEncGetEncodeGUIDs`/`NvEncGetEncodeCaps` (verify H264 + 420, expect no HEVC); preset `P4` + `NV_ENC_TUNING_INFO_ULTRA_LOW_LATENCY`; `NV_ENC_PARAMS_RC_CBR`, `averageBitRate=80M`, `vbvBufferSize=bitrate/fps`; `frameIntervalP=1` (no B), `enableLookahead=0`, `enableAQ=0`; `gopLength=NVENC_INFINITE_GOPLENGTH`, `repeatSPSPPS=1`, `enableIntraRefresh=1`; **VUI**: `videoSignalTypePresentFlag=1, videoFullRangeFlag=1, colourPrimaries=12 (P3-D65), transferCharacteristics=13 (sRGB), matrixCoefficients=1 (BT.709)`; `NvEncRegisterResource(NV_ENC_INPUT_RESOURCE_TYPE_DIRECTX, ARGB)` cached per texture; `NvEncEncodePicture` (`NV_ENC_PIC_FLAG_FORCEIDR` on entry); sync `NvEncLockBitstream`.

**Transport:** `usbmuxd_connect(handle, 7000)`, `usbmuxd_get_device_list`, `usbmuxd_subscribe` (UE_DEVICE_ADD/REMOVE); `iproxy` for tests; `TCP_NODELAY`, `SO_SNDBUF≥4MiB`; `writev`/`WSASend` scatter-gather.

**Decode:** `VTDecompressionSessionCreate` (`RequireHardwareAcceleratedVideoDecoder=true`, `RealTime=true`, `MaximizePowerEfficiency=false`); `CMVideoFormatDescriptionCreateFromH264ParameterSets` (nalUnitHeaderLength:4); `VTDecompressionSessionDecodeFrame(_EnableAsynchronousDecompression)`; output `kCVPixelFormatType_420YpCbCr8BiPlanarFullRange` (420f), `MetalCompatibility`, `IOSurfaceProperties`; read `kCVImageBufferColorPrimaries/TransferFunction/YCbCrMatrix`. `kVTInvalidSessionErr`→teardown+request IDR.

**Render:** `CAMetalLayer` (`drawableSize=2732×2048`, `pixelFormat=.bgra8Unorm` NOT _srgb, `colorspace=displayP3`, `framebufferOnly=false`, `maximumDrawableCount=3`, `presentsWithTransaction=false`); `CVMetalTextureCacheCreateTextureFromImage` (.r8Unorm plane0, .rg8Unorm plane1); `MTLBlitCommandEncoder.copy` for still; fullscreen-triangle for motion; `commandBuffer.present(drawable); commit()`; `CADisplayLink.preferredFrameRateRange = CAFrameRateRange(60,60,60)`; `UIScreen.nativeScale=2.0`; Info.plist `UIRequiresFullScreen=true`. Retain CVMetalTextures until `addCompletedHandler`.

---

## 7. Consolidated Latency Budget (one 5.6 MP frame @ 60 Hz, ~16.7 ms interval)

| Stage | Typical | Worst | Notes |
|-------|---------|-------|-------|
| Capture (Acquire + GPU CopyResource) | 1–3 ms | 3 ms | bandwidth-trivial; scheduling jitter dominates |
| Encode (NVENC H.264 ULL, no B) | ~10 ms | ~12 ms | dominant cost; ~68% ASIC util at 60fps; rises if GM204 < 500 MP/s |
| Transport (motion AU ~104 KB over ~1 Gbps) | 1–3 ms | 3 ms | + 0.74 ms RTT; **not** on critical path |
| Decode (VT HW, no reorder) | 4–6 ms | 8 ms | session recreate on bg is a transient tail |
| Render compute | <0.5 ms | 0.5 ms | M1 wildly over-provisioned |
| Present pacing (vsync + drawable backpressure) | ~17 ms | ~25 ms | ~1–1.5 frames; trim 8 ms via maxDrawableCount=2 |
| Pacing window (INFLIGHT=2) | up to 33 ms | — | **policy**, not framing; use 1 for lower latency |
| **End-to-end** | **~30–50 ms** | — | encode + present + pacing are the levers |

Lossless still: framing free; **16.8 MB BGRA8 full frame ≈ 134 ms at ~1 Gbps** (matches AFC 107 ms floor) vs ~500 ms if stuck at 272 Mbps — the concrete reason the transport question matters for fidelity. Static→still trigger latency = one Acquire timeout (~17–80 ms debounce).

---

## 8. Top Risks + Mitigations

1. **Color: NVENC RGB→YUV CSC may use BT.601 regardless of the BT.709 `matrixCoefficients` VUI tag** (priority #2). → M7 test pattern; if confirmed, do exact-matrix RGB→NV12 in a D3D11 compute shader, feed NV12. iPad must drive color from CVPixelBuffer **attachments**, not assumptions, and trust `STREAM_CONFIG` over the bitstream.
2. **272 Mbps host→device is still an open hypothesis** until M1. → Run the iproxy re-test *before* writing any C; if iproxy also caps, the limit is AMDS's window and we accept it (motion unaffected; only lossless still slows).
3. **2732 is not mod-16; encoder pads coded width to 2736.** → SPS frame_cropping must crop to 2732; render samples clean aperture (`CMVideoFormatDescriptionGetCleanAperture`), not coded width, or 4 garbage columns appear. (2048 is fine.)
4. **TCP head-of-line blocking**: a multi-MB lossless still queued ahead of motion stalls video. → v1 whole-layer swap means only one path is active + drop in-flight motion before going still; real fix is the reserved `:7001` bulk channel post-v1.
5. **DXGI returns black for fullscreen-exclusive/DRM content.** → Low risk for an editing-app target; capturing the future VDD sidesteps it. Document.
6. **Cursor is not composited into the duplication surface** (separate `GetFramePointerShape` + PointerPosition). → Open ownership; recommend shipping pointer pos/shape as metadata and compositing in the Metal renderer (lower bandwidth, clean for the still path).
7. **Session/device loss**: GPU TDR → `ACCESS_LOST` invalidates the NVENC-registered ring; `kVTInvalidSessionErr` on iPad background. → Recreate device + re-register on host; teardown + `awaitingIDR` + request keyframe on iPad. Host **must** resend param sets + IDR on every reconnect.
8. **CVPixelBuffer pool starvation / unretained CVMetalTextures** → green/garbage frames or decode stall. → Keep ≤3 buffers in flight, copy into Metal texture within a frame, retain CVMetalTextures until `addCompletedHandler`.
9. **`.bgra8Unorm_srgb` + displayP3 tag double-applies gamma.** → Use plain `.bgra8Unorm`; keep both still and motion paths in Display P3 so `layer.colorspace` never changes mid-stream (avoids flash).
10. **iOS backgrounding kills the listener** (`connect()` → ECONNREFUSED). → App stays foreground, `isIdleTimerDisabled`; host reconnect backoff 250 ms→2 s.

---

## 9. Fork vs Greenfield (explicit)

- **Sunshine (host) — GREENFIELD, reference only.** Read `src/platform/windows/display_vram.cpp` + `display_base.cpp` for the ACCESS_LOST / secure-desktop recovery state machine, and its `src/nvenc` wrapper + NAL handling, **as reference**. Do **not** fork: it drags GPLv3, CMake/session abstractions, and its whole app is built around the NVIDIA GameStream session model (RTSP handshake, ENet/UDP, AES-RTP, Reed-Solomon FEC, ~1 KB MTU fragmentation) — all dead weight on a reliable in-order lossless usbmux TCP link. The clean ~one-file components we own would have to be surgically extracted anyway. Write our own ~400–500 LOC capture + ~400 LOC encode + ~200 LOC protocol.
- **Moonlight-iOS (iPad) — GREENFIELD, reference only.** Its value (RTP depacketizer, crypto, ENet, FEC, connection FSM) is exactly what we delete. Lift only the ~60 lines of VideoToolbox setup pattern (param-sets→CMVideoFormatDescription, AVCC AU→CMSampleBuffer) and the VTInvalidSession recovery pattern. Keep our own BSD-socket listener (grown from PerfServer).

### Resolved conflicts between component designs
- **Annex-B vs AVCC on the wire.** Encode component emits Annex-B (dumb host); decode component wants AU-framed AVCC. **Resolution:** host NVENC produces Annex-B internally, but the **host converts to AVCC** (4-byte length prefix) before sending, per the protocol component (`VIDEO_FRAME` = AVCC, `VIDEO_PARAM_SETS` = u16-len form). iPad never scans for AU boundaries across socket reads. The Annex-B→AVCC + SPS/PPS extraction is the host's job (borrow Sunshine NAL handling for emulation-prevention bytes / multi-slice AUs).
- **Color VUI triplet.** Encode/render agree on `primaries=12 (P3-D65) / transfer=13 (sRGB) / matrix=1 (BT.709)`, full-range, with capture pre-mapped to Display P3. Decode/render must still self-configure from CVPixelBuffer attributes and fall back to a gamut 3×3 if the stream signals plain BT.709 instead of P3. **Identity/RGB matrix is not available on 4:2:0 H.264**, so motion is always a 709 matrix; only the lossless still path is true RGB.
- **Full-range vs limited.** Committed to **full-range** (420f, `video_full_range_flag=1`) end to end. Render handles both but the encoder is pinned full-range.
- **Pacing source of truth.** Render component offered drawable back-pressure *or* CADisplayLink. **Resolution:** drawable back-pressure paces present; a CADisplayLink pinned 60/60/60 enforces the refresh-last priority; host enforces INFLIGHT≤2 + frame-drop so TCP send buffer can't grow unbounded latency.
- **SET_LAYER authoritative vs implicit.** Keep **explicit `SET_LAYER`** as the tiebreaker to avoid races during the motion↔static handoff.
- **HDR/FP16 capture vs 8-bit codec.** v1 is **SDR BGRA8 8-bit** end to end (honest ceiling — desktop + NVENC are 8-bit). `DuplicateOutput1`/FP16 scRGB is reserved for a later wide-gamut path; GM204 NVENC cannot ingest FP16, so that path needs a separate convert step — out of scope for the mirror milestone.

---

## Adversarial verification results (workflow Verify phase, 2026-06-28)

5 independent skeptic agents stress-tested the riskiest claims. 3 confirmed, 2 corrected.

| Claim | Verdict | Conf | Takeaway |
|-------|---------|-----:|----------|
| C transport fixes host→device 272→~1Gbps | **uncertain** | 0.72 | "Python vs C" is the wrong frame (pymobiledevice3 is pure-Python end to end). Real suspect = forward relay's I/O pattern, but an AMDS per-channel window could cap a C client too. MUST be tested with libusbmuxd/iproxy, not assumed. |
| VideoToolbox decode 2732×2048@60 sub-frame | **confirmed** | 0.78 | ~335 MP/s < 4K60's ~498; no B-frames = no reorder latency; Duet/Luna/Astropad do exactly this. Needs H.264 Level 5.2 for MBPS. |
| NVENC GM204 H.264 real-time encode | **confirmed** | 0.82 | 336 MP/s ≈ 67% of ~500 ceiling; D3D11 zero-copy via nvEncRegisterResource. |
| DXGI zero-copy capture to NVENC | **confirmed** | 0.85 | Standard OBS/ShadowPlay path; one GPU→GPU CopyResource, no CPU round trip. |
| "bit-exact RGB to the P3 panel" color | **uncertain** | 0.60 | Overclaim: bit-exact *transport* ≠ color-*accurate* display (sRGB→P3 is non-identity). Correct guarantee: bit-exact into the Metal texture + color-managed render. NVENC RGB→YUV CSC may use 601 regardless of 709 VUI — validate with a deltaE test pattern (M7). |

### Transport — empirical follow-up (host→device)

Three Python paths gave three answers: `usbmux forward` 272 Mbps; direct
`MuxDevice.connect()` + big write **stalled**; AFC 1252 Mbps. None is authoritative
for the production C client. **Design decision: plan conservatively for ~272 Mbps
host→device** (motion path fine; full-frame lossless still ~0.5s → lean on dirty-rect
tiles). The libusbmuxd/iproxy C-client test (M1) is an **optimization gate, not a
blocker** — the motion-path build (M2–M6) proceeds regardless. See
`spikes/stage-b/RESULTS.md`.

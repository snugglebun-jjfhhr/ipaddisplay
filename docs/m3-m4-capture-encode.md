# M3 + M4: Capture + Encode (FFmpeg shortcut)

This is the **FFmpeg-shortcut realization** of M3 (capture) and M4 (encode) from
`mirror-milestone-plan.md`. There is no MSVC toolchain on the host yet, so instead of
the hand-rolled C++ DXGI + zero-copy NVENC pipeline we use `ffmpeg.exe`
(`ddagrab` capture + `h264_nvenc`) to produce a correct, low-latency, Annex-B
H.264 elementary stream.

> **DEFERRED:** the hand-rolled C++ zero-copy path AND the full-GPU zero-copy
> ffmpeg path (`ddagrab -> hwmap=cuda -> nvenc` with NVENC's internal CSC). Both are
> lower latency but leave the RGB->YUV matrix uncontrolled (see Color, below). Do
> not ship either until the M7 deltaE test pattern validates NVENC's matrix.

Runnable script: `host/capture_encode.ps1` (defaults to a 5s test dump).

---

## The command (PRIMARY: GPU capture + CPU color convert + NVENC)

Run in an **interactive, logged-in Windows desktop session** — `ddagrab` fails under
WSL interop / SSH / service contexts.

```powershell
& 'C:\ffmpeg\bin\ffmpeg.exe' -hide_banner -y `
  -init_hw_device d3d11va `
  -filter_complex "ddagrab=output_idx=0:framerate=60:draw_mouse=1,hwdownload,format=bgra,scale=in_range=full:out_range=full:out_color_matrix=bt709:flags=full_chroma_int+accurate_rnd,format=nv12,setparams=range=pc:color_primaries=smpte432:color_trc=iec61966-2-1:colorspace=bt709" `
  -c:v h264_nvenc -preset p4 -tune ull -profile:v high -level 5.2 `
  -rc cbr -b:v 80M -maxrate 80M -bufsize 1333k `
  -bf 0 -g 60 -no-scenecut 1 -forced-idr 1 -zerolatency 1 -rc-lookahead 0 `
  -f h264 'C:\Users\aidan\ipaddisplay-spike\capture.h264'
```

### Per-flag rationale

**Capture**
- `-init_hw_device d3d11va` — D3D11 device that `ddagrab` (a GPU source filter) draws into.
- `ddagrab=output_idx=0` — Desktop Duplication capture of monitor 0 (primary). Raise for other outputs.
- `:framerate=60` — **mandatory**; ddagrab defaults to 30.
- `:draw_mouse=1` — bakes the cursor into the frame. Production ships the cursor as
  metadata and composites on the iPad (plan risk #6) — flip to `0` when that exists.
- `hwdownload,format=bgra` — moves the D3D11 BGRA surface to system memory so libswscale can run.

**Color (the load-bearing pair — see Color section)**
- `scale=...:out_range=full:out_color_matrix=bt709:flags=full_chroma_int+accurate_rnd`
  — the **actual** RGB->YUV math (libswscale). `in_range=full` because the desktop is
  full-range RGB; the flags improve 4:2:0 chroma siting/rounding.
- `format=nv12` — produces 8-bit 4:2:0 semi-planar **before** NVENC, so NVENC does
  **no** color conversion of its own. This single choice defuses the #1 risk.
- `setparams=range=pc:color_primaries=smpte432:color_trc=iec61966-2-1:colorspace=bt709`
  — writes **only the VUI tag** NVENC copies into the SPS.

**Encode**
- `-preset p4` — balanced quality/latency (per plan). `-tune ull` — ultra-low-latency.
- `-profile:v high -level 5.2` — High profile; Level 5.2 needed for 2736x2048@60.
- `-rc cbr -b:v 80M -maxrate 80M -bufsize 1333k` — CBR ~80 Mbps with a ~1-frame
  VBV (80M/60 ≈ 1333k) for minimal rate-control latency. Raise `-bufsize` toward
  `80M` (1s) if quality pumps too hard, at the cost of latency.
- `-bf 0` — no B-frames (no reorder delay).
- `-g 60` + raw `-f h264` output — IDR every 1s; with no global header NVENC emits
  **SPS+PPS in-band at every IDR**, so each IDR is self-contained for reconnect.
- `-forced-idr 1` — forced keyframes become true IDRs (host triggers on reconnect/mode-swap).
- `-no-scenecut 1` — deterministic GOP. `-zerolatency 1 -rc-lookahead 0` — no output reordering/lookahead.
- `-f h264` — raw **Annex-B** elementary stream (start codes, in-band param sets) for the M5 NAL splitter.

> **Intra-refresh note:** `-intra-refresh 1` (+ long `-g`, e.g. 600) gives smoother
> bitrate via rolling intra, BUT removes IDRs and therefore the in-band SPS/PPS
> repetition. For the spike we use short-GOP IDR (simpler, self-contained reconnect)
> and treat intra-refresh as a later refinement.

### Fallback: gdigrab (no ddagrab / debugging)
CPU capture — slow, high CPU, may miss 60fps at 5.6 MP, skips some HW/DRM windows
(plan risk #5). Same CSC + VUI chain. `host/capture_encode.ps1 -UseGdigrab` selects it,
and the script auto-falls-back if `ffmpeg -filters` shows no `ddagrab`.

```powershell
& 'C:\ffmpeg\bin\ffmpeg.exe' -hide_banner -y `
  -f gdigrab -framerate 60 -i desktop `
  -vf "scale=in_range=full:out_range=full:out_color_matrix=bt709:flags=full_chroma_int+accurate_rnd,format=nv12,setparams=range=pc:color_primaries=smpte432:color_trc=iec61966-2-1:colorspace=bt709" `
  -c:v h264_nvenc -preset p4 -tune ull -profile:v high -level 5.2 `
  -rc cbr -b:v 80M -maxrate 80M -bufsize 1333k `
  -bf 0 -g 60 -no-scenecut 1 -forced-idr 1 -zerolatency 1 -rc-lookahead 0 `
  -f h264 'C:\Users\aidan\ipaddisplay-spike\capture_gdi.h264'
```

---

## Color: tag vs. actual matrix (priority #2 risk)

The desktop is effectively sRGB; the iPad is Display-P3 Reference Mode. We want a
709-matrix 4:2:0 full-range stream **tagged** with P3 primaries + sRGB transfer.
Two separate things must agree:

1. **The actual RGB->YUV conversion matrix + range** — set by the `scale` filter
   (`out_color_matrix=bt709:out_range=full`). This is libswscale math; it changes pixels.
2. **The VUI tag in the SPS** — set by the `setparams` filter. This is a *promise* to
   the decoder; it changes no pixels.

`setparams` writes: `colorspace=bt709` → `matrix_coefficients=1`; `range=pc` →
`video_full_range_flag=1`; `color_primaries=smpte432` → `colour_primaries=12`
(Display P3 D65); `color_trc=iec61966-2-1` → `transfer_characteristics=13` (sRGB).

Because the conversion matrix and the tag are set explicitly and identically, the
classic **BT.601-vs-BT.709 / PC-vs-TV-range** bug cannot occur.

> **primaries=12 and transfer=13 are pure TAGS** describing the RGB gamut + gamma.
> They are *not* part of the YCbCr matrix (which is 709). A clean 709 YCbCr roundtrip
> does **not** validate them — the iPad uses them only for color-managed Display-P3
> render. Their on-the-wire correctness is confirmed by `trace_headers` (12/13/1);
> their *visual* correctness is M7 deltaE on the panel.

**Build quirk (verified):** output-side `-color_primaries` / `-color_trc` are
**silently dropped** by the validated build (they came out as `2`/unspecified); only
`-colorspace` / `-color_range` stuck. **You must use the `setparams` filter** to get
primaries=12 and transfer=13 into the SPS. (One verify self-test below still uses the
output-side flags — it is only a matrix self-test and does not gate the pipeline; see note.)

**Why NVENC's internal CSC is avoided:** on the GPU-only path NVENC converts RGB->YUV
itself with an uncontrolled (historically BT.601) matrix while the VUI claims 709 — the
pixels and the tag disagree, and *no ffprobe/trace_headers check can detect it*. Feeding
NVENC pre-made NV12 sidesteps this entirely. Optionally swap `scale` for `zscale`
(`zscale=matrixin=rgb:matrix=bt709:rangein=full:range=full`) for numerically more
rigorous CSC.

---

## Cropping: 2732 is not mod-16 (plan risk #3)

H.264 codes width to the next mod-16 (2736 = 171 MBs) and uses SPS `frame_cropping` to
crop back to 2732. 2048 is already mod-16. The encoder/SPS handle this automatically;
**the iPad render must honor the clean aperture (2732), not the coded width (2736)**, or 4
garbage columns appear.

> **`ffprobe coded_width` is unreliable** — it reported 2732 (not 2736) on the
> validated build. The authoritative check is the SPS via `trace_headers`:
> `pic_width_in_mbs_minus1=170` ⇒ (170+1)×16 = **2736 coded**; `frame_cropping_flag=1`,
> `frame_crop_right_offset=2`; for 4:2:0 CropUnitX=2, so 2×2 = **4 px cropped** ⇒ 2732
> displayed. The crop travels into the avcC on remux, so VideoToolbox yields a 2732-wide buffer.
> Confirm the iPad reads `CMVideoFormatDescriptionGetCleanAperture`.

---

## Verify recipe

Run against the produced `capture.h264` (the script echoes these at the end). Use
**default verbosity** for `trace_headers` — never `-v trace` (floods ~10M lines).

```powershell
# 1) Stream summary -> profile=High level=52 width=2732 height=2048 pix_fmt=yuvj420p
#    color_range=pc color_space=bt709 color_primaries=smpte432 color_transfer=iec61966-2-1 has_b_frames=0
ffprobe -v error -select_streams v:0 -show_entries stream=profile,level,width,height,coded_width,coded_height,pix_fmt,color_range,color_space,color_primaries,color_transfer,has_b_frames -of default=noprint_wrappers=1 capture.h264

# 2) AUTHORITATIVE SPS / VUI / crop -> pic_width_in_mbs_minus1=170 (=>2736),
#    frame_crop_right_offset=2 (=>2732), video_full_range_flag=1,
#    colour_primaries=12 transfer_characteristics=13 matrix_coefficients=1
ffmpeg -hide_banner -i capture.h264 -c copy -bsf:v trace_headers -f null - 2>&1 | Select-String -Pattern "profile_idc|level_idc|chroma_format_idc|pic_width_in_mbs|pic_height_in_map|frame_mbs_only|frame_cropping_flag|frame_crop_|video_full_range_flag|colour_primaries|transfer_characteristics|matrix_coefficients" | Select-Object -First 17

# 3) Decode the whole dump (must exit 0)
ffmpeg -hide_banner -v error -xerror -i capture.h264 -f null -

# 4) Frame types -> one I then all P, ZERO B
ffprobe -v error -select_streams v:0 -show_entries frame=pict_type -of csv=p=0 -read_intervals "%+#120" capture.h264

# 5) Prove AVCC-consumable + exercise the Annex-B->AVCC + SPS/PPS extraction the M5 pump does.
#    EXPECT nal_length_size=4
ffmpeg -hide_banner -v error -y -i capture.h264 -c copy capture.mp4
ffprobe -v error -show_entries stream=codec_name,profile,level,width,height,nal_length_size -of default=noprint_wrappers=1 capture.mp4

# 6) OPTIONAL CSC matrix self-test (pre-M7, no iPad): flat swatch -> SAME filter+VUI -> decode -> PSNR.
#    Near-inf PSNR = applied matrix == signaled matrix; low (<~50 dB) = the 601/709 bug.
#    NOTE: this self-test uses output-side color flags purely to label the roundtrip; it does NOT
#    gate the pipeline (pipeline correctness is enforced by the setparams filter + check #2).
ffmpeg -hide_banner -y -f lavfi -i "color=c=0x3060C0:size=2732x2048:rate=1:duration=1" -pix_fmt rgb24 ref.png
ffmpeg -hide_banner -y -i ref.png -vf "scale=out_color_matrix=bt709:out_range=pc,format=nv12" -c:v h264_nvenc -profile:v high -colorspace bt709 -color_range pc -frames:v 1 -f h264 swatch.h264
ffmpeg -hide_banner -i swatch.h264 -i ref.png -lavfi "[0:v]format=rgb24[d];[d][1:v]psnr" -f null -
```

PNG dumps for eyeballing are **not** color-managed — slightly-off greens/skins are
expected (P3 data shown unmanaged) and are not a bug; only gross corruption
(swapped/posterized chroma, green faces) signals a real CSC fault.

---

## Requirements

- **ffmpeg >= 6.1** for `ddagrab` (gyan.dev or BtbN full build; host runs a Jan-2026 build).
- `h264_nvenc` (GTX 970 / GM204: **H.264 8-bit only** — no HEVC, no 10-bit; ~500 MP/s,
  target 2732x2048@60 ≈ 336 MP/s fits).
- Interactive logged-in Windows desktop session.
- Capture monitor should be **exactly 2732x2048** (set the panel; don't abuse
  `video_size`, which distorts). Confirm which `output_idx` it is.

---

## How this feeds M5 (downstream NAL pump)

The output is an Annex-B elementary stream: NALs separated by `00 00 00 01` / `00 00 01`
start codes; the byte after a start code is the NAL header (`type = byte & 0x1F`):
7=SPS, 8=PPS, 5=IDR slice, 1=non-IDR/P slice, 6=SEI, 9=AUD.

- **VIDEO_PARAM_SETS (0x10):** scan/cache SPS(7)+PPS(8); on first frame, on change, or
  on every (re)connect, emit `u8 count` then per NAL `[u16 nalLen][raw NAL]`. The iPad
  feeds these to `CMVideoFormatDescriptionCreateFromH264ParameterSets(nalUnitHeaderLength:4)`,
  so the u16 framing is internal — **little-endian** is fine.
- **VIDEO_FRAME (0x11):** one coded picture = one access unit = one VIDEO_FRAME. Strip the
  AUD (type 9); for each remaining NAL (SEI + VCL slice) write `[u32 nalLen][raw NAL]` and
  concatenate — **this is AVCC**. `flags bit0=IDR` when a VCL NAL type==5.
- **CRITICAL endianness:** the MsgHeader and the VIDEO_PARAM_SETS u16 are little-endian
  (per `protocol.md`), but the **u32 length prefixes inside VIDEO_FRAME AVCC must be
  BIG-ENDIAN** (AVCC / VideoToolbox convention; confirmed by `nal_length_size=4` on remux).
  Get this wrong and the iPad decodes garbage. Confirm with the iPad VideoDecoder author.
- Recommend enabling AUDs (`-aud 1`) so the pump splits access units unambiguously on the
  type-9 NAL; otherwise it relies on "a VCL NAL completes the current picture."
- With short-GOP IDR (this spike), SPS/PPS recur before each IDR. With infinite-GOP +
  intra-refresh there is one IDR at start and SPS/PPS do **not** repeat — the pump must
  then cache them and resend + force an IDR on every reconnect.

---

## Reconciling the two component designs

The two design inputs agreed on the pipeline (GPU capture → CPU swscale → NV12 → NVENC),
flags, level, crop math, and verify approach. Two points were reconciled:

1. **Setting primaries/transfer.** Design A proved output-side `-color_primaries`/`-color_trc`
   are silently dropped and require the `setparams` filter; Design B's verify self-test used
   the output-side flags. **Resolution:** the production pipeline uses `setparams` (the
   authoritative path); the output-side flags survive only in the optional PSNR self-test,
   which doesn't gate correctness. Called out inline at verify step 6.
2. **trace_headers verbosity.** Design A used `-v trace`; Design B warned that floods ~10M
   lines and that default verbosity already shows the SPS. **Resolution:** default verbosity
   (Design B), reflected in the script and verify step 2.

---

## CORRECTION (post-review, supersedes any P3-primaries guidance above)

The adversarial color review caught a real bug in the originally-designed command:
it tagged the stream `color_primaries=smpte432` (P3-D65) while doing **no gamut
conversion** from the sRGB desktop. Tagging P3 on sRGB-primary pixels makes the
iPad read sRGB as P3 → **oversaturation** — the opposite of color-accurate.

**Fix (applied in `host/capture_encode.ps1`):** tag the stream with its TRUE
source colorimetry — `setparams=...:color_primaries=bt709:color_trc=iec61966-2-1:colorspace=bt709`,
full range. The iPad then color-manages sRGB→Display-P3 for an accurate result.

Consequences:
- The SPS VUI now carries `colour_primaries=1` (BT.709), `transfer=13` (sRGB),
  `matrix=1` (BT.709), `video_full_range_flag=1`.
- **M5 must send `STREAM_CONFIG.colorPrimaries=1`** (not 12) so the handshake
  matches the actual stream. (The M0 handshake test used 12; reconcile in M5.)
- A future "tell Windows the display is P3" path (ICC profile on the virtual
  display) or an in-pipeline sRGB→P3 gamut convert (then tag 12) is the wide-gamut
  upgrade — deferred; tagging the true source is correct and simplest for now.
- `-level auto` (NVENC picks its GM204 max, likely 5.1) instead of hard 5.2.
- `-aud 1` added so the M5 NAL pump can split access units on the AUD (type 9).

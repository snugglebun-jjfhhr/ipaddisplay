# M5 "frozen image" root-cause investigation (2026-07-02)

Symptom: full pipeline ran (handshake, decode, draw — dec/pres/drew counters all
climbing, texFail=0, noDrw=0) but the iPad image was stale, updating ~1/min
(that cadence = the Windows taskbar clock, the only thing DDA saw changing).

## Bisection ladder (each step ran on the real hardware)

| # | Experiment | Result | Eliminates |
|---|-----------|--------|------------|
| 1 | `stream_host.py --test` (lavfi testsrc2 720p through the FULL pump+usbmux+decode+render path) | animates on iPad | iPad display pipeline (both standing hypotheses: SwiftUI churn, stale IOSurface textures) |
| 2 | ddagrab chain -> file, ffplay motion window on screen, extract PNGs | real animation, real-time, 60 fps | capture/encode |
| 3 | live desktop stream, ffplay animating | 18.4 fps wire, iPad stale | — (reproduced with both ends proven good) |
| 4 | live desktop at `--fps 15` | **1.9 fps** wire, ~0 Mbit/s | "fixed ~18 msg/s socket cap" theory |
| 5 | ddagrab chain -> file at framerate=15, same moment | perfect 15 fps, dup=1, 80 Mbit/s | production-side capture (again, decisively) |
| 6 | `pipe_probe.py`: ffmpeg -> pipe -> Python discard | 80 Mbit/s sustained | pipe + Python read loop |
| 7 | `parser_probe.py`: ffmpeg -> pipe -> REAL parser machinery -> discard | **1.8 AU/s** | everything except the parser. Culprit found. |

## Root cause

`protocol._find_start_codes` was a per-byte pure-Python loop, and
`AccessUnitParser.feed` re-scanned (and `bytes()`-copied) the entire retained
buffer on every 64 KiB read — quadratic in AU size. NVENC CBR pads to the target
bitrate, so at fps=15 every AU is ~660 KB -> ~3.6 MB of interpreted scanning in
`feed` per AU, plus two more full scans (`split_nals`, `avcc_from_au_for_decode`)
≈ 0.5 s/AU ≈ the observed 1.9 fps. At fps=60 the same tax landed at ~18.4 fps.

The freeze mechanism: reader slower than capture -> stdout pipe fills -> ffmpeg's
filter graph stalls -> ddagrab falls behind its CFR schedule and (dup_frames=true)
emits **duplicates of its last cached frame** to catch up. The wire genuinely
carried a frozen desktop as near-zero-byte P-frames; the iPad rendered exactly
what it received. Why testsrc2 masked it: lavfi has no wall clock — when stalled
it just generates frames late, every frame unique, so the iPad showed a smooth
(slow-motion, delayed) animation that looked "fine".

## Fix (host-only, no iPad rebuild)

- `protocol._find_start_codes`: `bytes.find` (C speed) instead of the byte loop.
- `AccessUnitParser`: incremental — scans only new bytes (3-byte lookback for
  split start codes), carries committed positions across feeds, rebases on trim.
  Gotcha found by fuzzing: on a rescan the chunk can begin mid-start-code, so
  4-byte-form widening must check the leading zero against the FULL buffer, not
  the chunk (else a tail start code gets committed twice, one byte apart).
- Verified vs the old scanner as oracle: 500 randomized streams x random chunk
  splits, identical AU output. Perf: 15/15 AU/s (probe), then live end-to-end
  60.0 fps wire, visually smooth on the iPad.

## Incidental findings worth keeping

- NVENC CBR padding arrives as filler NALs that `avcc_from_au_for_decode`
  already strips — wire payload stays proportional to real content.
- The usbmux socket path (Python relay included) sustains 60 msg/s without
  strain; the old "272 Mbps host->device" number was never the constraint here.
- "Updates once a minute" during a stall = the taskbar clock, the only DDA
  change event on an otherwise idle desktop.
- 308/308 unique framemd5 hashes does NOT prove content changes — CBR recon
  noise makes static frames hash uniquely. Extract PNGs and look instead.

Probes: `pipe_probe.py` (stage 6), `parser_probe.py` (stage 7); both expect
`host/` modules on `sys.path` (run from a dir containing copies, as staged at
`C:\Users\aidan\ipaddisplay-spike\host\`).

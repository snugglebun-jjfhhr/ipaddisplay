# Spike #1 results — usbmux throughput (Stage A)

**Date:** 2026-06-27
**Device:** iPad13,8 (12.9" iPad Pro, M1, 2021), iPadOS 27.0, USB connection
**Host:** Windows (Ryzen 9 5900X), Apple Mobile Device Service running
**Method:** `afc_throughput.py` — AFC file push/pull over usbmux, differential
timing (time(2S) − time(S)) to cancel fixed CLI startup/connect overhead.
**Tooling:** pymobiledevice3 9.30.1 on Windows Python 3.12.

## Result

| Direction | Throughput | Marginal payload / time |
|-----------|-----------:|-------------------------|
| **push (host→device)** | **~1252 Mbps** | 128 MiB in 0.86 s |
| **pull (device→host)** | **~1234 Mbps** | 128 MiB in 0.87 s |

**Verdict: GREEN with large margin** (threshold for green was ~300 Mbps).

## What this means

- ~1.25 Gbps is ~3-4× the conservative ~300-400 Mbps the design doc assumed. The
  "usbmux is protocol-capped at ~400 Mbps" lore is outdated for an M1 iPad on USB4.
- Visually-transparent HEVC/H.264 of native-res 60Hz desktop content needs roughly
  50-200 Mbps — so there is large headroom for the motion path.
- A full **uncompressed** native still frame (2732×2048×3 ≈ 16.8 MB ≈ 134 Mbit)
  transfers in **~107 ms** at this rate, before any compression. With QOI/zstd it's
  well under that. The lossless reference-still mode is more comfortable than the
  doc's ~0.5 s worst-case estimate.

## Caveats / what this did NOT prove

- **Latency is unmeasured.** AFC bulk transfer proves *bandwidth*, not per-frame
  round-trip *latency*. The project needs both. Latency requires a persistent
  socket = the iPad app = **Stage B (needs a Mac)**. A rough Stage-A.5 proxy is
  possible (many small AFC ops in one persistent pymobiledevice3 process, measuring
  per-op round trip), but it conflates filesystem overhead with link RTT.
- **AFC is a conservative proxy** for a raw socket; a custom socket should meet or
  beat this. So ~1.25 Gbps is a floor, not a ceiling.
- Push time may reflect USB link + device RAM buffering rather than flash-write,
  which is exactly what we want (it mirrors writing to a socket, not to flash).

## Reproduce

```powershell
# Windows, iPad plugged in + unlocked + trusted, Apple Devices app installed:
cd C:\Users\aidan\ipaddisplay-spike   # or this repo's spikes/usbmux-throughput
py -3.12 -m pip install pymobiledevice3
py -3.12 afc_throughput.py --size-mb 128 --runs 3
```

## Next

- Bandwidth gate: **PASSED.** Proceed to build the pipeline (mirror milestone).
- Still open: round-trip latency (Stage B, needs a Mac) — see the Mac problem in
  README.md.

---

## Stage A.5 — round-trip latency proxy (2026-06-27)

**Method:** `afc_latency.py` — one persistent pymobiledevice3 AFC connection,
300 `get_device_info` "ping" ops, per-op round trip timed.

| metric | value |
|--------|------:|
| min    | 0.55 ms |
| median | 0.77 ms |
| mean   | 0.76 ms |
| p95    | 0.93 ms |
| p99    | 1.10 ms |

**Verdict: GREEN.** Sub-millisecond, and this is an *upper bound* (includes
device-side AFC processing) on raw usbmux small-message latency. Transport
latency is negligible vs the ~30-50ms end-to-end frame budget.

**Transport spike overall: PASSED on both axes (bandwidth + latency).** Any
latency in the final product will come from capture/encode/decode/present, not
the wire. End-to-end display latency still needs Stage B (iPad app, needs a Mac).

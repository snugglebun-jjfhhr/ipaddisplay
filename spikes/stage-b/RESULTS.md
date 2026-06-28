# Stage B results — real socket over usbmux (2026-06-28)

**Setup:** iPad13,8 (12.9" M1, iPadOS 27) running the ipaddisplay Stage B app
(TCP server on :7000); Windows host `perf_host.py` driving over
`pymobiledevice3 usbmux forward 7000 7000`. 256 MiB x3 each direction, 300 pings.

| metric | value |
|--------|------:|
| host->device throughput | **271.9 Mbps** |
| device->host throughput | **1381.0 Mbps** |
| RTT min / median / p95 / p99 | 0.51 / 0.74 / 0.90 / 0.95 ms |

## The asymmetry (open risk)

host->device is the DISPLAY direction (Windows pushes frames to the iPad) and it
measured only ~272 Mbps over the socket — yet spike #1's AFC *push* (same
physical direction) hit ~1252 Mbps. ~5x gap.

**Hypothesis:** the bottleneck is pymobiledevice3's pure-Python `usbmux forward`
pump in the host->device direction (small writes / GIL / no batching), NOT
usbmux or USB. Evidence: AFC push (pymobiledevice3's optimized native transfer)
reached 1252 Mbps host->device; device->host through the same `forward` reached
1381 Mbps (so the forwarder is fast one way but not the other). The production
host will not use this Python forwarder.

**To verify (next):** re-test host->device with a C transport — `iproxy`
(libimobiledevice) or a direct in-process usbmux `connect()` — and confirm it
reaches ~1 Gbps. If it stays ~272 Mbps, the architecture must treat host->device
as the constraint (still enough for tuned 60Hz HEVC, but the lossless-still frame
becomes ~0.5s again instead of ~0.1s).

## Latency: GREEN, confirmed

Sub-millisecond round trip on a real socket, matching the AFC proxy. Transport
latency contributes nothing to the frame budget.

---

## Transport investigation follow-up (2026-06-28) — host->device is inconclusive in Python

Tried to resolve the 272 Mbps host->device number with two more Python paths:

| path | host->device | result |
|------|-------------:|--------|
| `pymobiledevice3 usbmux forward` (relay) | 272 Mbps | works |
| direct `MuxDevice.connect()` + one big write | **stalled** | timed out at 32 MiB and 256 MiB |
| AFC service (spike #1) | 1252 Mbps | different channel type |

Three paths, three answers. Conclusion: **pymobiledevice3's pure-Python stack has
path-specific quirks in the host->device direction; none is authoritative for what
the PRODUCTION client will achieve.** The direct-connect stall is almost certainly a
library/flow-control quirk (production won't use pymobiledevice3). The definitive
test is a **C libusbmuxd client** (or `iproxy`, which is libusbmuxd-based), which is
also what production uses. That test (`perf_host_iproxy.py` + an `iproxy 7000 7000`)
requires installing the libimobiledevice Windows binaries; deferred as an
optimization gate, NOT a blocker.

### Design decision: plan conservatively for ~272 Mbps host->device

- **Motion path (H.264 60Hz):** needs ~50-200 Mbps. Fine at 272. No impact.
- **Lossless full-frame still:** 16.8 MB = 134 Mbit -> ~0.5 s at 272 Mbps (NOT the
  ~0.1 s that the AFC 1252 figure suggested). 
- **Mitigation:** the lossless path must lean on **dirty-rect tiles** (send only
  changed regions). A full-frame lossless refresh (~0.5 s) happens only on a scene
  change; incremental edits refresh fast. Acceptable for photo work.
- If the C-client test later shows host->device ~1 Gbps, full-frame stills drop to
  ~0.1 s — treat that as upside, not a dependency.

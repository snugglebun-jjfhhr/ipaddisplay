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

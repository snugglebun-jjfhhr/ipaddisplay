#!/usr/bin/env python3
"""
Stage A.5 usbmux round-trip latency proxy — no Mac, no iPad app required.

Spike #1 proved bandwidth (~1.25 Gbps). This estimates the OTHER half:
small-message round-trip latency over usbmux. It opens ONE persistent
pymobiledevice3 AFC connection (paying startup once) and fires many tiny
request/response ops (`get_device_info` — an AFC "ping" with no filesystem
path), timing each round trip.

What this is and isn't:
  - It measures AFC-operation round-trip = usbmux link RTT + device-side AFC
    service processing. So it's an UPPER BOUND on the raw usbmux small-message
    latency a custom socket would see. If even this upper bound is small, the
    transport adds negligible latency to the frame budget.
  - It is NOT end-to-end display latency (capture+encode+decode+present dominate
    that). The precise socket round-trip needs the iPad app = Stage B (Mac).

Interpretation (median AFC-op RTT, treat as upper bound on link RTT):
  < 5 ms   GREEN  - transport latency negligible vs the ~30-50ms frame budget.
  5-15 ms  YELLOW - fine, but measure end-to-end carefully in Stage B.
  > 15 ms  INVESTIGATE - though AFC processing inflates this; Stage B is truer.

Run on the OS where the iPad is plugged in (Windows):
    py -3.12 afc_latency.py --count 300
"""
import argparse
import asyncio
import inspect
import statistics
import sys
import time


async def maybe_await(value):
    return await value if inspect.isawaitable(value) else value


def pct(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    k = min(len(sorted_vals) - 1, int(round((p / 100) * (len(sorted_vals) - 1))))
    return sorted_vals[k]


def verdict(median_ms: float) -> str:
    if median_ms < 5:
        return "GREEN  - transport latency negligible vs the ~30-50ms frame budget."
    if median_ms < 15:
        return "YELLOW - fine, but verify end-to-end latency in Stage B."
    return "INVESTIGATE - but AFC processing inflates this; Stage B is the truer test."


async def main() -> int:
    ap = argparse.ArgumentParser(description="usbmux round-trip latency proxy via AFC ops")
    ap.add_argument("--count", type=int, default=300, help="number of timed ops (default 300)")
    ap.add_argument("--warmup", type=int, default=20, help="warmup ops discarded (default 20)")
    args = ap.parse_args()

    from pymobiledevice3.usbmux import list_devices
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService

    devices = await maybe_await(list_devices())
    if not devices:
        print("ERROR: no device over usbmux (plugged in, unlocked, trusted?)")
        return 2
    udid = devices[0].serial
    print(f"Device: {udid}")

    lockdown = await maybe_await(create_using_usbmux(serial=udid))
    afc = AfcService(lockdown)

    async def ping():
        return await maybe_await(afc.get_device_info())

    # Warm up (first op pays service-start cost).
    for _ in range(args.warmup):
        await ping()

    samples = []
    for _ in range(args.count):
        t0 = time.perf_counter()
        await ping()
        samples.append((time.perf_counter() - t0) * 1000.0)  # ms

    samples.sort()
    mn = samples[0]
    med = statistics.median(samples)
    mean = statistics.fmean(samples)
    p95 = pct(samples, 95)
    p99 = pct(samples, 99)

    print(f"\nAFC-op round-trip over usbmux  (n={args.count}, op=get_device_info)")
    print(f"  min    {mn:6.2f} ms")
    print(f"  median {med:6.2f} ms")
    print(f"  mean   {mean:6.2f} ms")
    print(f"  p95    {p95:6.2f} ms")
    print(f"  p99    {p99:6.2f} ms")
    print(f"\n  verdict: {verdict(med)}")
    print("\nThis is an UPPER BOUND on raw usbmux small-message latency (includes")
    print("device-side AFC processing). A custom socket should do better. End-to-end")
    print("display latency still needs Stage B (iPad app, needs a Mac).")

    for closer in ("aclose", "close"):
        fn = getattr(afc, closer, None)
        if fn:
            try:
                await maybe_await(fn())
            except Exception:
                pass
            break
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

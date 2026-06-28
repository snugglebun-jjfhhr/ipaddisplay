#!/usr/bin/env python3
"""
Stage A usbmux throughput probe — no Mac, no iPad app required.

Measures how fast bytes move host <-> iPad over Apple's usbmux/USB transport by
pushing and pulling a large file through AFC (Apple File Conduit), which rides
the same usbmux multiplexing + USB bulk pipe a custom display-streaming socket
would use. This answers the make-or-break question for the ipaddisplay project
without an iPad app or a Mac.

Implementation note: pymobiledevice3 9.x moved its device/lockdown API to async,
which is awkward to mix with the sync AFC methods across an event loop. So this
drives the maintained `pymobiledevice3 afc` CLI via subprocess (it manages its
own loop). Each CLI call carries a fixed ~1-2s interpreter-startup + connect +
AFC-service-start cost. To cancel it exactly we use DIFFERENTIAL timing: time a
push of size S and a push of size 2S; the transfer time of S bytes is
t(2S) - t(S), because the fixed overhead is identical in both and subtracts out.
throughput = S_bytes * 8 / (t(2S) - t(S)). Same for pull.

Caveats (read before trusting the number):
  - AFC has protocol overhead a raw socket wouldn't, so a real socket stream
    should meet or slightly beat this. Treat AFC Mbps as a conservative floor.
  - Push (host->device) is the direction the display pipeline uses.
  - The usbmux ceiling is often a *protocol* limit (~300-400 Mbps), not a cable
    limit. A Thunderbolt/USB4 iPad does not guarantee more.

Interpretation (per the design doc), on push Mbps:
  >= ~300  GREEN  - proceed to build the pipeline.
  ~150-300 YELLOW - workable at native 60Hz with tuned bitrate + dirty-rects.
  < ~150   RED    - rethink transport (custom USB bulk) before investing.

Run on the OS where the iPad is plugged in (Windows):
    py -3.12 afc_throughput.py --size-mb 256 --runs 3
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time


def log(msg: str) -> None:
    print(msg, flush=True)


def mbps(num_bytes: int, seconds: float) -> float:
    if seconds <= 0:
        return float("inf")
    return (num_bytes * 8) / 1_000_000 / seconds


def verdict(push_mbps: float) -> str:
    if push_mbps >= 300:
        return "GREEN  - transport is fine; proceed to build the pipeline."
    if push_mbps >= 150:
        return "YELLOW - workable at native 60Hz with tuned bitrate + dirty-rects."
    return "RED    - rethink transport (custom USB bulk) before investing further."


def afc(args: list[str]) -> tuple[float, int]:
    """Run a `pymobiledevice3 afc` subcommand; return (elapsed_seconds, returncode).

    Only one device is connected, so the CLI auto-selects it; no UDID needed.
    """
    cmd = [sys.executable, "-m", "pymobiledevice3", "afc"] + args
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    t1 = time.perf_counter()
    if proc.returncode != 0:
        log(f"  [afc {' '.join(args)}] rc={proc.returncode}")
        if proc.stderr.strip():
            log("  stderr: " + proc.stderr.strip().splitlines()[-1])
    return t1 - t0, proc.returncode


def time_transfers(local_up: str, local_down: str, remote: str, n: int, runs: int):
    """Return (min_push_total, min_pull_total) seconds over `runs`, incl. fixed overhead."""
    min_push = float("inf")
    min_pull = float("inf")
    for i in range(1, runs + 1):
        up_total, rc1 = afc(["push", local_up, remote])
        dn_total, rc2 = afc(["pull", remote, local_down, "-i"])
        if rc1 != 0 or rc2 != 0:
            log(f"    rep {i}: FAILED (push rc={rc1}, pull rc={rc2})")
            continue
        size_ok = os.path.exists(local_down) and os.path.getsize(local_down) == n
        tag = "ok" if size_ok else "SIZE MISMATCH"
        log(f"    rep {i}: push raw {up_total:6.2f}s | pull raw {dn_total:6.2f}s | {tag}")
        min_push = min(min_push, up_total)
        min_pull = min(min_pull, dn_total)
        if os.path.exists(local_down):
            os.remove(local_down)
    return min_push, min_pull


def main() -> int:
    ap = argparse.ArgumentParser(description="usbmux throughput probe via AFC CLI (differential timing)")
    ap.add_argument("--size-mb", type=int, default=128, help="base size S in MiB (also tests 2S; default 128)")
    ap.add_argument("--runs", type=int, default=3, help="reps per size; min time used (default 3)")
    ap.add_argument("--remote", default="ipaddisplay_spike.bin", help="remote name under /var/mobile/Media")
    args = ap.parse_args()

    s = args.size_mb * 1024 * 1024          # S bytes
    big = 2 * s                              # 2S bytes
    tmpdir = tempfile.mkdtemp(prefix="ipaddisplay_spike_")
    up_s = os.path.join(tmpdir, "up_s.bin")
    up_2s = os.path.join(tmpdir, "up_2s.bin")
    down = os.path.join(tmpdir, "down.bin")

    log(f"Differential probe: S={args.size_mb} MiB, 2S={2*args.size_mb} MiB, {args.runs} reps each")
    log("Writing local payloads...")
    with open(up_s, "wb") as f:
        f.write(b"\x00" * s)
    with open(up_2s, "wb") as f:
        f.write(b"\x00" * big)

    try:
        # Warm-up: pay one-time first-connect / pairing cost so it doesn't skew run 1.
        log("Warm-up push (discarded)...")
        afc(["push", up_s, args.remote])

        log(f"\nTiming S = {args.size_mb} MiB:")
        push_s, pull_s = time_transfers(up_s, down, args.remote, s, args.runs)
        log(f"\nTiming 2S = {2*args.size_mb} MiB:")
        push_2s, pull_2s = time_transfers(up_2s, down, args.remote, big, args.runs)
    finally:
        afc(["rm", args.remote])
        for p in (up_s, up_2s, down):
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass

    # Differential: (2S - S) bytes transferred in (t_2S - t_S) seconds; overhead cancels.
    d_push = push_2s - push_s
    d_pull = pull_2s - pull_s
    log("\n=== RESULT (differential, fixed overhead cancelled) ===")
    if d_push <= 0 or d_pull <= 0:
        log(f"  Inconclusive: t(2S)-t(S) non-positive (push {d_push:.2f}s, pull {d_pull:.2f}s).")
        log("  Overhead likely dominates; re-run with a larger --size-mb (e.g. 256).")
        return 1
    push_mbps = mbps(s, d_push)             # S bytes is the marginal payload
    pull_mbps = mbps(s, d_pull)
    log(f"  push (host->device): {push_mbps:7.1f} Mbps   <- gates the project   "
        f"[(t2S {push_2s:.2f}s - tS {push_s:.2f}s) = {d_push:.2f}s for {args.size_mb} MiB]")
    log(f"  pull (device->host): {pull_mbps:7.1f} Mbps   "
        f"[(t2S {pull_2s:.2f}s - tS {pull_s:.2f}s) = {d_pull:.2f}s]")
    log(f"  verdict: {verdict(push_mbps)}")
    log("\nAFC is a conservative proxy; a custom socket should meet or beat this.")
    log("Stage B (precise socket echo + round-trip latency) needs a Mac for the iPad app.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

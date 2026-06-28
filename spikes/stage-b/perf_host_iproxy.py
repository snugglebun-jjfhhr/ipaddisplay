#!/usr/bin/env python3
"""
Transport verification (Stage B.2) — host->device throughput WITHOUT the
pure-Python usbmux forwarder.

This is the test that resolves the 272 Mbps asymmetry. It speaks the exact same
PerfServer wire protocol as perf_host.py, but it does NOT spawn
`pymobiledevice3 usbmux forward`. Instead you run a NATIVE C relay first
(libimobiledevice's iproxy), and this script just connects to the local port it
opened. If host->device jumps from ~272 Mbps to ~1 Gbps, the bottleneck was the
Python pump, exactly as hypothesized.

PREP (Windows PowerShell, iPad plugged in + unlocked + trusted, PerfServer app
foreground showing "listening on :7000"):

  # 1. Get iproxy (pick ONE):
  #    a) scoop:        scoop install libimobiledevice
  #    b) chocolatey:   choco install libimobiledevice
  #    c) prebuilt zip: github.com/libimobiledevice-win32/imobiledevice-net releases
  #                     (unzip; iproxy.exe + *.dll in one folder; run from there)
  #
  # 2. Start the NATIVE relay (C, no Python). Leave it running in its own window:
  #       iproxy 7000 7000
  #    (newer builds also accept:  iproxy 7000:7000)
  #    iproxy talks to Apple Mobile Device Service's usbmuxd at 127.0.0.1:27015.
  #
  # 3. In a second window, run this against the port iproxy opened:
  #       py -3.12 perf_host_iproxy.py
  #       py -3.12 perf_host_iproxy.py --mb 256 --reps 5

PASS CRITERION: host->device >= ~900 Mbps (vs 272 Mbps via the Python forwarder).
"""
import argparse
import socket
import statistics
import struct
import sys
import time


def mbps(nbytes, seconds):
    return (nbytes * 8) / 1_000_000 / seconds if seconds > 0 else float("inf")


def recv_exact(sock, n):
    got = 0
    view = bytearray(1024 * 1024)
    while got < n:
        c = sock.recv_into(view, min(len(view), n - got))
        if c == 0:
            break
        got += c
    return got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7000, help="LOCAL port iproxy opened")
    ap.add_argument("--mb", type=int, default=256)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--pings", type=int, default=300)
    args = ap.parse_args()

    print(f"Connecting to native relay at {args.host}:{args.port} "
          f"(start `iproxy {args.port} 7000` first)...")
    sock = socket.create_connection((args.host, args.port), timeout=10)
    sock.settimeout(120)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
    except OSError:
        pass
    print("Connected.\n")

    n = args.mb * 1024 * 1024
    payload = b"\x00" * n

    # host -> device (the display direction)
    best_down = 0.0
    print(f"[1/3] host->device ({args.mb} MiB x{args.reps})...")
    for _ in range(args.reps):
        t0 = time.perf_counter()
        sock.sendall(b"D" + struct.pack(">Q", n))
        sock.sendall(payload)
        if sock.recv(1) != b"\x06":
            raise SystemExit("bad ACK")
        best_down = max(best_down, mbps(n, time.perf_counter() - t0))
    print(f"      {best_down:.1f} Mbps   <- compare to 272 Mbps (python forward)\n")

    # device -> host
    best_up = 0.0
    print(f"[2/3] device->host ({args.mb} MiB x{args.reps})...")
    for _ in range(args.reps):
        sock.sendall(b"U" + struct.pack(">Q", n))
        t0 = time.perf_counter()
        if recv_exact(sock, n) != n:
            raise SystemExit("short upload")
        best_up = max(best_up, mbps(n, time.perf_counter() - t0))
    print(f"      {best_up:.1f} Mbps\n")

    # round-trip latency
    print(f"[3/3] latency ({args.pings} pings, 64B)...")
    hdr = b"P" + struct.pack(">I", 64)
    ping = b"\x00" * 64
    for _ in range(20):
        sock.sendall(hdr + ping); recv_exact(sock, 64)
    s = []
    for _ in range(args.pings):
        t0 = time.perf_counter()
        sock.sendall(hdr + ping); recv_exact(sock, 64)
        s.append((time.perf_counter() - t0) * 1000.0)
    s.sort()
    try:
        sock.sendall(b"Q")
    except OSError:
        pass
    sock.close()

    p = lambda q: s[min(len(s) - 1, int(round(q / 100 * (len(s) - 1))))]
    print("\n=== TRANSPORT RESULT (native iproxy, NO python forward) ===")
    print(f"  host->device: {best_down:7.1f} Mbps   (forward was 271.9)")
    print(f"  device->host: {best_up:7.1f} Mbps   (forward was 1381.0)")
    print(f"  latency:      median {statistics.median(s):.2f} ms, p99 {p(99):.2f} ms")
    verdict = "PASS" if best_down >= 900 else ("PARTIAL" if best_down >= 500 else "FAIL")
    print(f"  host->device verdict: {verdict} (pass >= ~900 Mbps)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Stage B perf host (Windows) — precise usbmux socket throughput + latency.

Drives the iPad PerfServer (ios/Sources/PerfServer.swift) over a real TCP socket
tunneled through usbmux, the same path the display pipeline will use. Unlike the
AFC proxy in spike #1, this measures the actual socket the app will use.

Prereqs:
  - iPad plugged in, unlocked, trusted; Apple Devices app installed.
  - The ipaddisplay Stage B app RUNNING and FOREGROUND on the iPad (it must show
    "listening on :7000").
  - pip install pymobiledevice3 (already done in Python 3.12).

Run:
  py -3.12 perf_host.py
  py -3.12 perf_host.py --mb 256 --pings 300

Protocol (big-endian; see PerfServer.swift):
  'D' + uint64 L          host->device throughput (device ACKs 1 byte when done)
  'U' + uint64 L          device->host throughput
  'P' + uint32 P + Pbytes ping echo
  'Q'                     quit
"""
import argparse
import socket
import statistics
import struct
import subprocess
import sys
import time

LOCAL_PORT = 7000
DEVICE_PORT = 7000


def mbps(nbytes: int, seconds: float) -> float:
    return (nbytes * 8) / 1_000_000 / seconds if seconds > 0 else float("inf")


def recv_exact(sock: socket.socket, n: int) -> int:
    """Receive and discard exactly n bytes; return count actually read."""
    got = 0
    view = bytearray(1024 * 1024)
    while got < n:
        chunk = sock.recv_into(view, min(len(view), n - got))
        if chunk == 0:
            break
        got += chunk
    return got


def start_forward():
    proc = subprocess.Popen(
        [sys.executable, "-m", "pymobiledevice3", "usbmux", "forward",
         str(LOCAL_PORT), str(DEVICE_PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # Wait for the local port to accept connections (and for the app to be listening).
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", LOCAL_PORT), timeout=1)
            return proc, s
        except OSError:
            time.sleep(0.3)
    proc.terminate()
    raise SystemExit(
        "Could not connect to the iPad app on :7000.\n"
        "  - Is the ipaddisplay Stage B app open and FOREGROUND (showing 'listening')?\n"
        "  - Is the iPad unlocked and trusted?"
    )


def throughput_down(sock, payload, reps):  # host -> device
    best = 0.0
    n = len(payload)
    for _ in range(reps):
        t0 = time.perf_counter()
        sock.sendall(b"D" + struct.pack(">Q", n))
        sock.sendall(payload)
        ack = sock.recv(1)            # device ACKs after consuming all n bytes
        dt = time.perf_counter() - t0
        if ack != b"\x06":
            raise SystemExit(f"bad ACK from device: {ack!r}")
        best = max(best, mbps(n, dt))
    return best


def throughput_up(sock, n, reps):       # device -> host
    best = 0.0
    for _ in range(reps):
        sock.sendall(b"U" + struct.pack(">Q", n))
        t0 = time.perf_counter()
        got = recv_exact(sock, n)
        dt = time.perf_counter() - t0
        if got != n:
            raise SystemExit(f"short upload: got {got} of {n}")
        best = max(best, mbps(n, dt))
    return best


def latency(sock, count, warmup, plen=64):
    payload = b"\x00" * plen
    hdr = b"P" + struct.pack(">I", plen)
    for _ in range(warmup):
        sock.sendall(hdr + payload)
        recv_exact(sock, plen)
    samples = []
    for _ in range(count):
        t0 = time.perf_counter()
        sock.sendall(hdr + payload)
        got = recv_exact(sock, plen)
        samples.append((time.perf_counter() - t0) * 1000.0)
        if got != plen:
            raise SystemExit("short ping echo")
    samples.sort()
    return samples


def pct(s, p):
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage B usbmux socket perf host")
    ap.add_argument("--mb", type=int, default=256, help="throughput payload MiB (default 256)")
    ap.add_argument("--reps", type=int, default=3, help="throughput reps, best wins (default 3)")
    ap.add_argument("--pings", type=int, default=300, help="latency samples (default 300)")
    args = ap.parse_args()

    print("Starting usbmux forward 7000->7000 and connecting to the app...")
    proc, sock = start_forward()
    sock.settimeout(120)   # reset the 1s connect timeout; transfers take seconds
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print("Connected to iPad PerfServer.\n")

    n = args.mb * 1024 * 1024
    payload = b"\x00" * n
    try:
        print(f"[1/3] host->device throughput ({args.mb} MiB x{args.reps})...")
        down = throughput_down(sock, payload, args.reps)
        print(f"      {down:.1f} Mbps\n")

        print(f"[2/3] device->host throughput ({args.mb} MiB x{args.reps})...")
        up = throughput_up(sock, n, args.reps)
        print(f"      {up:.1f} Mbps\n")

        print(f"[3/3] round-trip latency ({args.pings} pings, 64B)...")
        s = latency(sock, args.pings, warmup=20)
        try:
            sock.sendall(b"Q")
        except OSError:
            pass
    finally:
        sock.close()
        proc.terminate()

    print(f"      min {s[0]:.2f} | median {statistics.median(s):.2f} | "
          f"mean {statistics.fmean(s):.2f} | p95 {pct(s,95):.2f} | p99 {pct(s,99):.2f} ms")

    print("\n=== STAGE B RESULT (real socket over usbmux) ===")
    print(f"  host->device throughput: {down:7.1f} Mbps   <- the display direction")
    print(f"  device->host throughput: {up:7.1f} Mbps")
    print(f"  round-trip latency:      median {statistics.median(s):.2f} ms, p99 {pct(s,99):.2f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())

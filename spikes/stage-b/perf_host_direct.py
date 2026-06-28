#!/usr/bin/env python3
"""
Transport diagnosis: direct usbmux connect (no `forward` relay).

Stage B measured host->device at only 272 Mbps THROUGH `pymobiledevice3 usbmux
forward`, which relays bytes in a select-loop with small chunked copies. This
test instead opens a DIRECT usbmux channel to the device port (MuxDevice.connect)
and does ONE large contiguous write, isolating the question:

  Is the 272 Mbps cap the forward relay's I/O pattern, or a deeper per-channel limit?

  - If host->device here jumps to ~1 Gbps  -> the relay was the bottleneck; a
    production C client (libusbmuxd) doing big writes will be fine.
  - If it stays ~272 Mbps                  -> deeper cap (AMDS per-channel window
    or device-side relay / app recv loop); architecture must treat it as a limit.

No new install: uses pymobiledevice3 (already present). The iPad Stage B app must
be RUNNING/foreground (PerfServer listening on :7000).

Run:  py -3.12 perf_host_direct.py --mb 256 --reps 5
"""
import argparse
import asyncio
import socket
import statistics
import struct
import sys
import time

DEVICE_PORT = 7000


def mbps(n, s):
    return (n * 8) / 1_000_000 / s if s > 0 else float("inf")


def recv_exact(sock, n):
    got, view = 0, bytearray(1024 * 1024)
    while got < n:
        c = sock.recv_into(view, min(len(view), n - got))
        if c == 0:
            break
        got += c
    return got


async def open_socket():
    from pymobiledevice3 import usbmux
    dev = await usbmux.select_device()
    if dev is None:
        raise SystemExit("no usbmux device (plugged in, unlocked, trusted?)")
    return await dev.connect(DEVICE_PORT)   # returns a plain socket.socket


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mb", type=int, default=256)
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--pings", type=int, default=200)
    args = ap.parse_args()

    print(f"Direct usbmux connect to device :{DEVICE_PORT} (no forward relay)...")
    sock = asyncio.run(open_socket())
    sock.settimeout(120)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print("Connected. The iPad app must show a client connected.\n")

    n = args.mb * 1024 * 1024
    payload = b"\x00" * n
    try:
        # host->device: one big contiguous write per rep, then await ACK
        down = 0.0
        for _ in range(args.reps):
            t0 = time.perf_counter()
            sock.sendall(b"D" + struct.pack(">Q", n))
            sock.sendall(payload)
            if sock.recv(1) != b"\x06":
                raise SystemExit("bad ACK")
            down = max(down, mbps(n, time.perf_counter() - t0))
        print(f"host->device (direct): {down:.1f} Mbps")

        # device->host
        up = 0.0
        for _ in range(args.reps):
            sock.sendall(b"U" + struct.pack(">Q", n))
            t0 = time.perf_counter()
            if recv_exact(sock, n) != n:
                raise SystemExit("short upload")
            up = max(up, mbps(n, time.perf_counter() - t0))
        print(f"device->host (direct): {up:.1f} Mbps")

        # latency
        hdr = b"P" + struct.pack(">I", 64)
        for _ in range(20):
            sock.sendall(hdr + b"\x00" * 64); recv_exact(sock, 64)
        s = []
        for _ in range(args.pings):
            t0 = time.perf_counter()
            sock.sendall(hdr + b"\x00" * 64); recv_exact(sock, 64)
            s.append((time.perf_counter() - t0) * 1000)
        s.sort()
        try:
            sock.sendall(b"Q")
        except OSError:
            pass
    finally:
        sock.close()

    print(f"RTT: min {s[0]:.2f} median {statistics.median(s):.2f} p99 {s[int(0.99*(len(s)-1))]:.2f} ms")
    print("\n=== DIRECT-CONNECT RESULT ===")
    print(f"  host->device: {down:7.1f} Mbps   (forward relay gave 272)")
    print(f"  device->host: {up:7.1f} Mbps")
    verdict = ("relay I/O pattern was the bottleneck — production C client will be fine"
               if down >= 700 else
               "still capped — deeper per-channel limit (AMDS window or device-side relay)")
    print(f"  -> {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

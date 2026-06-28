#!/usr/bin/env python3
"""
M2 host handshake harness — exercises the iPad LinkServer over usbmux.

Drives the iPad app (ios/Sources/LinkServer.swift) through the full handshake
FSM and a PING/PONG keepalive burst, then prints a PASS/FAIL summary with the
negotiated config and median round-trip time.

This is the runnable stand-in for the future C++ host (M3). It uses ONLY the
canonical wire codec in host/protocol.py — no byte layouts are redefined here.

DEVIATION FROM THE MILESTONE PLAN (intentional): the plan calls for a C++ host
built on libusbmuxd, but there is no MSVC toolchain on the dev machine yet, so
M2 is delivered as this Python harness (testable today against the real iPad).
host/protocol.h is emitted alongside so the C++ host stays unblocked.

FSM:
  connect
    -> HELLO            (H->D)  codecMask = H264 | HEVC
    -> DEVICE_INFO      (D->H)  print native res + capabilities
    -> STREAM_CONFIG    (H->D)  2732x2048 H264 420 8 fullRange prim1 transfer13 matrix1
    -> STREAM_CONFIG_ACK(D->H)  assert ok == 1
    -> 20x PING/PONG    keepalive, measure RTT

Prereqs:
  - iPad plugged in, unlocked, trusted.
  - The ipaddisplay app RUNNING and FOREGROUND (showing "listening on :7000").
  - pip install pymobiledevice3.

Run:
  py -3.12 handshake_host.py
  py -3.12 handshake_host.py --pings 50
"""
from __future__ import annotations

import argparse
import os
import socket
import statistics
import struct
import subprocess
import sys
import time

# Import the canonical codec. Support running both as `python handshake_host.py`
# from the host/ dir and as a module from elsewhere.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import protocol as p  # noqa: E402

LOCAL_PORT = 7000
DEVICE_PORT = 7000


# --- transport -------------------------------------------------------------


def start_forward():
    """Start `pymobiledevice3 usbmux forward 7000 7000` and connect to it.

    Returns (proc, sock). Reuses the pattern from spikes/stage-b/perf_host.py:
    the handshake is tiny so throughput is irrelevant, we just need the tunnel.
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "pymobiledevice3", "usbmux", "forward",
         str(LOCAL_PORT), str(DEVICE_PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        # If the forwarder died early (no device / no pymobiledevice3), stop.
        if proc.poll() is not None:
            raise SystemExit(
                "pymobiledevice3 usbmux forward exited immediately.\n"
                "  - Is the iPad plugged in, unlocked, and trusted?\n"
                "  - Is pymobiledevice3 installed for this interpreter?"
            )
        try:
            s = socket.create_connection(("127.0.0.1", LOCAL_PORT), timeout=1)
            return proc, s
        except OSError:
            time.sleep(0.3)
    proc.terminate()
    raise SystemExit(
        "Could not connect to the iPad app on :7000.\n"
        "  - Is the ipaddisplay app open and FOREGROUND (showing 'listening on :7000')?\n"
        "  - Is the iPad unlocked and trusted?"
    )


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Receive exactly n bytes or raise. Returns b'' only when n == 0."""
    if n == 0:
        return b""
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        r = sock.recv_into(view[got:], n - got)
        if r == 0:
            raise SystemExit(
                f"Connection closed by device after {got} of {n} bytes.\n"
                "  - Did the app crash or leave the foreground?"
            )
        got += r
    return bytes(buf)


# --- message framing -------------------------------------------------------


def send_msg(sock: socket.socket, msg_type: int, payload: bytes, seq: int) -> None:
    """Frame and send one message: 24-byte header + payload."""
    hdr = p.make_header(int(msg_type), len(payload), seq=seq)
    sock.sendall(hdr + payload)


def recv_msg(sock: socket.socket) -> tuple[p.MsgHeader, bytes]:
    """Read one framed message: header(24) then header.length payload bytes."""
    hdr = p.MsgHeader.unpack(recv_exact(sock, p.HEADER_SIZE))
    payload = recv_exact(sock, hdr.length)
    return hdr, payload


def expect(sock: socket.socket, want_type: int) -> tuple[p.MsgHeader, bytes]:
    """Receive a message and assert its type, surfacing ERROR frames clearly."""
    hdr, payload = recv_msg(sock)
    if hdr.type == int(p.MessageType.ERROR):
        raise SystemExit(f"Device sent ERROR (payload={payload!r}) while "
                         f"expecting {p.MessageType(want_type).name}.")
    if hdr.type != int(want_type):
        try:
            got_name = p.MessageType(hdr.type).name
        except ValueError:
            got_name = f"0x{hdr.type:02x}"
        raise SystemExit(
            f"Protocol error: expected {p.MessageType(want_type).name} "
            f"(0x{int(want_type):02x}) but got {got_name}."
        )
    return hdr, payload


# --- main FSM --------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="M2 host handshake harness")
    ap.add_argument("--pings", type=int, default=20, help="PING/PONG samples (default 20)")
    ap.add_argument("--ping-bytes", type=int, default=64, help="PING payload size (default 64)")
    args = ap.parse_args()

    print("Starting usbmux forward 7000->7000 and connecting to the app...")
    proc, sock = start_forward()
    sock.settimeout(10)  # reset the 1s connect timeout
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print("Connected to iPad LinkServer.\n")

    seq = 0
    ok_overall = True
    device_info: p.DeviceInfo | None = None
    stream_config: p.StreamConfig | None = None
    rtts: list[float] = []

    try:
        # 1) HELLO -> DEVICE_INFO ------------------------------------------
        hello = p.Hello(
            codec_mask=p.CODEC_H264 | p.CODEC_HEVC,
            max_bitrate_kbps=80_000,
        )
        send_msg(sock, p.MessageType.HELLO, hello.pack(), seq)
        seq += 1
        print(f"-> HELLO  codecMask=H264|HEVC maxBitrate={hello.max_bitrate_kbps}kbps")

        _, payload = expect(sock, p.MessageType.DEVICE_INFO)
        if len(payload) != p.DEVICE_INFO_SIZE:
            raise SystemExit(f"DEVICE_INFO wrong size: {len(payload)} != {p.DEVICE_INFO_SIZE}")
        device_info = p.DeviceInfo.unpack(payload)
        decoders = []
        if device_info.decoder_mask & p.CODEC_H264:
            decoders.append("H264")
        if device_info.decoder_mask & p.CODEC_HEVC:
            decoders.append("HEVC")
        print(f"<- DEVICE_INFO  native={device_info.native_w}x{device_info.native_h} "
              f"maxDecode={device_info.max_decode_w}x{device_info.max_decode_h} "
              f"decoders={'|'.join(decoders) or 'none'} "
              f"refresh={device_info.max_refresh_hz}Hz "
              f"P3={device_info.supports_p3} hwHEVC={device_info.hw_hevc}")

        # 2) STREAM_CONFIG -> STREAM_CONFIG_ACK ----------------------------
        cfg = p.StreamConfig(
            w=2732, h=2048,
            codec=p.CODEC_ID_H264,
            chroma=p.CHROMA_420,
            bit_depth=8,
            full_range=1,
            color_primaries=1,   # BT.709 (RECONCILED from 12; stream is 709/sRGB)
            transfer=13,         # sRGB
            matrix=1,            # BT.709
        )
        send_msg(sock, p.MessageType.STREAM_CONFIG, cfg.pack(), seq)
        seq += 1
        print(f"-> STREAM_CONFIG  {cfg.w}x{cfg.h} codec={cfg.codec} chroma={cfg.chroma} "
              f"bitDepth={cfg.bit_depth} fullRange={cfg.full_range} "
              f"prim={cfg.color_primaries} transfer={cfg.transfer} matrix={cfg.matrix}")

        _, payload = expect(sock, p.MessageType.STREAM_CONFIG_ACK)
        if len(payload) != p.STREAM_CONFIG_ACK_SIZE:
            raise SystemExit(f"ACK wrong size: {len(payload)} != {p.STREAM_CONFIG_ACK_SIZE}")
        ack = p.StreamConfigAck.unpack(payload)
        print(f"<- STREAM_CONFIG_ACK  ok={ack.ok} reason={ack.reason}")
        if ack.ok != 1:
            ok_overall = False
            print(f"   !! device REJECTED stream config (reason={ack.reason})")
        else:
            stream_config = cfg

        # 3) PING/PONG keepalive -------------------------------------------
        if ok_overall:
            ping_payload = bytes(range(args.ping_bytes % 256)) \
                if args.ping_bytes <= 256 else os.urandom(args.ping_bytes)
            ping_payload = ping_payload[:args.ping_bytes].ljust(args.ping_bytes, b"\x00")
            print(f"\nRunning {args.pings} PING/PONG ({args.ping_bytes}B)...")
            for _ in range(args.pings):
                t0 = time.perf_counter()
                send_msg(sock, p.MessageType.PING, ping_payload, seq)
                seq += 1
                hdr, pong = expect(sock, p.MessageType.PONG)
                dt_ms = (time.perf_counter() - t0) * 1000.0
                if pong != ping_payload:
                    ok_overall = False
                    print(f"   !! PONG payload mismatch ({len(pong)}B vs {len(ping_payload)}B)")
                    break
                rtts.append(dt_ms)

        # 4) Graceful teardown --------------------------------------------
        try:
            send_msg(sock, p.MessageType.TEARDOWN, b"", seq)
        except OSError:
            pass
    finally:
        sock.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    # --- summary -----------------------------------------------------------
    print("\n=== M2 HANDSHAKE RESULT ===")
    if device_info is not None:
        print(f"  device native:     {device_info.native_w}x{device_info.native_h} "
              f"@ {device_info.max_refresh_hz}Hz  (P3={device_info.supports_p3}, "
              f"hwHEVC={device_info.hw_hevc})")
    if stream_config is not None:
        print(f"  negotiated config: {stream_config.w}x{stream_config.h} "
              f"H264 420 {stream_config.bit_depth}-bit fullRange={stream_config.full_range} "
              f"prim={stream_config.color_primaries} transfer={stream_config.transfer} "
              f"matrix={stream_config.matrix}")
    if rtts:
        print(f"  PING/PONG RTT:     median {statistics.median(rtts):.2f} ms "
              f"(min {min(rtts):.2f}, max {max(rtts):.2f}) over {len(rtts)} pings")

    passed = ok_overall and stream_config is not None and len(rtts) == args.pings
    print(f"\n  {'PASS' if passed else 'FAIL'}: handshake "
          f"{'completed' if passed else 'did not complete'}.")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

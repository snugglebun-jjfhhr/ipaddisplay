#!/usr/bin/env python3
"""M5 host NAL pump — the FIRST MOVING IMAGE path (Windows -> iPad).

Pipeline:
  ddagrab/NVENC (ffmpeg, same args as capture_encode.ps1) -> Annex-B H.264 on a
  stdout PIPE -> this program splits access units on AUD(type 9) boundaries,
  extracts SPS(7)/PPS(8), and pushes them over the usbmux TCP tunnel as:
    - VIDEO_PARAM_SETS (0x10) once on first/changed parameter sets
    - VIDEO_FRAME      (0x11) one AVCC access unit per message
  to the iPad LinkServer, which decodes (VideoToolbox) and renders (Metal).

This is the runnable Python stand-in for the future C++ host. It uses ONLY the
canonical wire codec in host/protocol.py (no byte layouts redefined here) and
reuses host/handshake_host.py for the usbmux forward + framing helpers.

FSM:
  start `pymobiledevice3 usbmux forward 7000 7000` and connect
    -> HELLO             (H->D)  codecMask = H264
    -> DEVICE_INFO       (D->H)
    -> STREAM_CONFIG     (H->D)  H264 420 8 fullRange prim=1 transfer=13 matrix=1
    -> STREAM_CONFIG_ACK (D->H)  assert ok == 1
  spawn ffmpeg -> read stdout in a reader thread -> parse AUs -> enqueue msgs
  main thread is the sole socket writer: drains the queue, sends video, prints
  fps/bitrate stats, and on exit sends TEARDOWN and kills ffmpeg + the forward.

Color contract (must match the live M3/M4 stream and protocol.py):
  BT.709 primaries (1), sRGB transfer (13), BT.709 matrix (1), FULL range.

Prereqs:
  - iPad plugged in, unlocked, trusted; the ipaddisplay app running + FOREGROUND.
  - pip install pymobiledevice3.
  - ffmpeg >= 6.1 with ddagrab + h264_nvenc, run in an interactive desktop session
    (ddagrab fails under WSL/SSH/service contexts).

Run (PowerShell on the Windows desktop):
  py -3.12 stream_host.py
  py -3.12 stream_host.py --bitrate 60M --fps 60 --output-idx 0
  py -3.12 stream_host.py --ffmpeg "C:\\ffmpeg\\bin\\ffmpeg.exe"
"""
from __future__ import annotations

import argparse
import os
import queue
import socket
import subprocess
import sys
import threading
import time

# Canonical codec + the M2 transport/framing helpers (no redefinition here).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import protocol as p          # noqa: E402
import handshake_host as hh   # noqa: E402

DEFAULT_FFMPEG = r"C:\ffmpeg\bin\ffmpeg.exe"
READ_CHUNK = 1 << 16          # 64 KiB reads off the ffmpeg pipe
QUEUE_MAX = 240               # ~4 s at 60 fps of buffered AUs before backpressure
STATS_INTERVAL_S = 2.0

_SENTINEL = object()          # reader -> sender "ffmpeg ended" marker


# --- incremental Annex-B access-unit parser --------------------------------


class AccessUnitParser:
    """Split a streamed Annex-B byte feed into whole access units at AUD (type 9)
    boundaries. An AU is only emitted once the *next* AUD has been seen, so a
    start code (or AUD) split across socket/pipe reads is simply retained until
    more bytes arrive. Robust to partial reads; never emits a partial AU.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        # _find_start_codes is typed for bytes; pass an immutable bytes view of
        # the working bytearray so the (runtime-equivalent) call is also type-clean.
        positions = p._find_start_codes(bytes(self._buf))
        n = len(self._buf)
        # Indices (into `positions`) of start codes that begin an AUD NAL. A
        # start code can sit at the very end of the buffer before its NAL type
        # byte has arrived (ps == n); skip it until the next read fills it in.
        aud_idxs = [
            i for i, (_sc, ps) in enumerate(positions)
            if ps < n and (self._buf[ps] & 0x1F) == p.NAL_AUD
        ]
        if len(aud_idxs) < 2:
            return []  # need a trailing AUD to know the current AU is complete
        aus: list[bytes] = []
        for k in range(len(aud_idxs) - 1):
            start = positions[aud_idxs[k]][0]
            end = positions[aud_idxs[k + 1]][0]
            aus.append(bytes(self._buf[start:end]))
        # Retain everything from the last (still-open) AUD onward.
        keep_from = positions[aud_idxs[-1]][0]
        self._buf = self._buf[keep_from:]
        return aus


# --- ffmpeg ----------------------------------------------------------------


def build_ffmpeg_args(ffmpeg: str, fps: int, bitrate: str, output_idx: int,
                      draw_mouse: int, test: bool = False) -> list[str]:
    """Mirror capture_encode.ps1's PRIMARY ddagrab->NVENC path, but stream the
    Annex-B elementary stream to stdout (`-f h264 pipe:1`). Keep the exact color
    convert + VUI tag pair so the 601-vs-709 / PC-vs-TV trap cannot occur.

    test=True swaps the desktop capture for a guaranteed-moving testsrc2 pattern
    (a diagnostic to separate capture-content issues from the iPad display path)."""
    scale_csc = ("scale=in_range=full:out_range=full:out_color_matrix=bt709:"
                 "flags=full_chroma_int+accurate_rnd,format=nv12")
    setparams = ("setparams=range=pc:color_primaries=bt709:"
                 "color_trc=iec61966-2-1:colorspace=bt709")
    # ~1-frame VBV, matching the .ps1 computation: bitrate(Mbit) * 1000 / fps.
    bitrate_mbit = float(bitrate.rstrip("Mm"))
    vbv = f"{round(bitrate_mbit * 1000 / fps)}k"
    enc = [
        "-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ull",
        "-profile:v", "high", "-level", "auto", "-aud", "1",
        "-rc", "cbr", "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", vbv,
        "-bf", "0", "-g", str(fps), "-no-scenecut", "1", "-forced-idr", "1",
        "-zerolatency", "1", "-rc-lookahead", "0",
        "-f", "h264", "pipe:1",
    ]
    if test:
        # Synthetic moving pattern (CPU lavfi source) -> same CSC/VUI -> NVENC.
        vf = f"{scale_csc},{setparams}"
        return [ffmpeg, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate={fps}",
                "-vf", vf] + enc
    fc = (f"ddagrab=output_idx={output_idx}:framerate={fps}:draw_mouse={draw_mouse},"
          f"hwdownload,format=bgra,{scale_csc},{setparams}")
    return [ffmpeg, "-hide_banner", "-loglevel", "error",
            "-init_hw_device", "d3d11va",
            "-filter_complex", fc] + enc


def spawn_ffmpeg(args: list[str]) -> subprocess.Popen:
    try:
        # bufsize=0 -> unbuffered raw stdout for low-latency reads; stderr is
        # inherited so ffmpeg's (loglevel=error) diagnostics reach the console.
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, bufsize=0)
    except FileNotFoundError:
        raise SystemExit(
            f"ffmpeg not found: {args[0]}\n"
            "  - Pass --ffmpeg with the full path (e.g. C:\\ffmpeg\\bin\\ffmpeg.exe).\n"
            "  - Need a gyan.dev / BtbN build >= 6.1 with ddagrab + h264_nvenc."
        )
    if proc.stdout is None:
        raise SystemExit("ffmpeg was started but its stdout pipe is None.")
    return proc


# --- reader thread: ffmpeg stdout -> parse -> enqueue ----------------------


def reader_loop(proc: subprocess.Popen, q: "queue.Queue", stop: threading.Event) -> None:
    """Read the Annex-B stream, split into AUs, extract SPS/PPS, and enqueue
    (msg_type, flags, payload) tuples for the sender. Forces the stream to begin
    on an IDR: any pre-IDR access units are dropped."""
    parser = AccessUnitParser()
    last_ps_key: tuple[bytes, bytes] | None = None
    seen_idr = False

    # Bind the stdout pipe once (Popen.stdout is Optional); spawn_ffmpeg already
    # guarantees it is not None, but bind locally so the read loop is type-clean.
    stdout = proc.stdout
    if stdout is None:
        raise RuntimeError("ffmpeg stdout pipe is None")

    def safe_put(item) -> bool:
        # Block with backpressure, but stay responsive to shutdown.
        while not stop.is_set():
            try:
                q.put(item, timeout=0.2)
                return True
            except queue.Full:
                continue
        return False

    try:
        while not stop.is_set():
            data = stdout.read(READ_CHUNK)
            if not data:
                break  # ffmpeg closed stdout (exited / capture ended)
            for au in parser.feed(data):
                nals = p.split_nals(au)
                if not nals:
                    continue
                sps = [n for n in nals if p.nal_type(n) == p.NAL_SPS]
                pps = [n for n in nals if p.nal_type(n) == p.NAL_PPS]
                if sps and pps:
                    key = (b"".join(sps), b"".join(pps))
                    if key != last_ps_key:
                        last_ps_key = key
                        payload = p.pack_video_param_sets(sps + pps)
                        if not safe_put((p.MessageType.VIDEO_PARAM_SETS, 0, payload)):
                            return
                flags = p.video_frame_flags(nals)
                if not seen_idr:
                    if not (flags & p.VIDEO_FLAG_IDR):
                        continue  # drop pre-roll so the iPad starts on an IDR
                    seen_idr = True
                # Feed VideoToolbox only the slice/SEI NALs: strip the AUD (type 9)
                # and any in-band SPS/PPS (sent separately via VIDEO_PARAM_SETS).
                # `flags` is still derived from the FULL nals list above, so IDR /
                # paramSetsPrecede detection is unaffected by the stripping.
                if not safe_put((p.MessageType.VIDEO_FRAME, flags,
                                 p.avcc_from_au_for_decode(au))):
                    return
    finally:
        q.put(_SENTINEL)


# --- sender (main thread, sole socket writer) ------------------------------


def send_msg(sock: socket.socket, msg_type, payload: bytes, seq: int, flags: int) -> None:
    """Frame and send one message: 24-byte header (with flags) + payload."""
    hdr = p.make_header(int(msg_type), len(payload), seq=seq, flags=flags)
    sock.sendall(hdr + payload)


def sender_loop(sock: socket.socket, q: "queue.Queue", seq: int) -> int:
    """Drain the queue and send. Returns the next sequence number. Exits when the
    reader enqueues the sentinel (ffmpeg ended) or on KeyboardInterrupt."""
    frames = 0
    idr = 0
    bytes_sent = 0
    win_frames = 0
    win_bytes = 0
    t_start = time.monotonic()
    t_win = t_start

    while True:
        item = q.get()
        if item is _SENTINEL:
            break
        msg_type, flags, payload = item
        send_msg(sock, msg_type, payload, seq, flags)
        seq += 1

        if msg_type == p.MessageType.VIDEO_PARAM_SETS:
            print(f"-> VIDEO_PARAM_SETS  {len(payload)}B "
                  f"({len(p.unpack_video_param_sets(payload))} NALs)")
            continue

        # VIDEO_FRAME stats.
        frames += 1
        win_frames += 1
        bytes_sent += len(payload)
        win_bytes += len(payload)
        if flags & p.VIDEO_FLAG_IDR:
            idr += 1
        now = time.monotonic()
        dt = now - t_win
        if dt >= STATS_INTERVAL_S:
            fps = win_frames / dt
            mbps = (win_bytes * 8) / dt / 1e6
            print(f"   stream: {fps:5.1f} fps  {mbps:6.1f} Mbit/s  "
                  f"({frames} frames, {idr} IDR, {bytes_sent / 1e6:.1f} MB total)")
            t_win = now
            win_frames = 0
            win_bytes = 0

    total_dt = max(time.monotonic() - t_start, 1e-6)
    print(f"\n  sent {frames} frames ({idr} IDR), {bytes_sent / 1e6:.1f} MB "
          f"in {total_dt:.1f}s  ({frames / total_dt:.1f} fps avg).")
    return seq


# --- handshake -------------------------------------------------------------


def do_handshake(sock: socket.socket, width: int, height: int, fps: int) -> int:
    """HELLO -> DEVICE_INFO -> STREAM_CONFIG -> ACK. Returns the next seq."""
    seq = 0
    hello = p.Hello(codec_mask=p.CODEC_H264, max_bitrate_kbps=80_000)
    hh.send_msg(sock, p.MessageType.HELLO, hello.pack(), seq)
    seq += 1
    print("-> HELLO  codecMask=H264 maxBitrate=80000kbps")

    _, payload = hh.expect(sock, p.MessageType.DEVICE_INFO)
    di = p.DeviceInfo.unpack(payload)
    print(f"<- DEVICE_INFO  native={di.native_w}x{di.native_h} "
          f"maxDecode={di.max_decode_w}x{di.max_decode_h} "
          f"refresh={di.max_refresh_hz}Hz P3={di.supports_p3} hwHEVC={di.hw_hevc}")

    # w/h are advisory for M5: the iPad drives the render size from the decoded
    # CVPixelBuffer, not from this field. Color values are the load-bearing part.
    cfg = p.StreamConfig(
        w=width, h=height, fps_cap=fps,
        codec=p.CODEC_ID_H264, chroma=p.CHROMA_420, bit_depth=8,
        full_range=1, color_primaries=1, transfer=13, matrix=1,
    )
    hh.send_msg(sock, p.MessageType.STREAM_CONFIG, cfg.pack(), seq)
    seq += 1
    print(f"-> STREAM_CONFIG  {cfg.w}x{cfg.h}@{cfg.fps_cap} H264 420 8 "
          f"fullRange={cfg.full_range} prim={cfg.color_primaries} "
          f"transfer={cfg.transfer} matrix={cfg.matrix}")

    _, payload = hh.expect(sock, p.MessageType.STREAM_CONFIG_ACK)
    ack = p.StreamConfigAck.unpack(payload)
    print(f"<- STREAM_CONFIG_ACK  ok={ack.ok} reason={ack.reason}")
    if ack.ok != 1:
        raise SystemExit(f"device REJECTED stream config (reason={ack.reason}).")
    return seq


# --- main ------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="M5 host NAL pump (ffmpeg -> iPad)")
    ap.add_argument("--ffmpeg", default=DEFAULT_FFMPEG,
                    help=f"ffmpeg path (default {DEFAULT_FFMPEG})")
    ap.add_argument("--bitrate", default="80M", help="CBR target (default 80M)")
    ap.add_argument("--fps", type=int, default=60, help="capture/encode fps (default 60)")
    ap.add_argument("--output-idx", type=int, default=0,
                    help="ddagrab output index (0 = primary monitor)")
    ap.add_argument("--draw-mouse", type=int, default=1, help="bake cursor (default 1)")
    ap.add_argument("--width", type=int, default=1920,
                    help="STREAM_CONFIG width (advisory; default 1920)")
    ap.add_argument("--height", type=int, default=1080,
                    help="STREAM_CONFIG height (advisory; default 1080)")
    ap.add_argument("--test", action="store_true",
                    help="diagnostic: stream a moving testsrc2 pattern instead of the desktop")
    args = ap.parse_args()

    print("Starting usbmux forward 7000->7000 and connecting to the app...")
    proc_fwd, sock = hh.start_forward()
    sock.settimeout(None)  # blocking sends for the stream
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print("Connected to iPad LinkServer.\n")

    proc_ff: subprocess.Popen | None = None
    reader: threading.Thread | None = None
    stop = threading.Event()
    q: "queue.Queue" = queue.Queue(maxsize=QUEUE_MAX)
    seq = 0

    try:
        seq = do_handshake(sock, args.width, args.height, args.fps)

        ff_args = build_ffmpeg_args(args.ffmpeg, args.fps, args.bitrate,
                                    args.output_idx, args.draw_mouse, test=args.test)
        print(f"\nspawning ffmpeg:\n  {' '.join(ff_args)}\n")
        proc_ff = spawn_ffmpeg(ff_args)

        reader = threading.Thread(target=reader_loop, args=(proc_ff, q, stop),
                                  name="ffmpeg-reader", daemon=True)
        reader.start()
        print("streaming... (Ctrl+C to stop)\n")
        seq = sender_loop(sock, q, seq)
    except KeyboardInterrupt:
        print("\ninterrupted; shutting down...")
    finally:
        stop.set()
        # Unblock the reader if it is parked on a full queue.
        try:
            while True:
                q.get_nowait()
        except queue.Empty:
            pass
        if proc_ff is not None:
            proc_ff.terminate()
            try:
                proc_ff.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc_ff.kill()
        if reader is not None:
            reader.join(timeout=5)
        # Best-effort graceful TEARDOWN, then close the tunnel.
        try:
            send_msg(sock, p.MessageType.TEARDOWN, b"", seq, 0)
        except OSError:
            pass
        sock.close()
        proc_fwd.terminate()
        try:
            proc_fwd.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc_fwd.kill()

    print("\ndone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

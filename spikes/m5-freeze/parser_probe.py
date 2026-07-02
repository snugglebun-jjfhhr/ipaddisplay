"""Probe C': ffmpeg -> pipe -> REAL reader machinery (AccessUnitParser +
split_nals + flags + AVCC repack) -> discard. No queue, no socket, no iPad.

If this reaches 15 fps, the parser is fine and the socket send path is the
throttle. If it crawls at ~2 fps, the parser is the throttle.
"""
import subprocess
import time

from stream_host import AccessUnitParser, build_ffmpeg_args
import protocol as p

ARGS = build_ffmpeg_args(r"C:\ffmpeg\bin\ffmpeg.exe", fps=15, bitrate="80M",
                         output_idx=0, draw_mouse=1, test=False)
ARGS = ARGS[:-3] + ["-t", "10"] + ARGS[-3:]  # ... -t 10 -f h264 pipe:1

proc = subprocess.Popen(ARGS, stdout=subprocess.PIPE, bufsize=0)
parser = AccessUnitParser()
aus = 0
au_bytes = 0
win_aus = 0
t0 = time.monotonic()
t_win = t0
while True:
    data = proc.stdout.read(1 << 16)
    if not data:
        break
    for au in parser.feed(data):
        nals = p.split_nals(au)
        if not nals:
            continue
        flags = p.video_frame_flags(nals)
        payload = p.avcc_from_au_for_decode(au)
        aus += 1
        win_aus += 1
        au_bytes += len(payload)
    now = time.monotonic()
    if now - t_win >= 2.0 and win_aus:
        print(f"  {win_aus / (now - t_win):5.1f} AU/s  ({aus} AUs, "
              f"{au_bytes / 1e6:.1f} MB payload)", flush=True)
        t_win = now
        win_aus = 0
dt = time.monotonic() - t0
print(f"DONE: {aus} AUs in {dt:.1f}s = {aus / dt:.1f} AU/s", flush=True)
proc.wait()

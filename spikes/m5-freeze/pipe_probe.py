"""Probe B: ffmpeg ddagrab -> stdout pipe -> python reader that discards.

Measures whether the pipe+python-reader alone throttles ffmpeg production
(no AU parsing, no queue, no socket, no iPad). Prints bytes/s and read-call
stats every 2s for 10s, then exits.
"""
import subprocess
import sys
import time

FFMPEG = r"C:\ffmpeg\bin\ffmpeg.exe"
FC = ("ddagrab=output_idx=0:framerate=15:draw_mouse=1,"
      "hwdownload,format=bgra,"
      "scale=in_range=full:out_range=full:out_color_matrix=bt709:"
      "flags=full_chroma_int+accurate_rnd,format=nv12,"
      "setparams=range=pc:color_primaries=bt709:"
      "color_trc=iec61966-2-1:colorspace=bt709")
ARGS = [FFMPEG, "-hide_banner", "-loglevel", "error",
        "-init_hw_device", "d3d11va", "-filter_complex", FC,
        "-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ull",
        "-profile:v", "high", "-level", "auto", "-aud", "1",
        "-rc", "cbr", "-b:v", "80M", "-maxrate", "80M", "-bufsize", "5333k",
        "-bf", "0", "-g", "15", "-no-scenecut", "1", "-forced-idr", "1",
        "-zerolatency", "1", "-rc-lookahead", "0",
        "-t", "10", "-f", "h264", "pipe:1"]

proc = subprocess.Popen(ARGS, stdout=subprocess.PIPE, bufsize=0)
total = 0
reads = 0
win_bytes = 0
t0 = time.monotonic()
t_win = t0
while True:
    data = proc.stdout.read(1 << 16)
    if not data:
        break
    total += len(data)
    reads += 1
    win_bytes += len(data)
    now = time.monotonic()
    if now - t_win >= 2.0:
        print(f"  {win_bytes * 8 / (now - t_win) / 1e6:7.1f} Mbit/s "
              f"({reads} reads, {total / 1e6:.1f} MB total)", flush=True)
        t_win = now
        win_bytes = 0
dt = time.monotonic() - t0
print(f"DONE: {total / 1e6:.1f} MB in {dt:.1f}s = {total * 8 / dt / 1e6:.1f} Mbit/s "
      f"({reads} reads)", flush=True)
proc.wait()

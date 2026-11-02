"""Reproduces the reported timeline bug: motion clips exported from a cloud DVR.

* folder named by date (``31Oct26New``), file names carry NO time
  (``G225087WSFW-cloud-18056409.mp4``),
* the recording time exists only in the burnt-in DVR clock (OSD, top-right),
* container ``creation_time`` and file mtime are the *download* time on the next
  day, assigned in an order unrelated to the recording order.

Usage: python tests/make_cloud_clips.py <out_root> [n_clips]
Writes ``<out_root>/31Oct26New/*.mp4`` and ``<out_root>/truth.json``
({file name: recording start "YYYY-MM-DD HH:MM:SS"}).
"""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys

import cv2
import numpy as np

# (cloud sequence number, recording start) – mirrors the screenshots: 05:38:27, 20:38:34, 22:00:46 ...
CLIPS = [
    (18056409, dt.datetime(2026, 10, 31, 5, 38, 27)),
    (18060945, dt.datetime(2026, 10, 31, 20, 38, 34)),
    (18061426, dt.datetime(2026, 10, 31, 22, 0, 46)),
    (18050112, dt.datetime(2026, 10, 31, 1, 12, 5)),
    (18058870, dt.datetime(2026, 10, 31, 13, 47, 50)),
]
# download (mtime) order deliberately scrambled relative to recording order
DOWNLOAD_ORDER = [2, 0, 4, 1, 3]
W, H, FPS, SECS = 640, 360, 10, 14


def _frame(bg, clock: dt.datetime, s: float, walker: bool):
    f = bg.copy()
    if walker and 3 <= s < 11:
        x = int(40 + (s - 3) * 60)
        cv2.rectangle(f, (x, 130), (x + 45, 320), (40, 40, 200), -1)
        cv2.circle(f, (x + 22, 112), 18, (150, 170, 210), -1)
    txt = clock.strftime("%Y-%m-%d %H:%M:%S")
    # light-grey DVR clock, top-right, like the screenshots
    cv2.putText(f, txt, (W - 300, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (215, 215, 215), 2, cv2.LINE_AA)
    return f


def make(root: str, n: int = len(CLIPS)) -> dict:
    folder = os.path.join(root, "31Oct26New")
    os.makedirs(folder, exist_ok=True)
    rng = np.random.default_rng(3)
    truth = {}
    download = dt.datetime(2026, 11, 1, 11, 21, 50)
    for k, idx in enumerate(DOWNLOAD_ORDER[:n] if n <= len(DOWNLOAD_ORDER) else range(n)):
        seq, start = CLIPS[idx]
        name = f"G225087WSFW-cloud-{seq}.mp4"
        tmp = os.path.join(root, f".tmp_{seq}.mp4")
        bg = np.full((H, W, 3), 25 if start.hour < 6 or start.hour >= 19 else 110, np.uint8)
        cv2.rectangle(bg, (420, 200), (640, 360), (50, 60, 90), -1)
        vw = cv2.VideoWriter(tmp, cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
        for i in range(SECS * FPS):
            s = i / FPS
            f = _frame(bg, start + dt.timedelta(seconds=int(s)), s, walker=True)
            vw.write(cv2.add(f, rng.integers(0, 4, f.shape, dtype=np.uint8)))
        vw.release()
        dl = download + dt.timedelta(seconds=3 * k)
        out = os.path.join(folder, name)
        ff = shutil.which("ffmpeg")
        if ff:   # cloud export stamps the *export* time as creation_time
            subprocess.run([ff, "-nostdin", "-y", "-v", "error", "-i", tmp, "-c", "copy", "-metadata",
                            f"creation_time={(dl - dt.timedelta(hours=5, minutes=30)).isoformat()}Z", out], check=True)
            os.remove(tmp)
        else:
            os.replace(tmp, out)
        ts = dl.timestamp()
        os.utime(out, (ts, ts))
        truth[name] = start.strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(root, "truth.json"), "w") as fh:
        json.dump(truth, fh, indent=1)
    return truth


if __name__ == "__main__":
    print(json.dumps(make(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else len(CLIPS)), indent=1))


def make_stitched(path: str, first=dt.datetime(2026, 10, 31, 10, 0, 0), second=dt.datetime(2026, 10, 31, 15, 30, 0),
                  half: int = 12) -> None:
    """One file made of two recordings: the DVR clock jumps from `first` to `second` half-way.
    A walker appears only in the second part (an event whose time must come from its own clock)."""
    rng = np.random.default_rng(5)
    bg = np.full((H, W, 3), 110, np.uint8)
    vw = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    for i in range(2 * half * FPS):
        s = i / FPS
        part2 = s >= half
        clock = (second + dt.timedelta(seconds=int(s - half))) if part2 else (first + dt.timedelta(seconds=int(s)))
        f = _frame(bg, clock, (s - half) if part2 else -99, walker=part2)
        vw.write(cv2.add(f, rng.integers(0, 4, f.shape, dtype=np.uint8)))
    vw.release()

"""Three seized DVR sources for a case, with DVR clocks that disagree with the true time.

The same suspect (a red-and-blue figure) walks past three cameras in true order:

    Shop A (ch01, MP4)         true 21:14:03   DVR clock 11 minutes FAST  (shows 21:25:03)
    Junction B (ch03, raw .264) true 21:18:03   DVR clock 5 minutes SLOW   (shows 21:13:03)
    Alley C (ch07, MPEG-TS)    true 21:23:03   DVR clock correct

File names carry no time: the recording time can only come from the clock burnt into the
picture (OCR), as with real seized footage. In DVR time the order is B → C → A; only with the
officer's clock corrections (-00:11:00, +00:05:00, 0) does it become A → B → C.

Usage: python tests/make_case_sources.py <root>   (writes <root>/<folder>/… and prints the truth)
"""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys

import cv2
import numpy as np

W, H, FPS, SECS = 640, 360, 25, 14
SOURCES = [
    # folder, file, camera, true start, DVR skew (DVR = true + skew), container
    ("ShopA_DVR", "ch01_record.mp4", "ch01", dt.datetime(2026, 10, 31, 21, 14, 0), dt.timedelta(minutes=11), "mp4"),
    ("TrafficCam_North", "ch03_export.264", "ch03", dt.datetime(2026, 10, 31, 21, 18, 0), dt.timedelta(minutes=-5), "h264"),
    ("Jeweler_NVR", "ch07_backup.ts", "ch07", dt.datetime(2026, 10, 31, 21, 23, 0), dt.timedelta(0), "mpegts"),
]
WALK = (3.0, 11.0)          # the suspect is in view from 3 s to 11 s of each clip


def _frames(dvr_start: dt.datetime, seed: int):
    rng = np.random.default_rng(seed)
    bg = np.full((H, W, 3), 95 + 15 * seed, np.uint8)
    cv2.rectangle(bg, (420, 200), (640, 360), (50, 60, 90), -1)
    for i in range(SECS * FPS):
        s = i / FPS
        f = bg.copy()
        if WALK[0] <= s < WALK[1]:
            x = int(40 + (s - WALK[0]) * 55)
            cv2.rectangle(f, (x, 140), (x + 44, 240), (40, 40, 200), -1)    # red top
            cv2.rectangle(f, (x + 4, 240), (x + 40, 330), (150, 70, 30), -1)  # blue trousers
            cv2.circle(f, (x + 22, 120), 18, (150, 170, 210), -1)
        clock = (dvr_start + dt.timedelta(seconds=int(s))).strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(f, clock, (W - 300, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (225, 225, 225), 2, cv2.LINE_AA)
        yield cv2.add(f, rng.integers(0, 4, f.shape, dtype=np.uint8))


def make(root: str) -> dict:
    ff = shutil.which("ffmpeg")
    truth = {}
    for k, (folder, name, cam, true_start, skew, fmt) in enumerate(SOURCES):
        d = os.path.join(root, folder)
        os.makedirs(d, exist_ok=True)
        out = os.path.join(d, name)
        enc = ["-c:v", "libx264", "-preset", "veryfast", "-g", "25", "-pix_fmt", "yuv420p"]
        tail = {"mp4": ["-movflags", "+faststart"], "h264": ["-bsf:v", "h264_mp4toannexb", "-f", "h264"],
                "mpegts": ["-f", "mpegts"]}[fmt]
        p = subprocess.Popen([ff, "-nostdin", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s",
                              f"{W}x{H}", "-r", str(FPS), "-i", "pipe:0", *enc, *tail, out], stdin=subprocess.PIPE)
        for f in _frames(true_start + skew, k):
            p.stdin.write(f.tobytes())
        p.stdin.close()
        assert p.wait() == 0
        truth[folder] = {"file": name, "camera": cam, "true_start": true_start.strftime("%Y-%m-%d %H:%M:%S"),
                         "dvr_start": (true_start + skew).strftime("%Y-%m-%d %H:%M:%S"),
                         "skew_seconds": skew.total_seconds(), "walk": WALK}
    return truth


if __name__ == "__main__":
    print(json.dumps(make(sys.argv[1]), indent=1))

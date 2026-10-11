"""Builds a synthetic 'shop CCTV' clip: static scene + burnt-in clock, idle
stretches, a walking person-like blob, a lights-off moment and a covered lens."""
import sys, datetime as dt
import cv2, numpy as np
out = sys.argv[1]
W, H, FPS, SECS = 640, 360, 12, 150
vw = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
rng = np.random.default_rng(0)
bg = np.full((H, W, 3), 120, np.uint8)
cv2.rectangle(bg, (400, 200), (640, 360), (60, 80, 140), -1)   # counter
for x in range(20, 380, 60):
    cv2.rectangle(bg, (x, 60), (x + 40, 300), (90, 140, 90), -1)  # shelves
t0 = dt.datetime(2026, 10, 11, 22, 15, 0)
for i in range(SECS * FPS):
    s = i / FPS
    f = bg.copy()
    if 20 <= s < 45:                                    # walker 1
        x = int(30 + (s - 20) * 22)
        cv2.rectangle(f, (x, 120), (x + 40, 300), (40, 40, 200), -1)
        cv2.circle(f, (x + 20, 105), 18, (150, 170, 210), -1)
    if 70 <= s < 72:                                    # lights off
        f = (f * 0.25).astype(np.uint8)
    if 90 <= s < 110:                                   # walker 2 into counter zone
        x = int(300 + (s - 90) * 14)
        cv2.rectangle(f, (x, 130), (x + 45, 330), (200, 60, 40), -1)
    if 125 <= s < 135:                                  # covered lens
        f[:] = 8
    f = cv2.add(f, rng.integers(0, 6, f.shape, dtype=np.uint8))
    cv2.putText(f, (t0 + dt.timedelta(seconds=int(s))).strftime("%Y-%m-%d %H:%M:%S"),
                (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    vw.write(f)
vw.release()

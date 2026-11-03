"""A car with an Indian number plate driving across a static scene (for ANPR tests).

python tests/make_plate_video.py out.mp4 [PLATE] [--blur N]
--blur N applies N-pixel horizontal motion blur to every frame (a fast vehicle)."""
import os
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PLATE_BOX = (307, 187, 387, 210)          # plate in assets/car_rear.png (x1, y1, x2, y2)
CAR_BOX = (40, 40, 455, 272)


def plate_image(text: str, w: int, h: int) -> np.ndarray:
    big = np.full((h * 4, w * 4, 3), 245, np.uint8)
    cv2.rectangle(big, (2, 2), (w * 4 - 3, h * 4 - 3), (20, 20, 20), 4)
    scale = 1.0
    while True:
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_DUPLEX, scale, 3)
        if tw > w * 4 * 0.9 or th > h * 4 * 0.7:
            scale -= 0.05
            break
        scale += 0.05
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_DUPLEX, scale, 3)
    cv2.putText(big, text, ((w * 4 - tw) // 2, (h * 4 + th) // 2), cv2.FONT_HERSHEY_DUPLEX, scale, (15, 15, 15), 3,
                cv2.LINE_AA)
    return cv2.resize(big, (w, h), interpolation=cv2.INTER_AREA)


def car_with_plate(text: str, scale: float = 2.4) -> np.ndarray:
    img = cv2.imread(os.path.join(HERE, "assets", "car_rear.png"))
    x1, y1, x2, y2 = PLATE_BOX
    img[y1:y2, x1:x2] = plate_image(text, x2 - x1, y2 - y1)
    cx1, cy1, cx2, cy2 = CAR_BOX
    car = img[cy1:cy2, cx1:cx2]
    return cv2.resize(car, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)


def make(out: str, text: str = "MH12AB1234", blur: int = 0, secs: int = 10, fps: int = 10,
         W: int = 1600, H: int = 900, scale: float = 2.4) -> None:
    car = car_with_plate(text, scale)
    ch, cw = car.shape[:2]
    bg = np.full((H, W, 3), 95, np.uint8)
    cv2.rectangle(bg, (0, int(H * 0.72)), (W, H), (70, 72, 75), -1)
    rng = np.random.default_rng(7)
    vw = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*"mp4v"), fps, (W, H))
    k = None
    if blur:
        k = np.zeros((1, blur), np.float32)
        k[0, :] = 1.0 / blur
    for i in range(secs * fps):
        s = i / fps
        f = bg.copy()
        if 1.5 <= s < secs - 1.5:
            x = int(-cw * 0.3 + (s - 1.5) / (secs - 3) * (W - cw * 0.4))
            y = H - ch - 40
            xa, xb = max(0, x), min(W, x + cw)
            if xb > xa:
                f[y:y + ch, xa:xb] = car[:, xa - x:xb - x]
        if k is not None:
            f = cv2.filter2D(f, -1, k)
        vw.write(cv2.add(f, rng.integers(0, 4, f.shape, dtype=np.uint8)))
    vw.release()


if __name__ == "__main__":
    args = sys.argv[1:]
    opts = {"--blur": 0, "--scale": 2.4}
    for k in list(opts):
        if k in args:
            i = args.index(k)
            opts[k] = type(opts[k])(args[i + 1])
            del args[i:i + 2]
    make(args[0], args[1] if len(args) > 1 else "MH12AB1234", opts["--blur"], scale=opts["--scale"])

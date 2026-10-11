"""Synthetic clips with real faces (OpenCV sample photos) moving across a scene."""
import sys, cv2, numpy as np
def make(out, actors, secs=50, W=960, H=540, fps=10):
    vw = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*"mp4v"), fps, (W, H))
    rng = np.random.default_rng(1)
    bg = np.full((H, W, 3), 110, np.uint8)
    cv2.rectangle(bg, (0, 380), (W, H), (70, 90, 120), -1)
    for x in range(0, W, 70):                                   # shelves with products (texture)
        cv2.rectangle(bg, (x + 5, 40), (x + 60, 370), (60, 120, 80), -1)
        for y in range(50, 360, 30):
            cv2.rectangle(bg, (x + 10, y), (x + 55, y + 22), tuple(int(c) for c in rng.integers(40, 230, 3)), -1)
    for i in range(secs * fps):
        s = i / fps; f = bg.copy()
        for img, t0, t1, y in actors:
            if t0 <= s < t1:
                x = int(50 + (s - t0) / (t1 - t0) * (W - img.shape[1] - 100))
                f[y:y + img.shape[0], x:x + img.shape[1]] = img
        vw.write(f)
    vw.release()
lena = cv2.resize(cv2.imread("test_data/lena.jpg"), (300, 300))
messi = cv2.imread("test_data/messi5.jpg")[0:342, 150:400]
messi = cv2.resize(messi, (250, 342))
make(sys.argv[1], [(lena, 5, 20, 120), (messi, 28, 44, 100)])
make(sys.argv[2], [(lena, 8, 25, 140)], secs=35)

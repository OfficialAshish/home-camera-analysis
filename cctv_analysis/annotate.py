"""Drawing helpers: boxes, labels, header bars, zones, contact sheets, heatmaps."""
from __future__ import annotations

from typing import List, Optional

import cv2
import numpy as np

PALETTE = [(56, 56, 255), (151, 157, 255), (31, 112, 255), (29, 178, 255), (49, 210, 207),
           (10, 249, 72), (23, 204, 146), (134, 219, 61), (52, 147, 26), (187, 212, 0),
           (168, 153, 44), (255, 194, 0), (147, 69, 52), (255, 115, 100), (236, 24, 0),
           (255, 56, 132), (133, 0, 82), (255, 56, 203), (200, 149, 255), (199, 55, 255)]


def color_for(i) -> tuple:
    if i is None:
        return PALETTE[0]
    k = i if isinstance(i, int) else sum(map(ord, str(i)))
    return PALETTE[(k * 7) % len(PALETTE)]


def label(img, text, x, y, color, scale=0.5):
    (tw, th), bl = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    y = max(th + 4, y)
    cv2.rectangle(img, (x, y - th - 4), (x + tw + 4, y + bl - 2), color, -1)
    cv2.putText(img, text, (x + 2, y - 3), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 1, cv2.LINE_AA)


def draw_zones(img, zones: dict):
    h, w = img.shape[:2]
    over = img.copy()
    for name, poly in zones.items():
        pts = np.array([[int(x * w), int(y * h)] for x, y in poly], np.int32)
        cv2.fillPoly(over, [pts], (0, 0, 255))
        cv2.polylines(img, [pts], True, (0, 0, 255), 2)
        label(img, name, int(pts[:, 0].min()), int(pts[:, 1].min()) + 14, (0, 0, 180))
    cv2.addWeighted(over, 0.15, img, 0.85, 0, img)


def draw_dets(frame, dets: List[dict], faces: List[dict] = (), ids: Optional[dict] = None,
              header: str = "", zones: Optional[dict] = None) -> np.ndarray:
    img = frame.copy()
    s = max(0.45, img.shape[1] / 1600)
    lw = max(1, int(round(img.shape[1] / 640)))
    if zones:
        draw_zones(img, zones)
    for d in dets:
        x1, y1, x2, y2 = [int(v) for v in d["xyxy"]]
        c = color_for(d.get("track"))
        cv2.rectangle(img, (x1, y1), (x2, y2), c, lw + 1)
        txt = f"{d['cls']} {d['conf']:.2f}"
        if d.get("track") is not None:
            txt = f"#{d['track']} " + txt
            if ids and ids.get(d["track"]):
                txt += f" [{ids[d['track']]}]"
        label(img, txt, x1, y1, c, s)
    for f in faces:
        x1, y1, x2, y2 = [int(v) for v in f["box"]]
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 255), lw)
        label(img, f"face {f['score']:.2f}", x1, y2 + 16, (0, 140, 140), s * 0.9)
    if header:
        bar = int(28 * s / 0.5)
        cv2.rectangle(img, (0, img.shape[0] - bar), (img.shape[1], img.shape[0]), (0, 0, 0), -1)
        cv2.putText(img, header, (8, img.shape[0] - int(bar * 0.3)), cv2.FONT_HERSHEY_SIMPLEX,
                    s * 1.1, (255, 255, 255), 1, cv2.LINE_AA)
    return img


def contact_sheet(frames: List[np.ndarray], captions: List[str], cols: int = 3, tile_w: int = 480) -> np.ndarray:
    if not frames:
        return np.zeros((10, 10, 3), np.uint8)
    tiles = []
    for f, c in zip(frames, captions):
        h = int(f.shape[0] * tile_w / f.shape[1])
        t = cv2.resize(f, (tile_w, h), interpolation=cv2.INTER_AREA)
        label(t, c, 4, 18, (40, 40, 40), 0.5)
        tiles.append(t)
    th = max(t.shape[0] for t in tiles)
    tiles = [cv2.copyMakeBorder(t, 0, th - t.shape[0], 0, 0, cv2.BORDER_CONSTANT) for t in tiles]
    cols = min(cols, len(tiles))
    while len(tiles) % cols:
        tiles.append(np.zeros_like(tiles[0]))
    rows = [np.hstack(tiles[i:i + cols]) for i in range(0, len(tiles), cols)]
    return np.vstack(rows)


def heatmap_overlay(base_gray_or_bgr: np.ndarray, heat: np.ndarray) -> np.ndarray:
    base = base_gray_or_bgr
    if base.ndim == 2:
        base = cv2.cvtColor(base, cv2.COLOR_GRAY2BGR)
    hm = cv2.resize(heat, (base.shape[1], base.shape[0]))
    hm = np.uint8(255 * np.sqrt(np.clip(hm, 0, 1)))
    col = cv2.applyColorMap(hm, cv2.COLORMAP_JET)
    return cv2.addWeighted(base, 0.55, col, 0.45, 0)


def thumb(img: np.ndarray, w: int = 320) -> np.ndarray:
    h = int(img.shape[0] * w / img.shape[1])
    return cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)

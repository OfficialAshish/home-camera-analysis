"""Pass 1: cheap full-video motion scan.

* MOG2 background subtraction on a small grey stream (default 384px @ 4 fps)
* automatic masking of the burnt-in DVR clock (OSD) + user ignore regions
* rejection of global lighting changes (lights on/off, IR switch, auto exposure)
* camera tamper detection (covered lens, blackout, defocus, moved camera)
* per-video activity heatmap
* motion intervals -> padded, merged, length-limited events
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import cv2
import numpy as np

from .video_io import iter_frames


@dataclass
class MotionResult:
    events: List[dict] = field(default_factory=list)
    times: np.ndarray = None
    ratios: np.ndarray = None
    heatmap: Optional[np.ndarray] = None
    tamper: List[dict] = field(default_factory=list)
    lighting_changes: List[float] = field(default_factory=list)
    osd_mask: Optional[np.ndarray] = None
    preview: Optional[np.ndarray] = None
    frames: int = 0


def poly_mask(shape: Tuple[int, int], polys, value=255) -> np.ndarray:
    h, w = shape
    m = np.zeros((h, w), np.uint8)
    for poly in polys or []:
        pts = np.array([[int(x * w), int(y * h)] for x, y in poly], np.int32)
        if len(pts) >= 3:
            cv2.fillPoly(m, [pts], value)
    return m


def detect_osd_mask(path: str, meta: dict, width: int, seconds: float = 90.0) -> Optional[np.ndarray]:
    """Find the DVR clock: small text-like regions near the frame border that change
    roughly once per second in an otherwise static picture."""
    frames = [f for _, f in iter_frames(path, 2.0, width, 0, min(seconds, meta.get("duration") or seconds),
                                        gray=True, src_size=(meta["width"], meta["height"]))]
    if len(frames) < 8:
        return None
    h, w = frames[0].shape
    freq = np.zeros((h, w), np.float32)
    for a, b in zip(frames, frames[1:]):
        freq += (cv2.absdiff(a, b) > 30)
    freq /= len(frames) - 1
    cand = (freq > 0.25).astype(np.uint8) * 255
    band = np.zeros_like(cand)
    band[: int(h * 0.18)] = 255
    band[int(h * 0.85):] = 255
    cand = cv2.bitwise_and(cand, band)
    cand = cv2.dilate(cand, cv2.getStructuringElement(cv2.MORPH_RECT, (15, 7)))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(cand)
    mask = np.zeros((h, w), np.uint8)
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if bh < h * 0.12 and bw < w * 0.75 and area > 20:
            cv2.rectangle(mask, (max(0, x - 4), max(0, y - 4)), (min(w, x + bw + 4), min(h, y + bh + 4)), 255, -1)
    return mask if mask.any() else None


def _intervals(times, active, gap):
    out = []
    s = prev = None
    for t, a in zip(times, active):
        if not a:
            continue
        if s is None:
            s = prev = t
        elif t - prev > gap:
            out.append([s, prev])
            s = prev = t
        else:
            prev = t
    if s is not None:
        out.append([s, prev])
    return out


def scan_motion(path: str, meta: dict, cfg, camera: str = "") -> MotionResult:
    res = MotionResult()
    W, H = meta["width"], meta["height"]
    width = min(cfg.motion_width, W) if W else cfg.motion_width
    sens = float(np.clip(cfg.motion_sensitivity, 0, 1))
    var_thr = 12 + (1 - sens) * 44
    diff_thr = int(14 + (1 - sens) * 26)
    min_area = cfg.min_motion_area * (1.6 - sens)
    fps = cfg.motion_fps

    mog = cv2.createBackgroundSubtractorMOG2(history=int(fps * 120), varThreshold=var_thr,
                                             detectShadows=True)
    # only *mild* darkening counts as shadow; dark clothing must stay foreground
    mog.setShadowThreshold(0.82)
    kernel_o = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    kernel_d = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))

    ign = None
    times, ratios, bright, sharp, glob = [], [], [], [], []
    heat = None
    prev = None
    for i, (t, g) in enumerate(iter_frames(path, fps, width, gray=True, src_size=(W, H))):
        if ign is None:
            h, w = g.shape
            polys = list(cfg.ignore_regions.get("*", [])) + list(cfg.ignore_regions.get(camera, []))
            ign = poly_mask((h, w), polys)
            if cfg.auto_mask_osd:
                osd = detect_osd_mask(path, meta, width)
                if osd is not None and osd.shape == ign.shape:
                    ign = cv2.bitwise_or(ign, osd)
                    res.osd_mask = osd
            keep = cv2.bitwise_not(ign)
            heat = np.zeros((h, w), np.float32)
            valid_px = max(1, int(np.count_nonzero(keep)))
        gb = cv2.GaussianBlur(g, (5, 5), 0)
        lr = 1.0 if i == 0 else -1
        fg = mog.apply(gb, learningRate=lr)
        fg = np.where(fg > 200, 255, 0).astype(np.uint8)          # drop shadows (127)
        if prev is not None:                                        # confirm with frame diff
            d = cv2.absdiff(gb, prev)
            dm = (d > diff_thr).astype(np.uint8) * 255
            dm = cv2.dilate(dm, kernel_d, iterations=2)
            fg = cv2.bitwise_and(fg, dm)
        prev = gb
        fg = cv2.bitwise_and(fg, keep)
        raw_ratio = np.count_nonzero(fg) / valid_px
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, kernel_o)
        fg = cv2.dilate(fg, kernel_d)
        n, _, stats, _ = cv2.connectedComponentsWithStats(fg)
        blob_min = valid_px * min_area * 0.25
        area = sum(int(stats[k, 4]) for k in range(1, n) if stats[k, 4] >= blob_min)
        ratio = area / valid_px
        is_global = raw_ratio > cfg.global_change_ratio
        if i < 3:
            ratio = 0.0
        if is_global:
            # lighting/exposure jump: rebuild the background from this frame
            if not res.lighting_changes or t - res.lighting_changes[-1] > 1.5:
                res.lighting_changes.append(float(t))
            mog.apply(gb, learningRate=1.0)
            ratio = 0.0
        else:
            heat += (fg > 0)
        times.append(t)
        ratios.append(ratio)
        glob.append(is_global)
        if cfg.detect_tamper and i % max(1, int(fps)) == 0:
            bright.append((t, float(g.mean()), float(cv2.Laplacian(g, cv2.CV_32F).var()), float(g.std())))
        if res.preview is None and i == int(fps * 2):
            res.preview = g.copy()
    res.frames = len(times)
    res.times = np.asarray(times, np.float32)
    res.ratios = np.asarray(ratios, np.float32)
    if heat is not None and heat.max() > 0:
        res.heatmap = heat / heat.max()
    if not times:
        return res

    # ---------------- events
    active = res.ratios >= min_area
    # require 2 active samples within a short window to suppress single-frame noise
    sm = np.convolve(active.astype(np.float32), np.ones(3), mode="same")
    active = active & (sm >= 2)
    ivs = _intervals(res.times, active, cfg.event_merge_gap)
    dur = float(meta.get("duration") or res.times[-1])
    for s, e in ivs:
        if e - s + 1.0 / fps < cfg.event_min_duration:
            continue
        s2, e2 = max(0.0, s - cfg.event_pre_roll), min(dur, e + cfg.event_post_roll)
        # split very long events
        cur = s2
        while cur < e2:
            nxt = min(e2, cur + cfg.event_max_duration)
            sel = (res.times >= cur) & (res.times <= nxt)
            r = res.ratios[sel]
            res.events.append({"start": float(cur), "end": float(nxt),
                               "motion_peak": float(r.max()) if r.size else 0.0,
                               "motion_mean": float(r.mean()) if r.size else 0.0,
                               "motion_active_s": float(np.count_nonzero(active[sel]) / fps)})
            cur = nxt
    # merge padded overlaps
    merged = []
    for ev in res.events:
        if merged and ev["start"] <= merged[-1]["end"] and \
                ev["end"] - merged[-1]["start"] <= cfg.event_max_duration:
            m = merged[-1]
            m["end"] = max(m["end"], ev["end"])
            m["motion_peak"] = max(m["motion_peak"], ev["motion_peak"])
            m["motion_mean"] = (m["motion_mean"] + ev["motion_mean"]) / 2
            m["motion_active_s"] += ev["motion_active_s"]
        else:
            merged.append(ev)
    res.events = merged

    # ---------------- tamper detection
    if cfg.detect_tamper and len(bright) >= 10:
        arr = np.array(bright)
        base_b = np.median(arr[:, 1])
        base_s = np.median(arr[:, 2])
        base_d = np.median(arr[:, 3])
        flags = []
        for t, b, s, d in arr:
            kind = None
            if b < 12 and base_b > 35:
                kind = "blackout/covered"
            elif d < 0.25 * base_d and base_d > 15:
                kind = "lens covered (uniform image)"
            elif s < 0.12 * base_s and base_s > 30:
                kind = "defocus/blur"
            flags.append((t, kind))
        cur = None
        for t, kind in flags:
            if kind and cur and cur["kind"] == kind and t - cur["end"] <= 3:
                cur["end"] = float(t)
            elif kind:
                if cur:
                    res.tamper.append(cur)
                cur = {"start": float(t), "end": float(t), "kind": kind}
        if cur:
            res.tamper.append(cur)
        res.tamper = [x for x in res.tamper if x["end"] - x["start"] >= 3]
        # sustained global change = camera moved / scene changed
        g = np.array(glob)
        run = 0
        for t, gg in zip(res.times, g):
            run = run + 1 if gg else 0
            if run == int(fps * 4):
                res.tamper.append({"start": float(t - 4), "end": float(t), "kind": "scene change / camera moved"})
    return res

"""Forensic image enhancement (the "Forensic Studio") with a reproducible audit log.

Every filter is deterministic NumPy/OpenCV code with validated parameters. A **chain** is a list
of ``{"op": name, **params}``; ``apply_chain`` runs it and returns the result together with a
text log in the form courts expect from enhancement software, for example::

    CLAHE(clip_limit=3.0, tile_grid=(8,8)) → UnsharpMask(sigma=1.5, amount=1.0, threshold=0)

Applying the same chain to the same input reproduces the output bit for bit on the same build;
``save_enhancement`` stores input and output hashes, the chain and the log next to the result.

Filters
-------
CLAHE            contrast-limited adaptive histogram equalisation on the lightness channel (night
                 footage, backlit faces) – clip_limit, tile_grid
Levels           black/white point and gamma – black, white, gamma
Denoise          non-local means – strength
UnsharpMask      edge enhancement – sigma, amount, threshold
HighPass         high-pass sharpening (image + amount × Laplacian detail) – radius, amount
WienerDeblur     Wiener deconvolution of a linear motion blur (length px, angle °) or a defocus
                 disc (radius px) – psf, length, angle, radius, nsr (noise-to-signal ratio)
RichardsonLucy   iterative Richardson–Lucy deconvolution, same PSFs – iterations
Perspective      4-point homography to a rectangle (number plates, signboards seen at an angle) –
                 points [[x,y]×4 clockwise from top-left, pixels], width, height
Crop             x, y, w, h (pixels)
Upscale          factor (Lanczos interpolation; adds no detail, only size)

``stack_frames`` (multi-frame) aligns 3–9 consecutive frames to the middle one with OpenCV ECC
(``findTransformECC``; translation, euclidean, affine or homography) and takes the per-pixel
median or mean: uncorrelated sensor noise falls by about √N, detail present in every frame stays.

Limits: deconvolution needs the blur to be roughly uniform and linear; it amplifies noise and
compression artefacts (ringing), so ``nsr`` must grow with noise. Results are an aid for viewing
detail that is in the recording; they do not create information that is not there.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

# name -> {param: (type, min, max, default)}
OPS: Dict[str, Dict[str, tuple]] = {
    "CLAHE": {"clip_limit": (float, 0.5, 40.0, 2.0), "tile_grid": (int, 1, 32, 8)},
    "Levels": {"black": (int, 0, 254, 0), "white": (int, 1, 255, 255), "gamma": (float, 0.1, 5.0, 1.0)},
    "Denoise": {"strength": (float, 1.0, 30.0, 7.0)},
    "UnsharpMask": {"sigma": (float, 0.3, 10.0, 1.5), "amount": (float, 0.0, 5.0, 1.0), "threshold": (int, 0, 64, 0)},
    "HighPass": {"radius": (int, 1, 15, 3), "amount": (float, 0.0, 3.0, 0.6)},
    "WienerDeblur": {"psf": (str, None, None, "motion"), "length": (float, 1.0, 101.0, 9.0),
                     "angle": (float, -180.0, 180.0, 0.0), "radius": (float, 0.5, 30.0, 3.0),
                     "nsr": (float, 1e-4, 1.0, 0.01)},
    "RichardsonLucy": {"psf": (str, None, None, "motion"), "length": (float, 1.0, 101.0, 9.0),
                       "angle": (float, -180.0, 180.0, 0.0), "radius": (float, 0.5, 30.0, 3.0),
                       "iterations": (int, 1, 200, 25)},
    "Perspective": {"points": (list, None, None, None), "width": (int, 0, 4000, 0), "height": (int, 0, 4000, 0)},
    "Crop": {"x": (int, 0, 20000, 0), "y": (int, 0, 20000, 0), "w": (int, 0, 20000, 0), "h": (int, 0, 20000, 0)},
    "Upscale": {"factor": (float, 1.0, 8.0, 2.0)},
}
PSF_KINDS = ("motion", "defocus")
MAX_PIXELS = 40_000_000


def validate(step: dict) -> dict:
    """The step with every parameter present, typed and in range (ValueError otherwise)."""
    if not isinstance(step, dict) or step.get("op") not in OPS:
        raise ValueError(f"unknown filter {step.get('op') if isinstance(step, dict) else step!r}; "
                         f"available: {', '.join(OPS)}")
    spec = OPS[step["op"]]
    extra = set(step) - set(spec) - {"op"}
    if extra:
        raise ValueError(f"{step['op']}: unknown parameter(s) {sorted(extra)}")
    out = {"op": step["op"]}
    for k, (typ, lo, hi, default) in spec.items():
        v = step.get(k, default)
        if typ is list:
            if step["op"] == "Perspective":
                if not isinstance(v, list) or len(v) != 4 or not all(
                        isinstance(p, (list, tuple)) and len(p) == 2 and all(isinstance(c, (int, float)) for c in p)
                        for p in v):
                    raise ValueError("Perspective: points must be four [x, y] pairs (top-left, top-right, "
                                     "bottom-right, bottom-left)")
                v = [[round(float(p[0]), 2), round(float(p[1]), 2)] for p in v]
            out[k] = v
            continue
        if typ is str:
            if v not in PSF_KINDS:
                raise ValueError(f"{step['op']}: psf must be one of {list(PSF_KINDS)}")
            out[k] = v
            continue
        try:
            v = typ(v)
        except (TypeError, ValueError):
            raise ValueError(f"{step['op']}: {k} must be a number")
        if isinstance(v, float) and not np.isfinite(v):
            raise ValueError(f"{step['op']}: {k} must be finite")
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            raise ValueError(f"{step['op']}: {k}={v} is outside {lo}..{hi}")
        out[k] = round(v, 4) if isinstance(v, float) else v
    if out["op"] == "Levels" and out["white"] <= out["black"]:
        raise ValueError("Levels: white must be above black")
    return out


def describe_step(s: dict) -> str:
    def fmt(k, v):
        if k == "tile_grid":
            return f"tile_grid=({v},{v})"
        if isinstance(v, float):
            return f"{k}={v:g}" if abs(v) >= 1e-3 else f"{k}={v:.0e}"
        if isinstance(v, list):
            return f"{k}=" + ",".join(f"({p[0]:g},{p[1]:g})" for p in v)
        return f"{k}={v}"
    params = {k: v for k, v in s.items() if k != "op"}
    if s["op"] in ("WienerDeblur", "RichardsonLucy"):
        params = {k: v for k, v in params.items() if not (s["psf"] == "motion" and k == "radius")
                  and not (s["psf"] == "defocus" and k in ("length", "angle"))}
    return f"{s['op']}(" + ", ".join(fmt(k, v) for k, v in params.items()) + ")"


def describe(chain: Sequence[dict]) -> str:
    return " → ".join(describe_step(validate(s)) for s in chain) or "(no filter)"


# ------------------------------------------------------------------ helpers
def _as_bgr(img: np.ndarray) -> np.ndarray:
    if img is None or not isinstance(img, np.ndarray) or img.size == 0:
        raise ValueError("empty image")
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.ndim == 3 and img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    elif img.ndim != 3 or img.shape[2] != 3:
        raise ValueError(f"unsupported image shape {img.shape}")
    if img.dtype != np.uint8:
        f = np.nan_to_num(img.astype(np.float64))
        img = (f * 255 if f.max() <= 1.0 else f).clip(0, 255).astype(np.uint8)
    if img.shape[0] * img.shape[1] > MAX_PIXELS:
        raise ValueError("image too large (over 40 megapixels)")
    return img


def psf(kind: str, length: float = 9.0, angle: float = 0.0, radius: float = 3.0) -> np.ndarray:
    """Normalised point-spread function: a line (motion) or a disc (defocus)."""
    if kind == "motion":
        n = int(np.ceil(length)) | 1
        k = np.zeros((n, n), np.float32)
        c = n // 2
        a = np.deg2rad(angle)
        dx, dy = np.cos(a), -np.sin(a)
        for t in np.linspace(-(length - 1) / 2, (length - 1) / 2, max(2, int(length * 4))):
            x, y = c + t * dx, c + t * dy
            x0, y0 = int(np.floor(x)), int(np.floor(y))
            fx, fy = x - x0, y - y0
            for xx, yy, w in ((x0, y0, (1 - fx) * (1 - fy)), (x0 + 1, y0, fx * (1 - fy)),
                              (x0, y0 + 1, (1 - fx) * fy), (x0 + 1, y0 + 1, fx * fy)):
                if 0 <= xx < n and 0 <= yy < n:
                    k[yy, xx] += w
    else:
        n = int(np.ceil(radius)) * 2 + 1
        yy, xx = np.mgrid[:n, :n] - n // 2
        k = (xx ** 2 + yy ** 2 <= radius ** 2).astype(np.float32)
    s = k.sum()
    return k / s if s > 0 else np.ones((1, 1), np.float32)


def _taper(img: np.ndarray, pad: int) -> np.ndarray:
    """Reflect-pad so the FFT's periodic boundary does not ring at the image edges."""
    return cv2.copyMakeBorder(img, pad, pad, pad, pad, cv2.BORDER_REFLECT)


def _wiener(img: np.ndarray, k: np.ndarray, nsr: float) -> np.ndarray:
    pad = max(k.shape) * 2
    out = np.empty_like(img)
    for c in range(img.shape[2]):
        ch = _taper(img[:, :, c].astype(np.float64) / 255.0, pad)
        H_ = np.fft.fft2(k, s=ch.shape)
        G = np.fft.fft2(ch)
        F = G * np.conj(H_) / (np.abs(H_) ** 2 + nsr)
        f = np.real(np.fft.ifft2(F))
        # the PSF's origin is its top-left corner in fft2(k, s=...): undo the shift of its centre
        f = np.roll(f, (k.shape[0] // 2, k.shape[1] // 2), axis=(0, 1))
        out[:, :, c] = (f[pad:-pad, pad:-pad] * 255).clip(0, 255).astype(np.uint8)
    return out


def _richardson_lucy(img: np.ndarray, k: np.ndarray, iterations: int) -> np.ndarray:
    pad = max(k.shape) * 2
    kf = k[::-1, ::-1].copy()
    out = np.empty_like(img)
    for c in range(img.shape[2]):
        obs = _taper(img[:, :, c].astype(np.float32) / 255.0 + 1e-4, pad)
        est = np.full_like(obs, 0.5)
        for _ in range(iterations):
            conv = cv2.filter2D(est, -1, k, borderType=cv2.BORDER_REFLECT)
            est *= cv2.filter2D(obs / np.maximum(conv, 1e-6), -1, kf, borderType=cv2.BORDER_REFLECT)
            np.clip(est, 0, 2, out=est)
        out[:, :, c] = (est[pad:-pad, pad:-pad] * 255).clip(0, 255).astype(np.uint8)
    return out


def order_points(pts: Sequence[Sequence[float]]) -> np.ndarray:
    """Four points in any order -> top-left, top-right, bottom-right, bottom-left."""
    p = np.array(pts, np.float32)
    s, d = p.sum(1), np.diff(p, axis=1).ravel()
    return np.array([p[np.argmin(s)], p[np.argmin(d)], p[np.argmax(s)], p[np.argmax(d)]], np.float32)


# ------------------------------------------------------------------ filters
def apply_step(img: np.ndarray, s: dict) -> np.ndarray:
    s = validate(s)
    img = _as_bgr(img)
    op = s["op"]
    if op == "CLAHE":
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        cl = cv2.createCLAHE(clipLimit=s["clip_limit"], tileGridSize=(s["tile_grid"], s["tile_grid"]))
        lab[:, :, 0] = cl.apply(lab[:, :, 0])
        return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    if op == "Levels":
        x = (np.arange(256, dtype=np.float64) - s["black"]) / (s["white"] - s["black"])
        lut = (np.power(x.clip(0, 1), 1.0 / s["gamma"]) * 255).round().astype(np.uint8)
        return cv2.LUT(img, lut)
    if op == "Denoise":
        h = float(s["strength"])
        return cv2.fastNlMeansDenoisingColored(img, None, h, h, 7, 21)
    if op == "UnsharpMask":
        blur = cv2.GaussianBlur(img, (0, 0), s["sigma"])
        f, b = img.astype(np.float32), blur.astype(np.float32)
        detail = f - b
        if s["threshold"]:
            detail[np.abs(detail) < s["threshold"]] = 0
        return (f + s["amount"] * detail).clip(0, 255).astype(np.uint8)
    if op == "HighPass":
        r = s["radius"]
        blur = cv2.GaussianBlur(img, (2 * r + 1, 2 * r + 1), 0).astype(np.float32)
        return (img.astype(np.float32) + s["amount"] * (img.astype(np.float32) - blur)).clip(0, 255).astype(np.uint8)
    if op in ("WienerDeblur", "RichardsonLucy"):
        k = psf(s["psf"], s["length"], s["angle"], s["radius"])
        if k.shape[0] >= min(img.shape[:2]):
            raise ValueError(f"{op}: blur size {k.shape[0]} px is not smaller than the image")
        return _wiener(img, k, s["nsr"]) if op == "WienerDeblur" else _richardson_lucy(img, k, s["iterations"])
    if op == "Perspective":
        src = order_points(s["points"])
        h_img, w_img = img.shape[:2]
        if not ((src >= -1).all() and (src[:, 0] <= w_img + 1).all() and (src[:, 1] <= h_img + 1).all()):
            raise ValueError("Perspective: a point is outside the image")
        if cv2.contourArea(src) < 16:
            raise ValueError("Perspective: the four points do not enclose an area")
        tw = s["width"] or int(round(max(np.linalg.norm(src[1] - src[0]), np.linalg.norm(src[2] - src[3]))))
        th = s["height"] or int(round(max(np.linalg.norm(src[3] - src[0]), np.linalg.norm(src[2] - src[1]))))
        tw, th = max(4, tw), max(4, th)
        dst = np.array([[0, 0], [tw - 1, 0], [tw - 1, th - 1], [0, th - 1]], np.float32)
        M = cv2.getPerspectiveTransform(src, dst)
        return cv2.warpPerspective(img, M, (tw, th), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    if op == "Crop":
        h_img, w_img = img.shape[:2]
        x, y = min(s["x"], w_img - 1), min(s["y"], h_img - 1)
        w = min(s["w"] or w_img, w_img - x)
        h = min(s["h"] or h_img, h_img - y)
        return img[y:y + h, x:x + w].copy()
    if op == "Upscale":
        h_img, w_img = img.shape[:2]
        f = s["factor"]
        if h_img * w_img * f * f > MAX_PIXELS:
            raise ValueError("Upscale: result would be larger than 40 megapixels")
        return cv2.resize(img, (int(round(w_img * f)), int(round(h_img * f))), interpolation=cv2.INTER_LANCZOS4)
    raise ValueError(op)                     # pragma: no cover - validate() rejects unknown ops


def apply_chain(img: np.ndarray, chain: Sequence[dict]) -> Tuple[np.ndarray, dict]:
    if not isinstance(chain, (list, tuple)) or len(chain) > 20:
        raise ValueError("chain: a list of at most 20 filters")
    steps = [validate(s) for s in chain]
    out = _as_bgr(img)
    timings = []
    for s in steps:
        t0 = time.perf_counter()
        out = apply_step(out, s)
        timings.append(round((time.perf_counter() - t0) * 1000, 1))
    return out, {"chain": steps, "log": " → ".join(describe_step(s) for s in steps) or "(no filter)",
                 "ms": timings}


# ------------------------------------------------------------------ multi-frame
MOTIONS = {"translation": cv2.MOTION_TRANSLATION, "euclidean": cv2.MOTION_EUCLIDEAN,
           "affine": cv2.MOTION_AFFINE, "homography": cv2.MOTION_HOMOGRAPHY}


def stack_frames(frames: Sequence[np.ndarray], method: str = "median", motion: str = "euclidean",
                 iterations: int = 100, eps: float = 1e-5) -> Tuple[np.ndarray, dict]:
    """Align frames to the middle one (ECC) and stack them. Returns (image, record)."""
    if method not in ("median", "mean"):
        raise ValueError("method must be median or mean")
    if motion not in MOTIONS:
        raise ValueError(f"motion must be one of {list(MOTIONS)}")
    frames = [_as_bgr(f) for f in frames]
    if not 2 <= len(frames) <= 15:
        raise ValueError("stack 2 to 15 frames")
    ref_i = len(frames) // 2
    ref = frames[ref_i]
    h, w = ref.shape[:2]
    if any(f.shape != ref.shape for f in frames):
        raise ValueError("all frames must have the same size")
    ref_g = _smooth_gray(ref)
    aligned, scores = [], []
    mode = MOTIONS[motion]
    for i, f in enumerate(frames):
        if i == ref_i:
            aligned.append(f.astype(np.float32))
            scores.append(1.0)
            continue
        g = _smooth_gray(f)
        warp = np.eye(3, 3, dtype=np.float32) if mode == cv2.MOTION_HOMOGRAPHY else np.eye(2, 3, dtype=np.float32)
        try:
            cc, warp = cv2.findTransformECC(ref_g, g, warp, mode,
                                            (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, iterations, eps), None, 5)
        except cv2.error:
            scores.append(0.0)                   # could not align: leave this frame out
            continue
        flags = cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP
        warped = cv2.warpPerspective(f, warp, (w, h), flags=flags, borderMode=cv2.BORDER_REPLICATE) \
            if mode == cv2.MOTION_HOMOGRAPHY else cv2.warpAffine(f, warp, (w, h), flags=flags,
                                                                  borderMode=cv2.BORDER_REPLICATE)
        aligned.append(warped.astype(np.float32))
        scores.append(round(float(cc), 4))
    stack = np.stack(aligned)
    out = (np.median(stack, axis=0) if method == "median" else stack.mean(axis=0)).round().clip(0, 255).astype(np.uint8)
    used = sum(1 for s in scores if s > 0)
    return out, {"op": "MultiFrameStack", "method": method, "motion": motion, "frames": len(frames),
                 "aligned": used, "ecc": scores,
                 "log": f"MultiFrameStack(frames={len(frames)}, aligned={used}, motion={motion}, method={method})"}


# ------------------------------------------------------------------ multi-frame super-resolution
def _grad(img: np.ndarray) -> np.ndarray:
    """Blurred gradient magnitude for the alignment, computed bit-exactly: OpenCV's float32 filters
    can differ in the last bit with the memory alignment of the buffer (SIMD paths), which made the
    same frames give a slightly different picture – a saved still must reproduce bit for bit. So:
    integer Sobel, element-wise NumPy magnitude, the bit-exact 8-bit Gaussian blur."""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gx = cv2.Sobel(g, cv2.CV_16S, 1, 0, ksize=3).astype(np.float32)
    gy = cv2.Sobel(g, cv2.CV_16S, 0, 1, ksize=3).astype(np.float32)
    m = np.minimum(np.sqrt(gx * gx + gy * gy) / 4.0, 255.0).round().astype(np.uint8)
    return cv2.GaussianBlur(m, (0, 0), 1.0).astype(np.float32) / 255.0


def _smooth_gray(img: np.ndarray, k: int = 5) -> np.ndarray:
    """Grey, blurred, float32 in 0..1 – bit-exact (8-bit blur first, see _grad)."""
    return cv2.GaussianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (k, k), 0).astype(np.float32) / 255.0


def super_resolve(frames: Sequence[np.ndarray], roi: Optional[Sequence[int]] = None, factor: int = 4,
                  motion: str = "affine", sharpen: bool = True) -> Tuple[np.ndarray, dict]:
    """Multi-frame super-resolution of a region (shift-and-add).

    A plate or a face in CCTV footage covers few pixels, but it is seen in many consecutive
    frames, each sampled at a slightly different sub-pixel position (the vehicle moves, the
    sensor noise differs). The region of the middle frame is found in every other frame by
    normalised cross-correlation (the object moves several pixels per frame; frames where it is
    not found with correlation ≥ 0.5 or not fully in view are left out); every frame's region
    (with a common margin for the alignment) is enlarged `factor` times, aligned to the middle
    frame with ECC (``findTransformECC`` on gradient images, so lighting changes do not disturb
    it) at the enlarged resolution – sub-pixel registration – and the aligned stack is fused with
    the per-pixel median: detail that is in every frame adds up, noise and compression blocks of
    single frames do not. A mild unsharp mask (``sharpen``) undoes the interpolation blur.
    ``factor=1`` is a stack of the followed region without enlarging (``MultiFrameRegionStack``).
    It shows what the frames contain; it invents nothing (no learned model). Returns (image, record).
    Measured in ``tests/test_forensic_enhancement.py`` (44 × 11 px plate, 15 frames, noise and
    JPEG): PSNR against the true picture 18.0 dB vs 16.6 dB for the best single frame enlarged,
    13.4 dB for a median without alignment; the text reader reads the plate only after it."""
    if motion not in MOTIONS:
        raise ValueError(f"motion must be one of {list(MOTIONS)}")
    if not 1 <= int(factor) <= 8:
        raise ValueError("factor must be 1 to 8")
    frames = [_as_bgr(f) for f in frames]
    if not 2 <= len(frames) <= 31:
        raise ValueError("super-resolution needs 2 to 31 frames")
    h, w = frames[0].shape[:2]
    if any(f.shape != frames[0].shape for f in frames):
        raise ValueError("all frames must have the same size")
    x, y, rw, rh = (0, 0, w, h) if roi is None else [int(v) for v in roi]
    x, y = max(0, min(x, w - 2)), max(0, min(y, h - 2))
    rw, rh = max(2, min(rw, w - x)), max(2, min(rh, h - y))
    m = max(4, int(0.25 * max(rw, rh)))                        # context for the alignment
    f = int(factor)
    if (rw + 2 * m) * (rh + 2 * m) * f * f > MAX_PIXELS / 4:
        raise ValueError("region too large for this factor – select a smaller region")
    ref_i = len(frames) // 2
    # 1. follow the region through the frames (a vehicle moves several pixels per frame): the
    #    reference region is found in every frame by normalised cross-correlation near its position
    gray = [cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY) for fr in frames]
    tpl = gray[ref_i][y:y + rh, x:x + rw]
    reach = int(1.5 * max(rw, rh)) + 40
    offsets, found = [], []
    for i, g in enumerate(gray):
        if i == ref_i:
            offsets.append((0, 0))
            found.append(1.0)
            continue
        sx0, sy0 = max(0, x - reach), max(0, y - reach)
        sx1, sy1 = min(w, x + rw + reach), min(h, y + rh + reach)
        res = cv2.matchTemplate(g[sy0:sy1, sx0:sx1], tpl, cv2.TM_CCOEFF_NORMED)
        _, best, _, loc = cv2.minMaxLoc(res)
        offsets.append((sx0 + loc[0] - x, sy0 + loc[1] - y))
        found.append(float(best))
    def room(i):                                                 # free pixels around the region in frame i
        dx, dy = offsets[i]
        return min(x + dx, y + dy, w - (x + dx + rw), h - (y + dy + rh))
    keep = [i for i in range(len(frames)) if found[i] >= 0.5 and room(i) >= 0]   # same object, fully in view
    if ref_i not in keep:
        keep = [ref_i] + keep
    m = max(0, min([m] + [room(i) for i in keep]))               # one margin that every kept frame has
    crops = [frames[i][y + offsets[i][1] - m:y + offsets[i][1] + rh + m, x + offsets[i][0] - m:x + offsets[i][0] + rw + m]
             for i in keep]
    # 2. enlarge and register every crop to the reference with sub-pixel ECC at the enlarged size
    up = [cv2.resize(c, (c.shape[1] * f, c.shape[0] * f), interpolation=cv2.INTER_CUBIC) for c in crops]
    ref_i = keep.index(ref_i)
    ref_g = _grad(up[ref_i])
    mode = MOTIONS[motion]
    aligned, scores = [], []
    H, W = up[ref_i].shape[:2]
    for i, u in enumerate(up):
        if i == ref_i:
            aligned.append(u.astype(np.float32))
            scores.append(1.0)
            continue
        warp = np.eye(3, 3, dtype=np.float32) if mode == cv2.MOTION_HOMOGRAPHY else np.eye(2, 3, dtype=np.float32)
        try:
            cc, warp = cv2.findTransformECC(ref_g, _grad(u), warp, mode,
                                            (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 200, 1e-6), None, 5)
        except cv2.error:
            scores.append(0.0)
            continue
        if cc < 0.3:                                             # a frame that does not match (occlusion, wrong object)
            scores.append(round(float(cc), 4))
            continue
        flags = cv2.INTER_CUBIC + cv2.WARP_INVERSE_MAP
        a = cv2.warpPerspective(u, warp, (W, H), flags=flags, borderMode=cv2.BORDER_REPLICATE) \
            if mode == cv2.MOTION_HOMOGRAPHY else cv2.warpAffine(u, warp, (W, H), flags=flags, borderMode=cv2.BORDER_REPLICATE)
        aligned.append(a.astype(np.float32))
        scores.append(round(float(cc), 4))
    fused = np.median(np.stack(aligned), axis=0).round().clip(0, 255).astype(np.uint8)
    if sharpen:                                                  # unsharp mask, bit-exact (8-bit blur)
        blur = cv2.GaussianBlur(fused, (0, 0), 0.6 * f).astype(np.float32)
        fused = (fused.astype(np.float32) * 1.8 - 0.8 * blur).round().clip(0, 255).astype(np.uint8)
    out = fused
    out = out[m * f:m * f + rh * f, m * f:m * f + rw * f].copy()
    used = len(aligned)
    op = "MultiFrameSuperResolution" if f > 1 else "MultiFrameRegionStack"
    return out, {"op": op, "frames": len(frames), "aligned": used, "factor": f, "motion": motion,
                 "roi": [x, y, rw, rh], "ecc": scores, "sharpen": bool(sharpen),
                 "log": f"{op}(frames={len(frames)}, aligned={used}, factor={f}, motion={motion}, "
                        f"roi=({x},{y},{rw},{rh}){', sharpen' if sharpen else ''})"}


def crop_roi(img: np.ndarray, roi: Optional[Sequence[int]]) -> np.ndarray:
    if roi is None:
        return img
    h, w = img.shape[:2]
    x, y, rw, rh = [int(v) for v in roi]
    x, y = max(0, min(x, w - 1)), max(0, min(y, h - 1))
    return img[y:y + max(1, min(rh, h - y)), x:x + max(1, min(rw, w - x))].copy()


# ------------------------------------------------------------------ records
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def png_bytes(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img, [cv2.IMWRITE_PNG_COMPRESSION, 6])
    if not ok:
        raise ValueError("could not encode the image")
    return buf.tobytes()


def save_enhancement(dest_dir: str, name: str, source: dict, input_img: np.ndarray, output_img: np.ndarray,
                     record: dict, operator: str = "") -> dict:
    """Write output PNG + record JSON; returns the record (with hashes)."""
    from . import __version__
    os.makedirs(dest_dir, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    base = f"{name}_{stamp}"
    inp, outp = png_bytes(_as_bgr(input_img)), png_bytes(output_img)
    with open(os.path.join(dest_dir, base + "_input.png"), "wb") as fh:
        fh.write(inp)
    with open(os.path.join(dest_dir, base + ".png"), "wb") as fh:
        fh.write(outp)
    rec = {"name": base + ".png", "input": base + "_input.png", "source": source,
           "input_sha256": sha256_bytes(inp), "output_sha256": sha256_bytes(outp),
           "log": record.get("log", ""), "chain": record.get("chain", []),
           **({"picture_from": {k: v for k, v in record.items() if k not in ("log", "chain")}} if record.get("op") else {}),
           "tool": f"Drishti-AI (home-camera-analysis {__version__})", "opencv": cv2.__version__,
           "numpy": np.__version__, "operator": operator,
           "created": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    with open(os.path.join(dest_dir, base + ".json"), "w", encoding="utf-8") as fh:
        json.dump(rec, fh, indent=1)
    return rec


def list_enhancements(dest_dir: str) -> List[dict]:
    out: List[dict] = []
    if not os.path.isdir(dest_dir):
        return out
    for f in sorted(os.listdir(dest_dir)):
        if f.endswith(".json"):
            try:
                with open(os.path.join(dest_dir, f), encoding="utf-8") as fh:
                    out.append(json.load(fh))
            except (OSError, ValueError):
                continue
    return out


def reproduce(dest_dir: str, rec: dict) -> bool:
    """Re-run a saved chain on its saved input: True if the output hash is identical."""
    img = cv2.imread(os.path.join(dest_dir, rec["input"]), cv2.IMREAD_COLOR)
    if img is None or not rec.get("chain"):
        return False
    out, _ = apply_chain(img, rec["chain"])
    return sha256_bytes(png_bytes(out)) == rec["output_sha256"]

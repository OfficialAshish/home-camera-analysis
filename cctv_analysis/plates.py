"""Vehicle number-plate reading (ANPR) for recorded footage. Off unless ``Config.use_plates``.

Per vehicle track the pipeline keeps the best few observations (large, sharp, confident);
this module re-reads those frames at full resolution and:

1. localises the plate inside the vehicle box – ``yolo-plate``: open-image-models YOLOv9
   plate detector (ONNX, MIT); ``classic``: OpenCV morphology (no download needed);
2. reads it – ``fast-plate-ocr`` (ONNX, MIT, plate-specialised, per-character confidence) or the
   generic OCR engine (RapidOCR / Tesseract);
3. with ``plate_enhance`` (fast vehicles): motion deblurring – the track gives the direction of
   motion, so the blur is modelled as a linear PSF along it; Wiener deconvolution is run for a
   range of blur lengths and the reconstruction the plate OCR is most confident in (and that
   fits the plate format) is kept. Plus a multi-frame fusion (crops from several frames
   aligned with ECC and median-stacked) that is deblurred the same way;
4. with ``plate_vlm_read``: asks the vision-language model when OCR confidence stays low;
5. corrects letter/digit confusions against the plate format (Indian formats incl. BH series)
   and votes across all reads, character by character.
"""
from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from typing import Callable, List, Optional, Tuple

import cv2
import numpy as np

log = logging.getLogger("cctv")

PLATE_DET_MODEL = "yolo-v9-t-384-license-plate-end2end"
PLATE_OCR_MODEL = "cct-s-v2-global-model"

IN_STATES = {"AN", "AP", "AR", "AS", "BR", "CG", "CH", "DD", "DL", "DN", "GA", "GJ", "HP", "HR", "JH", "JK",
             "KA", "KL", "LA", "LD", "MH", "ML", "MN", "MP", "MZ", "NL", "OD", "OR", "PB", "PY", "RJ", "SK",
             "TN", "TR", "TS", "UK", "UA", "UP", "WB"}
TO_DIGIT = {"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6",
            "A": "4", "T": "7"}
TO_LETTER = {v: k for k, v in [("O", "0"), ("I", "1"), ("Z", "2"), ("S", "5"), ("B", "8"), ("G", "6"),
                               ("A", "4"), ("T", "7")]}


# ---------------------------------------------------------------- formats

def normalize(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def _fit(text: str, classes: str) -> Optional[Tuple[str, int]]:
    """Map text onto a class string ('L' letter / 'D' digit); returns (fixed, substitutions)."""
    if len(text) != len(classes):
        return None
    out, cost = [], 0
    for ch, cl in zip(text, classes):
        if cl == "D":
            if ch.isdigit():
                out.append(ch)
            elif ch in TO_DIGIT:
                out.append(TO_DIGIT[ch])
                cost += 1
            else:
                return None
        else:
            if ch.isalpha():
                out.append(ch)
            elif ch in TO_LETTER:
                out.append(TO_LETTER[ch])
                cost += 1
            else:
                return None
    return "".join(out), cost


_LETTER_ALTS = {"0": "ODQ", "1": "IL", "2": "Z", "4": "A", "5": "S", "6": "G", "7": "T", "8": "B",
                "O": "D", "D": "O", "I": "L", "L": "I", "U": "W", "V": "W", "H": "MN", "M": "HN", "N": "MH",
                "W": "M"}


def _state_fix(two: str) -> str:
    """A valid state code reachable by substituting OCR look-alikes (0L -> DL, NH -> MH is not tried)."""
    opts = [[c] + list(_LETTER_ALTS.get(c, "")) for c in two]
    for a in opts[0]:
        for b in opts[1]:
            if a + b in IN_STATES and a.isalpha() and b.isalpha():
                return a + b
    return ""


def _in_templates(n: int):
    """State (2 letters) + RTO (2 digits, 1 for some older plates) + series (1-3 letters; none
    only on old two-digit-RTO plates) + 4 digits; or Bharat series 22BH1234AB."""
    for d in (2, 1):
        for s in (2, 1, 3, 0):
            if s == 0 and d == 1:
                continue
            if 2 + d + s + 4 == n:
                yield "LL" + "D" * d + "L" * s + "DDDD"
    if n in (9, 10):                 # Bharat series: 22BH1234AB
        yield "DDLLDDDD" + "L" * (n - 8)


def plate_fit(text: str, region: str = "IN") -> Tuple[str, float]:
    """(corrected text, fit 0..1): 1.0 = matches the regional format as read; each letter/digit
    substitution needed costs a quarter; more than one substitution = not a plate (0.3) – a
    truncated read must never be 'corrected' into a plausible but wrong plate."""
    t = normalize(text)
    if not t:
        return "", 0.0
    if region != "IN":
        return t, 1.0
    best = None
    for tpl in _in_templates(len(t)):
        r = _fit(t, tpl)
        if r is None:
            continue
        fixed, cost = r
        bh = tpl.startswith("DDLL")
        if bh and fixed[2:4] != "BH":
            continue
        if not bh and fixed[:2] not in IN_STATES:
            state = _state_fix(t[:2])
            if not state:
                continue
            fixed, cost = state + fixed[2:], cost + 1
        if best is None or cost < best[1]:
            best = (fixed, cost)
    if best is None or best[1] > 1:
        return t, 0.3
    return best[0], 1.0 - 0.25 * best[1]


def format_plate(text: str, region: str = "IN") -> Tuple[str, bool]:
    """(best corrected text, matches the regional format with at most one correction)."""
    t, fit = plate_fit(text, region)
    return t, fit > 0.5


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def is_reportable(text: str, confidence: float, valid_format: bool) -> bool:
    """Shown in event summaries / matched against the watchlist. Partial reads (vehicle cut by
    the frame edge, 'MH12') stay in the plates table as evidence but are not reported."""
    return bool(text) and (valid_format or (len(text) >= 6 and confidence >= 0.7))


def watchlist_match(text: str, watchlist: List[str], max_edits: int = 1) -> str:
    t = normalize(text)
    if not t:
        return ""
    best = ("", max_edits + 1)
    for w in watchlist or []:
        n = normalize(w)
        if not n:
            continue
        d = edit_distance(t, n)
        if d < best[1]:
            best = (n, d)
    return best[0] if best[1] <= max_edits else ""


def vote(reads: List[Tuple[str, float]], region: str = "IN") -> Tuple[str, float, bool]:
    """Fuse several (text, confidence) reads. Same-length candidates vote per character, weighted
    by confidence; the most supported length wins. Returns (text, confidence 0..1, valid format)."""
    fmt = [(plate_fit(t, region), float(c)) for t, c in reads if normalize(t)]
    if not fmt:
        return "", 0.0, False
    # weight = confidence x format fit: garbage from blurred frames and corrected reads count less
    reads = [(f, c * fit) for (f, fit), c in fmt]
    by_len = defaultdict(list)
    for t, c in reads:
        by_len[len(t)].append((t, c))
    L, group = max(by_len.items(), key=lambda kv: (sum(c for _, c in kv[1]), len(kv[1])))
    chars, agree = [], []
    for i in range(L):
        w = defaultdict(float)
        for t, c in group:
            w[t[i]] += max(c, 0.05)
        ch, s = max(w.items(), key=lambda kv: kv[1])
        chars.append(ch)
        agree.append(s / sum(w.values()))
    fused, valid = format_plate("".join(chars), region)
    mean_conf = min(1.0, sum(c for _, c in group) / len(group))
    support = len(group) / len(reads)
    conf = float(min(agree) * (0.5 + 0.5 * mean_conf) * (0.6 + 0.4 * support))
    return fused, round(conf, 3), valid


# ---------------------------------------------------------- enhancement

def sharpness(img: np.ndarray) -> float:
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    return float(cv2.Laplacian(g, cv2.CV_32F).var())


def motion_psf(length: int, angle: float) -> np.ndarray:
    """Normalised linear motion-blur kernel of `length` px along `angle` (radians, image coords)."""
    size = length + 2
    k = np.zeros((size, size), np.float32)
    c = (size - 1) / 2
    for i in range(length):
        t = i - (length - 1) / 2
        k[int(round(c + t * np.sin(angle))), int(round(c + t * np.cos(angle)))] = 1
    return k / k.sum()


def wiener_deconvolve(img: np.ndarray, psf: np.ndarray, k: float) -> np.ndarray:
    pad = psf.shape[0] * 2
    out = np.empty(img.shape, np.float32)
    chans = img.shape[2] if img.ndim == 3 else 1
    for ch in range(chans):
        x = img[:, :, ch] if img.ndim == 3 else img
        f = cv2.copyMakeBorder(x, pad, pad, pad, pad, cv2.BORDER_REFLECT).astype(np.float32) / 255
        h = np.zeros_like(f)
        h[:psf.shape[0], :psf.shape[1]] = psf
        h = np.roll(h, (-(psf.shape[0] // 2), -(psf.shape[1] // 2)), (0, 1))
        H = np.fft.fft2(h)
        r = np.real(np.fft.ifft2(np.conj(H) / (np.abs(H) ** 2 + k) * np.fft.fft2(f)))[pad:-pad, pad:-pad]
        if img.ndim == 3:
            out[:, :, ch] = r
        else:
            out = r
    return np.clip(out * 255, 0, 255).astype(np.uint8)


class Enhancer:
    """Blind motion deblurring scored by the OCR, and multi-frame aligned fusion."""
    method = "deblur"
    LENGTHS = tuple(range(3, 27))         # blur length in px at 2x upscale (every length: the true one must be tried)
    NOISE = (0.002, 0.006, 0.02)

    def __init__(self, region: str = "IN"):
        self.region = region

    def score(self, text: str, conf: float) -> float:
        f, fit = plate_fit(text, self.region)
        return conf * fit * min(1.0, len(f) / 10) if f else 0.0

    def best_read(self, crop: np.ndarray, reader, angle: float = 0.0, inner=None, second=None):
        """(text, conf, image) of the most convincing deblurred reconstruction, or None.
        `crop` should include a margin around the plate; `inner` = plate rect (x1, y1, x2, y2) in it.
        The plate reader scores every blur length; the 3 best reconstructions are also read by
        `second` (general OCR), which is more tolerant of deconvolution ringing (measured: 7/12
        exact reads vs 0/12 with the plate reader alone on H.264-like compressed blurred video)."""
        up = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        cands = []
        for L in self.LENGTHS:
            psf = motion_psf(L, angle)
            for k in self.NOISE:
                img = wiener_deconvolve(up, psf, k)
                if inner is not None:
                    x1, y1, x2, y2 = (2 * v for v in inner)
                    img = img[y1:y2, x1:x2]
                txt, c = reader.read(img)
                if txt:
                    cands.append((self.score(txt, c), txt, c, img))
        if not cands:
            return None
        cands.sort(key=lambda r: -r[0])
        best = cands[0]
        if second is not None:
            for _, _, _, img in cands[:3]:
                txt, c = second.read(img)
                sc = self.score(txt, c)
                if txt and sc > best[0]:
                    best = (sc, txt, c, img)
        return best[1:]

    @staticmethod
    def fuse(crops: List[np.ndarray]) -> Optional[np.ndarray]:
        """Align plate crops from several frames to the sharpest one (ECC, affine) and take the
        per-pixel median: noise and single-frame artefacts average out."""
        crops = [c for c in crops if c is not None and c.size and min(c.shape[:2]) >= 6]
        if len(crops) < 2:
            return None
        crops.sort(key=sharpness, reverse=True)
        h, w = crops[0].shape[:2]
        ref = cv2.cvtColor(crops[0], cv2.COLOR_BGR2GRAY).astype(np.float32)
        stack = [crops[0].astype(np.float32)]
        for c in crops[1:]:
            c = cv2.resize(c, (w, h), interpolation=cv2.INTER_CUBIC)
            g = cv2.cvtColor(c, cv2.COLOR_BGR2GRAY).astype(np.float32)
            warp = np.eye(2, 3, dtype=np.float32)
            try:
                cc, warp = cv2.findTransformECC(ref, g, warp, cv2.MOTION_AFFINE,
                                                (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 60, 1e-4), None, 3)
            except cv2.error:
                continue
            if cc < 0.5:
                continue
            stack.append(cv2.warpAffine(c, warp, (w, h), flags=cv2.INTER_CUBIC + cv2.WARP_INVERSE_MAP,
                                        borderMode=cv2.BORDER_REPLICATE).astype(np.float32))
        if len(stack) < 2:
            return None
        return np.clip(np.median(np.stack(stack), axis=0), 0, 255).astype(np.uint8)


# ------------------------------------------------------------- localisers

class YoloPlateDetector:
    name = "yolo-plate"

    def __init__(self):
        import pathlib
        from open_image_models.detection.core.hub import download_model
        from open_image_models.detection.factory import DETECTION_MODELS, create_detector
        from . import modelcache
        p = download_model(PLATE_DET_MODEL, save_directory=pathlib.Path(modelcache.sub("plates")) / PLATE_DET_MODEL)
        spec = DETECTION_MODELS[PLATE_DET_MODEL]
        self.det = create_detector(str(p), backend=spec.backend, class_labels=spec.class_labels, conf_thresh=0.25)

    def find(self, img: np.ndarray) -> List[Tuple[List[int], float]]:
        out = []
        for r in self.det.predict(img):
            b = r.bounding_box
            out.append(([int(b.x1), int(b.y1), int(b.x2), int(b.y2)], float(r.confidence)))
        return sorted(out, key=lambda x: -x[1])


class ClassicPlateDetector:
    """Bright/dark text-dense rectangles with a plate-like aspect ratio (no model download)."""
    name = "classic"

    def find(self, img: np.ndarray) -> List[Tuple[List[int], float]]:
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        H, W = g.shape
        k = cv2.getStructuringElement(cv2.MORPH_RECT, (max(9, W // 25), max(3, H // 40)))
        grad = cv2.morphologyEx(g, cv2.MORPH_GRADIENT, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
        _, th = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        cl = cv2.morphologyEx(th, cv2.MORPH_CLOSE, k)
        cnts, _ = cv2.findContours(cl, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        out = []
        for c in cnts:
            x, y, w, h = cv2.boundingRect(c)
            ar = w / max(1, h)
            if 2.0 <= ar <= 6.5 and w >= W * 0.08 and h >= 8 and w < W * 0.9:
                fill = cv2.contourArea(c) / float(w * h)
                out.append(([x, y, x + w, y + h], float(min(0.6, fill * 0.6))))
        return sorted(out, key=lambda x: -x[1])[:3]


# ------------------------------------------------------------------ OCR

class FastPlateOCR:
    name = "fast-plate-ocr"

    def __init__(self):
        import pathlib
        from fast_plate_ocr import LicensePlateRecognizer
        from fast_plate_ocr.inference.hub import download_model
        from . import modelcache
        m, c = download_model(PLATE_OCR_MODEL, save_directory=pathlib.Path(modelcache.sub("plates")) / PLATE_OCR_MODEL)
        self.rec = LicensePlateRecognizer(onnx_model_path=str(m), plate_config_path=str(c), device="cpu")

    def read(self, crop: np.ndarray) -> Tuple[str, float]:
        p = self.rec.run(crop, return_confidence=True)
        if not p:
            return "", 0.0
        p = p[0]
        probs = getattr(p, "char_probs", None)
        probs = np.asarray(probs if probs is not None else [], dtype=np.float32)
        return normalize(p.plate), float(probs.mean()) if probs.size else 0.0


class GenericPlateOCR:
    name = "generic"

    def __init__(self, ocr):
        self.ocr = ocr

    def read(self, crop: np.ndarray) -> Tuple[str, float]:
        if not self.ocr:
            return "", 0.0
        big = cv2.resize(crop, None, fx=max(1.0, 64 / max(1, crop.shape[0])), fy=max(1.0, 64 / max(1, crop.shape[0])),
                         interpolation=cv2.INTER_CUBIC)
        lines = self.ocr.read(big)
        if not lines:
            return "", 0.0
        text = "".join(normalize(t) for t, _ in lines)
        return text, float(np.mean([c for _, c in lines]))


# --------------------------------------------------------------- reader

class PlateReader:
    def __init__(self, cfg, ocr_getter: Callable, vlm_getter: Optional[Callable] = None):
        self.cfg = cfg
        self.vlm_getter = vlm_getter
        self.detector = None
        if cfg.plate_detector in ("auto", "yolo-plate"):
            try:
                self.detector = YoloPlateDetector()
            except Exception as e:
                if cfg.plate_detector == "yolo-plate":
                    raise
                log.warning("plate detector model unavailable (%s) – using the OpenCV plate finder", e)
        if self.detector is None:
            self.detector = ClassicPlateDetector()
        self.reader = None
        if cfg.plate_ocr in ("auto", "fast-plate-ocr"):
            try:
                self.reader = FastPlateOCR()
            except Exception as e:
                if cfg.plate_ocr == "fast-plate-ocr":
                    raise
                log.warning("plate OCR model unavailable (%s) – using generic OCR", e)
        generic = GenericPlateOCR(ocr_getter())
        if self.reader is None:
            self.reader = generic
        self.second = generic if (generic.ocr and self.reader is not generic) else None
        self.enhancer = Enhancer(cfg.plate_region) if cfg.plate_enhance else None
        log.info("Plate reading: detector=%s, ocr=%s, enhance=%s, vlm=%s", self.detector.name, self.reader.name,
                 self.enhancer.method if self.enhancer else "off", bool(cfg.plate_vlm_read))

    def locate(self, frame: np.ndarray, vbox):
        """Best plate inside a vehicle box (full-resolution pixel coords):
        (crop, plate box in the frame, detector confidence, (context crop, plate rect inside it))."""
        H, W = frame.shape[:2]
        x1, y1, x2, y2 = [int(v) for v in vbox]
        px, py = int(0.06 * (x2 - x1)), int(0.06 * (y2 - y1))
        x1, y1, x2, y2 = max(0, x1 - px), max(0, y1 - py), min(W, x2 + px), min(H, y2 + py)
        roi = frame[y1:y2, x1:x2]
        if roi.size == 0:
            return None
        found = self.detector.find(roi)
        if not found:
            return None
        (bx1, by1, bx2, by2), conf = found[0]
        bw, bh = bx2 - bx1, by2 - by1
        pad_x, pad_y = int(0.08 * bw), int(0.15 * bh)
        cx1, cy1 = max(0, bx1 - pad_x), max(0, by1 - pad_y)
        cx2, cy2 = min(roi.shape[1], bx2 + pad_x), min(roi.shape[0], by2 + pad_y)
        crop = roi[cy1:cy2, cx1:cx2].copy()
        if crop.size == 0:
            return None
        # wider context for deblurring: motion blur smears characters beyond the plate box
        fx1, fy1, fx2, fy2 = x1 + cx1, y1 + cy1, x1 + cx2, y1 + cy2
        mx, my = max(16, int(0.4 * bw)), max(10, int(0.6 * bh))
        kx1, ky1, kx2, ky2 = max(0, fx1 - mx), max(0, fy1 - my), min(W, fx2 + mx), min(H, fy2 + my)
        context = (frame[ky1:ky2, kx1:kx2].copy(), (fx1 - kx1, fy1 - ky1, fx2 - kx1, fy2 - ky1))
        return crop, [x1 + bx1, y1 + by1, x1 + bx2, y1 + by2], conf, context

    def read_track(self, samples: List[tuple]) -> Optional[dict]:
        """samples: (t_offset, full-res frame, vehicle box in that frame[, motion angle in radians]).
        Returns the fused result (or None when no plate was found in any sample)."""
        cfg = self.cfg
        crops, reads = [], []
        enhanced = None
        for smp in samples:
            t, frame, vbox = smp[:3]
            angle = smp[3] if len(smp) > 3 else 0.0
            loc = self.locate(frame, vbox)
            if loc is None:
                continue
            crop, box, dconf, (context, inner) = loc
            crops.append((t, crop, box, angle))
            txt, c = self.reader.read(crop)
            if txt:
                reads.append((txt, c, "frame", t))
            if self.enhancer is not None and self.enhancer.score(txt, c) < 0.9:
                r = self.enhancer.best_read(context, self.reader, angle, inner, self.second)
                if r:
                    reads.append((r[0], r[1], "deblur", t))
                    if enhanced is None or self.enhancer.score(r[0], r[1]) > enhanced[0]:
                        enhanced = (self.enhancer.score(r[0], r[1]), r[2])
        if not crops:
            return None
        best_crop = max(crops, key=lambda x: sharpness(x[1]) * x[1].shape[1])
        if self.enhancer is not None:
            fused = Enhancer.fuse([c for _, c, _, _ in crops])
            if fused is not None:
                txt, c = self.reader.read(fused)
                if txt:
                    reads.append((txt, c, "multi-frame", best_crop[0]))
                r = self.enhancer.best_read(fused, self.reader, float(np.median([a for *_, a in crops])),
                                            second=self.second)
                if r:
                    reads.append((r[0], r[1], "multi-frame+deblur", best_crop[0]))
                    if enhanced is None or self.enhancer.score(r[0], r[1]) > enhanced[0]:
                        enhanced = (self.enhancer.score(r[0], r[1]), r[2])
        enhanced = enhanced[1] if enhanced else None
        text, conf, valid = vote([(r[0], r[1]) for r in reads], cfg.plate_region)
        method = "ocr"
        if cfg.plate_vlm_read and (not text or conf < 0.6) and self.vlm_getter is not None:
            vlm = self.vlm_getter()
            if vlm is not None:
                try:
                    vt = vlm.read_plate(enhanced if enhanced is not None else best_crop[1])
                    if vt:
                        reads.append((vt, 0.7, "vlm", best_crop[0]))
                        text, conf, valid = vote([(r[0], r[1]) for r in reads], cfg.plate_region)
                        method = "ocr+vlm"
                except Exception as e:
                    log.warning("  VLM plate read failed: %s", e)
        out = {"text": text, "confidence": conf if text else 0.0, "valid_format": valid if text else False,
               "t_off": best_crop[0], "crop": best_crop[1], "enhanced": enhanced, "box": best_crop[2],
               "reads": [{"text": r[0], "conf": round(r[1], 3), "source": r[2], "t_off": round(r[3], 2)} for r in reads],
               "method": (method + ("+deblur" if self.enhancer is not None else "")) if text else "unreadable"}
        return out

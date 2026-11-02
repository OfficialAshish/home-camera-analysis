"""Vehicle number-plate reading (ANPR) for recorded footage. Off unless ``Config.use_plates``.

Per vehicle track the pipeline keeps the best few observations (large, sharp, confident);
this module re-reads those frames at full resolution and:

1. localises the plate inside the vehicle box – ``yolo-plate``: open-image-models YOLOv9
   plate detector (ONNX, MIT); ``classic``: OpenCV morphology (no download needed);
2. reads it – ``fast-plate-ocr`` (ONNX, MIT, plate-specialised, per-character confidence) or the
   generic OCR engine (RapidOCR / Tesseract);
3. with ``plate_enhance``: also reads an enhanced crop (super-resolution ×4, CLAHE, unsharp mask)
   and a multi-frame fusion (crops from several frames aligned with ECC and median-stacked –
   removes noise/blur of a fast vehicle better than any single frame);
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
FSRCNN_URL = "https://github.com/Saafke/FSRCNN_Tensorflow/raw/master/models/FSRCNN_x4.pb"

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
                "O": "D", "D": "O", "I": "L", "L": "I", "U": "W", "V": "W"}


def _state_fix(two: str) -> str:
    """A valid state code reachable by substituting OCR look-alikes (0L -> DL, NH -> MH is not tried)."""
    opts = [[c] + list(_LETTER_ALTS.get(c, "")) for c in two]
    for a in opts[0]:
        for b in opts[1]:
            if a + b in IN_STATES and a.isalpha() and b.isalpha():
                return a + b
    return ""


def _in_templates(n: int):
    for d in (2, 1):                 # district digits
        for s in (2, 1, 3, 0):       # series letters
            if 2 + d + s + 4 == n:
                yield "LL" + "D" * d + "L" * s + "DDDD"
    if n in (9, 10):                 # Bharat series: 22BH1234AB
        yield "DDLLDDDD" + "L" * (n - 8)


def format_plate(text: str, region: str = "IN") -> Tuple[str, bool]:
    """(best corrected text, matches the regional format)."""
    t = normalize(text)
    if region != "IN" or not t:
        return t, bool(t)
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
            if state:
                fixed, cost = state + fixed[2:], cost + 1
            else:
                cost += 3
        if best is None or cost < best[1]:
            best = (fixed, cost)
    if best is None or best[1] >= 3:
        return t, False
    return best[0], True


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


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
    reads = [(format_plate(t, region)[0], float(c)) for t, c in reads if normalize(t)]
    if not reads:
        return "", 0.0, False
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
    mean_conf = sum(c for _, c in group) / len(group)
    support = len(group) / len(reads)
    conf = float(min(agree) * (0.5 + 0.5 * mean_conf) * (0.6 + 0.4 * support))
    return fused, round(conf, 3), valid


# ---------------------------------------------------------- enhancement

def sharpness(img: np.ndarray) -> float:
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    return float(cv2.Laplacian(g, cv2.CV_32F).var())


class Enhancer:
    """Super-resolution (OpenCV dnn_superres FSRCNN ×4 when available, else Lanczos), local
    contrast (CLAHE) and unsharp masking; plus multi-frame aligned fusion."""

    def __init__(self):
        self.sr = None
        try:
            if hasattr(cv2, "dnn_superres"):
                from . import modelcache
                p = os.path.join(modelcache.sub("plates"), "FSRCNN_x4.pb")
                if not os.path.exists(p):
                    import urllib.request
                    urllib.request.urlretrieve(FSRCNN_URL, p + ".part")
                    os.replace(p + ".part", p)
                sr = cv2.dnn_superres.DnnSuperResImpl_create()
                sr.readModel(p)
                sr.setModel("fsrcnn", 4)
                self.sr = sr
        except Exception as e:
            log.info("plate super-resolution model unavailable (%s) – using Lanczos upscaling", e)
        self.method = "fsrcnn-x4" if self.sr is not None else "lanczos-x4"

    def upscale(self, img: np.ndarray) -> np.ndarray:
        if self.sr is not None and img.shape[0] * img.shape[1] <= 160 * 480:
            try:
                return self.sr.upsample(img)
            except Exception:
                pass
        return cv2.resize(img, None, fx=4, fy=4, interpolation=cv2.INTER_LANCZOS4)

    def enhance(self, img: np.ndarray) -> np.ndarray:
        up = self.upscale(img)
        lab = cv2.cvtColor(up, cv2.COLOR_BGR2LAB)
        lab[:, :, 0] = cv2.createCLAHE(2.0, (8, 8)).apply(lab[:, :, 0])
        up = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
        blur = cv2.GaussianBlur(up, (0, 0), 2.0)
        return cv2.addWeighted(up, 1.7, blur, -0.7, 0)

    @staticmethod
    def fuse(crops: List[np.ndarray]) -> Optional[np.ndarray]:
        """Align plate crops from several frames to the sharpest one (ECC, affine) and take the
        per-pixel median: noise and single-frame motion blur average out."""
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
        probs = np.asarray(getattr(p, "char_probs", []) or [0.0], dtype=np.float32)
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
        if self.reader is None:
            self.reader = GenericPlateOCR(ocr_getter())
        self.enhancer = Enhancer() if cfg.plate_enhance else None
        log.info("Plate reading: detector=%s, ocr=%s, enhance=%s, vlm=%s", self.detector.name, self.reader.name,
                 self.enhancer.method if self.enhancer else "off", bool(cfg.plate_vlm_read))

    def locate(self, frame: np.ndarray, vbox) -> Optional[Tuple[np.ndarray, List[int], float]]:
        """Best plate crop inside a vehicle box (full-resolution pixel coords)."""
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
        return crop, [x1 + bx1, y1 + by1, x1 + bx2, y1 + by2], conf

    def read_track(self, samples: List[Tuple[float, np.ndarray, list]]) -> Optional[dict]:
        """samples: (t_offset, full-res frame, vehicle box in that frame). Returns the fused result."""
        cfg = self.cfg
        crops, reads = [], []
        for t, frame, vbox in samples:
            loc = self.locate(frame, vbox)
            if loc is None:
                continue
            crop, box, dconf = loc
            crops.append((t, crop, box, dconf))
            txt, c = self.reader.read(crop)
            if txt:
                reads.append((txt, c, "frame", t))
            if self.enhancer is not None:
                txt, c = self.reader.read(self.enhancer.enhance(crop))
                if txt:
                    reads.append((txt, c, "enhanced", t))
        if not crops:
            return None
        best_crop = max(crops, key=lambda x: sharpness(x[1]) * x[1].shape[1])
        enhanced = None
        if self.enhancer is not None:
            fused = Enhancer.fuse([c for _, c, _, _ in crops])
            if fused is not None:
                enhanced = self.enhancer.enhance(fused)
                txt, c = self.reader.read(enhanced)
                if txt:
                    reads.append((txt, c, "multi-frame", best_crop[0]))
            if enhanced is None:
                enhanced = self.enhancer.enhance(best_crop[1])
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
        if not text:
            return {"text": "", "confidence": 0.0, "valid_format": False, "reads": [], "t_off": best_crop[0],
                    "crop": best_crop[1], "enhanced": enhanced, "box": best_crop[2], "method": "unreadable"}
        return {"text": text, "confidence": conf, "valid_format": valid, "t_off": best_crop[0],
                "reads": [{"text": r[0], "conf": round(r[1], 3), "source": r[2], "t_off": round(r[3], 2)} for r in reads],
                "crop": best_crop[1], "enhanced": enhanced, "box": best_crop[2],
                "method": method + ("+enhanced" if self.enhancer is not None else "")}

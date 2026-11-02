"""Text recognition providers used for the DVR clock (OSD) and number plates.

One interface, several engines – the first available one is used for ``auto``:

* ``rapidocr``  – RapidOCR (PaddleOCR models on ONNX Runtime, Apache-2.0); pip-installable,
                  works offline, CPU friendly. Recommended default.
* ``tesseract`` – the Tesseract command-line program, if installed.
* ``vlm``       – the configured vision-language model (slow, used only when asked).
* ``none``      – disabled.

Every engine returns a list of ``(text, confidence 0..1)`` lines.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Callable, List, Optional, Tuple

import cv2
import numpy as np

log = logging.getLogger("cctv")
ENGINES = ("auto", "rapidocr", "tesseract", "vlm", "none")
Lines = List[Tuple[str, float]]


class OCREngine:
    name = "none"

    def read(self, img_bgr: np.ndarray) -> Lines:  # pragma: no cover - interface
        return []

    def __bool__(self):
        return self.name != "none"


class NullOCR(OCREngine):
    name = "none"


class RapidOCREngine(OCREngine):
    name = "rapidocr"

    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR
        self._r = RapidOCR()

    def read(self, img_bgr):
        res, _ = self._r(img_bgr)
        return [(str(x[1]), float(x[2])) for x in res or []]


class TesseractEngine(OCREngine):
    name = "tesseract"

    def __init__(self, binary: Optional[str] = None, psm: int = 6):
        self.bin = binary or shutil.which("tesseract")
        if not self.bin:
            raise RuntimeError("tesseract program not found on PATH")
        self.psm = psm

    def read(self, img_bgr):
        ok, buf = cv2.imencode(".png", img_bgr)
        if not ok:
            return []
        r = subprocess.run([self.bin, "stdin", "stdout", "--psm", str(self.psm)], input=buf.tobytes(),
                           capture_output=True, timeout=60)
        txt = r.stdout.decode("utf-8", "ignore")
        # tesseract's CLI gives no per-line confidence; 0.6 marks it as "unscored"
        return [(ln.strip(), 0.6) for ln in txt.splitlines() if ln.strip()]


class VLMOCR(OCREngine):
    name = "vlm"

    def __init__(self, vlm_getter: Callable):
        self._get = vlm_getter

    def read(self, img_bgr):
        vlm = self._get()
        if vlm is None:
            return []
        txt = vlm.ask(img_bgr, "Transcribe all text visible in this image exactly, one line per text item. "
                               "Reply with the text only.", 60)
        return [(ln.strip(), 0.5) for ln in txt.splitlines() if ln.strip()]


def _try(factory):
    try:
        return factory()
    except Exception as e:
        log.debug("OCR engine unavailable: %s", e)
        return None


def available_engines() -> dict:
    """{engine: True/False} for the system check (does not load the VLM)."""
    out = {}
    try:
        import importlib.util
        out["rapidocr"] = importlib.util.find_spec("rapidocr_onnxruntime") is not None
    except Exception:
        out["rapidocr"] = False
    out["tesseract"] = shutil.which("tesseract") is not None
    return out


_CACHE = {}


def get_ocr(engine: str = "auto", vlm_getter: Optional[Callable] = None) -> OCREngine:
    """Return a ready engine (cached per process). Unknown names raise ValueError."""
    if engine not in ENGINES:
        raise ValueError(f"ocr_engine must be one of {ENGINES}, got {engine!r}")
    if engine == "none":
        return NullOCR()
    if engine == "vlm":
        return VLMOCR(vlm_getter) if vlm_getter else NullOCR()
    order = ["rapidocr", "tesseract"] if engine == "auto" else [engine]
    for name in order:
        if name in _CACHE:
            return _CACHE[name]
        eng = _try(RapidOCREngine if name == "rapidocr" else TesseractEngine)
        if eng is not None:
            _CACHE[name] = eng
            log.info("OCR engine: %s", name)
            return eng
    if engine == "auto" and vlm_getter is not None:
        return VLMOCR(vlm_getter)
    log.warning("No OCR engine available (install rapidocr-onnxruntime or tesseract) – "
                "the DVR clock cannot be read from frames")
    return NullOCR()

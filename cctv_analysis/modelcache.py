"""Keeps every model (YOLO weights, face ONNX, Hugging Face models) in one
persistent folder – on Colab that is your Google Drive – so each model is
downloaded only once and reused by all later runs."""
from __future__ import annotations

import logging
import os
import shutil

log = logging.getLogger("cctv")
_ROOT = {"dir": os.path.expanduser("~/.cache/cctv_analysis")}


def init(cache_dir: str) -> str:
    """Use cache_dir if its parent exists (e.g. Drive mounted), else a local cache."""
    d = cache_dir
    if d:
        parent = os.path.dirname(os.path.abspath(d))
        if os.path.isdir(parent) or os.path.isdir(d):
            try:
                os.makedirs(d, exist_ok=True)
                _ROOT["dir"] = d
            except OSError as e:
                log.warning("model cache %s unusable (%s) – using local cache", d, e)
        else:
            log.warning("model cache parent %s not found – using local cache %s", parent, _ROOT["dir"])
    os.makedirs(_ROOT["dir"], exist_ok=True)
    # HF env vars help when transformers has not been imported yet; we also pass cache_dir explicitly.
    os.environ.setdefault("HF_HUB_CACHE", hf_dir())
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    log.info("Model cache: %s", _ROOT["dir"])
    return _ROOT["dir"]


def root() -> str:
    os.makedirs(_ROOT["dir"], exist_ok=True)
    return _ROOT["dir"]


def sub(name: str) -> str:
    d = os.path.join(root(), name)
    os.makedirs(d, exist_ok=True)
    return d


def hf_dir() -> str:
    return sub("huggingface")


def yolo_weights(name: str) -> str:
    """Return a local path to YOLO weights, downloading into the cache only once."""
    if not name or os.path.isabs(name) or os.sep in name:
        return name
    dst = os.path.join(sub("yolo"), name)
    if os.path.exists(dst) and os.path.getsize(dst) > 100_000:
        return dst
    from ultralytics import YOLO
    YOLO(name)                                   # downloads into the working directory
    for cand in (name, os.path.join(os.getcwd(), name)):
        if os.path.exists(cand):
            try:
                shutil.copyfile(cand, dst + ".part")
                os.replace(dst + ".part", dst)
                log.info("cached %s -> %s", name, dst)
                return dst
            except OSError as e:
                log.warning("could not cache %s: %s", name, e)
                return cand
    return name

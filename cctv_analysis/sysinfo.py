"""Hardware / software check, model inventory, a short on-this-machine benchmark and the
processing-time estimate shown before a job starts.

Every estimate is a projection: it multiplies footage length by rates measured on this
computer (``benchmark``) or, before a benchmark has run, by conservative defaults. The
assumptions are returned with the numbers so the UI can show them."""
from __future__ import annotations

import importlib.util
import logging
import os
import platform
import shutil
import subprocess
import time
from typing import Dict, List, Optional

log = logging.getLogger("cctv")

# Rates used before a benchmark has run: measured with `benchmark()` (balanced preset) on the
# development machine – Intel Xeon 2.8 GHz, 4 vCPU, no GPU – on OpenCV's vtest.avi (detector,
# motion, encode) and a cloud clip with a DVR clock (OCR). See docs/PERFORMANCE.md. The detector
# rate includes pose. VLM figures are not measured here (labelled assumptions).
DEFAULT_RATES = {
    "motion_x_realtime": 9.9,         # pass-1 decode + motion analysis, × real time
    "detect_fps": 2.9,                # detector (+ pose) frames per second
    "face_s_per_frame": 0.03,         # extra face work per detection frame with people (assumption)
    "pose_s_per_frame": 0.0,          # included in detect_fps
    "ocr_s_per_read": 0.7,            # one DVR-clock read on a frame that has a clock
    "reel_x_realtime": 10.3,          # highlight-reel encode, × real time
    "plate_s_per_track": 1.5,         # measured: ~12 s plain vs ~45-110 s enhanced per 1-car clip
    "plate_deblur_s_per_track": 60.0,
    "vlm_s_per_event_transformers_cpu": 45.0,     # assumption (2B model, CPU)
    "vlm_s_per_event_transformers_gpu": 4.0,      # assumption
    "vlm_s_per_event_openai": 8.0,                # assumption (depends on the server)
    "semantic_s_per_event": 1.0,                  # assumption
}
ASSUMED_MOTION_FRACTION = 0.15        # share of footage with movement (typical shop/home DVR)
ASSUMED_EVENT_SECONDS = 30.0


def _has(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def _ffmpeg_info() -> dict:
    from . import video_io as V
    out = {"ffmpeg": bool(V.FFMPEG), "ffprobe": bool(V.FFPROBE), "version": "", "cuda_decode": False}
    if V.FFMPEG:
        try:
            r = subprocess.run([V.FFMPEG, "-hide_banner", "-version"], capture_output=True, text=True, timeout=20)
            out["version"] = (r.stdout.splitlines() or [""])[0][:80]
            r = subprocess.run([V.FFMPEG, "-hide_banner", "-hwaccels"], capture_output=True, text=True, timeout=20)
            out["cuda_decode"] = "cuda" in r.stdout
        except Exception:
            pass
    return out


def gpu_info() -> dict:
    """{'available', 'name', 'memory_gb', 'backend'} – torch first, then nvidia-smi."""
    if _has("torch"):
        try:
            import torch
            if torch.cuda.is_available():
                p = torch.cuda.get_device_properties(0)
                return {"available": True, "name": p.name, "memory_gb": round(p.total_memory / 1e9, 1),
                        "backend": "cuda"}
            if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                return {"available": True, "name": "Apple GPU (MPS)", "memory_gb": None, "backend": "mps"}
        except Exception as e:
            log.debug("torch GPU probe failed: %s", e)
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            r = subprocess.run([smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=20)
            name, mem = r.stdout.strip().splitlines()[0].split(",")
            return {"available": True, "name": name.strip(), "memory_gb": round(float(mem) / 1024, 1),
                    "backend": "cuda (driver only – install CUDA PyTorch to use it)"}
        except Exception:
            pass
    return {"available": False, "name": "", "memory_gb": None, "backend": "cpu"}


def _nearest_existing(path: str) -> str:
    p = os.path.abspath(path or ".")
    while not os.path.exists(p) and os.path.dirname(p) != p:
        p = os.path.dirname(p)
    return p


def system_check(output_dir: str = "", model_dir: str = "") -> dict:
    import psutil
    vm = psutil.virtual_memory()
    du = shutil.disk_usage(_nearest_existing(output_dir))
    gpu = gpu_info()
    ff = _ffmpeg_info()
    from .ocr import available_engines
    deps = {"opencv": True, "ultralytics": _has("ultralytics"), "torch": _has("torch"),
            "transformers": _has("transformers"), "rapidocr": _has("rapidocr_onnxruntime"),
            "fast_plate_ocr": _has("fast_plate_ocr"), "open_image_models": _has("open_image_models"),
            "xlsxwriter": _has("xlsxwriter"), "matplotlib": _has("matplotlib")}
    ram = vm.total / 1e9
    checks: List[dict] = []

    def chk(name, ok, detail, level="error"):
        checks.append({"item": name, "ok": bool(ok), "level": "ok" if ok else level, "detail": detail})

    chk("ffmpeg", ff["ffmpeg"] and ff["ffprobe"], ff["version"] or "ffmpeg/ffprobe not found – install ffmpeg")
    chk("Object detector (YOLO)", deps["ultralytics"] and deps["torch"],
        "ultralytics + torch installed" if deps["ultralytics"] else "pip install ultralytics torch")
    chk("DVR clock reader (OCR)", any(available_engines().values()),
        ", ".join(k for k, v in available_engines().items() if v) or "pip install rapidocr-onnxruntime",
        "warning")
    chk("Number-plate models", deps["fast_plate_ocr"] and deps["open_image_models"],
        "fast-plate-ocr + open-image-models" if deps["fast_plate_ocr"] else "pip install fast-plate-ocr open-image-models",
        "warning")
    chk("Memory", ram >= 8, f"{ram:.1f} GB RAM ({vm.available / 1e9:.1f} GB free)", "warning")
    chk("Local AI descriptions (VLM on this PC)", gpu["available"] or vm.available / 1e9 >= 10,
        "GPU available" if gpu["available"] else
        "CPU only: a 2B model needs ~9 GB free RAM and ~45 s per event – consider an Ollama/LM Studio server",
        "warning")
    chk("Free disk space", du.free / 1e9 >= 5, f"{du.free / 1e9:.1f} GB free at {_nearest_existing(output_dir)}",
        "warning")
    chk("GPU", gpu["available"], gpu["name"] or "no GPU – everything runs on the CPU (slower)", "info")
    return {"os": f"{platform.system()} {platform.release()}", "python": platform.python_version(),
            "cpu": {"cores": os.cpu_count(), "model": platform.processor() or platform.machine(),
                    "freq_mhz": int(psutil.cpu_freq().max) if psutil.cpu_freq() else None},
            "ram_gb": round(ram, 1), "ram_free_gb": round(vm.available / 1e9, 1), "gpu": gpu,
            "disk_free_gb": round(du.free / 1e9, 1), "ffmpeg": ff, "dependencies": deps,
            "models": model_inventory(model_dir), "checks": checks}


def model_inventory(model_dir: str = "") -> List[dict]:
    from . import modelcache
    root = model_dir or modelcache.root()

    def present(*parts, min_bytes=100_000):
        p = os.path.join(root, *parts)
        if os.path.isdir(p):
            return any(os.path.getsize(os.path.join(dp, f)) >= min_bytes
                       for dp, _, fs in os.walk(p) for f in fs)
        return os.path.exists(p) and os.path.getsize(p) >= min_bytes

    items = [
        ("yolo26n.pt", "Object detector – fast", ("yolo", "yolo26n.pt")),
        ("yolo26s.pt", "Object detector – balanced", ("yolo", "yolo26s.pt")),
        ("yolo26m.pt", "Object detector – accurate", ("yolo", "yolo26m.pt")),
        ("yolo26n-pose.pt", "Posture (pose)", ("yolo", "yolo26n-pose.pt")),
        ("yunet", "Face detector (YuNet)", ("opencv", "face_detection_yunet_2023mar.onnx")),
        ("sface", "Face recognition (SFace)", ("opencv", "face_recognition_sface_2021dec.onnx")),
        ("plate-detector", "Number-plate detector", ("plates", "yolo-v9-t-384-license-plate-end2end")),
        ("plate-ocr", "Number-plate reader", ("plates", "cct-s-v2-global-model")),
        ("siglip2", "Scene search (SigLIP-2)", ("huggingface", "models--google--siglip2-base-patch16-224")),
        ("qwen3-vl-2b", "AI descriptions (Qwen3-VL-2B)", ("huggingface", "models--Qwen--Qwen3-VL-2B-Instruct")),
    ]
    return [{"id": i, "name": n, "installed": present(*p)} for i, n, p in items]


def download_model(model_id: str, model_dir: str = "") -> str:
    """Fetch one model into the cache (used by the app's Models page)."""
    from . import modelcache
    if model_dir:
        modelcache.init(model_dir)
    if model_id.startswith("yolo"):
        return modelcache.yolo_weights(model_id)
    if model_id in ("yunet", "sface"):
        from .faces import fetch
        return fetch(model_id)
    if model_id in ("plate-detector", "plate-ocr"):
        from .plates import FastPlateOCR, YoloPlateDetector
        (YoloPlateDetector if model_id == "plate-detector" else FastPlateOCR)()
        return model_id
    if model_id in ("siglip2", "qwen3-vl-2b"):
        from huggingface_hub import snapshot_download
        repo = {"siglip2": "google/siglip2-base-patch16-224", "qwen3-vl-2b": "Qwen/Qwen3-VL-2B-Instruct"}[model_id]
        return snapshot_download(repo, cache_dir=modelcache.hf_dir())
    raise ValueError(f"unknown model {model_id!r}")


# ---------------------------------------------------------------- benchmark

def benchmark(cfg, sample_video: str, seconds: float = 20.0) -> dict:
    """Measure the rates the estimate needs on a real sample (motion pass, detector fps, OCR,
    reel encode). Takes ~20-60 s."""
    from . import modelcache
    from . import video_io as V
    from .motion import scan_motion
    modelcache.init(cfg.model_cache_dir)
    rates = dict(DEFAULT_RATES)
    measured = []
    meta = V.probe(sample_video)
    if not meta["duration"]:
        raise ValueError("sample video unreadable")
    seg = min(seconds, meta["duration"])
    sub = dict(meta, duration=seg)
    t0 = time.time()
    scan_motion(sample_video, sub, cfg, "bench")
    dt_ = max(1e-3, time.time() - t0)
    rates["motion_x_realtime"] = round(seg / dt_, 1)
    measured.append("motion_x_realtime")
    try:
        from .detector import Detector
        det = Detector(cfg)
        frames = [f for _, f in V.iter_frames(sample_video, 2.0, cfg.detect_width, 0, min(seg, 8),
                                              src_size=(meta["width"], meta["height"]))][:12]
        if frames:
            det(frames[0])                                    # warm-up
            t0 = time.time()
            for f in frames:
                det(f)
            rates["detect_fps"] = round(len(frames) / max(1e-3, time.time() - t0), 1)
            measured.append("detect_fps")
    except Exception as e:
        log.warning("benchmark: detector unavailable (%s)", e)
    try:
        from .ocr import get_ocr
        from .timeline import read_clock
        ocr = get_ocr(cfg.ocr_engine)
        if ocr:
            f = V.read_frame_at(sample_video, 0.5, 1280, (meta["width"], meta["height"]))
            read_clock(f, ocr)
            t0 = time.time()
            read_clock(f, ocr)
            rates["ocr_s_per_read"] = round(time.time() - t0, 2)
            measured.append("ocr_s_per_read")
    except Exception as e:
        log.warning("benchmark: OCR unavailable (%s)", e)
    if V.FFMPEG:
        import tempfile

        from .reels import encode_segment
        with tempfile.TemporaryDirectory() as td:
            seg_ev = {"event_id": "bench", "video_path": sample_video, "start_off": 0.0, "end_off": min(seg, 10.0),
                      "camera": "", "level": "", "score": 0, "rel_path": "", "start_dt": None}
            t0 = time.time()
            if encode_segment(seg_ev, os.path.join(td, "b.mp4"), cfg):
                rates["reel_x_realtime"] = round(seg_ev["end_off"] / max(1e-3, time.time() - t0), 1)
                measured.append("reel_x_realtime")
    return {"rates": rates, "measured": measured, "sample": os.path.basename(sample_video)}


# ----------------------------------------------------------------- estimate

def estimate(cfg, total_seconds: float, n_videos: int, rates: Optional[Dict[str, float]] = None,
             n_untimed: int = 0, gpu: bool = False) -> dict:
    """Projected processing time for `total_seconds` of footage in `n_videos` files.
    `n_untimed` = files whose name carries no time (their DVR clock is read by OCR)."""
    r = dict(DEFAULT_RATES, **(rates or {}))
    m = ASSUMED_MOTION_FRACTION
    motion_s = total_seconds * m
    events = max(1.0, motion_s / ASSUMED_EVENT_SECONDS) if total_seconds else 0.0
    parts = {}
    parts["motion scan"] = total_seconds / max(0.1, r["motion_x_realtime"])
    det_frames = motion_s * cfg.detect_fps
    per = 1.0 / max(0.1, r["detect_fps"])
    if cfg.use_faces:
        per += r["face_s_per_frame"]
    if cfg.use_pose:
        per += r["pose_s_per_frame"]
    parts["detection"] = det_frames * per
    clock_reads = n_untimed * (cfg.osd_samples + 1) + (events * (n_untimed / max(1, n_videos))
                                                       if cfg.osd_verify_events else 0)
    if "osd" in cfg.time_sources:
        parts["reading DVR clock"] = clock_reads * r["ocr_s_per_read"]
    if cfg.use_semantic:
        parts["scene tags"] = events * r["semantic_s_per_event"]
    if cfg.use_vlm:
        share = {"all": 1.0, "people": 0.5, "flagged": 0.15}.get(cfg.vlm_scope, 0.15)
        n = min(cfg.vlm_max_events, events * share)
        key = ("vlm_s_per_event_openai" if cfg.vlm_provider == "openai" else
               "vlm_s_per_event_transformers_gpu" if gpu else "vlm_s_per_event_transformers_cpu")
        parts["AI descriptions"] = n * r[key]
    if cfg.use_plates:
        tracks = events * 0.5
        parts["number plates"] = tracks * (r["plate_deblur_s_per_track"] if cfg.plate_enhance
                                           else r["plate_s_per_track"])
    if cfg.make_highlight_reel:
        parts["highlight reels"] = motion_s / max(0.1, r["reel_x_realtime"])
    parts["reports"] = 10 + events * 0.05
    total = sum(parts.values())
    return {"total_seconds": round(total), "breakdown": {k: round(v) for k, v in parts.items()},
            "footage_seconds": round(total_seconds), "videos": n_videos, "projection": True,
            "assumptions": [f"{int(m * 100)}% of the footage contains movement (real share decides the detection time)",
                            f"about one event per {int(ASSUMED_EVENT_SECONDS)} s of movement",
                            "rates measured on this computer" if rates else
                            "rates of a 4-core CPU without GPU (run 'Measure speed' for this computer's real speed)"]}

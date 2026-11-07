"""Run a whole case: for every source, ingest (hashes, playback copies) then analyse it.

Each source is analysed by the ordinary pipeline (``pipeline.Analyzer``) into its own output
folder, with the case's chosen preset and options; nothing about the per-source analysis is
special. After analysis the source's appearance index (``reid.build_index``) is updated so that
"search this appearance across all cameras" covers it. Sources are processed one after another
(one CPU-heavy analysis at a time is what an 8 GB laptop can sustain); the run can be cancelled
between sources, files and events and resumes where it stopped (finished videos are skipped).
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from typing import Callable, Dict, List, Optional

from .cases import Case
from .config import PRESETS, Config

log = logging.getLogger("cctv")


# What the face analysis of a case run does – chosen before the run (public cameras: often only
# the reference persons may be searched for). The only place these modes are defined.
FACE_MODES = {
    "off": "no face analysis",
    "detect": "detect faces and keep the face pictures; no face is compared or grouped",
    "unique": "detect faces and group repeat faces into persons (P0001, …)",
    "reference": "detect faces and compare them only with the reference persons' photos",
}


def face_options(mode: str, references_dir: str = "") -> dict:
    """The Config fields of a face mode (FACE_MODES)."""
    if mode not in FACE_MODES:
        raise ValueError(f"faces must be one of {list(FACE_MODES)}")
    if mode == "reference" and not references_dir:
        raise ValueError("faces=reference needs the reference persons' photos")
    return {"use_faces": mode != "off", "face_identities": mode == "unique",
            "face_watchlist_dir": references_dir if mode == "reference" else "", "known_faces_dir": ""}


def run_options(options: Optional[dict] = None, preset: str = "balanced") -> dict:
    """The analysis options of a case run merged over the preset, validated (ValueError or
    TypeError name the problem) – before anything is created or started."""
    if preset not in PRESETS:
        raise ValueError(f"preset must be one of {list(PRESETS)}")
    merged = {**PRESETS[preset], **(options or {})}
    for k in ("input_dir", "output_dir", "work_dir"):
        merged.pop(k, None)
    problems = Config.from_dict(merged).validate()
    if problems:
        raise ValueError("invalid settings: " + "; ".join(problems))
    return merged


def source_config(case: Case, sid: str, options: Optional[dict] = None, preset: str = "balanced",
                  work_root: str = "", model_cache_dir: str = "") -> Config:
    """The analysis configuration of one source of a case."""
    merged = run_options(options, preset)
    src = case.source(sid)
    out = case.source_dir(sid)
    merged.update(input_dir=src.path, output_dir=out,
                  work_dir=os.path.join(work_root or os.path.join(case.root, ".work"),
                                        hashlib.sha1(os.path.realpath(out).encode()).hexdigest()[:12]),
                  drive_links=False)
    if model_cache_dir and not (options or {}).get("model_cache_dir"):
        merged["model_cache_dir"] = model_cache_dir
    return Config.from_dict(merged)


def run_case(case: Case, options: Optional[dict] = None, preset: str = "balanced",
             progress: Optional[Callable[[dict], None]] = None, cancel: Optional[threading.Event] = None,
             work_root: str = "", model_cache_dir: str = "", sources: Optional[List[str]] = None,
             remux_files: bool = True, analyse: bool = True, appearance_index: bool = True,
             max_videos_total: int = 0) -> dict:
    """`max_videos_total` > 0 caps the videos analysed by the whole run (all sources together):
    the trial licence's per-job limit. Ingest (hashing) is never limited."""
    cancel = cancel or threading.Event()
    todo = [s for s in case.sources if sources is None or s.source_id in sources]
    if not todo:
        raise ValueError("this case has no sources – add the seized folders first")
    results: Dict[str, dict] = {}
    n = len(todo)

    def emit(i: int, src, d: dict):
        if progress:
            inner = float(d.get("percent") or 0)
            progress({**d, "source": src.source_id, "source_label": src.label, "source_index": i, "sources": n,
                      "percent": round(100 * (i - 1 + inner / 100) / n, 1)})

    from . import video_io as V
    from .ingest import ingest_source
    from .pipeline import Analyzer
    budget = max_videos_total
    case.audit.append("case_run_started", case_id=case.case_id, sources=[s.source_id for s in todo], preset=preset)
    for i, src in enumerate(todo, 1):
        if cancel.is_set():
            break
        out = case.source_dir(src.source_id)
        os.makedirs(out, exist_ok=True)
        res: dict = {"label": src.label}
        if not os.path.isdir(src.path):
            res["error"] = f"folder not available: {src.path} (is the drive plugged in?)"
            results[src.source_id] = res
            log.error("source %s: %s", src.source_id, res["error"])
            continue
        emit(i, src, {"stage": "ingest", "percent": 0})

        def ingest_progress(d: dict, i: int = i, src=src) -> None:
            emit(i, src, {**d, "percent": 0.2 * (d.get("percent") or 0)})

        def analysis_progress(d: dict, i: int = i, src=src) -> None:
            emit(i, src, {**d, "percent": 20 + 0.75 * _pct(d)})
        rec = ingest_source(src.path, out, remux_files=remux_files, progress=ingest_progress, cancel=cancel)
        register_proxies(src.path, out, rec)
        res["files"] = len(rec["files"])
        res["merkle_root"] = rec["merkle_root"]
        res["remuxed"] = sum(1 for f in rec["files"] if f.get("playback"))
        case.audit.append("source_ingested", case_id=case.case_id, source=src.source_id, files=res["files"],
                          bytes=rec["total_bytes"], merkle_root=rec["merkle_root"],
                          changed=rec["changed_since_last_ingest"])
        if analyse and not cancel.is_set():
            cfg = source_config(case, src.source_id, options, preset, work_root, model_cache_dir)
            if max_videos_total and budget <= 0:
                res["skipped"] = f"trial limit: at most {max_videos_total} videos per run"
            else:
                if max_videos_total:
                    cfg.max_videos = min(cfg.max_videos or budget, budget)
                    budget -= len(V.find_videos(cfg))
                an = Analyzer(cfg, analysis_progress, cancel)
                res["stats"] = an.run()
                device, semantic_on = an.device, cfg.use_semantic    # the analyzer turns it off if its model failed
                del an                                              # free the detector before loading SigLIP
                if semantic_on and not cancel.is_set():
                    from .semantic import backfill_index
                    emit(i, src, {"stage": "scene search index", "percent": 95})
                    try:
                        res["text_index_added"] = backfill_index(out, cfg, device)
                    except Exception as e:                       # search is an aid: never fail the run for it
                        log.warning("source %s: scene-search index of earlier events not built: %s", src.source_id, e)
        if appearance_index and not cancel.is_set() and os.path.exists(os.path.join(out, "analysis.db")):
            from .reid import build_index
            emit(i, src, {"stage": "appearance index", "percent": 96})
            res["appearances"] = build_index(out)["tracks"]
        results[src.source_id] = res
    stopped = cancel.is_set()
    case.audit.append("case_run_finished", case_id=case.case_id, cancelled=stopped,
                      results={k: {kk: vv for kk, vv in v.items() if kk != "stats"} for k, v in results.items()})
    return {"case_id": case.case_id, "sources": results, "cancelled": stopped}


def register_proxies(src_dir: str, out_dir: str, rec: Optional[dict] = None) -> int:
    """Raw .264/.265 streams cannot seek: decode their lossless remux instead (video_io proxy).
    Called after ingest and whenever a case is opened (the registry lives in memory)."""
    from . import video_io as V
    from .ingest import load_ingest
    rec = rec if rec is not None else load_ingest(out_dir)
    n = 0
    for f in (rec or {}).get("files", []):
        orig = os.path.join(src_dir, f["rel"])
        if f.get("playback") and V.is_raw(orig):
            V.register_proxy(orig, os.path.join(out_dir, f["playback"]))
            n += 1
    return n


def _pct(d: dict) -> float:
    """Analyzer progress (stage/index/total) as 0..100."""
    if d.get("stage") in ("video", "planning") and d.get("total"):
        return 100.0 * (max(0, d.get("index", 1) - 1)) / d["total"]
    if d.get("stage") in ("reports", "finished"):
        return 100.0
    return 0.0

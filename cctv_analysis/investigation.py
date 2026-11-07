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


def source_config(case: Case, sid: str, options: Optional[dict] = None, preset: str = "balanced",
                  work_root: str = "", model_cache_dir: str = "") -> Config:
    """The analysis configuration of one source of a case."""
    if preset not in PRESETS:
        raise ValueError(f"preset must be one of {list(PRESETS)}")
    src = case.source(sid)
    out = case.source_dir(sid)
    merged = {**PRESETS[preset], **(options or {})}
    for k in ("input_dir", "output_dir", "work_dir"):
        merged.pop(k, None)
    merged.update(input_dir=src.path, output_dir=out,
                  work_dir=os.path.join(work_root or os.path.join(case.root, ".work"),
                                        hashlib.sha1(os.path.realpath(out).encode()).hexdigest()[:12]),
                  drive_links=False)
    if model_cache_dir:
        merged["model_cache_dir"] = model_cache_dir
    cfg = Config.from_dict(merged)
    problems = cfg.validate()
    if problems:
        raise ValueError("invalid settings: " + "; ".join(problems))
    return cfg


def run_case(case: Case, options: Optional[dict] = None, preset: str = "balanced",
             progress: Optional[Callable[[dict], None]] = None, cancel: Optional[threading.Event] = None,
             work_root: str = "", model_cache_dir: str = "", sources: Optional[List[str]] = None,
             remux_files: bool = True, analyse: bool = True, appearance_index: bool = True,
             limits: Optional[Callable[[Config], None]] = None) -> dict:
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

    from .ingest import ingest_source
    from .pipeline import Analyzer
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
        from . import video_io as V
        for f in rec["files"]:                          # raw streams cannot seek: decode their lossless remux
            orig = os.path.join(src.path, f["rel"])
            if f.get("playback") and V.is_raw(orig):
                V.register_proxy(orig, os.path.join(out, f["playback"]))
        res["files"] = len(rec["files"])
        res["merkle_root"] = rec["merkle_root"]
        res["remuxed"] = sum(1 for f in rec["files"] if f.get("playback"))
        case.audit.append("source_ingested", case_id=case.case_id, source=src.source_id, files=res["files"],
                          bytes=rec["total_bytes"], merkle_root=rec["merkle_root"],
                          changed=rec["changed_since_last_ingest"])
        if analyse and not cancel.is_set():
            cfg = source_config(case, src.source_id, options, preset, work_root, model_cache_dir)
            if limits:
                limits(cfg)
            an = Analyzer(cfg, analysis_progress, cancel)
            res["stats"] = an.run()
        if appearance_index and not cancel.is_set() and os.path.exists(os.path.join(out, "analysis.db")):
            from .reid import build_index
            emit(i, src, {"stage": "appearance index", "percent": 96})
            res["appearances"] = build_index(out)["tracks"]
        results[src.source_id] = res
    stopped = cancel.is_set()
    case.audit.append("case_run_finished", case_id=case.case_id, cancelled=stopped,
                      results={k: {kk: vv for kk, vv in v.items() if kk != "stats"} for k, v in results.items()})
    return {"case_id": case.case_id, "sources": results, "cancelled": stopped}


def _pct(d: dict) -> float:
    """Analyzer progress (stage/index/total) as 0..100."""
    if d.get("stage") in ("video", "planning") and d.get("total"):
        return 100.0 * (max(0, d.get("index", 1) - 1)) / d["total"]
    if d.get("stage") in ("reports", "finished"):
        return 100.0
    return 0.0

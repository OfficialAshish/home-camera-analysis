"""Fix recording times of already-analysed footage without analysing it again.

For every finished video: resolve the start time again (current rules: file name → DVR clock
→ metadata → file time), re-read each event's own DVR clock, then recompute every stored
timestamp as ``base + offset`` (events, tracks, faces, detections, plates, alerts, tamper),
refresh identity first/last-seen, and rebuild the reels/reports of every day that changed.
Event IDs and event folders keep their original names (they are identifiers, not times).

  python -m cctv_analysis --input /videos --output /results --retime
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from typing import Optional

from . import timeline as T
from . import video_io as V
from .storage import Store

log = logging.getLogger("cctv")
FMT = "%Y-%m-%d %H:%M:%S"


def _iso(d: Optional[dt.datetime]) -> Optional[str]:
    return d.strftime(FMT) if d else None


def _shift_sql(table: str, col: str, off_col: str, key: str):
    # SQLite strftime truncates fractional seconds exactly like the analyzer's strftime does
    return (f"UPDATE {table} SET {col} = CASE WHEN ? IS NULL THEN NULL ELSE "
            f"strftime('%Y-%m-%d %H:%M:%S', ?, '+' || {off_col} || ' seconds') END WHERE {key} = ?")


def retime(cfg, progress=None, cancel=None, build_reports: bool = True) -> dict:
    from . import modelcache
    from .ocr import get_ocr
    problems = cfg.validate()
    if problems:
        raise ValueError("Invalid configuration:\n  - " + "\n  - ".join(problems))
    modelcache.init(cfg.model_cache_dir)
    store = Store(os.path.join(cfg.work_dir, "analysis.db"), os.path.join(cfg.output_dir, "analysis.db"))
    ocr = get_ocr(cfg.ocr_engine)
    vids = store.query("SELECT id, path, start_dt, time_source FROM videos WHERE status='done' ORDER BY id")
    stats: dict = {"videos": 0, "videos_changed": 0, "events_changed": 0, "missing": 0}
    days = set()
    db = store.db
    for i, v in enumerate(vids, 1):
        if cancel is not None and cancel.is_set():
            stats["cancelled"] = 1
            break
        if progress:
            progress({"stage": "retime", "index": i, "total": len(vids), "path": v["path"],
                      "percent": round(100 * i / max(1, len(vids)), 1)})
        if not os.path.exists(v["path"]):
            stats["missing"] += 1
            log.warning("retime: source missing, kept as is: %s", v["path"])
            continue
        stats["videos"] += 1
        meta = V.probe(v["path"])
        start, src = T.resolve_start_time(v["path"], meta, cfg, ocr)
        if _iso(start) != v["start_dt"] or src != v["time_source"]:
            stats["videos_changed"] += 1
            log.info("retime %s: %s (%s) -> %s (%s)", os.path.basename(v["path"]), v["start_dt"], v["time_source"],
                     _iso(start), src)
        db.execute("UPDATE videos SET start_dt=?, end_dt=?, time_source=? WHERE id=?",
                   (_iso(start), _iso(start + dt.timedelta(seconds=meta["duration"])) if start else None, src, v["id"]))
        b = _iso(start)
        db.execute("UPDATE tamper SET start_dt = CASE WHEN ? IS NULL THEN NULL ELSE "
                   "strftime('%Y-%m-%d %H:%M:%S', ?, '+' || start_off || ' seconds') END WHERE video_id = ?",
                   (b, b, v["id"]))
        for e in store.query("SELECT event_id, start_off, end_off, start_dt FROM events WHERE video_id=?", (v["id"],)):
            base, esrc = T.event_clock(v["path"], meta, start, src, e["start_off"], cfg, ocr)
            bs = _iso(base)
            new_start = _iso(base + dt.timedelta(seconds=e["start_off"])) if base else None
            if new_start != e["start_dt"]:
                stats["events_changed"] += 1
            days.add((e["start_dt"] or "unknown-date")[:10] if e["start_dt"] else "unknown-date")
            days.add(new_start[:10] if new_start else "unknown-date")
            db.execute("UPDATE events SET start_dt=?, end_dt=?, time_source=? WHERE event_id=?",
                       (new_start, _iso(base + dt.timedelta(seconds=e["end_off"])) if base else None, esrc,
                        e["event_id"]))
            for table, col, off in (("tracks", "first_dt", "first_off"), ("faces", "dt", "t_off"),
                                    ("detections", "dt", "t_off"), ("plates", "dt", "t_off"),
                                    ("face_alerts", "dt", "t_off")):
                db.execute(_shift_sql(table, col, off, "event_id"), (bs, bs, e["event_id"]))
        store.commit(sync=False)
    _refresh_identities(store, cfg)
    store.commit()
    stats["days_affected"] = sorted(days)
    if build_reports:
        from .report import build_reports as br
        br(store, cfg, days)
        store.commit()
    store.close()
    log.info("retime finished: %s", stats)
    return stats


def _refresh_identities(store, cfg) -> None:
    path = os.path.join(cfg.output_dir, "identities.json")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        ids = json.load(fh)
    span = {r["identity"]: (r["a"], r["b"]) for r in store.query(
        "SELECT identity, MIN(first_dt) a, MAX(first_dt) b FROM tracks WHERE identity IS NOT NULL AND identity != '' "
        "GROUP BY identity")}
    for r in ids:
        if r["id"] in span and span[r["id"]][0]:
            r["first_seen"], r["last_seen"] = span[r["id"]]
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(ids, fh, indent=1)
    os.replace(path + ".tmp", path)

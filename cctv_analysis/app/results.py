"""Read-only queries on an output folder for the app (events, highlights, plates, faces).

Every query opens analysis.db read-only (the analyzer may be writing a new copy), uses bound
parameters only, paginates, and returns only what the UI shows: file references are paths
relative to the output folder (the server turns them into URLs); no detection boxes, track
paths, embeddings, raw model output or scores the UI does not display."""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sqlite3
import threading
from typing import List, Optional

from ..plates import normalize, within_edits
from ..storage import rel_to

LEVELS = ("HIGH", "REVIEW", "LOW", "NORMAL")


class NotAnOutputFolder(ValueError):
    pass


def _db(out: str) -> sqlite3.Connection:
    """Read-only connection to analysis.db with reviews.db attached as `rv` (an empty in-memory
    table when the folder has no reviews yet), so review filters are a SQL join."""
    p = os.path.join(out, "analysis.db")
    if not os.path.isfile(p):
        raise NotAnOutputFolder("no analysis results in this folder yet")
    db = sqlite3.connect(pathlib.Path(os.path.abspath(p)).as_uri() + "?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    rp = os.path.join(out, "reviews.db")
    if os.path.isfile(rp):
        db.execute("ATTACH DATABASE ? AS rv", (pathlib.Path(os.path.abspath(rp)).as_uri() + "?mode=ro",))
    else:
        db.execute("ATTACH DATABASE ':memory:' AS rv")
        db.execute("CREATE TABLE rv.reviews (event_id TEXT PRIMARY KEY, status TEXT, notes TEXT, reviewer TEXT, "
                   "updated TEXT)")
    return db


def _j(s, default):
    if s in (None, ""):
        return default
    try:
        return json.loads(s)
    except (TypeError, json.JSONDecodeError):
        return default


_SUMMARY: dict = {}


def summary(out: str) -> dict:
    """KPIs of a results folder; cached until analysis.db changes."""
    p = os.path.abspath(os.path.join(out, "analysis.db"))
    if not os.path.isfile(p):
        raise NotAnOutputFolder("no analysis results in this folder yet")
    key = os.stat(p).st_mtime_ns
    hit = _SUMMARY.get(p)
    if hit and hit[0] == key:
        return hit[1]
    res = _summary(out)
    _SUMMARY[p] = (key, res)
    return res


def _summary(out: str) -> dict:
    db = _db(out)
    try:
        v = db.execute("SELECT COUNT(*) n, SUM(duration) d, SUM(motion_seconds) m FROM videos WHERE status='done'").fetchone()
        lv = {r["level"]: r["n"] for r in db.execute("SELECT level, COUNT(*) n FROM events GROUP BY level")}
        days = db.execute("SELECT MIN(substr(start_dt,1,10)) a, MAX(substr(start_dt,1,10)) b FROM events").fetchone()
        cams = [r[0] for r in db.execute("SELECT DISTINCT camera FROM events WHERE camera IS NOT NULL ORDER BY camera")]
        failed = db.execute("SELECT COUNT(*) FROM videos WHERE status NOT IN ('done','duplicate')").fetchone()[0]
        dup = db.execute("SELECT COUNT(*) FROM videos WHERE status='duplicate'").fetchone()[0]
        approx = db.execute("SELECT COUNT(*) FROM videos WHERE status='done' AND (time_source IS NULL OR NOT ("
                            "time_source LIKE 'filename%' OR time_source LIKE 'osd%' OR time_source LIKE 'override%'))"
                            ).fetchone()[0]
        plates = db.execute("SELECT COUNT(DISTINCT text) FROM plates WHERE text != ''").fetchone()[0]
        faces = db.execute("SELECT COUNT(DISTINCT identity) FROM tracks WHERE identity IS NOT NULL AND identity != ''").fetchone()[0]
        alerts = {r["kind"]: r["n"] for r in db.execute("SELECT kind, COUNT(*) n FROM face_alerts GROUP BY kind")}
        reels = db.execute("SELECT COUNT(DISTINCT day) FROM reel_segments").fetchone()[0]
    finally:
        db.close()
    return {"videos": v["n"], "footage_hours": round((v["d"] or 0) / 3600, 2),
            "motion_hours": round((v["m"] or 0) / 3600, 2), "levels": {k: lv.get(k, 0) for k in LEVELS},
            "events": sum(lv.values()), "first_day": days["a"], "last_day": days["b"], "cameras": cams,
            "videos_failed": failed, "duplicates": dup, "videos_with_approx_time": approx,
            "distinct_plates": plates, "face_identities": faces, "new_face_alerts": alerts.get("new_face", 0),
            "watchlist_alerts": alerts.get("watchlist", 0), "unauthorized_alerts": alerts.get("unauthorized", 0),
            "highlight_days": reels}


def _best_snapshot(snaps) -> str:
    if not snaps:
        return ""
    best = max(snaps, key=lambda s: s.get("persons", 0))
    return best.get("annotated") or best.get("raw") or ""


def events(out: str, day_from: str = "", day_to: str = "", camera: str = "", levels=(), q: str = "",
           person: str = "", plate: str = "", people_only: bool = False, review: str = "", sort: str = "time",
           limit: int = 50, offset: int = 0, min_score: float = 0.0) -> dict:
    cond: List[str] = ["1=1"]
    args: List[object] = []
    if min_score > 0:                         # the rule score (0–100) the event was ranked with
        cond.append("score >= ?")
        args.append(float(min_score))
    # plain range predicates so the start_dt index is used (substr() would force a full scan)
    if day_from:
        cond.append("start_dt >= ?")
        args.append(day_from)
    if day_to:
        cond.append("start_dt < ?")
        args.append((dt.date.fromisoformat(day_to) + dt.timedelta(days=1)).isoformat())
    if camera:
        cond.append("camera = ?")
        args.append(camera)
    lv = [x for x in levels if x in LEVELS]
    if lv:
        cond.append("level IN (" + ",".join("?" * len(lv)) + ")")
        args += lv
    if q:
        like = "%" + q.replace("%", "").replace("_", "") + "%"
        cond.append("(flags LIKE ? OR objects LIKE ? OR vlm LIKE ? OR plates LIKE ? OR identities LIKE ? OR event_id LIKE ?)")
        args += [like] * 6
    if person:
        cond.append("identities LIKE ?")
        args.append('%"' + person.strip().replace("%", "") + '"%')
    if plate:
        cond.append("plates LIKE ?")
        args.append("%" + normalize(plate) + "%")
    if people_only:
        cond.append("n_person_tracks > 0")
    if review == "Not reviewed":
        cond.append("r.status IS NULL")
    elif review:
        cond.append("r.status = ?")
        args.append(review)
    order = "score DESC, start_dt" if sort == "score" else "COALESCE(start_dt,''), camera, start_off"
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    where = " AND ".join(cond)
    db = _db(out)
    try:
        frm = "events LEFT JOIN rv.reviews r USING(event_id)"
        total = db.execute(f"SELECT COUNT(*) FROM {frm} WHERE {where}", args).fetchone()[0]
        rows = db.execute(f"SELECT event_id, level, score, camera, start_dt, end_dt, duration, kind, n_person_tracks, "
                          f"max_persons, plates, identities, flags, snapshots, time_source, r.status AS review "
                          f"FROM {frm} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
    finally:
        db.close()
    items = []
    for r in rows:
        items.append({"event_id": r["event_id"], "level": r["level"], "score": r["score"], "camera": r["camera"],
                      "start": r["start_dt"], "end": r["end_dt"], "duration": r["duration"], "kind": r["kind"],
                      "people": r["max_persons"], "plates": _j(r["plates"], []), "identities": _j(r["identities"], []),
                      "flags": [f["flag"] for f in _j(r["flags"], [])],
                      "thumb": rel_to(out, _best_snapshot(_j(r["snapshots"], []))),
                      "approx_time": not ((r["time_source"] or "").startswith(("filename", "osd", "override"))),
                      "review": r["review"] or "Not reviewed"})
    return {"total": total, "limit": limit, "offset": offset, "items": items}


def event_detail(out: str, event_id: str) -> Optional[dict]:
    db = _db(out)
    try:
        e = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        if not e:
            return None
        tracks = db.execute("SELECT track, cls, identity, known_name, watchlist, duration, face_crop, person_crop "
                            "FROM tracks WHERE event_id=? ORDER BY track", (event_id,)).fetchall()
        plates = db.execute(f"SELECT {_plate_cols(db)} FROM plates WHERE event_id=? ORDER BY confidence DESC",
                            (event_id,)).fetchall()
        vid = db.execute("SELECT duration, time_source FROM videos WHERE id=?", (e["video_id"],)).fetchone()
        rv = db.execute("SELECT event_id, status, notes, reviewer, updated FROM rv.reviews WHERE event_id=?",
                        (event_id,)).fetchone()
    finally:
        db.close()
    from ..reviews import effective_plate, plate_readings
    readings = plate_readings(out)
    vlm = _j(e["vlm"], None) or {}
    snaps = _j(e["snapshots"], [])
    return {"event_id": e["event_id"], "level": e["level"], "score": e["score"], "camera": e["camera"],
            "start": e["start_dt"], "end": e["end_dt"], "duration": e["duration"], "kind": e["kind"],
            "time_source": e["time_source"] or (vid["time_source"] if vid else ""),
            "video": e["rel_path"], "offset_start": e["start_off"], "offset_end": e["end_off"],
            "flags": [{"flag": f["flag"], "points": f["points"], "reason": f["reason"]} for f in _j(e["flags"], [])],
            "ai": {"description": vlm.get("description", ""), "suspicious": vlm.get("suspicious"),
                   "category": vlm.get("category", "")} if vlm else None,
            "objects": _j(e["objects"], {}), "people": e["max_persons"], "crossings": _j(e["crossings"], {}),
            "snapshots": [{"t": s.get("t_off"), "time": s.get("dt"), "annotated": rel_to(out, s.get("annotated")),
                           "raw": rel_to(out, s.get("raw"))} for s in snaps],
            "contact_sheet": rel_to(out, e["contact_sheet"]), "clip": rel_to(out, e["clip_path"]),
            "tracks": [{"track": t["track"], "cls": t["cls"], "identity": t["known_name"] or t["identity"] or "",
                        "identity_id": t["identity"] or "",
                        "watchlist": t["watchlist"] or "", "seconds": t["duration"], "face": rel_to(out, t["face_crop"]),
                        "person": rel_to(out, t["person_crop"])} for t in tracks],
            "plates": [{**_plate_public(out, p), "method": p["method"]} for p in
                       (effective_plate(dict(x), readings) for x in plates)],
            "review": dict(rv) if rv else None}


def highlights(out: str) -> list:
    """One row per day (newest first); the index of a day comes from highlight_day()."""
    db = _db(out)
    try:
        rows = db.execute("SELECT day, COUNT(*) n, MAX(reel_end) secs, MAX(reel_path) reel FROM reel_segments "
                          "GROUP BY day ORDER BY day DESC").fetchall()
    finally:
        db.close()
    return [{"day": r["day"], "reel": rel_to(out, r["reel"]), "movements": r["n"], "seconds": r["secs"]} for r in rows]


def highlight_day(out: str, day: str) -> Optional[dict]:
    db = _db(out)
    try:
        segs = db.execute("SELECT r.idx, r.reel_start, r.reel_end, r.reel_path, e.event_id, e.camera, e.start_dt, "
                          "e.level, e.score, e.rel_path, e.start_off FROM reel_segments r JOIN events e "
                          "USING(event_id) WHERE r.day = ? ORDER BY r.idx", (day,)).fetchall()
    finally:
        db.close()
    if not segs:
        return None
    return {"day": day, "reel": rel_to(out, segs[0]["reel_path"]), "movements": len(segs), "seconds": segs[-1]["reel_end"],
            "index": [{"n": s["idx"], "at": s["reel_start"], "until": s["reel_end"], "camera": s["camera"],
                       "time": s["start_dt"], "level": s["level"], "score": s["score"], "event_id": s["event_id"],
                       "video": s["rel_path"], "offset": s["start_off"]} for s in segs]}


_TEXTS: dict = {}
_TEXTS_LOCK = threading.Lock()


def _plate_texts(out: str, db) -> list:
    """Distinct plate texts, cached per output folder until analysis.db changes."""
    p = os.path.join(out, "analysis.db")
    key = (os.path.abspath(p), os.stat(p).st_mtime_ns)
    with _TEXTS_LOCK:
        hit = _TEXTS.get(key[0])
        if hit and hit[0] == key[1]:
            return hit[1]
    texts = [r[0] for r in db.execute("SELECT DISTINCT text FROM plates WHERE text != ''")]
    with _TEXTS_LOCK:
        _TEXTS[key[0]] = (key[1], texts)
    return texts


_PLATE_COLS = ("id", "event_id", "track", "text", "ocr_text", "confidence", "valid_format", "dt", "camera", "vehicle",
               "crop_path", "enhanced_path", "watchlist", "method")


def _plate_cols(db) -> str:
    """The plate columns to select; a column an older version did not write yet (results opened
    read-only are not migrated) is selected as NULL."""
    have = {r[1] for r in db.execute("PRAGMA table_info(plates)")}
    return ", ".join(c if c in have else f"NULL AS {c}" for c in _PLATE_COLS)


def _plate_public(out: str, p: dict) -> dict:
    """A plate for the browser. ``text``: the plate that counts – the examiner's reading, else the
    confident machine read – or null (not read). ``machine_text``: the confident machine read or
    null; ``ocr_text``: the machine's best guess, also when it was not confident enough to report
    (null when there was none); ``reading``: the examiner's reading ({"text": ""} = examined, not
    readable) or null."""
    rd = p.get("reading")
    return {"event_id": p["event_id"], "track": p["track"], "text": p["text"] or None,
            "machine_text": p["machine_text"] or None, "ocr_text": p.get("ocr_text") or p["machine_text"] or None,
            "confidence": p["confidence"], "valid_format": bool(p["valid_format"]), "time": p["dt"],
            "camera": p["camera"], "vehicle": p["vehicle"], "crop": rel_to(out, p["crop_path"]),
            "enhanced": rel_to(out, p["enhanced_path"]), "watchlist": p["watchlist"] or "",
            "reading": {k: rd[k] for k in ("text", "reviewer", "updated", "note")} if rd else None}


def plates_search(out: str, q: str = "", max_edits: int = 1, limit: int = 100, unread: bool = False) -> list:
    """Plates whose text (the examiner's reading when there is one, else the confident machine
    read) matches `q` (or all); `unread`: the vehicles whose plate nobody could read yet."""
    from ..reviews import effective_plate, plate_readings
    nq = normalize(q)
    rd = plate_readings(out)
    db = _db(out)
    try:
        cols = _plate_cols(db)
        if unread:
            rows = [dict(r) for r in db.execute(f"SELECT {cols} FROM plates WHERE text = '' OR text IS NULL "
                                                "ORDER BY dt DESC LIMIT ?", (int(limit) * 3,))]
        elif nq:
            match = [t for t in _plate_texts(out, db) if nq in t or within_edits(nq, t, max_edits)][:900]
            rows = [dict(r) for r in db.execute(f"SELECT {cols} FROM plates WHERE text IN (" +
                                                ",".join("?" * len(match)) + ")", match)] if match else []
        else:
            rows = [dict(r) for r in db.execute(f"SELECT {cols} FROM plates WHERE text != '' ORDER BY dt DESC "
                                                "LIMIT ?", (int(limit) * 3,))]
        have = {r["id"] for r in rows}
        for (eid, tr), v in rd.items():                    # examiners' readings are searchable too
            if unread or not v["text"] or (nq and not (nq in v["text"] or within_edits(nq, v["text"], max_edits))):
                continue
            rows += [dict(r) for r in db.execute(f"SELECT {cols} FROM plates WHERE event_id=? AND track=?",
                                                 (eid, tr)) if r["id"] not in have]
    finally:
        db.close()
    eff = [effective_plate(r, rd) for r in rows]
    if unread:
        eff = [p for p in eff if not p["text"] and p["reading"] is None]
    else:
        eff = [p for p in eff if p["text"] and (not nq or nq in p["text"] or within_edits(nq, p["text"], max_edits))]
    eff.sort(key=lambda p: p["dt"] or "", reverse=True)
    return [_plate_public(out, p) for p in eff[:int(limit)]]


def set_plate_reading(out: str, event_id: str, track: int, text: Optional[str], note: str = "",
                      reviewer: str = "") -> dict:
    """The examiner's reading of one vehicle's plate (text None = remove it, '' = not readable).
    Audited in the folder's audit log with the machine's read next to it."""
    from ..audit import AuditLog
    from ..reviews import Reviews, effective_plate
    db = _db(out)
    try:
        r = db.execute(f"SELECT {_plate_cols(db)} FROM plates WHERE event_id=? AND track=?",
                       (event_id, int(track))).fetchone()
    finally:
        db.close()
    if r is None:
        raise KeyError("no plate of this vehicle in this event")
    row = dict(r)
    rv = Reviews(out)
    try:
        if text is None:
            rv.clear_plate(event_id, track)
        else:
            rv.set_plate(event_id, track, text, row.get("ocr_text") or row.get("text") or "", reviewer, note)
        readings = rv.plates()
    finally:
        rv.close()
    eff = effective_plate(row, readings)
    AuditLog(os.path.join(out, "audit_log.jsonl")).append(
        "plate_reading" if text is not None else "plate_reading_removed", event_id=event_id, track=int(track),
        examiner_text=eff["text"] if text is not None else None, machine_text=row.get("text") or "",
        machine_guess=row.get("ocr_text") or "", machine_confidence=row.get("confidence"), note=note, by=reviewer)
    return _plate_public(out, eff)


def faces(out: str, limit: int = 300) -> list:
    db = _db(out)
    try:
        rows = db.execute("SELECT identity, MAX(known_name) name, COUNT(DISTINCT event_id) events, MIN(first_dt) first, "
                          "MAX(first_dt) last, GROUP_CONCAT(DISTINCT e.camera) cams, MAX(face_crop) sample "
                          "FROM tracks JOIN events e USING(event_id) WHERE identity IS NOT NULL AND identity != '' "
                          "GROUP BY identity ORDER BY events DESC LIMIT ?", (int(limit),)).fetchall()
        new = {r[0] for r in db.execute("SELECT identity FROM face_alerts WHERE kind='new_face' AND run_id="
                                        "(SELECT run_id FROM runs ORDER BY started DESC LIMIT 1)")}
    finally:
        db.close()
    return [{"identity": r["identity"], "name": r["name"] or "", "events": r["events"], "first": r["first"],
             "last": r["last"], "cameras": sorted(set((r["cams"] or "").split(","))), "sample": rel_to(out, r["sample"]),
             "new": r["identity"] in new} for r in rows]


def appearances(out: str, matches) -> list:
    """Events of each matched identity (search by photo)."""
    db = _db(out)
    try:
        res = []
        for pid, sim in matches[:20]:
            rows = db.execute("SELECT t.event_id, t.first_dt, t.face_crop, e.camera, e.rel_path, t.first_off FROM tracks t "
                              "JOIN events e USING(event_id) WHERE t.identity=? ORDER BY t.first_dt LIMIT 200",
                              (pid,)).fetchall()
            res.append({"identity": pid, "similarity": round(sim, 3), "sample": rel_to(out, rows[0]["face_crop"]) if rows else "",
                        "appearances": [{"event_id": r["event_id"], "time": r["first_dt"], "camera": r["camera"],
                                         "video": r["rel_path"], "offset": r["first_off"]} for r in rows]})
    finally:
        db.close()
    return res

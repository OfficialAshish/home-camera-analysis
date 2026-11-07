"""Cross-camera timeline, suspect sightings, journey and highlight reel for a case.

Every query here spans all sources of a case and works on **calibrated time** (DVR time +
the source's clock offset, see cctv_analysis.cases); the DVR time is always returned as well.

* ``timeline``  – events of all sources in calibrated order (the cross-camera timeline);
* ``sightings`` – every occurrence of one identity: a face (an identity ``P0003`` of one source,
  found in the other sources by face similarity – identity numbers are per source – or an
  enrolled name), a number plate (exact, or within one OCR edit), an appearance (person re-ID
  matches of a chosen track above a score), or a list the officer curated;
* ``journey``   – sightings collapsed into an ordered path of camera stops (arrival, departure,
  travel time to the next stop, map position when the source has one);
* ``highlight_reel`` – one video with only those sightings, each part burnt in with
  source / camera / calibrated time / DVR time, cut from the original recordings.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import sqlite3
import subprocess
from typing import Dict, List, Optional, Sequence

from . import video_io as V
from .cases import Case, calibrate
from .plates import normalize, within_edits
from .storage import rel_to

log = logging.getLogger("cctv")
FMT = "%Y-%m-%d %H:%M:%S"


def _db(out_dir: str) -> Optional[sqlite3.Connection]:
    p = os.path.join(out_dir, "analysis.db")
    if not os.path.exists(p):
        return None
    db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def sources(case: Case) -> List[dict]:
    return [{"source_id": s.source_id, "label": s.label, "out_dir": case.source_dir(s.source_id),
             "offset_for": s.offset_for, "recording": s.recording, "location": s.location} for s in case.sources]


def _best_snapshot(snaps_json) -> str:
    import json
    try:
        snaps = json.loads(snaps_json or "[]")
    except (TypeError, ValueError):
        return ""
    if not snaps:
        return ""
    best = max(snaps, key=lambda s: s.get("persons", 0))
    return best.get("annotated") or best.get("raw") or ""


def timeline(case: Case, day_from: str = "", day_to: str = "", levels: Sequence[str] = (), people_only: bool = False,
             limit: int = 500) -> List[dict]:
    return timeline_page(case, day_from, day_to, levels, people_only, limit)[0]


def timeline_page(case: Case, day_from: str = "", day_to: str = "", levels: Sequence[str] = (),
                  people_only: bool = False, limit: int = 500):
    """(the first `limit` events in calibrated order, how many events match in total)."""
    out = []
    for s in sources(case):
        db = _db(s["out_dir"])
        if db is None:
            continue
        try:
            q = ("SELECT event_id, camera, start_dt, end_dt, duration, kind, level, score, n_person_tracks, "
                 "plates, identities, snapshots, time_source, rel_path, start_off FROM events")
            conds, args = [], []
            if levels:
                conds.append(f"level IN ({','.join('?' * len(levels))})")
                args += list(levels)
            if people_only:
                conds.append("n_person_tracks > 0")
            rows = db.execute(q + (" WHERE " + " AND ".join(conds) if conds else ""), args).fetchall()
        finally:
            db.close()
        for r in rows:
            off = s["offset_for"](r["camera"])
            cal = calibrate(r["start_dt"], off)
            day = (cal or "")[:10]
            if day_from and (not day or day < day_from) or day_to and (not day or day > day_to):
                continue
            out.append({"source_id": s["source_id"], "source_label": s["label"], "event_id": r["event_id"],
                        "camera": r["camera"], "dvr_time": r["start_dt"], "calibrated_time": cal,
                        "calibrated_end": calibrate(r["end_dt"], off), "offset_seconds": off,
                        "duration": r["duration"], "kind": r["kind"], "level": r["level"], "score": r["score"],
                        "persons": r["n_person_tracks"], "time_source": r["time_source"],
                        "snapshot": rel_to(s["out_dir"], _best_snapshot(r["snapshots"])),
                        "rel_path": r["rel_path"], "start_off": r["start_off"]})
    out.sort(key=lambda e: (e["calibrated_time"] or "9999", e["source_id"], e["event_id"]))
    return out[:limit], len(out)


def face_identities(srcs: List[dict], value: str, query: Optional[dict] = None,
                    threshold: Optional[float] = None) -> Dict[str, Dict[str, float]]:
    """{source_id: {identity: similarity}} of the person `value` in every source.

    Face identities (P0001 …) are numbered per analysed folder: P0003 of one DVR and P0003 of
    another are unrelated people. So the person is taken from the source the officer picked it
    in (``query["source_id"]``) and found in the other sources by face similarity (SFace cosine
    ≥ ``face_match_threshold``) against their identities. Enrolled names (known-faces folder)
    are the same everywhere and need no source."""
    import json

    import numpy as np

    from .config import Config
    thr = Config().face_match_threshold if threshold is None else threshold

    def load(out_dir):
        p = os.path.join(out_dir, "identities")
        try:
            with open(p + ".json", encoding="utf-8") as fh:
                ids = json.load(fh)
            emb = np.load(p + ".npy").astype(np.float32)
        except (OSError, ValueError):
            return [], None
        return (ids, emb) if len(ids) == len(emb) and len(ids) else ([], None)

    sid = (query or {}).get("source_id", "")
    home = next((s for s in srcs if s["source_id"] == sid), None)
    if home is None:
        raise ValueError(f"{value} is a face identity of one source: say which source it is from (source_id)")
    ids, emb = load(home["out_dir"])
    i = next((k for k, r in enumerate(ids) if r.get("id") == value), None)
    if i is None or emb is None:
        raise KeyError(f"no face identity {value} in source {sid}")
    q = emb[i] / (np.linalg.norm(emb[i]) + 1e-9)
    out: Dict[str, Dict[str, float]] = {sid: {value: 1.0}}
    for s in srcs:
        if s is home:
            continue
        ids2, emb2 = load(s["out_dir"])
        if emb2 is None:
            continue
        sims = (emb2 / (np.linalg.norm(emb2, axis=1, keepdims=True) + 1e-9)) @ q
        hit = {ids2[j]["id"]: round(float(sims[j]), 3) for j in np.nonzero(sims >= thr)[0]}
        if hit:
            out[s["source_id"]] = hit
    return out


def sightings(case: Case, kind: str, value: str = "", query: Optional[dict] = None, min_score: float = 0.7,
              fuzzy: bool = False, limit: int = 200) -> List[dict]:
    """All sightings of one identity across the case, in calibrated time order."""
    if kind not in ("face", "plate", "appearance", "list"):
        raise ValueError("kind must be face, plate, appearance or list")
    pts: List[dict] = []
    srcs = sources(case)
    if kind == "appearance":
        from . import reid
        q = query or {}
        src = next((s for s in srcs if s["source_id"] == q.get("source_id")), None)
        if src is None:
            raise ValueError("appearance search needs source_id, event_id and track of a person")
        qv = reid.query_vector(src["out_dir"], q.get("event_id", ""), int(q.get("track", -1)))
        if qv is None:
            raise KeyError("that person has no appearance vector yet – run the case (or index the source) first")
        matches = reid.search(srcs, qv["hand"], qv["deep"], qv["model"], min_score, limit, colors=q.get("colors"))
        if not any((m["source_id"], m["event_id"], int(m["track"])) ==
                   (src["source_id"], q["event_id"], int(q["track"])) for m in matches):
            matches.append({**qv["meta"], "source_id": src["source_id"], "score": 1.0,
                            "dvr_time": qv["meta"].get("first_dt"), "method": "query",
                            "calibrated_time": calibrate(qv["meta"].get("first_dt"),
                                                         src["offset_for"](qv["meta"].get("camera")))})
        for m in matches:
            s = next(x for x in srcs if x["source_id"] == m["source_id"])
            pts.append(_point(s, m["event_id"], int(m["track"]), m.get("camera"), m["dvr_time"], m.get("first_off"),
                              m.get("duration"), m.get("person_crop"), m.get("face_crop"), "", m["score"],
                              m.get("rel_path"), m.get("start_off")))
    elif kind == "list":
        items = (query or {}).get("items") or []
        if not isinstance(items, list) or len(items) > 200:
            raise ValueError("items: a list of at most 200 {source_id, event_id, track?}")
        for it in items:
            s = next((x for x in srcs if x["source_id"] == it.get("source_id")), None)
            if s is None:
                raise ValueError(f"unknown source {it.get('source_id')!r}")
            pts += _from_event(s, str(it.get("event_id", "")), it.get("track"))
    else:
        v = (value or "").strip()
        if not v:
            raise ValueError("a face identity or plate text is required")
        by_name = kind == "face" and not re.fullmatch(r"P\d{4,}", v)        # an enrolled name, not P0001
        matched = face_identities(srcs, v, query) if kind == "face" and not by_name else {}
        for s in srcs:
            db = _db(s["out_dir"])
            if db is None:
                continue
            try:
                if kind == "face":
                    sql = ("SELECT t.event_id, t.track, t.first_dt, t.first_off, t.duration, t.person_crop, t.face_crop, "
                           "t.identity, e.camera, e.rel_path, e.start_off FROM tracks t JOIN events e USING(event_id) ")
                    if by_name:                      # enrolled (known faces) or a reference person (watchlist)
                        wl = "watchlist" in {c[1] for c in db.execute("PRAGMA table_info(tracks)")}
                        rows = db.execute(sql + ("WHERE t.known_name=? OR t.watchlist=?" if wl else
                                                 "WHERE t.known_name=?"), (v, v) if wl else (v,)).fetchall()
                        sims: Dict[str, float] = {}
                    else:
                        sims = matched.get(s["source_id"], {})
                        rows = db.execute(sql + f"WHERE t.identity IN ({','.join('?' * len(sims))})",
                                          list(sims)).fetchall() if sims else []
                    for r in rows:
                        pts.append(_point(s, r["event_id"], r["track"], r["camera"], r["first_dt"], r["first_off"],
                                          r["duration"], r["person_crop"], r["face_crop"], "",
                                          sims.get(r["identity"]), r["rel_path"], r["start_off"]))
                else:
                    from .reviews import effective_plate, plate_readings
                    norm = normalize(v)
                    readings = plate_readings(s["out_dir"])         # an examiner's reading counts over the machine's
                    rows = [effective_plate(dict(x), readings) for x in db.execute(
                        "SELECT p.event_id, p.track, p.dt, p.t_off, p.text, p.crop_path, p.confidence, e.camera, "
                        "e.rel_path, e.start_off FROM plates p JOIN events e USING(event_id)")]
                    for r in rows:
                        if r["text"] and (r["text"] == norm or (fuzzy and within_edits(r["text"], norm, 1))):
                            pts.append(_point(s, r["event_id"], r["track"], r["camera"], r["dt"], r["t_off"], 2.0,
                                              r["crop_path"], "", r["text"], r["confidence"], r["rel_path"],
                                              r["start_off"]))
            finally:
                db.close()
    pts.sort(key=lambda p: (p["calibrated_time"] or "9999", p["source_id"], p["event_id"]))
    return pts[:limit]


def _from_event(s: dict, event_id: str, track) -> List[dict]:
    db = _db(s["out_dir"])
    if db is None:
        return []
    try:
        e = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        if e is None:
            raise KeyError(f"no event {event_id} in source {s['source_id']}")
        if track not in (None, ""):
            t = db.execute("SELECT * FROM tracks WHERE event_id=? AND track=?", (event_id, int(track))).fetchone()
            if t is None:
                raise KeyError(f"no track {track} in event {event_id}")
            return [_point(s, event_id, t["track"], e["camera"], t["first_dt"], t["first_off"], t["duration"],
                           t["person_crop"], t["face_crop"], "", None, e["rel_path"], e["start_off"])]
        return [_point(s, event_id, None, e["camera"], e["start_dt"], e["start_off"], e["duration"],
                       _best_snapshot(e["snapshots"]), "", "", None, e["rel_path"], e["start_off"])]
    finally:
        db.close()


def _point(s, event_id, track, camera, dvr_time, t_off, duration, crop, face, plate, score, rel_path, ev_off):
    off = s["offset_for"](camera)
    return {"source_id": s["source_id"], "source_label": s["label"], "camera": camera, "event_id": event_id,
            "track": track, "dvr_time": dvr_time, "calibrated_time": calibrate(dvr_time, off), "offset_seconds": off,
            "t_off": float(t_off or 0), "duration": float(duration or 0), "thumb": rel_to(s["out_dir"], crop),
            "face": rel_to(s["out_dir"], face), "plate": plate, "score": score, "rel_path": rel_path,
            "video_path": s["recording"](rel_path) if rel_path else "", "location": s["location"]}


def journey(points: List[dict], gap: float = 120.0) -> dict:
    """Ordered camera stops: consecutive sightings on the same camera within `gap` seconds merge."""
    stops: List[dict] = []
    for p in points:
        if not p["calibrated_time"]:
            continue
        t = dt.datetime.strptime(p["calibrated_time"], FMT)
        end = t + dt.timedelta(seconds=p["duration"] or 0)
        last = stops[-1] if stops else None
        if last and last["source_id"] == p["source_id"] and last["camera"] == p["camera"] and \
                (t - last["_end"]).total_seconds() <= gap:
            last["_end"] = max(last["_end"], end)
            last["sightings"] += 1
            last["departure"] = last["_end"].strftime(FMT)
            continue
        stops.append({"n": len(stops) + 1, "source_id": p["source_id"], "source_label": p["source_label"],
                      "camera": p["camera"], "arrival": p["calibrated_time"], "departure": end.strftime(FMT),
                      "dvr_time": p["dvr_time"], "thumb": p["thumb"], "face": p["face"], "plate": p["plate"],
                      "event_id": p["event_id"], "track": p["track"], "location": p["location"], "sightings": 1,
                      "_end": end})
    for a, b in zip(stops, stops[1:]):
        a["to_next_seconds"] = round((dt.datetime.strptime(b["arrival"], FMT) - a["_end"]).total_seconds())
    for s in stops:
        s.pop("_end")
    located = [s for s in stops if s["location"].get("lat") is not None or s["location"].get("x") is not None]
    return {"stops": stops, "unordered": [p for p in points if not p["calibrated_time"]],
            "mapped": len(located), "summary": " → ".join(f"{s['source_label']}/{s['camera']} @ {s['arrival'][11:]}"
                                                         for s in stops)}


def _dt(text: Optional[str]) -> Optional[dt.datetime]:
    try:
        return dt.datetime.strptime(text, FMT) if text else None
    except ValueError:
        return None


def _at_zero(points: List[dict], key: str) -> Optional[dt.datetime]:
    """The time at video offset 0 of one recording, from the first sighting that has the time."""
    for p in points:
        d = _dt(p.get(key))
        if d is not None:
            return d - dt.timedelta(seconds=float(p.get("t_off") or 0))
    return None


def reel_segments(points: List[dict], pre: float = 2.0, post: float = 4.0, gap: float = 1.0) -> List[dict]:
    """Non-overlapping pieces of footage that together cover every sighting, in calibrated order.

    Several sightings often share the same seconds of one recording – several people of one
    event, one person split into several tracks, a curated list with overlapping events. Each
    sighting's window (``t_off - pre`` … ``t_off + duration + post``) is merged with the windows of
    the same recording it overlaps (or nearly touches, ``gap``), so no second of footage is shown
    twice. Times are anchored on the recording: DVR time at video offset 0 = the sighting's DVR
    time − its offset; the calibrated time likewise."""
    items = []
    for p in points:
        vp = p.get("video_path")
        if not vp or not os.path.exists(vp):
            continue
        s = max(0.0, float(p.get("t_off") or 0) - pre)
        e = float(p.get("t_off") or 0) + max(0.0, float(p.get("duration") or 0)) + post
        items.append((vp, s, max(e, s + 1.0), p))
    items.sort(key=lambda x: (x[0], x[1]))
    segs: List[dict] = []
    for vp, s, e, p in items:
        last = segs[-1] if segs and segs[-1]["video_path"] == vp else None
        if last is not None and s <= last["end"] + gap:
            last["end"] = max(last["end"], e)
            last["points"].append(p)
            continue
        segs.append({"video_path": vp, "start": s, "end": e, "points": [p]})
    for g in segs:
        p0 = g["points"][0]
        g["cal_at_zero"] = _at_zero(g["points"], "calibrated_time")      # calibrated time at video offset 0
        g["dvr_at_zero"] = _at_zero(g["points"], "dvr_time")
        g["calibrated_start"] = (g["cal_at_zero"] + dt.timedelta(seconds=g["start"])).strftime(FMT) \
            if g["cal_at_zero"] else None
        for k in ("source_id", "source_label", "camera", "rel_path", "offset_seconds"):
            g[k] = p0.get(k)
    segs.sort(key=lambda g: (g["calibrated_start"] or "9999", g["source_id"] or "", g["start"]))
    return segs


def highlight_reel(points: List[dict], dest: str, pre: float = 2.0, post: float = 4.0, width: int = 960,
                   fps: int = 12) -> dict:
    """One MP4 of the sightings in calibrated order, every second of footage once
    (``reel_segments``); every frame carries source / camera / calibrated time / DVR time."""
    import cv2
    import numpy as np
    segs = reel_segments(points, pre, post)
    if not segs:
        raise ValueError("no sighting has an available source video")
    W = width // 2 * 2
    H = int(round(W * 9 / 16)) // 2 * 2
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    part = dest + ".part.mp4"
    proc = subprocess.Popen([V.FFMPEG, "-nostdin", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                             "-s", f"{W}x{H}", "-r", str(fps), "-i", "pipe:0", *V.h264_args(23),
                             "-movflags", "+faststart", part], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    sc = W / 1100

    def bar(img, text, y, scale, color):
        (tw, th), bl = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
        cv2.rectangle(img, (4, y - th - 8), (12 + tw, y + bl), (0, 0, 0), -1)
        cv2.putText(img, text, (8, y - 4), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 2, cv2.LINE_AA)

    index, frames = [], 0
    try:
        for n, g in enumerate(segs, 1):
            meta = V.probe(g["video_path"])
            index.append({"n": n, "reel_start": round(frames / fps, 2), "source_id": g["source_id"],
                          "camera": g["camera"], "rel_path": g["rel_path"], "video_start": round(g["start"], 2),
                          "video_end": round(g["end"], 2), "calibrated_time": g["calibrated_start"],
                          "sightings": [{"event_id": p["event_id"], "track": p.get("track")} for p in g["points"]]})
            for t, fr in V.iter_frames(g["video_path"], fps, W, g["start"], g["end"] - g["start"],
                                       src_size=(meta["width"], meta["height"])):
                h, w = fr.shape[:2]
                s = min(W / w, H / h)
                img = np.zeros((H, W, 3), np.uint8)
                rs = cv2.resize(fr, (max(2, int(w * s)), max(2, int(h * s))), interpolation=cv2.INTER_AREA)
                y0, x0 = (H - rs.shape[0]) // 2, (W - rs.shape[1]) // 2
                img[y0:y0 + rs.shape[0], x0:x0 + rs.shape[1]] = rs
                cal = (g["cal_at_zero"] + dt.timedelta(seconds=t)).strftime(FMT) if g["cal_at_zero"] else "time unknown"
                dvr = (g["dvr_at_zero"] + dt.timedelta(seconds=t)).strftime(FMT) if g["dvr_at_zero"] else ""
                bar(img, f"{n}/{len(segs)}  {g['source_label']} / {g['camera']}   {cal}", int(34 * sc) + 8,
                    0.85 * sc, (0, 255, 255))
                bar(img, f"DVR clock {dvr}  ({float(g['offset_seconds'] or 0):+.0f} s)  {g['rel_path']} @ "
                         f"{V.fmt_offset(t)}", H - 6, 0.5 * sc, (255, 255, 255))
                proc.stdin.write(img.tobytes())
                frames += 1
        proc.stdin.close()
        proc.wait(timeout=1800)
    except Exception:
        proc.kill()
        if os.path.exists(part):
            os.remove(part)
        raise
    if proc.returncode != 0 or frames == 0:
        raise RuntimeError("the reel could not be encoded")
    os.replace(part, dest)
    return {"path": dest, "parts": len(segs), "sightings": sum(len(g["points"]) for g in segs),
            "seconds": round(frames / fps, 1), "index": index}


__all__ = ["face_identities", "highlight_reel", "reel_segments", "journey", "sightings", "timeline", "timeline_page"]

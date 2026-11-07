"""Case endpoints of the desktop app (docs/CASES.md, docs/APP-API.md § Cases).

Cases, their seized sources and device particulars, case runs, the cross-camera timeline,
sightings and journey, appearance / clothing-colour / natural-language search, the highlight
reel, the Forensic Studio and the Section 63 court bundle. ``register`` adds the routes to the
app created by ``server.create_app``; authentication, host check and rate limits are the
server's ``before_request`` guard (``is_media`` tells it which GET routes a cookie may load).

Responses carry what the screens show and nothing else: paths are relative to the case folder
(served by ``/api/cases/<id>/file``), never absolute paths of the computer.
"""
from __future__ import annotations

import base64
import collections
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import threading
import time
from typing import Callable, Dict, List, Optional

from flask import Response, jsonify, request, send_file

from .. import licensing as LIC
from ..cases import CASE_FIELDS, Case, CaseStore, Source, calibrate, format_offset, parse_offset
from ..config import VIDEO_EXTENSIONS, Config
from . import features
from .common import ApiError, abs_dir, inside

log = logging.getLogger("cctv")
MEDIA = re.compile(r"^/api/cases/[A-Za-z0-9_-]+/(file|video|frame|certificate|exports/[A-Za-z0-9_.-]+)$")
PLAYABLE = {".mp4", ".m4v", ".mov", ".webm"}
BUNDLE_NAME = re.compile(r"^bundle_[A-Za-z0-9_-]+\.zip$")
REEL_NAME = re.compile(r"^reel_[A-Za-z0-9_-]+\.mp4$")


def is_media(path: str) -> bool:
    """GET routes an <img>/<video>/<a download> may load with the session cookie alone."""
    return bool(MEDIA.match(path))


def _text(v, field: str, n: int = 200) -> str:
    if v is None:
        return ""
    if not isinstance(v, str) or len(v) > n:
        raise ApiError(400, f"{field} must be text (at most {n} characters)")
    return v.strip()


def _num(v, field: str, lo: float, hi: float, default: float) -> float:
    if v in (None, ""):
        return default
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ApiError(400, f"{field} must be a number")
    if not lo <= x <= hi:
        raise ApiError(400, f"{field} must be between {lo:g} and {hi:g}")
    return x


def _db(out_dir: str) -> Optional[sqlite3.Connection]:
    p = os.path.join(out_dir, "analysis.db")
    if not os.path.exists(p):
        return None
    db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def source_status(case: Case, sid: str) -> dict:
    """What the Cases screen shows about one source: ingested, analysed, searchable."""
    from ..ingest import load_ingest
    out = case.source_dir(sid)
    src = case.source(sid)
    rec = load_ingest(out) or {}
    st = {"available": os.path.isdir(src.path), "files": len(rec.get("files", [])),
          "playback_copies": sum(1 for f in rec.get("files", []) if f.get("playback")),
          "ingested_at": rec.get("ingested_at", ""), "merkle_root": rec.get("merkle_root", ""),
          "analysed": False, "events": 0, "cameras": [], "persons": 0,
          "text_search": os.path.exists(os.path.join(out, "semantic_index.npz"))}
    db = _db(out)
    if db is not None:
        try:
            st["analysed"] = True
            st["events"] = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            st["cameras"] = [r[0] for r in db.execute("SELECT DISTINCT camera FROM events ORDER BY camera")]
        except sqlite3.Error:
            pass
        finally:
            db.close()
    idx = os.path.join(out, "appearance_index.npz")
    if os.path.exists(idx):
        try:
            import numpy as np
            with np.load(idx, allow_pickle=False) as z:
                st["persons"] = int(z["keys"].shape[0])
        except Exception:
            pass
    return st


def case_view(case: Case) -> dict:
    d = case.to_dict()
    for s, sd in zip(case.sources, d["sources"]):
        sd["clock_offset_text"] = format_offset(s.clock_offset)
        sd["camera_offsets_text"] = {k: format_offset(v) for k, v in s.camera_offsets.items()}
        sd["status"] = source_status(case, s.source_id)
    return d


def _case_rel(sid: str, rel: str) -> str:
    """A path relative to a source's output folder -> relative to the case folder."""
    return f"sources/{sid}/{rel}" if rel else ""


def _public_point(p: dict) -> dict:
    """A sighting/journey point as the screens need it (no absolute paths)."""
    out = {k: p.get(k) for k in ("source_id", "source_label", "camera", "event_id", "track", "dvr_time",
                                 "calibrated_time", "offset_seconds", "t_off", "duration", "plate", "rel_path",
                                 "location")}
    out["thumb"] = _case_rel(p["source_id"], p.get("thumb", ""))
    out["face"] = _case_rel(p["source_id"], p.get("face", ""))
    if p.get("score") is not None:
        out["score"] = round(float(p["score"]), 2)
    return out


def locate_folder(name: str, files: List[dict], roots: List[str], max_dirs: int = 20000,
                  seconds: float = 6.0) -> List[str]:
    """Absolute paths of folders called `name` that contain the dropped files (same relative
    path and size). A browser never reveals where a dropped folder is; this finds it by content."""
    deadline = time.monotonic() + seconds
    want = [(f["rel"], int(f["size"])) for f in files[:8]]
    hits: List[str] = []
    seen = set()
    queue = collections.deque((os.path.abspath(r), 0) for r in roots if r and os.path.isdir(r))
    visited = 0
    while queue and visited < max_dirs and time.monotonic() < deadline:
        d, depth = queue.popleft()
        real = os.path.realpath(d)
        if real in seen:
            continue
        seen.add(real)
        visited += 1
        if os.path.basename(d.rstrip("\\/")) == name and want and all(
                os.path.isfile(os.path.join(d, rel)) and os.path.getsize(os.path.join(d, rel)) == size
                for rel, size in want):
            hits.append(d)
            if len(hits) >= 5:
                break
            continue
        if depth >= 4:
            continue
        try:
            with os.scandir(d) as it:
                for de in it:
                    if de.name.startswith((".", "$")) or de.name in ("node_modules", "Windows", "Program Files",
                                                                       "Program Files (x86)", "AppData", "proc", "sys"):
                        continue
                    try:
                        if de.is_dir(follow_symlinks=False):
                            queue.append((de.path, depth + 1))
                    except OSError:
                        continue
        except OSError:
            continue
    return hits


def search_roots(recent: List[str]) -> List[str]:
    import string
    import sys
    home = os.path.expanduser("~")
    roots = [os.path.dirname(p) for p in recent if p] + [os.path.join(home, x) for x in
                                                          ("Desktop", "Downloads", "Documents", "Videos")] + [home]
    if sys.platform.startswith("win"):
        roots += [f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]
    else:
        roots += [p for p in ("/media", "/mnt", "/Volumes", "/run/media") if os.path.isdir(p)]
    out = []
    for r in roots:
        if r and os.path.isdir(r) and r not in out:
            out.append(r)
    return out


class _SemanticModels:
    """SigLIP text encoders for natural-language search, loaded once per model name."""

    def __init__(self, models_dir: str):
        self.models_dir = models_dir
        self.lock = threading.Lock()
        self.cache: Dict[str, object] = {}

    def get(self, name: str):
        with self.lock:
            if name not in self.cache:
                from .. import modelcache
                from ..semantic import Semantic
                modelcache.init(self.models_dir)
                self.cache[name] = Semantic(Config(semantic_model=name, model_cache_dir=self.models_dir), "cpu")
            return self.cache[name]


def register(app, *, jobs, settings, lic_status: Callable, body: Callable[[], dict], data_dir: str) -> None:
    models_dir = os.path.join(data_dir, "models")
    locks: Dict[str, threading.Lock] = collections.defaultdict(threading.Lock)
    frames_cache: "collections.OrderedDict[tuple, list]" = collections.OrderedDict()
    frames_lock = threading.Lock()
    sem_models = _SemanticModels(models_dir)

    def cases_root() -> str:
        return settings.load().get("cases_root") or os.path.join(data_dir, "cases")

    def store() -> CaseStore:
        return CaseStore(cases_root())

    def user() -> str:
        return settings.load().get("operator", "")

    def open_case(cid: str, proxies: bool = False) -> Case:
        try:
            c = store().get(cid)
        except KeyError:
            raise ApiError(404, "no such case")
        if proxies:
            from ..investigation import register_proxies
            for s in c.sources:
                register_proxies(s.path, c.source_dir(s.source_id))
        return c

    def source_of(c: Case, sid) -> Source:
        try:
            return c.source(str(sid or ""))
        except KeyError:
            raise ApiError(404, "no such source in this case")

    def event_row(c: Case, sid: str, eid: str) -> dict:
        db = _db(c.source_dir(sid))
        if db is None:
            raise ApiError(409, "this source has not been analysed yet – run the case first")
        try:
            r = db.execute("SELECT * FROM events WHERE event_id=?", (str(eid or ""),)).fetchone()
        finally:
            db.close()
        if r is None:
            raise ApiError(404, "no such event in this source")
        return dict(r)

    # ------------------------------------------------------------ cases
    @app.get("/api/cases")
    def cases_list():
        return jsonify(root=cases_root(), cases=store().list())

    @app.post("/api/cases")
    def case_create():
        d = body()
        cid = _text(d.get("case_id"), "case_id", 64) or dt.datetime.now().strftime("CASE-%Y%m%d-%H%M%S")
        values = {k: _text(d.get(k), k, 2000) for k in CASE_FIELDS if d.get(k)}
        try:
            c = store().create(cid, user=user(), **values)
            if d.get("expert"):
                c.set_expert(d["expert"], user())
        except FileExistsError as e:
            raise ApiError(409, str(e))
        except ValueError as e:
            raise ApiError(400, str(e))
        return jsonify(case=case_view(c)), 201

    @app.get("/api/cases/<cid>")
    def case_get(cid):
        return jsonify(case=case_view(open_case(cid)))

    @app.put("/api/cases/<cid>")
    def case_put(cid):
        d = body()
        with locks[cid]:
            c = open_case(cid)
            try:
                if "expert" in d:
                    c.set_expert(d.pop("expert"), user())
                c.update(user=user(), **d)
            except ValueError as e:
                raise ApiError(400, str(e))
        return jsonify(case=case_view(c))

    @app.post("/api/cases/<cid>/sources")
    def source_add(cid):
        d = body()
        path = abs_dir(d.get("path", ""), "path")
        with locks[cid]:
            c = open_case(cid)
            try:
                s = c.add_source(path, _text(d.get("label"), "label", 80), d.get("clock_offset") or "0", user=user(),
                                 device=d.get("device"))
                if d.get("declarant"):
                    c.update_source(s.source_id, user=user(), declarant=d["declarant"])
            except ValueError as e:
                raise ApiError(400, str(e))
        return jsonify(case=case_view(c), source_id=s.source_id), 201

    @app.put("/api/cases/<cid>/sources/<sid>")
    def source_put(cid, sid):
        d = body()
        allowed = {"label", "clock_offset", "camera_offsets", "device", "location", "declarant"}
        bad = set(d) - allowed
        if bad:
            raise ApiError(400, f"unknown field(s): {sorted(bad)}")
        with locks[cid]:
            c = open_case(cid)
            source_of(c, sid)
            try:
                c.update_source(sid, user=user(), **d)
            except ValueError as e:
                raise ApiError(400, str(e))
        return jsonify(case=case_view(c))

    @app.post("/api/cases/<cid>/sources/<sid>/relink")
    def source_relink(cid, sid):
        """The seized folder is somewhere else now (another drive letter): verify it holds the
        acquired recordings (SHA-256 of every file, or sizes only) and point the source at it."""
        d = body()
        path = abs_dir(d.get("path", ""), "path")
        full = d.get("verify", "sha256") != "size"
        c = open_case(cid)
        source_of(c, sid)

        def run(job):
            with locks[cid]:
                cc = open_case(cid)
                try:
                    return cc.relink_source(sid, path, user(), full_hash=full, progress=job.update)
                except ValueError as e:
                    raise RuntimeError(str(e))
        job, _ = jobs.start("ingest", run)
        return jsonify(job=job.public()), 202

    @app.delete("/api/cases/<cid>/sources/<sid>")
    def source_delete(cid, sid):
        with locks[cid]:
            c = open_case(cid)
            source_of(c, sid)
            c.remove_source(sid, user=user())
        return jsonify(case=case_view(c))

    # ------------------------------------------------------------ runs
    def run_request(d: dict) -> dict:
        """Preset, options, source selection and request id of a run, validated before anything
        is created or started."""
        from ..investigation import run_options
        preset = d.get("preset") or "balanced"
        options = d.get("options") or {}
        if not isinstance(options, dict):
            raise ApiError(400, "options must be an object")
        options = {**features.APP_DEFAULTS, **options}
        options.setdefault("model_cache_dir", models_dir)
        try:
            run_options(options, preset)
        except (TypeError, ValueError) as e:
            raise ApiError(400, str(e))
        sel = d.get("sources")
        if sel is not None and (not isinstance(sel, list) or not sel or not all(isinstance(x, str) for x in sel)):
            raise ApiError(400, "sources must be a list of source ids")
        rid = d.get("request_id", "")
        if not isinstance(rid, str) or len(rid) > 64:
            raise ApiError(400, "request_id must be a short string")
        return {"preset": preset, "options": options, "sources": sel, "request_id": rid}

    def start_run(c: Case, req: dict):
        from ..investigation import run_case
        if not c.sources:
            raise ApiError(400, "this case has no sources – add the seized folders first")
        known = {s.source_id for s in c.sources}
        sel, options, preset, rid = req["sources"], req["options"], req["preset"], req["request_id"]
        if sel is not None and not set(sel) <= known:
            raise ApiError(400, f"sources must be ids of this case's sources: {sorted(known)}")
        lic = lic_status()
        key = settings.secret("vlm_api_key")
        if options.get("use_vlm") and options.get("vlm_provider") == "openai" and key:
            os.environ[Config().vlm_api_key_env] = key

        def run(job):
            res = run_case(c, options, preset, job.update, job.cancel, work_root=os.path.join(data_dir, "work"),
                           model_cache_dir=models_dir, sources=sel,
                           max_videos_total=0 if lic.valid else LIC.TRIAL_MAX_VIDEOS)
            failed = [k for k, v in res["sources"].items() if v.get("error")]
            if failed and len(failed) == len(res["sources"]):
                raise RuntimeError("; ".join(res["sources"][k]["error"] for k in failed))
            return {"case_id": c.case_id, "cancelled": res["cancelled"], "trial": not lic.valid,
                    "sources": {k: {kk: vv for kk, vv in v.items() if kk in ("label", "files", "remuxed", "appearances",
                                                                               "text_index_added", "error", "skipped")}
                                | {"videos_ok": (v.get("stats") or {}).get("videos_ok", 0),
                                   "events": (v.get("stats") or {}).get("events", 0)}
                                for k, v in res["sources"].items()}}
        job, created = jobs.start("analysis", run, request_id=rid, exclusive=True)
        if not created and job.request_id != rid:
            raise ApiError(409, "an analysis is already running", job=job.public())
        return job

    @app.post("/api/cases/<cid>/run")
    def case_run(cid):
        req = run_request(body())
        job = start_run(open_case(cid), req)
        return jsonify(job=job.public()), 202

    @app.post("/api/cases/quick")
    def case_quick():
        """Quick Investigation: folders (+ optional details) -> a new case, its sources, a run."""
        d = body()
        folders = d.get("folders")
        if not isinstance(folders, list) or not 1 <= len(folders) <= 50:
            raise ApiError(400, "folders: a list of 1-50 folders")
        specs = []
        for i, f in enumerate(folders, 1):
            if isinstance(f, str):
                f = {"path": f}
            if not isinstance(f, dict):
                raise ApiError(400, f"folder {i}: path required")
            specs.append({"path": abs_dir(f.get("path", ""), f"folder {i}"), "label": _text(f.get("label"), "label", 80),
                          "clock_offset": f.get("clock_offset") or "0"})
            try:
                parse_offset(specs[-1]["clock_offset"])
            except ValueError as e:
                raise ApiError(400, f"folder {i}: {e}")
        if len({os.path.realpath(s["path"]) for s in specs}) != len(specs):
            raise ApiError(400, "the same folder was given twice")
        req = run_request(d)
        cid = _text(d.get("case_id"), "case_id", 64) or dt.datetime.now().strftime("CASE-%Y%m%d-%H%M%S")
        values = {k: _text(d.get(k), k, 2000) for k in CASE_FIELDS if d.get(k)}
        try:
            c = store().create(cid, user=user(), **values)
            for s in specs:
                c.add_source(s["path"], s["label"], s["clock_offset"], user=user())
        except FileExistsError as e:
            raise ApiError(409, str(e))
        except ValueError as e:
            raise ApiError(400, str(e))
        for s in specs:
            settings.remember("recent_inputs", s["path"])
        job = start_run(c, req)
        return jsonify(case=case_view(c), job=job.public()), 202

    # ------------------------------------------------------------ media
    @app.get("/api/cases/<cid>/file")
    def case_file(cid):
        c = open_case(cid)
        full = inside(c.root, request.args.get("p", ""))
        return send_file(full, conditional=True, max_age=3600)

    @app.get("/api/cases/<cid>/video")
    def case_video(cid):
        """A recording for the browser player: its lossless playback copy, else the original
        when the browser can play it. Only files listed in the source's acquisition record."""
        from ..ingest import load_ingest
        c = open_case(cid)
        s = source_of(c, request.args.get("source"))
        rel = request.args.get("rel", "").replace("\\", "/")       # events store the OS separator, ingest uses '/'
        rec = load_ingest(c.source_dir(s.source_id)) or {}
        f = next((x for x in rec.get("files", []) if x["rel"] == rel), None)
        if f is None:
            raise ApiError(404, "not a recording of this source (ingest it first)")
        if f.get("playback"):
            return send_file(inside(c.source_dir(s.source_id), f["playback"]), conditional=True, max_age=3600,
                             mimetype="video/mp4")
        if os.path.splitext(rel)[1].lower() not in PLAYABLE:
            raise ApiError(415, "the browser cannot play this recording and no playback copy could be made – "
                                "open the original in VLC")
        return send_file(inside(s.path, rel), conditional=True, max_age=3600)

    def load_frames(c: Case, sid: str, eid: str, t: Optional[float], n: int, fresh: bool = False):
        from .. import video_io as V
        e = event_row(c, sid, eid)
        if t is None:
            t = float(e["start_off"] or 0)
        video = c.source(sid).recording(e["rel_path"])
        if not os.path.exists(video):
            raise ApiError(409, f"the recording is not available: {e['rel_path']} (is the drive plugged in? if it has "
                                "another drive letter now, relink the source)")
        key = (c.case_id, sid, eid, round(t, 3), n)
        with frames_lock:
            if key in frames_cache and not fresh:
                frames_cache.move_to_end(key)
                return e, t, frames_cache[key]
        meta = V.probe(video)
        size = (meta["width"], meta["height"])
        if n == 1:
            fr = V.read_frame_at(video, t, 0, size)
            frames = [fr] if fr is not None else []
        else:
            fps = meta["fps"] or 25.0
            frames = V.read_frames(video, max(0.0, t - (n // 2) / fps), n, size)
        if not frames:
            raise ApiError(422, "no picture could be decoded at this time")
        with frames_lock:
            frames_cache[key] = frames
            while len(frames_cache) > 6:
                frames_cache.popitem(last=False)
        return e, t, frames

    @app.get("/api/cases/<cid>/frame")
    def case_frame(cid):
        """The original frame (full resolution, lossless PNG) at `t` seconds into the event's video."""
        from ..enhance import png_bytes
        a = request.args
        c = open_case(cid, proxies=True)
        s = source_of(c, a.get("source"))
        t = _num(a.get("t"), "t", 0, 1e6, -1)
        _, _, frames = load_frames(c, s.source_id, a.get("event", ""), None if t < 0 else t, 1)
        return Response(png_bytes(frames[0]), mimetype="image/png", headers={"Cache-Control": "max-age=600"})

    # ------------------------------------------------------------ investigation views
    @app.get("/api/cases/<cid>/timeline")
    def case_timeline(cid):
        from .. import journey as J
        a = request.args
        for k in ("day_from", "day_to"):
            if a.get(k) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a[k]):
                raise ApiError(400, f"{k} must be YYYY-MM-DD")
        from ..rules import LEVELS
        levels = [x for x in a.get("levels", "").split(",") if x]
        if not set(levels) <= set(LEVELS):
            raise ApiError(400, f"levels: {', '.join(LEVELS)}")
        limit = int(_num(a.get("limit"), "limit", 1, 5000, 500))
        c = open_case(cid)
        rows, total = J.timeline_page(c, a.get("day_from", ""), a.get("day_to", ""), levels, a.get("people") == "1", limit)
        for r in rows:
            r["snapshot"] = _case_rel(r["source_id"], r["snapshot"])
        return jsonify(events=rows, total=total, sources=[{"source_id": s.source_id, "label": s.label} for s in c.sources])

    @app.get("/api/cases/<cid>/events/<sid>/<eid>")
    def case_event(cid, sid, eid):
        """One event of one source with both times, its people/plates and pictures."""
        from . import results as RS
        c = open_case(cid)
        s = source_of(c, sid)
        out = c.source_dir(sid)
        if not os.path.exists(os.path.join(out, "analysis.db")):
            raise ApiError(409, "this source has not been analysed yet – run the case first")
        e = RS.event_detail(out, eid)
        if e is None:
            raise ApiError(404, "no such event in this source")
        for x in e["snapshots"]:
            x["annotated"], x["raw"] = _case_rel(sid, x["annotated"]), _case_rel(sid, x["raw"])
        e["contact_sheet"], e["clip"] = _case_rel(sid, e["contact_sheet"]), _case_rel(sid, e["clip"])
        for t in e["tracks"]:
            t["face"], t["person"] = _case_rel(sid, t["face"]), _case_rel(sid, t["person"])
        for p in e["plates"]:
            p["crop"], p["enhanced"] = _case_rel(sid, p["crop"]), _case_rel(sid, p["enhanced"])
        off = s.offset_for(e["camera"])
        e.update(source_id=sid, source_label=s.label, offset_seconds=off, calibrated_start=calibrate(e["start"], off),
                 calibrated_end=calibrate(e["end"], off))
        e.pop("review", None)                     # reviews belong to the single-folder Results screen
        return jsonify(event=e)

    def sighting_points(c: Case, d: dict) -> List[dict]:
        from .. import journey as J
        from ..reid import COLOR_NAMES
        kind = d.get("kind", "")
        q = d.get("query") or {}
        if not isinstance(q, dict):
            raise ApiError(400, "query must be an object")
        colors = q.get("colors") or {}
        if not isinstance(colors, dict) or not set(colors) <= {"upper", "lower"} or \
                not all(v in COLOR_NAMES or v == "" for v in colors.values()):
            raise ApiError(400, f"colors: {{upper, lower}} each one of {list(COLOR_NAMES)}")
        try:
            return J.sightings(c, kind, _text(d.get("value"), "value", 40), q,
                               _num(d.get("min_score"), "min_score", 0.0, 1.0, 0.7), bool(d.get("fuzzy")),
                               int(_num(d.get("limit"), "limit", 1, 500, 200)))
        except KeyError as e:
            raise ApiError(404, str(e).strip("'\""))
        except ValueError as e:
            raise ApiError(400, str(e))

    @app.post("/api/cases/<cid>/sightings")
    def case_sightings(cid):
        """Every sighting of one face identity / plate / appearance / curated list, and the journey."""
        from .. import journey as J
        d = body()
        c = open_case(cid)
        pts = sighting_points(c, d)
        jr = J.journey(pts, _num(d.get("gap"), "gap", 0, 3600, 120))
        for s in jr["stops"]:
            s["thumb"] = _case_rel(s["source_id"], s.get("thumb", ""))
            s["face"] = _case_rel(s["source_id"], s.get("face", ""))
        return jsonify(points=[_public_point(p) for p in pts],
                       journey={"stops": jr["stops"], "mapped": jr["mapped"], "summary": jr["summary"],
                                "untimed": len(jr["unordered"])},
                       sources=[{"source_id": s.source_id, "label": s.label, "location": s.location} for s in c.sources])

    @app.get("/api/cases/<cid>/people")
    def case_people(cid):
        """Every person seen in the case, optionally only those wearing given colours (top / bottom)."""
        from ..reid import COLOR_NAMES, load_index
        a = request.args
        upper, lower = a.get("upper", ""), a.get("lower", "")
        for v in (upper, lower):
            if v and v not in COLOR_NAMES:
                raise ApiError(400, f"colour must be one of {list(COLOR_NAMES)}")
        limit = int(_num(a.get("limit"), "limit", 1, 2000, 300))
        c = open_case(cid)
        out = []
        for s in c.sources:
            idx = load_index(c.source_dir(s.source_id))
            for m in (idx or {}).get("meta", []):
                if (upper and m.get("upper") != upper) or (lower and m.get("lower") != lower):
                    continue
                dvr = m.get("first_dt") or m.get("start_dt")
                out.append({"source_id": s.source_id, "source_label": s.label, "camera": m.get("camera"),
                            "event_id": m["event_id"], "track": m["track"], "dvr_time": dvr,
                            "calibrated_time": calibrate(dvr, s.offset_for(m.get("camera"))),
                            "upper": m.get("upper", ""), "lower": m.get("lower", ""),
                            "identity": m.get("known_name") or m.get("identity") or "",
                            "thumb": _case_rel(s.source_id, m.get("person_crop", ""))})
        out.sort(key=lambda x: (x["calibrated_time"] or "9999", x["source_id"], x["event_id"], x["track"]))
        return jsonify(people=out[:limit], total=len(out), colors=list(COLOR_NAMES))

    @app.post("/api/cases/<cid>/text-search")
    def case_text_search(cid):
        """Natural-language search ("man in a red helmet on a motorbike") over the keyframes of
        every source analysed with scene search on (SigLIP-2 embeddings)."""
        from ..journey import rel_to
        from ..semantic import search_index
        d = body()
        query = _text(d.get("query"), "query", 200)
        if len(query) < 2:
            raise ApiError(400, "type what to look for, for example: man in a red helmet")
        limit = int(_num(d.get("limit"), "limit", 1, 200, 40))
        c = open_case(cid)
        by_model: Dict[str, list] = collections.defaultdict(list)
        for s in c.sources:
            out = c.source_dir(s.source_id)
            if not os.path.exists(os.path.join(out, "semantic_index.npz")):
                continue
            try:
                with open(os.path.join(out, "config_used.json"), encoding="utf-8") as fh:
                    model = json.load(fh).get("semantic_model", "")
            except (OSError, ValueError):
                model = ""
            if model:
                by_model[model].append(s)
        if not by_model:
            raise ApiError(409, "no source of this case has a describe-it search index: 'Scene tags & free-text "
                                "search' was off, or its model was not installed (Settings › Models › Scene search) – "
                                "turn it on and run the case again (finished videos are not analysed again)")
        hits = []
        for model, srcs in by_model.items():
            try:
                sem = sem_models.get(model)
            except Exception as e:
                log.warning("text search model %s unavailable: %s", model, e)
                raise ApiError(503, "the scene-search model is not installed – download 'Scene search (SigLIP-2)' "
                                    "under Settings › Models")
            for s in srcs:
                for key, score in search_index(sem, os.path.join(c.source_dir(s.source_id), "semantic_index.npz"),
                                               query, limit):
                    eid, _, t = key.rpartition("|")
                    hits.append((score, s, eid, float(t)))
        hits.sort(key=lambda h: -h[0])
        res = []
        for score, s, eid, t in hits[:limit]:
            db = _db(c.source_dir(s.source_id))
            if db is None:
                continue
            try:
                ev = db.execute("SELECT camera, start_dt, start_off, rel_path, snapshots FROM events WHERE event_id=?",
                               (eid,)).fetchone()
            finally:
                db.close()
            if ev is None:
                continue
            dvr = None
            if ev["start_dt"]:
                try:
                    dvr = (dt.datetime.strptime(ev["start_dt"], "%Y-%m-%d %H:%M:%S") +
                           dt.timedelta(seconds=t - float(ev["start_off"] or 0))).strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    dvr = None
            snaps = json.loads(ev["snapshots"] or "[]")
            snap = min(snaps, key=lambda x: abs(float(x.get("t_off", 0)) - t)) if snaps else {}
            thumb = snap.get("raw") or snap.get("annotated") or ""
            res.append({"source_id": s.source_id, "source_label": s.label, "camera": ev["camera"], "event_id": eid,
                        "t_off": round(t, 2), "dvr_time": dvr, "calibrated_time": calibrate(dvr, s.offset_for(ev["camera"])),
                        "rel_path": ev["rel_path"], "score": round(score, 3),
                        "thumb": _case_rel(s.source_id, rel_to(c.source_dir(s.source_id), thumb))})
        return jsonify(results=res, searched_sources=[s.source_id for v in by_model.values() for s in v])

    # ------------------------------------------------------------ highlight reel
    @app.post("/api/cases/<cid>/reel")
    def case_reel(cid):
        from .. import journey as J
        d = body()
        c = open_case(cid, proxies=True)
        pts = sighting_points(c, d)
        if not pts:
            raise ApiError(400, "no sightings to put in the reel")
        name = f"reel_{dt.datetime.now():%Y%m%d_%H%M%S}.mp4"
        dest = os.path.join(c.root, "exports", name)

        def run(job):
            job.update({"stage": "reel", "percent": 5})
            res = J.highlight_reel(pts, dest, _num(d.get("pre"), "pre", 0, 30, 2), _num(d.get("post"), "post", 0, 60, 4))
            from ..evidence import sha256_file
            sha = sha256_file(dest)
            c.audit.append("reel_exported", case_id=c.case_id, file=name, sha256=sha, parts=res["parts"],
                           seconds=res["seconds"], by=user(),
                           sightings=[f"{p['source_id']}/{p['event_id']}/{p['track']}" for p in pts])
            return {"name": name, "file": f"exports/{name}", "parts": res["parts"], "seconds": res["seconds"],
                    "sha256": sha, "index": res["index"]}
        job, _ = jobs.start("export", run)
        return jsonify(job=job.public()), 202

    # ------------------------------------------------------------ Forensic Studio
    def studio_input(c: Case, d: dict, fresh: bool = False):
        from .. import enhance as EN
        s = source_of(c, d.get("source_id"))
        t = d.get("t")
        t = None if t in (None, "") else _num(t, "t", 0, 1e6, 0)
        stack = d.get("stack") or {}
        if not isinstance(stack, dict):
            raise ApiError(400, "stack must be an object")
        n = int(_num(stack.get("frames"), "stack.frames", 1, 15, 1))
        e, t, frames = load_frames(c, s.source_id, d.get("event_id", ""), t, n, fresh)
        chain = d.get("chain") or []
        try:
            EN.describe(chain)                       # validates every step
        except ValueError as ex:
            raise ApiError(400, str(ex))
        stack_rec = None
        img = frames[0]
        if n > 1:
            if len(frames) < 2:
                raise ApiError(422, "not enough frames at this time to stack")
            try:
                img, stack_rec = EN.stack_frames(frames, stack.get("method", "median"), stack.get("motion", "euclidean"))
            except ValueError as ex:
                raise ApiError(400, str(ex))
        return s, e, t, img, chain, stack_rec

    def run_chain(img, chain):
        from .. import enhance as EN
        try:
            return EN.apply_chain(img, chain)
        except ValueError as ex:
            raise ApiError(400, str(ex))

    @app.post("/api/cases/<cid>/enhance/preview")
    def enhance_preview(cid):
        import cv2
        d = body()
        c = open_case(cid, proxies=True)
        _, _, _, img, chain, stack_rec = studio_input(c, d)
        out, rec = run_chain(img, chain)
        ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if not ok:
            raise ApiError(500, "could not encode the preview")
        log_text = " → ".join(x for x in ((stack_rec or {}).get("log", ""), rec["log"]) if x and x != "(no filter)") \
            or "(no filter)"
        return jsonify(image="data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode(), log=log_text,
                       ms=rec["ms"], width=int(out.shape[1]), height=int(out.shape[0]),
                       aligned=(stack_rec or {}).get("aligned"))

    @app.post("/api/cases/<cid>/enhance/save")
    def enhance_save(cid):
        from .. import enhance as EN
        d = body()
        name = re.sub(r"[^A-Za-z0-9_-]", "", _text(d.get("name"), "name", 40)) or "still"
        c = open_case(cid, proxies=True)
        s, e, t, img, chain, stack_rec = studio_input(c, d)
        out, rec = run_chain(img, chain)
        when = None
        if e["start_dt"]:
            try:
                when = (dt.datetime.strptime(e["start_dt"], "%Y-%m-%d %H:%M:%S") +
                        dt.timedelta(seconds=t - float(e["start_off"] or 0))).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                when = None
        source = {"source_id": s.source_id, "event_id": e["event_id"], "camera": f"{s.label}/{e['camera']}",
                  "video": e["rel_path"], "t_off": round(t, 3), "dvr_time": when or "",
                  "calibrated_time": calibrate(when, s.offset_for(e["camera"])) or ""}
        record = dict(rec)
        if stack_rec:
            record = {**stack_rec, "chain": rec["chain"], "log": stack_rec["log"] + (
                " → " + rec["log"] if rec["chain"] else "")}
        edir = os.path.join(c.root, "enhancements")
        saved = EN.save_enhancement(edir, name, source, img, out, record, user())
        # reproducible from the recording itself: decode the frame(s) again, stack and filter again
        _, _, _, img2, _, _ = studio_input(c, d, fresh=True)
        saved["reproducible"] = EN.sha256_bytes(EN.png_bytes(run_chain(img2, chain)[0])) == saved["output_sha256"] \
            and (not rec["chain"] or EN.reproduce(edir, saved))
        rec_path = os.path.join(edir, saved["name"][:-4] + ".json")
        with open(rec_path, encoding="utf-8") as fh:
            stored = json.load(fh)
        stored["reproduced_from_recording"] = saved["reproducible"]
        with open(rec_path, "w", encoding="utf-8") as fh:
            json.dump(stored, fh, indent=1)
        c.audit.append("enhancement_saved", case_id=c.case_id, name=saved["name"], source=source, log=saved["log"],
                       input_sha256=saved["input_sha256"], output_sha256=saved["output_sha256"],
                       reproduced_from_recording=saved["reproducible"], by=user())
        return jsonify(enhancement=_enh_public(saved)), 201

    def _enh_public(r: dict) -> dict:
        return {k: r.get(k) for k in ("name", "input", "log", "source", "input_sha256", "output_sha256", "created",
                                      "operator")} | {"reproducible": r.get("reproducible", r.get("reproduced_from_recording")),
                                                      "file": "enhancements/" + r["name"],
                                                                      "input_file": "enhancements/" + r["input"]}

    @app.get("/api/cases/<cid>/enhancements")
    def enhancements_list(cid):
        from ..enhance import list_enhancements
        c = open_case(cid)
        return jsonify(enhancements=[_enh_public(r) for r in list_enhancements(os.path.join(c.root, "enhancements"))])

    @app.get("/api/enhance/filters")
    def enhance_filters():
        from ..enhance import OPS, PSF_KINDS
        return jsonify(filters={op: {k: {"type": t.__name__, "min": lo, "max": hi, "default": dflt}
                                     for k, (t, lo, hi, dflt) in spec.items()} for op, spec in OPS.items()},
                       psf=list(PSF_KINDS))

    # ------------------------------------------------------------ court bundle
    @app.post("/api/cases/<cid>/bundle")
    def case_bundle(cid):
        from ..cases import parse_declarant
        from ..section63 import build_bundle
        d = body()
        items = d.get("items") or []
        enh = d.get("enhancements") or []
        if not isinstance(items, list) or len(items) > 500 or not all(
                isinstance(i, dict) and isinstance(i.get("source_id"), str) and isinstance(i.get("event_id"), str)
                for i in items):
            raise ApiError(400, "items: a list of at most 500 {source_id, event_id}")
        if not isinstance(enh, list) or len(enh) > 200 or not all(isinstance(x, str) and re.fullmatch(
                r"[A-Za-z0-9_.-]+", x) for x in enh):
            raise ApiError(400, "enhancements: a list of saved enhancement names")
        if not items and not enh:
            raise ApiError(400, "choose at least one event, sighting or enhanced still")
        part_a = d.get("part_a")
        part_b = d.get("part_b")
        with locks[cid]:
            c = open_case(cid, proxies=True)
            for it in items:
                source_of(c, it["source_id"])
            try:                                  # what the officer typed is kept for the next export (audited)
                if part_a is not None:
                    if not isinstance(part_a, dict):
                        raise ValueError("part_a: {source_id: declarant}")
                    for sid, decl in part_a.items():
                        source_of(c, sid)
                        if parse_declarant(decl) != c.source(sid).declarant:
                            c.update_source(sid, user=user(), declarant=decl)
                if part_b is not None:
                    c.set_expert(part_b, user())
            except ValueError as e:
                raise ApiError(400, str(e))
        operator = _text(d.get("operator") or user(), "operator", 100)
        notes = _text(d.get("notes"), "notes", 2000)
        name = f"bundle_{c.case_id}_{dt.datetime.now():%Y%m%d_%H%M%S}.zip"
        dest = os.path.join(c.root, "exports", name)

        def run(job):
            res = build_bundle(c, items, dest, enhancements=enh, operator=operator, notes=notes,
                               progress=lambda i, n, x: job.update({"stage": "bundle", "index": i, "total": n,
                                                                    "percent": round(90 * i / max(1, n))}))
            return {"name": name, "zip_sha256": res["zip_sha256"], "exhibits": res["exhibits"], "files": res["files"],
                    "certificates": res["certificates"]}
        job, _ = jobs.start("export", run)
        return jsonify(job=job.public()), 202

    @app.get("/api/cases/<cid>/exports")
    def case_exports(cid):
        c = open_case(cid)
        d = os.path.join(c.root, "exports")
        hashes = {}
        for e in c.audit.entries(100000):
            det = e.get("details") or {}
            if e.get("action") == "bundle_exported":
                hashes[det.get("zip")] = det.get("zip_sha256")
            elif e.get("action") == "reel_exported":
                hashes[det.get("file")] = det.get("sha256")
        out = []
        if os.path.isdir(d):
            for f in sorted(os.listdir(d), reverse=True):
                if BUNDLE_NAME.match(f) or REEL_NAME.match(f):
                    st = os.stat(os.path.join(d, f))
                    out.append({"name": f, "kind": "bundle" if f.startswith("bundle_") else "reel", "bytes": st.st_size,
                                "created": dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                                "sha256": hashes.get(f, "")})
        return jsonify(exports=out)

    @app.get("/api/cases/<cid>/exports/<name>")
    def case_export_get(cid, name):
        if not (BUNDLE_NAME.match(name) or REEL_NAME.match(name)):
            raise ApiError(400, "bad file name")
        c = open_case(cid)
        full = inside(os.path.join(c.root, "exports"), name)
        return send_file(full, as_attachment=name.endswith(".zip"), download_name=name, conditional=True)

    @app.post("/api/cases/<cid>/exports/<name>/verify")
    def case_export_verify(cid, name):
        from ..section63 import verify_bundle
        if not BUNDLE_NAME.match(name):
            raise ApiError(400, "bad file name")
        c = open_case(cid)
        v = verify_bundle(inside(os.path.join(c.root, "exports"), name))
        c.audit.append("bundle_verified", case_id=c.case_id, zip=name, ok=v["ok"], by=user())
        return jsonify(v)

    @app.get("/api/cases/<cid>/certificate")
    def case_certificate(cid):
        """Printable preview of one device's certificate with the saved declarants (the hash
        report is filled when the bundle is exported)."""
        from ..section63 import certificate_data, certificate_html
        c = open_case(cid)
        s = source_of(c, request.args.get("source"))
        page = certificate_html(certificate_data(c, [], s.declarant, c.expert, [s.source_id]))
        page = page.replace("<body>", "<body><p style='border:2px solid #b00;padding:6px;color:#b00;font-weight:bold'>"
                            "PREVIEW – the hash report is generated when the court bundle is exported.</p>", 1)
        r = Response(page, mimetype="text/html")
        # the certificate is a standalone printable page with its own inline style block
        r.headers["Content-Security-Policy"] = ("default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; "
                                                "base-uri 'none'; form-action 'none'")
        return r

    # ------------------------------------------------------------ audit
    @app.get("/api/cases/<cid>/audit")
    def case_audit(cid):
        c = open_case(cid)
        a = c.audit
        ok, n, bad = a.verify()
        return jsonify(intact=ok, entries_checked=n, first_broken_line=bad,
                       entries=[{k: e.get(k) for k in ("ts", "user", "action", "details")} for e in a.entries(300)])

    # ------------------------------------------------------------ folder location (drag & drop)
    @app.post("/api/fs/locate")
    def fs_locate():
        d = body()
        name = _text(d.get("name"), "name", 255)
        files = d.get("files") or []
        if not name or "/" in name or "\\" in name:
            raise ApiError(400, "name: the dropped folder's name")
        if not isinstance(files, list) or not files or not all(
                isinstance(f, dict) and isinstance(f.get("rel"), str) and ".." not in f["rel"].split("/")
                and isinstance(f.get("size"), int) and f["size"] >= 0 for f in files[:8]):
            raise ApiError(400, "files: [{rel, size}] of some files inside the dropped folder")
        exts = {e.lower() for e in VIDEO_EXTENSIONS}
        vids = [f for f in files if os.path.splitext(f["rel"])[1].lower() in exts] or files
        recent = settings.load().get("recent_inputs", [])
        return jsonify(candidates=locate_folder(name, vids, search_roots(recent)))

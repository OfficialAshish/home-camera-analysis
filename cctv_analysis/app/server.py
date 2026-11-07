"""Local web server for the desktop app.

Security model (single user, own computer):
* listens on 127.0.0.1 only unless started with --allow-lan;
* a random token is created per launch; the launcher opens /?t=<token>, which sets an
  HttpOnly, SameSite=Strict cookie. The page then sends the token in ``X-CCTV-Token`` with
  every API call. Media (<img>/<video>) requests authenticate with the cookie only, and only
  GET requests under /api/file and /api/exports accept it;
* the Host header must be 127.0.0.1/localhost:<port> (DNS-rebinding protection);
* files are served only from inside the output folder that was asked for (realpath check);
* JSON bodies are limited to 1 MB and validated; unknown config keys are rejected;
* mutating endpoints are rate limited; no stack traces are returned to the browser.
"""
from __future__ import annotations

import collections
import datetime as dt
import hmac
import json
import logging
import os
import re
import secrets
import string
import subprocess
import sys
import threading
import time
from typing import Optional

from flask import Flask, Response, jsonify, make_response, redirect, request, send_file

from .. import __version__
from .. import licensing as LIC
from ..audit import AuditLog
from ..config import PRESETS, VIDEO_EXTENSIONS, Config
from . import ai_providers, case_api, features
from . import results as RS
from .common import ApiError, abs_dir, inside
from .jobs import JobManager

log = logging.getLogger("cctv")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
COOKIE = "cctv_session"


def default_data_dir() -> str:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "CCTVAnalysis")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/CCTVAnalysis")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "cctv-analysis")


class RateLimiter:
    def __init__(self):
        self.hits = collections.defaultdict(collections.deque)
        self.lock = threading.Lock()

    def allow(self, key: str, n: int, per: float) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > per:
                q.popleft()
            if len(q) >= n:
                return False
            q.append(now)
            return True


class Settings:
    """App preferences (settings.json) + secrets (secrets.json, owner-only permissions)."""
    KEYS = {"recent_inputs": list, "recent_outputs": list, "last_config": dict, "operator": str, "language": str,
            "cases_root": str, "vlm_providers": list}

    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "settings.json")
        self.secret_path = os.path.join(data_dir, "secrets.json")
        self.lock = threading.Lock()

    def load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, json.JSONDecodeError):
            d = {}
        return {k: d.get(k, t()) if isinstance(d.get(k, t()), t) else t() for k, t in self.KEYS.items()}

    def save(self, upd: dict) -> dict:
        with self.lock:
            d = self.load()
            for k, v in upd.items():
                if k in self.KEYS and isinstance(v, self.KEYS[k]):
                    d[k] = v
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(d, fh, indent=1)
            os.replace(tmp, self.path)
            return d

    def remember(self, key: str, path: str):
        d = self.load()
        lst = [path] + [p for p in d.get(key, []) if p != path]
        self.save({key: lst[:10]})

    def secret(self, name: str) -> str:
        try:
            with open(self.secret_path, encoding="utf-8") as fh:
                return json.load(fh).get(name, "")
        except (OSError, json.JSONDecodeError):
            return ""

    def set_secret(self, name: str, value: str):
        with self.lock:
            try:
                with open(self.secret_path, encoding="utf-8") as fh:
                    d = json.load(fh)
            except (OSError, json.JSONDecodeError):
                d = {}
            if value:
                d[name] = value
            else:
                d.pop(name, None)
            fd = os.open(self.secret_path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(d, fh)
            os.replace(self.secret_path + ".tmp", self.secret_path)


def native_dialog() -> bool:
    """Can this computer show the system folder dialog (Windows PowerShell/Forms, tkinter, display on Linux)?"""
    if sys.platform.startswith("win"):
        return True
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        import tkinter  # noqa: F401
    except Exception:
        return False
    return True


def create_app(data_dir: Optional[str] = None, token: Optional[str] = None, port: int = 8765,
               allow_lan: bool = False, public_key_pem: Optional[bytes] = None) -> Flask:
    data_dir = data_dir or default_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    token = token or secrets.token_urlsafe(32)
    app = Flask(__name__, static_folder=STATIC, static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = 12 << 20         # photo uploads; JSON bodies are capped at 1 MB in body()
    app.config.update(CCTV_TOKEN=token, CCTV_DATA=data_dir, CCTV_PORT=port)
    jobs = JobManager()
    settings = Settings(data_dir)
    lic_store = LIC.LicenseStore(os.path.join(data_dir, "license"))
    app_audit = AuditLog(os.path.join(data_dir, "audit_log.jsonl"))
    limiter = RateLimiter()
    state = {"scan": None, "bench": None}
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    app.extensions["cctv"] = {"jobs": jobs, "settings": settings, "license": lic_store, "state": state}

    def lic_status():
        return lic_store.status(public_key_pem)

    # ------------------------------------------------------------ security
    @app.before_request
    def guard():
        if not allow_lan and request.host not in allowed_hosts:
            return Response("forbidden host", 403)
        path = request.path
        if path.startswith("/static/") or path == "/favicon.ico":
            return None
        if path == "/":
            return None
        if path.startswith("/api/"):
            media = request.method == "GET" and (path == "/api/file" or path.startswith("/api/exports/") or
                                                 case_api.is_media(path))
            hdr = request.headers.get("X-CCTV-Token", "")
            ck = request.cookies.get(COOKIE, "")
            ok = hmac.compare_digest(hdr, token) or (media and hmac.compare_digest(ck, token))
            if not ok:
                return jsonify(error="not authorised – open the app from its launcher"), 401
            if request.method != "GET":
                seg = path.split("/")
                key = seg[2] if seg[2] != "cases" else "cases:" + seg[-1]
                # Forensic Studio previews follow the sliders (debounced in the page): a higher budget
                if not limiter.allow("w:" + key, 300 if key == "cases:preview" else 60, 60):
                    return jsonify(error="too many requests, slow down"), 429
            elif not limiter.allow("r", 1200, 60):
                return jsonify(error="too many requests, slow down"), 429
            return None
        return Response("not found", 404)

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        # a route may set a stricter policy of its own (the printable certificate: no script at all)
        resp.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; "
                                "media-src 'self' blob:; script-src 'self'; style-src 'self'; frame-ancestors 'none'; "
                                "base-uri 'none'; form-action 'self'")
        if request.path.startswith("/api/") and request.path not in ("/api/file",) and \
                not request.path.startswith("/api/exports/") and not case_api.is_media(request.path):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(ApiError)
    def api_error(e):
        return jsonify(error=e.message, **e.extra), e.status

    @app.errorhandler(RS.NotAnOutputFolder)
    def no_results(e):
        return jsonify(error=str(e)), 404

    @app.errorhandler(413)
    def too_big(e):
        return jsonify(error="request too large"), 413

    @app.errorhandler(Exception)
    def crash(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return jsonify(error=e.description), e.code
        log.exception("app error on %s %s", request.method, request.path)
        return jsonify(error="internal error – see the app log"), 500

    def body() -> dict:
        if (request.content_length or 0) > (1 << 20):
            raise ApiError(413, "request too large")
        d = request.get_json(silent=True)
        if not isinstance(d, dict):
            raise ApiError(400, "JSON object body required")
        return d

    def out_param() -> str:
        return abs_dir(request.args.get("out", ""), "out")

    # ---------------------------------------------------------------- page
    @app.get("/")
    def index():
        t = request.args.get("t", "")
        if t:
            if not hmac.compare_digest(t, token):
                return Response("invalid link – start the app again", 401)
            resp = redirect("/")
            resp.set_cookie(COOKIE, token, httponly=True, samesite="Strict", secure=False)
            return resp
        if not hmac.compare_digest(request.cookies.get(COOKIE, ""), token):
            return Response("<h3>Open Drishti-AI (CCTV Analysis) from its desktop shortcut or run_app.bat.</h3>", 401,
                            mimetype="text/html")
        with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as fh:
            page = fh.read().replace("{{TOKEN}}", token).replace("{{VERSION}}", __version__)
        r = make_response(page)
        r.headers["Content-Type"] = "text/html; charset=utf-8"
        r.headers["Cache-Control"] = "no-store"
        return r

    # ------------------------------------------------------------- basics
    @app.get("/api/health")
    def health():
        return jsonify(ok=True, version=__version__)

    @app.get("/api/features")
    def feats():
        return jsonify(features.describe())

    @app.get("/api/system")
    def system():
        from ..sysinfo import system_check
        out = request.args.get("out", "")
        return jsonify(system_check(out if out else data_dir, request.args.get("models", "")))

    @app.get("/api/license")
    def license_get():
        return jsonify(license=lic_status().public(), machine_id=LIC.machine_id(),
                       trial_max_videos=LIC.TRIAL_MAX_VIDEOS)

    @app.post("/api/license")
    def license_set():
        key = body().get("key", "")
        if not isinstance(key, str) or len(key) > 4000:
            raise ApiError(400, "licence key text required")
        st = lic_store.install(key, public_key_pem)
        app_audit.append("license_install", state=st.state, customer=st.customer, expires=st.expires)
        if not st.valid:
            raise ApiError(400, st.reason, license=st.public())
        return jsonify(license=st.public())

    @app.get("/api/settings")
    def settings_get():
        return jsonify(settings.load())

    @app.put("/api/settings")
    def settings_put():
        d = body()
        d.pop("vlm_providers", None)                        # edited only through /api/ai/providers
        if d.get("cases_root"):
            root = abs_dir(d["cases_root"], "cases_root", must_exist=False)
            if not os.path.isdir(os.path.dirname(root)):
                raise ApiError(400, f"cases_root: the folder {os.path.dirname(root)} does not exist")
            os.makedirs(root, exist_ok=True)
            d["cases_root"] = root
            app_audit.append("cases_root_changed", path=root)
        return jsonify(settings.save(d))

    # --------------------------------------------------------- filesystem
    @app.get("/api/fs/roots")
    def fs_roots():
        roots = [os.path.expanduser("~")]
        if sys.platform.startswith("win"):
            roots += [f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]
        else:
            roots += [p for p in ("/", "/media", "/mnt", "/Volumes") if os.path.isdir(p)]
        return jsonify(roots=roots, recent=settings.load(), native_dialog=native_dialog())

    @app.get("/api/fs/list")
    def fs_list():
        p = abs_dir(request.args.get("path", "") or os.path.expanduser("~"), "path")
        exts = {e.lower() for e in VIDEO_EXTENSIONS}
        dirs, videos = [], 0
        try:
            with os.scandir(p) as it:
                for de in it:
                    if de.name.startswith("."):
                        continue
                    try:
                        if de.is_dir(follow_symlinks=False):
                            dirs.append(de.name)
                        elif os.path.splitext(de.name)[1].lower() in exts:
                            videos += 1
                    except OSError:
                        continue
        except PermissionError:
            raise ApiError(403, "no permission to open this folder")
        dirs.sort(key=str.lower)
        parent = os.path.dirname(p) if os.path.dirname(p) != p else ""
        return jsonify(path=p, parent=parent, dirs=dirs[:2000], videos_here=videos)

    @app.post("/api/fs/mkdir")
    def fs_mkdir():
        p = body().get("path", "")
        p = abs_dir(p, "path", must_exist=False)
        os.makedirs(p, exist_ok=True)
        return jsonify(path=p)

    @app.post("/api/fs/pick")
    def fs_pick():
        """System folder dialog in a helper process (Windows: the Windows folder dialog; elsewhere
        tkinter). Answers when the officer has chosen or cancelled: {"path": "" when cancelled}.
        501 when this computer cannot show one (the UI then opens the built-in folder browser)."""
        d = request.get_json(silent=True) or {}
        title = str(d.get("title") or "Choose a folder")[:120]
        start = str(d.get("start") or "")[:500]
        if not native_dialog():
            raise ApiError(501, "folder dialog not available – use the folder browser")
        args = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "cctv_analysis.app"]
        args += ["--pick-folder", "--pick-title", title, "--pick-start", start]
        pkg_parent = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        env = {**os.environ, "PYTHONPATH": pkg_parent + os.pathsep + os.environ.get("PYTHONPATH", ""),
               "PYTHONIOENCODING": "utf-8"}
        try:
            r = subprocess.run(args, capture_output=True, timeout=3600, env=env)
        except subprocess.TimeoutExpired:
            return jsonify(path="", note="the folder dialog was left open for an hour and closed")
        except OSError as e:
            log.warning("folder dialog helper could not start: %s", e)
            raise ApiError(501, "folder dialog not available – use the folder browser")
        if r.returncode == 3:
            raise ApiError(501, "folder dialog not available – use the folder browser")
        if r.returncode != 0:
            log.warning("folder dialog helper failed (%s): %s", r.returncode, r.stderr.decode("utf-8", "replace")[-300:])
            raise ApiError(502, "the folder dialog failed – use the folder browser (details in the app log)")
        path = r.stdout.decode("utf-8", "replace").strip()
        if path and not os.path.isdir(path):
            raise ApiError(502, f"the folder dialog returned a path that is not a folder: {path}")
        return jsonify(path=path)

    # ------------------------------------------------------ config & jobs
    def build_config(d: dict) -> Config:
        cfg_in = d.get("config")
        if not isinstance(cfg_in, dict):
            raise ApiError(400, "config object required")
        preset = d.get("preset") or "balanced"
        if preset not in PRESETS:
            raise ApiError(400, f"preset must be one of {list(PRESETS)}")
        merged = {**PRESETS[preset], **features.APP_DEFAULTS, **cfg_in}
        merged["input_dir"] = abs_dir(merged.get("input_dir", ""), "input_dir")
        merged["output_dir"] = abs_dir(merged.get("output_dir", ""), "output_dir", must_exist=False)
        inp, outp = os.path.realpath(merged["input_dir"]), os.path.realpath(merged["output_dir"])
        if outp == inp:
            raise ApiError(400, "choose an output folder different from the recordings folder")
        import hashlib
        merged["work_dir"] = os.path.join(data_dir, "work", hashlib.sha1(outp.encode()).hexdigest()[:12])
        merged.setdefault("model_cache_dir", os.path.join(data_dir, "models"))
        merged["drive_links"] = False
        merged.update(ai_providers.config_overrides(settings))   # Settings › AI model providers
        try:
            cfg = Config.from_dict(merged)
        except (TypeError, ValueError) as e:
            raise ApiError(400, str(e))
        problems = cfg.validate()
        if problems:
            raise ApiError(400, "invalid settings", problems=problems)
        return cfg

    @app.post("/api/scan")
    def scan():
        d = body()
        cfg = build_config({"config": {**(d.get("config") or {}), "output_dir": d.get("config", {}).get("output_dir")
                                       or os.path.join(data_dir, "scan-tmp")}, "preset": d.get("preset")})

        def run(job):
            from .. import timeline as T
            from .. import video_io as V
            vids = V.find_videos(cfg)
            total, untimed, cams, sample, sizes = 0.0, 0, collections.Counter(), [], 0
            tz = T.tz(cfg.timezone)
            for i, p in enumerate(vids, 1):
                if job.cancel.is_set():
                    break
                m = V.probe(p)
                total += m["duration"] or 0
                sizes += os.path.getsize(p)
                cams[V.camera_name(p, cfg)] += 1
                named = T.time_from_name(p, cfg.input_dir, cfg.filename_time_patterns, tz)
                untimed += named is None
                if len(sample) < 15:
                    sample.append({"file": os.path.relpath(p, cfg.input_dir), "seconds": round(m["duration"] or 0),
                                   "resolution": f"{m['width']}x{m['height']}",
                                   "time_in_name": named.strftime("%Y-%m-%d %H:%M:%S") if named else ""})
                job.update({"stage": "scanning", "index": i, "total": len(vids), "percent": round(100 * i / len(vids), 1)})
            res = {"videos": len(vids), "footage_seconds": round(total), "size_gb": round(sizes / 1e9, 2),
                   "untimed": untimed, "cameras": dict(cams), "sample": sample, "input_dir": cfg.input_dir}
            state["scan"] = res
            return res
        job, _ = jobs.start("scan", run)
        return jsonify(job=job.public()), 202

    @app.post("/api/benchmark")
    def bench():
        d = body()
        cfg = build_config({"config": {**(d.get("config") or {}), "output_dir": os.path.join(data_dir, "bench-tmp")},
                            "preset": d.get("preset")})

        def run(job):
            from .. import video_io as V
            from ..sysinfo import benchmark
            vids = V.find_videos(cfg)
            if not vids:
                raise ValueError("no videos in the recordings folder")
            job.update({"stage": "benchmark", "percent": 10})
            res = benchmark(cfg, vids[0])
            state["bench"] = res
            return res
        job, created = jobs.start("benchmark", run, exclusive=True)
        return jsonify(job=job.public()), 202 if created else 409

    @app.post("/api/estimate")
    def est():
        from ..sysinfo import estimate, gpu_info
        d = body()
        cfg = build_config(d)
        sc = state["scan"]
        if not sc or os.path.realpath(sc["input_dir"]) != os.path.realpath(cfg.input_dir):
            raise ApiError(409, "scan the recordings folder first")
        b = state["bench"]
        e = estimate(cfg, sc["footage_seconds"], sc["videos"], b["rates"] if b else None, sc["untimed"],
                     gpu=gpu_info()["available"] and cfg.device != "cpu")
        lic = lic_status()
        if not lic.valid:
            e["trial_note"] = f"Trial mode: each job analyses at most {LIC.TRIAL_MAX_VIDEOS} videos."
        return jsonify(e)

    @app.post("/api/jobs")
    def job_start():
        d = body()
        rid = d.get("request_id", "")
        if not isinstance(rid, str) or len(rid) > 64:
            raise ApiError(400, "request_id must be a short string")
        if rid:
            existing = jobs._by_request.get(rid)
            if existing:
                return jsonify(job=jobs.get(existing).public()), 200
        cfg = build_config(d)
        lic = lic_status()
        LIC.apply_limits(cfg, lic)
        settings.remember("recent_inputs", cfg.input_dir)
        settings.remember("recent_outputs", cfg.output_dir)
        settings.save({"last_config": {k: v for k, v in d.get("config", {}).items() if k not in ("input_dir", "output_dir")}})

        def run(job):
            from ..pipeline import Analyzer
            audit = AuditLog(os.path.join(cfg.output_dir, "audit_log.jsonl"))
            os.makedirs(cfg.output_dir, exist_ok=True)
            audit.append("analysis_started", job=job.id, input_dir=cfg.input_dir, licence=lic.state,
                         features={k: getattr(cfg, k) for k in ("use_faces", "face_identities", "use_plates",
                                                                "plate_enhance", "use_vlm", "use_semantic")})
            an = Analyzer(cfg, job.update, job.cancel)
            stats = an.run()
            audit.append("analysis_finished", job=job.id, stats=stats)
            if stats.get("videos_failed") and not stats.get("videos_ok"):
                raise RuntimeError(f"no video could be analysed ({stats['videos_failed']} failed) – see the log")
            return {"stats": stats, "output_dir": cfg.output_dir, "trial": not lic.valid}
        job, created = jobs.start("analysis", run, request_id=rid, exclusive=True)
        if not created and job.request_id != rid:
            raise ApiError(409, "an analysis is already running", job=job.public())
        return jsonify(job=job.public()), 202

    @app.get("/api/jobs")
    def job_list():
        return jsonify(jobs=jobs.list())

    @app.get("/api/jobs/<job_id>")
    def job_get(job_id):
        j = jobs.get(job_id)
        if j is None:
            raise ApiError(404, "no such job")
        return jsonify(job=j.public(log_lines=int(request.args.get("log", 60) or 0)))

    @app.post("/api/jobs/<job_id>/cancel")
    def job_cancel(job_id):
        j = jobs.get(job_id)
        if j is None:
            raise ApiError(404, "no such job")
        j.cancel.set()
        return jsonify(job=j.public())

    # ------------------------------------------------------------ results
    @app.get("/api/results/summary")
    def r_summary():
        return jsonify(RS.summary(out_param()))

    @app.get("/api/results/events")
    def r_events():
        a = request.args
        try:
            limit, offset = int(a.get("limit", 50)), int(a.get("offset", 0))
        except ValueError:
            raise ApiError(400, "limit/offset must be numbers")
        for k in ("day_from", "day_to"):
            if a.get(k) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a[k]):
                raise ApiError(400, f"{k} must be YYYY-MM-DD")
        return jsonify(RS.events(out_param(), a.get("day_from", ""), a.get("day_to", ""), a.get("camera", ""),
                                 [x for x in a.get("levels", "").split(",") if x], a.get("q", "")[:100],
                                 a.get("person", "")[:20], a.get("plate", "")[:20], a.get("people") == "1",
                                 a.get("review", ""), a.get("sort", "time"), limit, offset))

    @app.get("/api/results/events/<event_id>")
    def r_event(event_id):
        e = RS.event_detail(out_param(), event_id)
        if e is None:
            raise ApiError(404, "no such event")
        return jsonify(e)

    @app.put("/api/results/events/<event_id>/review")
    def r_review(event_id):
        from ..reviews import Reviews
        out = out_param()
        if RS.event_detail(out, event_id) is None:
            raise ApiError(404, "no such event")
        d = body()
        rv = Reviews(out)
        try:
            row = rv.set(event_id, d.get("status", ""), d.get("notes", ""), settings.load().get("operator", ""))
        except ValueError as e:
            raise ApiError(400, str(e))
        finally:
            rv.close()
        AuditLog(os.path.join(out, "audit_log.jsonl")).append("review", event_id=event_id, status=row["status"])
        return jsonify(row)

    @app.get("/api/results/highlights")
    def r_highlights():
        return jsonify(days=RS.highlights(out_param()))

    @app.get("/api/results/highlights/<day>")
    def r_highlight_day(day):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}|unknown-date", day):
            raise ApiError(400, "day must be YYYY-MM-DD")
        d = RS.highlight_day(out_param(), day)
        if d is None:
            raise ApiError(404, "no highlight video for this day")
        return jsonify(d)

    @app.get("/api/results/plates")
    def r_plates():
        try:
            edits = max(0, min(2, int(request.args.get("edits", 1))))
        except ValueError:
            raise ApiError(400, "edits must be 0, 1 or 2")
        return jsonify(plates=RS.plates_search(out_param(), request.args.get("q", "")[:20], edits))

    @app.get("/api/results/faces")
    def r_faces():
        return jsonify(faces=RS.faces(out_param()))

    @app.post("/api/results/face-search")
    def r_face_search():
        """Search by photo (multipart field 'photo', max 12 MB)."""
        import cv2
        import numpy as np

        from .. import modelcache
        from ..faces import FaceEngine, match_identities
        out = out_param()
        f = request.files.get("photo")
        if f is None:
            raise ApiError(400, "photo file required")
        data = f.read(12 << 20)
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR) if data else None
        if img is None:
            raise ApiError(400, "not an image (use JPG or PNG)")
        modelcache.init(os.path.join(data_dir, "models"))
        cfg = Config(output_dir=out)
        matches = match_identities(FaceEngine(cfg), img, os.path.join(out, "identities"), cfg.face_match_threshold - 0.05)
        if matches is None:
            raise ApiError(422, "no face found in the photo")
        return jsonify(matches=RS.appearances(out, matches))

    @app.get("/api/file")
    def file_():
        full = inside(out_param(), request.args.get("p", ""))
        return send_file(full, conditional=True, max_age=3600)

    @app.post("/api/retime")
    def retime_start():
        """Re-read recording times of an analysed folder (uses its saved config_used.json)."""
        out = abs_dir(body().get("out", ""), "out")
        cu = os.path.join(out, "config_used.json")
        if not os.path.exists(cu):
            raise ApiError(404, "this folder has no config_used.json – it was not produced by an analysis")
        with open(cu, encoding="utf-8") as fh:
            saved = json.load(fh)
        known = set(Config().to_dict())
        saved = {k: v for k, v in saved.items() if k in known}
        saved.pop("time_sources", None)                     # always use the current time rules
        saved.update(output_dir=out, model_cache_dir=os.path.join(data_dir, "models"))
        if not os.path.isdir(saved.get("input_dir", "")):
            raise ApiError(400, f"recordings folder of this analysis is not available: {saved.get('input_dir')}")
        try:
            cfg = Config.from_dict(saved)
        except (TypeError, ValueError) as e:
            raise ApiError(400, str(e))

        def run(job):
            from ..pipeline import setup_logging
            from ..retime import retime
            setup_logging(out, verbose=False)
            res = retime(cfg, job.update, job.cancel)
            AuditLog(os.path.join(out, "audit_log.jsonl")).append("retime", **{k: v for k, v in res.items()
                                                                               if k != "days_affected"})
            return res
        job, created = jobs.start("analysis", run, exclusive=True)
        if not created:
            raise ApiError(409, "an analysis is already running", job=job.public())
        return jsonify(job=job.public()), 202

    # ------------------------------------------------------------- export
    @app.post("/api/export")
    def export():
        from ..evidence import export_evidence
        d = body()
        out = abs_dir(d.get("out", ""), "out")
        ids = d.get("event_ids")
        if not isinstance(ids, list) or not ids or len(ids) > 500 or not all(isinstance(x, str) for x in ids):
            raise ApiError(400, "event_ids: list of 1-500 event ids required")
        case = re.sub(r"[^A-Za-z0-9_-]", "", str(d.get("case_id", "")))[:40]
        name = f"evidence_{case or 'export'}_{dt.datetime.now():%Y%m%d_%H%M%S}.zip"
        dest = os.path.join(out, "exports", name)
        cfg = Config(output_dir=out, model_cache_dir=os.path.join(data_dir, "models"))
        operator = str(d.get("operator") or settings.load().get("operator", ""))[:100]

        def run(job):
            from .. import modelcache
            modelcache.init(cfg.model_cache_dir)
            res = export_evidence(out, ids, dest, cfg, operator, case, str(d.get("notes", ""))[:2000],
                                  bool(d.get("redact")), bool(d.get("redact_plates")),
                                  progress=lambda i, n, e: job.update({"stage": "export", "index": i, "total": n,
                                                                       "percent": round(100 * i / n)}))
            AuditLog(os.path.join(out, "audit_log.jsonl")).append(
                "evidence_export", zip=name, zip_sha256=res["zip_sha256"], manifest_sha256=res["manifest_sha256"],
                events=ids, redacted=res["redacted"], operator=operator, case_id=case)
            return {**{k: v for k, v in res.items() if k != "zip"}, "name": name}
        job, _ = jobs.start("export", run)
        return jsonify(job=job.public()), 202

    @app.get("/api/exports/<name>")
    def export_get(name):
        if not re.fullmatch(r"evidence_[A-Za-z0-9_-]+\.zip", name):
            raise ApiError(400, "bad file name")
        full = inside(os.path.join(out_param(), "exports"), name)
        return send_file(full, as_attachment=True, download_name=name)

    @app.get("/api/audit")
    def audit_get():
        out = out_param()
        a = AuditLog(os.path.join(out, "audit_log.jsonl"))
        ok, n, bad = a.verify()
        return jsonify(intact=ok, entries_checked=n, first_broken_line=bad,
                       entries=[{k: e.get(k) for k in ("ts", "user", "action", "details")} for e in a.entries(200)])

    # ------------------------------------------------------------- models
    @app.get("/api/models")
    def models():
        from ..sysinfo import model_inventory
        return jsonify(models=model_inventory(os.path.join(data_dir, "models")))

    @app.post("/api/models/download")
    def model_dl():
        from ..sysinfo import download_model, model_inventory
        mid = body().get("id", "")
        if mid not in {m["id"] for m in model_inventory(os.path.join(data_dir, "models"))}:
            raise ApiError(400, "unknown model")

        def run(job):
            job.update({"stage": "download", "percent": 5})
            download_model(mid, os.path.join(data_dir, "models"))
            return {"id": mid}
        job, _ = jobs.start("download", run)
        return jsonify(job=job.public()), 202

    ai_providers.register(app, settings=settings, body=body)
    case_api.register(app, jobs=jobs, settings=settings, lic_status=lic_status, body=body, data_dir=data_dir)
    return app

"""Orchestrates the whole analysis: discovery -> planning (probe + recording time, then
chronological order) -> motion scan -> per-event deep analysis -> storage -> reports.
Safe to re-run: finished videos are skipped. Cancellable between videos and events."""
from __future__ import annotations

import datetime as dt
import gc
import heapq
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from collections import Counter, defaultdict
from typing import Callable, Dict, List, Optional

import cv2
import numpy as np

from . import annotate as A
from . import rules as R
from . import timeline as T
from . import video_io as V
from .config import Config
from .faces import sharpness
from .motion import scan_motion
from .storage import Store

log = logging.getLogger("cctv")
PEOPLE_LIKE = {"person"}
VEHICLES = {"bicycle", "car", "motorcycle", "bus", "truck"}
PLATE_VEHICLES = {"car", "motorcycle", "bus", "truck"}
ANIMALS = {"dog", "cat", "bird", "horse", "cow", "sheep"}


class Cancelled(Exception):
    """Raised inside a video when the user cancels; the video is left 'cancelled', not 'error'."""


def setup_logging(out_dir: str, verbose: bool = True):
    os.makedirs(out_dir, exist_ok=True)
    log.setLevel(logging.INFO)
    for h in list(log.handlers):            # replace our own handlers; keep ones the app attached
        if getattr(h, "_cctv_run", False):
            h.close()
            log.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    fh = logging.FileHandler(os.path.join(out_dir, "run_log.txt"), encoding="utf-8")
    fh.setFormatter(fmt)
    setattr(fh, "_cctv_run", True)
    log.addHandler(fh)
    if verbose:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        setattr(sh, "_cctv_run", True)
        log.addHandler(sh)
    log.propagate = False


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)[:80]


def _iso(d: Optional[dt.datetime]) -> Optional[str]:
    return d.strftime("%Y-%m-%d %H:%M:%S") if d else None


def _at(base: Optional[dt.datetime], sec: float) -> Optional[str]:
    return _iso(base + dt.timedelta(seconds=sec)) if base else None


def _imwrite(path: str, img: np.ndarray, q: int = 88) -> str:
    """Write an image; never let a single failed image stop the analysis."""
    try:
        if img is None or getattr(img, "size", 0) == 0:
            return ""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if cv2.imwrite(path, img, [cv2.IMWRITE_JPEG_QUALITY, q]):
            return path
    except Exception as e:
        log.warning("could not save image %s: %s", path, e)
    return ""


def frame_brightness(frames: List[np.ndarray]) -> float:
    """99th-percentile brightness of the picture centre (DVR clock bands excluded): near 0 for a
    black/blank picture even when white OSD text is burnt in."""
    vals = []
    for f in frames:
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) if f.ndim == 3 else f
        h = g.shape[0]
        c = g[int(h * 0.2):int(h * 0.8)]
        if c.size:
            vals.append(float(np.percentile(c, 99)))
    return float(np.median(vals)) if vals else 0.0


def _motion_angle(obs, t: float) -> float:
    """Direction of motion (radians, image coords) of a track around time t – the direction of
    any motion blur on the object."""
    i = min(range(len(obs)), key=lambda j: abs(obs[j][0] - t))
    a, b = obs[max(0, i - 1)], obs[min(len(obs) - 1, i + 1)]
    dx = (b[2][0] + b[2][2] - a[2][0] - a[2][2]) / 2
    dy = (b[2][1] + b[2][3] - a[2][1] - a[2][3]) / 2
    return float(np.arctan2(dy, dx)) if (dx or dy) else 0.0


class Analyzer:
    def __init__(self, cfg: Config, progress: Optional[Callable[[dict], None]] = None,
                 cancel: Optional[threading.Event] = None):
        problems = cfg.validate()
        if problems:
            raise ValueError("Invalid configuration:\n  - " + "\n  - ".join(problems))
        self.cfg = cfg
        self.progress = progress
        self.cancel = cancel or threading.Event()
        os.makedirs(cfg.output_dir, exist_ok=True)
        os.makedirs(cfg.work_dir, exist_ok=True)
        setup_logging(cfg.output_dir)
        cfg.save(os.path.join(cfg.output_dir, "config_used.json"))
        from . import modelcache
        modelcache.init(cfg.model_cache_dir)
        self.run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.new_days: set = set()
        self.new_faces: List[dict] = []
        self.store = Store(os.path.join(cfg.work_dir, "analysis.db"), os.path.join(cfg.output_dir, "analysis.db"))
        from .detector import resolve_device
        V.HW_DECODE = cfg.hw_decode
        self.device = resolve_device(cfg.device)
        log.info("Device: %s", self.device)
        self._det = self._faces = self._sem = self._vlm = self._ocr = self._plates = None
        self._vlm_failed = self._plates_failed = False
        self.idb = self.watch = None
        self._vlm_count = 0
        self._vlm_errors = 0
        self.events_failed = 0
        self.sem_keys, self.sem_embs = [], []
        idx = os.path.join(cfg.output_dir, "semantic_index.npz")
        if os.path.exists(idx):
            try:
                z = np.load(idx, allow_pickle=True)
                self.sem_keys, self.sem_embs = list(z["keys"]), list(z["emb"])
            except Exception:
                pass

    def _emit(self, **kw):
        if self.progress:
            try:
                self.progress(kw)
            except Exception as e:      # a UI problem must never stop the analysis
                log.debug("progress callback failed: %s", e)

    def _check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled()

    # ------------------------------------------------------------ lazy models
    @property
    def det(self):
        if self._det is None:
            from .detector import Detector
            self._det = Detector(self.cfg)
        return self._det

    @property
    def faces(self):
        if self._faces is None and self.cfg.use_faces:
            from .faces import FaceEngine, IdentityDB, Watchlist
            try:
                self._faces = FaceEngine(self.cfg)
                if self.cfg.face_identities:
                    self.idb = IdentityDB(os.path.join(self.cfg.output_dir, "identities"),
                                          self.cfg.face_match_threshold, self.cfg.known_face_threshold)
                    self.idb.run_id = self.run_id
                    self.idb.enroll_known(self._faces, self.cfg.known_faces_dir)
                if self.cfg.face_watchlist_dir:
                    self.watch = Watchlist(self._faces, self.cfg.face_watchlist_dir, self.cfg.known_face_threshold)
            except Exception as e:
                log.warning("Face analysis disabled: %s", e)
                self.cfg.use_faces = False
        return self._faces

    @property
    def sem(self):
        if self._sem is None and self.cfg.use_semantic:
            try:
                from .semantic import Semantic
                self._sem = Semantic(self.cfg, self.device)
            except Exception as e:
                log.warning("Semantic tagging disabled: %s", e)
                self.cfg.use_semantic = False
        return self._sem

    @property
    def vlm(self):
        if self._vlm is None and self.cfg.use_vlm and not self._vlm_failed:
            try:
                from .vlm import make_vlm
                log.info("Loading VLM %s via %s ...", self.cfg.vlm_model, self.cfg.vlm_provider)
                self._vlm = make_vlm(self.cfg, self.device)
            except Exception as e:
                log.warning("VLM disabled: %s", e)
                self._vlm_failed = True
        return self._vlm

    def _vlm_failure(self, ex):
        """Three failures in a row (server down, model missing) disable the VLM for this run instead
        of waiting for a timeout on every event."""
        self._vlm_errors += 1
        log.warning("  VLM failed (%d): %s", self._vlm_errors, ex)
        if self._vlm_errors >= 3 and not self._vlm_failed:
            self._vlm_failed = True
            self._vlm = None
            log.error("AI descriptions disabled for the rest of this run after 3 consecutive failures – check "
                      "the model / server (%s %s)", self.cfg.vlm_provider, self.cfg.vlm_base_url or self.cfg.vlm_model)

    def _vlm_if_enabled(self):
        return self.vlm if self.cfg.use_vlm else None

    @property
    def ocr(self):
        if self._ocr is None:
            from .ocr import get_ocr
            self._ocr = get_ocr(self.cfg.ocr_engine, vlm_getter=self._vlm_if_enabled)
        return self._ocr

    @property
    def plates(self):
        if self._plates is None and self.cfg.use_plates and not self._plates_failed:
            try:
                from .plates import PlateReader
                self._plates = PlateReader(self.cfg, lambda: self.ocr,
                                           self._vlm_if_enabled if self.cfg.plate_vlm_read else None,
                                           self._vlm_failure)
            except Exception as e:
                log.warning("Number-plate reading disabled: %s", e)
                self._plates_failed = True
        return self._plates

    # ------------------------------------------------------------------- run
    def _select(self, videos):
        """Skip finished videos (same path+size+mtime, or same content elsewhere) and duplicates."""
        cfg, st = self.cfg, self.store
        todo, stats, seen = [], Counter(), {}
        t0 = time.time()
        for p in videos:
            try:
                fst = os.stat(p)
            except OSError:
                continue
            if not cfg.reprocess and st.video_done(p, V.file_fingerprint(p)):
                stats["already_done"] += 1
                continue
            qh = None
            if cfg.dedupe:
                qh = st.cached_hash(p, fst.st_size, int(fst.st_mtime))
                if qh is None:
                    try:
                        qh = V.quick_hash(p)
                        st.put_hash(p, fst.st_size, int(fst.st_mtime), qh)
                    except OSError as e:
                        log.warning("cannot read %s: %s", p, e)
                        continue
                other = seen.get(qh) or (None if cfg.reprocess else st.done_with_hash(qh, p))
                if other:
                    stats["duplicates"] += 1
                    log.info("  duplicate skipped: %s  (same content as %s)", p, other)
                    rel = os.path.relpath(p, cfg.input_dir)
                    old = st.db.execute("SELECT id FROM videos WHERE path=?", (p,)).fetchone()
                    row = {"path": p, "rel_path": rel, "fingerprint": V.file_fingerprint(p), "qhash": qh,
                           "status": "duplicate", "duplicate_of": os.path.relpath(other, cfg.input_dir),
                           "size_mb": fst.st_size / 1e6, "run_id": self.run_id,
                           "processed_at": dt.datetime.now().isoformat(timespec="seconds")}
                    if old:
                        st.update_video(old["id"], **{k: v for k, v in row.items() if k != "path"})
                    else:
                        st.insert("videos", row)
                    continue
                seen[qh] = p
            todo.append((p, qh))
        st.commit(sync=False)
        log.info("%d new video(s) to analyse · %d already done · %d duplicate(s) skipped [%.0fs]",
                 len(todo), stats["already_done"], stats["duplicates"], time.time() - t0)
        return todo, stats

    def plan(self, todo) -> List[dict]:
        """Probe every video and resolve its recording time, then order chronologically
        (videos with unknown/approximate time last, in natural file-name order). Processing in time
        order makes face identities, 'new face' alerts and logs follow the real timeline."""
        items = []
        for i, (p, qh) in enumerate(todo, 1):
            self._check_cancel()
            self._emit(stage="planning", index=i, total=len(todo), path=p)
            item = {"path": p, "qhash": qh, "meta": None, "start_dt": None, "time_source": "unknown"}
            try:
                meta = V.probe(p)
                item["meta"] = meta
                if meta["width"] and meta["duration"]:
                    item["start_dt"], item["time_source"] = T.resolve_start_time(p, meta, self.cfg, self.ocr)
            except Exception as e:
                log.warning("could not plan %s: %s", p, e)
            items.append(item)
        far = dt.datetime.max.replace(tzinfo=dt.timezone.utc)
        items.sort(key=lambda x: (not T.is_exact(x["time_source"]),
                                  x["start_dt"] if (x["start_dt"] and T.is_exact(x["time_source"])) else far,
                                  V.natural_key(os.path.relpath(x["path"], self.cfg.input_dir)
                                              if os.path.isdir(self.cfg.input_dir) else x["path"])))
        return items

    def run(self, build_reports: bool = True) -> dict:
        cfg = self.cfg
        started = dt.datetime.now().isoformat(timespec="seconds")
        self._emit(stage="discovering")
        videos = V.find_videos(cfg)
        log.info("Found %d video file(s) under %s%s", len(videos), cfg.input_dir,
                 f" (folders matching {cfg.folder_keywords})" if cfg.folder_keywords else "")
        todo, stats = self._select(videos)
        t_all = time.time()
        try:
            plan = self.plan(todo)
        except Cancelled:
            plan, stats["cancelled"] = [], 1
        streak, last_error = 0, None
        for i, item in enumerate(plan, 1):
            p = item["path"]
            if self.cancel.is_set():
                stats["cancelled"] = 1
                stats["videos_deferred"] = len(plan) - i + 1
                log.warning("Cancelled – %d video(s) left for the next run", len(plan) - i + 1)
                break
            if cfg.max_runtime_minutes and (time.time() - t_all) / 60 >= cfg.max_runtime_minutes:
                log.warning("Time budget of %.0f min reached – %d video(s) left for the next run",
                            cfg.max_runtime_minutes, len(plan) - i + 1)
                stats["videos_deferred"] = len(plan) - i + 1
                break
            log.info("[%d/%d] %s", i, len(plan), p)
            self._emit(stage="video", index=i, total=len(plan), path=p, done_seconds=round(time.time() - t_all, 1))
            try:
                n = self.process_video(p, item["qhash"], item)
                stats["videos_ok"] += 1
                stats["events"] += n
            except KeyboardInterrupt:
                raise
            except Cancelled:
                stats["cancelled"] = 1
                stats["videos_deferred"] = len(plan) - i + 1
                self._mark(p, item["qhash"], "cancelled", "cancelled by user")
                log.warning("Cancelled during %s – it will be analysed again next run", p)
                break
            except Exception as e:
                stats["videos_failed"] += 1
                log.error("FAILED %s: %s\n%s", p, e, traceback.format_exc())
                self._mark(p, item["qhash"], "error", str(e)[:2000])
                same = str(e)[:200]
                streak = streak + 1 if same == last_error else 1
                last_error = same
                if streak >= 3:
                    stats["aborted"] = 1
                    stats["videos_deferred"] = len(plan) - i
                    log.error("Stopping: the same error on %d videos in a row – the installation or models are "
                              "probably broken: %s", streak, same)
                    break
            else:
                streak, last_error = 0, None
            gc.collect()
        self._save_side_data()
        stats["new_faces"] = len(self.new_faces)
        if self.events_failed:
            stats["events_failed"] = self.events_failed
        self.store.insert("runs", {"run_id": self.run_id, "started": started,
                                   "finished": dt.datetime.now().isoformat(timespec="seconds"),
                                   "videos": stats["videos_ok"], "events": stats["events"],
                                   "new_faces": len(self.new_faces), "duplicates": stats["duplicates"],
                                   "notes": json.dumps(dict(stats))})
        self.store.commit()
        log.info("Analysis finished in %.1f min: %s", (time.time() - t_all) / 60, dict(stats))
        if build_reports:
            self._emit(stage="reports")
            from .report import build_reports as br
            try:
                self.outputs = br(self.store, cfg, self.new_days, self.run_id)
            except Exception as e:
                log.exception("report building failed: %s", e)
        self.store.commit()
        self.print_summary(stats)
        self._emit(stage="finished", stats=dict(stats))
        return dict(stats)

    def _mark(self, p, qh, status, error):
        try:
            self.store.db.execute("UPDATE videos SET status=?, error=? WHERE path=?", (status, error, p))
            if not self.store.db.execute("SELECT 1 FROM videos WHERE path=?", (p,)).fetchone():
                self.store.insert("videos", {"path": p, "rel_path": os.path.relpath(p, self.cfg.input_dir),
                                             "status": status, "error": error, "qhash": qh,
                                             "fingerprint": V.file_fingerprint(p), "run_id": self.run_id})
            self.store.commit()
        except Exception:
            pass

    def print_summary(self, stats):
        log.info("=" * 70)
        log.info("RUN SUMMARY %s: %d video(s) analysed, %d event(s), %d duplicate(s) skipped, %d failed",
                 self.run_id, stats.get("videos_ok", 0), stats.get("events", 0), stats.get("duplicates", 0),
                 stats.get("videos_failed", 0))
        if self.new_days:
            log.info("Days updated: %s", ", ".join(sorted(self.new_days)))
        hi = self.store.query("SELECT event_id, level, score, start_dt, camera FROM events WHERE run_id=? AND "
                              "level IN ('HIGH','REVIEW') ORDER BY score DESC LIMIT 10", (self.run_id,))
        for e in hi:
            log.info("  ⚠️  %s %3.0f  %s  %s  %s", e["level"], e["score"], e["start_dt"], e["camera"], e["event_id"])
        if self.new_faces:
            log.warning("🆕 %d NEW face(s) never seen before – review Unique_Faces.xlsx:", len(self.new_faces))
            for f in self.new_faces[:30]:
                log.warning("     %s  first seen %s on %s (%s @ %s)", f["identity"], f["dt"], f["camera"],
                            f["video"], V.fmt_offset(f["t_off"] or 0))
        elif self.cfg.use_faces and self.cfg.face_identities:
            log.info("No new faces in this run.")
        al = self.store.query("SELECT COUNT(*) n FROM face_alerts WHERE run_id=? AND kind='unauthorized'", (self.run_id,))
        if al and al[0]["n"]:
            log.warning("🚫 %d unauthorised-person alert(s) on authorised-only cameras", al[0]["n"])
        pl = self.store.query("SELECT COUNT(*) n FROM plates p JOIN events e USING(event_id) WHERE e.run_id=? "
                              "AND p.text != ''", (self.run_id,))
        if pl and pl[0]["n"]:
            log.info("🚗 %d number plate(s) read in this run", pl[0]["n"])
        log.info("=" * 70)

    def _save_side_data(self):
        if self.idb is not None:
            self.idb.save()
        if self.sem_embs:
            np.savez_compressed(os.path.join(self.cfg.output_dir, "semantic_index.npz"),
                                emb=np.stack(self.sem_embs).astype(np.float16), keys=np.array(self.sem_keys, dtype=object))

    # --------------------------------------------------------- single video
    def process_video(self, path: str, qhash: Optional[str] = None, planned: Optional[dict] = None) -> int:
        cfg = self.cfg
        t0 = time.time()
        if planned and planned.get("meta"):
            meta, start_dt, tsrc = planned["meta"], planned["start_dt"], planned["time_source"]
        else:
            meta = V.probe(path)
            start_dt, tsrc = (T.resolve_start_time(path, meta, cfg, self.ocr) if meta["width"] and meta["duration"]
                              else (None, "unknown"))
        if not meta["width"] or not meta["duration"]:
            raise RuntimeError(f"unreadable video (probe: {meta})")
        camera = V.camera_name(path, cfg)
        rel = os.path.relpath(path, cfg.input_dir) if os.path.isdir(cfg.input_dir) else os.path.basename(path)
        vrow = {"path": path, "rel_path": rel, "fingerprint": V.file_fingerprint(path), "camera": camera,
                "start_dt": _iso(start_dt), "time_source": tsrc,
                "end_dt": _at(start_dt, meta["duration"]),
                "duration": meta["duration"], "fps": meta["fps"], "width": meta["width"],
                "height": meta["height"], "codec": meta["codec"], "size_mb": os.path.getsize(path) / 1e6,
                "status": "running", "error": None, "processed_at": dt.datetime.now().isoformat(timespec="seconds"),
                "drive_link": V.drive_link(path) if cfg.drive_links else "", "qhash": qhash,
                "run_id": self.run_id, "duplicate_of": None}
        vid = self.store.start_video(vrow)
        if self.store.purged_events and self.faces is not None and self.idb is not None:
            self.idb.forget_events(self.store.purged_events)
        log.info("  %s | %dx%d %.1ffps %s | %s | start %s (%s)", camera, meta["width"], meta["height"],
                 meta["fps"], V.fmt_offset(meta["duration"]), meta["codec"], _iso(start_dt), tsrc)
        if not T.is_exact(tsrc):
            log.warning("  recording time is not exact (%s) – add a filename pattern or time override, or make "
                        "sure the DVR clock is visible and an OCR engine is installed", tsrc)
        self._emit(stage="motion", path=path)
        mr = scan_motion(path, meta, cfg, camera)
        self._check_cancel()
        log.info("  motion pass: %d frames, %d event(s), %d tamper, %d lighting change(s) [%.0fs]",
                 mr.frames, len(mr.events), len(mr.tamper), len(mr.lighting_changes), time.time() - t0)
        vdir = os.path.join(cfg.output_dir, "events", _safe(camera or "cam"),
                            start_dt.strftime("%Y-%m-%d") if start_dt else "unknown-date")
        heat_path = ""
        if mr.heatmap is not None and mr.preview is not None:
            heat_path = _imwrite(os.path.join(cfg.output_dir, "heatmaps", _safe(rel) + ".jpg"),
                                 A.heatmap_overlay(mr.preview, mr.heatmap))
        for tp in mr.tamper:
            self.store.insert("tamper", {"video_id": vid, "start_off": tp["start"], "end_off": tp["end"],
                                         "start_dt": _at(start_dt, tp["start"]), "kind": tp["kind"]}, replace=False)
        # camera tampering or lighting changes outside motion events become their own events
        for kind, ivs in (("tamper", mr.tamper), ("lighting", [{"start": t, "end": t} for t in mr.lighting_changes])):
            for iv in ivs:
                if not any(e["start"] <= iv["end"] and e["end"] >= iv["start"] for e in mr.events):
                    mr.events.append({"start": max(0.0, iv["start"] - 3), "end": min(meta["duration"], iv["end"] + 4),
                                      "motion_peak": 0.0, "motion_mean": 0.0, "motion_active_s": 0.0, "origin": kind})
        mr.events.sort(key=lambda e: e["start"])
        for ev in mr.events:
            ev["lighting_changes"] = sum(1 for t in mr.lighting_changes if ev["start"] <= t <= ev["end"])
        n_saved = n_failed = 0
        first_error = None
        for k, ev in enumerate(mr.events, 1):
            self._check_cancel()
            self._emit(stage="event", path=path, event_index=k, events=len(mr.events))
            try:
                if self.analyze_event(path, meta, vid, rel, camera, start_dt, tsrc, ev, k, vdir, mr.tamper):
                    n_saved += 1
            except Cancelled:
                raise
            except Exception as e:
                n_failed += 1
                first_error = first_error or e
                log.error("  event %d failed: %s\n%s", k, e, traceback.format_exc())
            if k % 10 == 0:
                self.store.commit(sync=False)
        self.events_failed += n_failed
        if n_failed and not n_saved:
            # nothing could be analysed (e.g. the detector cannot run): this video is NOT done
            raise RuntimeError(f"all {n_failed} event(s) failed: {first_error}")
        motion_s = float(sum(e["end"] - e["start"] for e in mr.events))
        self.store.update_video(vid, status="done", n_events=n_saved, motion_seconds=motion_s,
                                proc_seconds=time.time() - t0, heatmap_path=heat_path,
                                lighting_changes=len(mr.lighting_changes),
                                osd_masked=int(mr.osd_mask is not None),
                                models=json.dumps({"detector": self._det.model_name if self._det else "",
                                                   "pose": self._det.pose_name if self._det else "",
                                                   "semantic": cfg.semantic_model if self._sem else "",
                                                   "vlm": cfg.vlm_model if self._vlm else "",
                                                   "ocr": self._ocr.name if self._ocr else "",
                                                   "plates": (f"{self._plates.detector.name}/{self._plates.reader.name}"
                                                              if self._plates else "")}))
        self._save_side_data()
        self.store.commit()
        log.info("  done: %d event(s) in %.1fs (%.1fx real-time)", n_saved, time.time() - t0,
                 meta["duration"] / max(1e-3, time.time() - t0))
        return n_saved

    def _event_clock(self, path, meta, start_dt, tsrc, s):
        return T.event_clock(path, meta, start_dt, tsrc, s, self.cfg, self.ocr)

    # --------------------------------------------------------- single event
    def analyze_event(self, path, meta, vid, rel, camera, start_dt, tsrc, ev, k, vdir, tamper) -> bool:
        cfg = self.cfg
        s, e = ev["start"], ev["end"]
        dur = max(0.5, e - s)
        base, ev_src = self._event_clock(path, meta, start_dt, tsrc, s)
        ev_dt = base + dt.timedelta(seconds=s) if base else None
        ev["start_dt"] = ev_dt
        ev["camera"] = camera
        eid = f"{_safe(camera or 'cam')}-{ev_dt:%Y%m%d-%H%M%S}-{vid:04d}{k:03d}" if ev_dt else \
            f"{_safe(os.path.splitext(os.path.basename(path))[0])}-{vid:04d}{k:03d}"
        if base is not start_dt and ev_dt:
            vdir = os.path.join(os.path.dirname(vdir), ev_dt.strftime("%Y-%m-%d"))
        edir = os.path.join(vdir, eid)
        zones = R.zones_for(cfg, camera)
        lines = R.lines_for(cfg, camera)
        det = self.det
        det.reset()
        fe = self.faces
        need_emb = fe is not None and (self.idb is not None or self.watch is not None)
        plate_reader = self.plates
        K = max(1, cfg.snapshots_per_event)
        slots: List[Optional[tuple]] = [None] * K
        tracks: Dict[int, dict] = {}
        frame_people = []
        det_rows = []
        last_logged: Dict[int, float] = {}
        cls_counter = defaultdict(set)
        faces_total = 0
        plate_cands: Dict[int, list] = defaultdict(list)     # track -> heap of (score, t, normalised box)
        H = W = None
        nframes = 0
        from .detector import posture
        for t, frame in V.iter_frames(path, cfg.detect_fps, cfg.detect_width, s, dur,
                                      src_size=(meta["width"], meta["height"])):
            if nframes % 25 == 0:
                self._check_cancel()
            nframes += 1
            H, W = frame.shape[:2]
            dets = det(frame)
            persons = [d for d in dets if d["cls"] == "person"]
            # ---- faces (only for people still needing good faces)
            ffaces = []
            if fe is not None and persons:
                need = [p for p in persons if p["track"] is None or
                        len(tracks.get(p["track"], {}).get("faces", [])) < cfg.faces_per_track or nframes % 4 == 0]
                ffaces = fe.detect_in_people(frame, need)
                attempted = {p["track"] for p in need}
            else:
                attempted = set()
            faces_total += len(ffaces)
            face_by_track = {f["track"]: f for f in ffaces if f["track"] is not None}
            for d in dets:
                tid = d["track"]
                if tid is None:
                    continue
                tr = tracks.setdefault(tid, {"track": tid, "cls": d["cls"], "obs": [], "votes": Counter(),
                                             "zone_frames": Counter(), "hidden_frames": 0, "hands_up": 0,
                                             "crouch": 0, "faces": [], "best_crop": None,
                                             "faces_found": 0})
                tr["votes"][d["cls"]] += d["conf"]
                tr["cls"] = tr["votes"].most_common(1)[0][0]
                tr["obs"].append((t, d["conf"], d["xyxy"]))
                cls_counter[d["cls"]].add(tid)
                x1, y1, x2, y2 = d["xyxy"]
                if plate_reader is not None and d["cls"] in PLATE_VEHICLES and (y2 - y1) >= cfg.plate_min_vehicle_px:
                    crop = frame[int(y1):int(y2), int(x1):int(x2)]
                    edge = x1 < 2 or y1 < 2 or x2 > W - 2 or y2 > H - 2
                    sc = d["conf"] * (y2 - y1) * np.sqrt(max(1.0, sharpness(crop))) * (0.5 if edge else 1.0)
                    hp = plate_cands[tid]
                    item = (sc, t, [x1 / W, y1 / H, x2 / W, y2 / H])
                    if len(hp) < cfg.plate_reads_per_track:
                        heapq.heappush(hp, item)
                    elif sc > hp[0][0]:
                        heapq.heapreplace(hp, item)
                if d["cls"] == "person":
                    fx, fy = ((x1 + x2) / 2) / W, y2 / H
                    for zn, poly in zones.items():
                        if R.point_in_poly(fx, fy, poly):
                            tr["zone_frames"][zn] += 1
                    ps = posture(d["kpts"])
                    tr["hands_up"] += ps["hands_up"]
                    tr["crouch"] += ps["crouching"]
                    if fe is not None and tid in attempted and tid not in face_by_track and \
                            ps["face_visible_pose"] and (y2 - y1) > 0.3 * H:
                        tr["hidden_frames"] += 1
                    # best full-body crop: confident, large, not cut by the border
                    edge = x1 < 2 or y1 < 2 or x2 > W - 2 or y2 > H - 2
                    sc = d["conf"] * np.sqrt((x2 - x1) * (y2 - y1)) * (0.6 if edge else 1.0)
                    if tr["best_crop"] is None or sc > tr["best_crop"][0]:
                        pad = 0.06
                        cx1, cy1 = int(max(0, x1 - pad * (x2 - x1))), int(max(0, y1 - pad * (y2 - y1)))
                        cx2, cy2 = int(min(W, x2 + pad * (x2 - x1))), int(min(H, y2 + pad * (y2 - y1)))
                        tr["best_crop"] = (sc, frame[cy1:cy2, cx1:cx2].copy(), t)
                    f = face_by_track.get(tid)
                    if f is not None:
                        tr["faces_found"] += 1
                        q = fe.quality(frame, f)
                        if len(tr["faces"]) < cfg.faces_per_track or q > min(x[0] for x in tr["faces"]):
                            emb = fe.embed(frame, f) if need_emb else None
                            tr["faces"].append((q, t, f, emb, fe.crop(frame, f["box"])))
                            tr["faces"].sort(key=lambda x: -x[0])
                            del tr["faces"][cfg.faces_per_track:]
                # sampled detection log: one row per track per second
                if t - last_logged.get(tid, -9) >= 1.0:
                    last_logged[tid] = t
                    det_rows.append({"event_id": eid, "t_off": round(t, 2), "dt": _at(base, t),
                                     "cls": d["cls"], "conf": round(d["conf"], 3), "track": tid,
                                     "x1": round(x1 / W, 4), "y1": round(y1 / H, 4),
                                     "x2": round(x2 / W, 4), "y2": round(y2 / H, 4)})
            frame_people.append((t, len(persons)))
            interest = 2.0 * len(persons) + sum(d["conf"] for d in dets) + 0.7 * len(ffaces) + 0.01
            si = min(K - 1, int((t - s) / dur * K))
            if slots[si] is None or interest > slots[si][0]:
                slots[si] = (interest, t, frame.copy(), dets, ffaces)
        if nframes == 0:
            log.warning("  event %s: no frames decoded", eid)
            return False

        # ---------------- track summaries + identities
        fps = cfg.detect_fps
        summaries = []
        id_map = {}
        crossings: Dict[str, Dict[str, int]] = defaultdict(lambda: {"in": 0, "out": 0})
        for tid, tr in tracks.items():
            if len(tr["obs"]) < 2 and max(o[1] for o in tr["obs"]) < 0.6:
                continue                                  # one-off noise
            sm = R.summarize_track(tr, fps, H)
            sm.update({"identity": "", "identity_sim": None, "known_name": "", "face_crop": "",
                       "person_crop": "", "identity_visits": 0, "watchlist": "", "crossings": {}})
            if lines and tr["cls"] == "person":
                pts = [(((o[2][0] + o[2][2]) / 2) / W, o[2][3] / H) for o in tr["obs"]]
                sm["crossings"] = R.count_crossings(pts, lines)
                for ln, c in sm["crossings"].items():
                    crossings[ln]["in"] += c["in"]
                    crossings[ln]["out"] += c["out"]
            if tr["best_crop"] is not None and tr["best_crop"][1].size:
                sm["person_crop"] = _imwrite(os.path.join(edir, "people", f"track{tid:03d}_{tr['cls'].replace(' ', '_')}.jpg"),
                                             tr["best_crop"][1], cfg.snapshot_jpeg_quality)
            embs = [x[3] for x in tr["faces"] if x[3] is not None and x[0] >= cfg.face_min_quality]
            if tr["faces"]:
                q, ft, f, _, crop = tr["faces"][0]
                if crop.size:
                    sm["face_crop"] = _imwrite(os.path.join(edir, "faces", f"track{tid:03d}_best.jpg"), crop, 92)
            if embs:
                m = np.mean(embs, 0)
                m /= np.linalg.norm(m) + 1e-9
                if self.watch is not None:
                    name, _ = self.watch.match(m)
                    sm["watchlist"] = name
                if self.idb is not None:
                    when = _at(base, sm["first"]) or rel
                    pid, sim, kn = self.idb.assign(m, when or "", sm["face_crop"], eid, camera)
                    if self.idb.last_new and not kn:
                        alert = {"run_id": self.run_id, "kind": "new_face", "identity": pid, "dt": when,
                                 "camera": camera, "event_id": eid, "video": rel, "t_off": round(sm["first"], 2),
                                 "face_crop": sm["face_crop"], "person_crop": sm["person_crop"],
                                 "note": "face never seen before"}
                        self.store.insert("face_alerts", alert, replace=False)
                        self.new_faces.append(alert)
                    if sm["face_crop"]:
                        self.idb.set_sample(pid, sm["face_crop"])
                    sm.update({"identity": pid, "identity_sim": round(sim, 3), "known_name": kn})
                    sm["identity_visits"] = next((len(r.get("events", [])) for r in self.idb.ids if r["id"] == pid), 1)
                    id_map[tid] = kn or pid
            if sm["watchlist"]:
                id_map[tid] = f"WATCH:{sm['watchlist']}"
                self.store.insert("face_alerts", {
                    "run_id": self.run_id, "kind": "watchlist", "identity": sm["identity"] or sm["watchlist"],
                    "dt": _at(base, sm["first"]), "camera": camera, "event_id": eid, "video": rel,
                    "t_off": round(sm["first"], 2), "face_crop": sm["face_crop"], "person_crop": sm["person_crop"],
                    "note": f"matches watchlist person '{sm['watchlist']}'"}, replace=False)
            for j, (q, ft, f, _, crop) in enumerate(tr["faces"]):
                cp = sm["face_crop"] if j == 0 else (
                    _imwrite(os.path.join(edir, "faces", f"track{tid:03d}_{j}.jpg"), crop, 90) if crop.size else "")
                self.store.insert("faces", {"event_id": eid, "track": tid, "t_off": round(ft, 2),
                                            "dt": _at(base, ft),
                                            "score": round(f["score"], 3), "quality": round(q, 3),
                                            "box": [round(v, 1) for v in f["box"]], "crop_path": cp,
                                            "identity": sm["identity"], "similarity": sm["identity_sim"],
                                            "known_name": sm["known_name"]}, replace=False)
            summaries.append(sm)

        # ---------------- number plates (full-resolution re-read of the best vehicle frames)
        plates = []
        if plate_reader is not None:
            kept_tracks = {sm["track"] for sm in summaries}
            for tid, hp in plate_cands.items():
                if tid not in kept_tracks:
                    continue
                self._check_cancel()
                samples = []
                obs = tracks[tid]["obs"]
                for _, t, nb in sorted(hp, key=lambda x: x[1]):
                    full = V.read_frame_at(path, t, 0, (meta["width"], meta["height"]))
                    if full is None:
                        continue
                    fh, fw = full.shape[:2]
                    samples.append((t, full, [nb[0] * fw, nb[1] * fh, nb[2] * fw, nb[3] * fh], _motion_angle(obs, t)))
                try:
                    res = plate_reader.read_track(samples)
                except Exception as ex:
                    log.warning("  plate reading failed for track %d: %s", tid, ex)
                    res = None
                if not res:
                    continue
                from .plates import is_reportable, watchlist_match
                ok = is_reportable(res["text"], res["confidence"], res["valid_format"])
                wl = watchlist_match(res["text"], cfg.plate_watchlist, cfg.plate_watchlist_max_edits) if ok else ""
                cp = _imwrite(os.path.join(edir, "plates", f"track{tid:03d}_plate.jpg"), res["crop"], 95)
                ep = _imwrite(os.path.join(edir, "plates", f"track{tid:03d}_enhanced.jpg"), res["enhanced"], 95) \
                    if res.get("enhanced") is not None else ""
                row = {"event_id": eid, "track": tid, "vehicle": tracks[tid]["cls"], "camera": camera,
                       "t_off": round(res["t_off"], 2), "dt": _at(base, res["t_off"]), "text": res["text"],
                       "confidence": res["confidence"], "valid_format": int(res["valid_format"]),
                       "n_reads": len(res["reads"]), "reads": res["reads"], "method": res["method"],
                       "crop_path": cp, "enhanced_path": ep, "watchlist": wl}
                self.store.insert("plates", row, replace=False)
                if ok:
                    plates.append({"text": res["text"], "confidence": res["confidence"], "track": tid,
                                   "vehicle": row["vehicle"], "watchlist": wl})
                    id_map[tid] = res["text"]
        ev["plates"] = plates

        persons = [x for x in summaries if x["cls"] == "person"]
        objects = {c: len(ids) for c, ids in cls_counter.items()}
        max_persons = max((n for _, n in frame_people), default=0)
        ev["max_persons"] = max_persons
        kinds = set(objects)
        kind = "person" if kinds & PEOPLE_LIKE else "vehicle" if kinds & VEHICLES else \
            "animal" if kinds & ANIMALS else ("object" if kinds else
                                              {"tamper": "camera-tamper", "lighting": "lighting-change"}.get(
                                                  ev.get("origin", ""), "motion-only"))
        if cfg.require_person_for_event and kind in ("motion-only", "object", "lighting-change"):
            return False

        # ---------------- keyframes, semantic tags
        kept = [x for x in slots if x is not None]
        luma = frame_brightness([x[2] for x in kept])
        semantic = []
        if self.sem is not None and kept:
            try:
                embs = self.sem.encode_images([x[2] for x in kept])
                semantic = self.sem.classify(embs)
                for x, emb in zip(kept, embs):
                    self.sem_keys.append(f"{eid}|{x[1]:.2f}")
                    self.sem_embs.append(emb.astype(np.float16))
            except Exception as ex:
                log.warning("  semantic failed: %s", ex)
        prelim, _ = R.score_event(ev, summaries, cfg, None, semantic, tamper)

        # ---------------- snapshots
        snaps = []
        sheet_imgs, sheet_caps, raw_imgs = [], [], []
        for j, (_, t, fr, dets, ffs) in enumerate(kept, 1):
            when = base + dt.timedelta(seconds=t) if base else None
            hdr = f"{camera} | {_iso(when) or ''} | {eid} | video {V.fmt_offset(t)}"
            ann = A.draw_dets(fr, dets, ffs, id_map, hdr, zones)
            ap = _imwrite(os.path.join(edir, f"snap{j:02d}_{V.fmt_offset(t).replace(':', '')}_annotated.jpg"),
                          ann, cfg.snapshot_jpeg_quality)
            rp = _imwrite(os.path.join(edir, f"snap{j:02d}_{V.fmt_offset(t).replace(':', '')}_raw.jpg"),
                          fr, cfg.snapshot_jpeg_quality) if cfg.save_raw_snapshots else ""
            npers = sum(1 for d in dets if d["cls"] == "person")
            snaps.append({"t_off": round(t, 2), "dt": _iso(when), "annotated": ap, "raw": rp,
                          "persons": npers, "objects": sorted({d["cls"] for d in dets}),
                          "semantic": semantic[j - 1]["top_label"] if j - 1 < len(semantic) else ""})
            sheet_imgs.append(ann)
            raw_imgs.append(fr)
            sheet_caps.append(f"{j}. {V.fmt_offset(t)}  ({_iso(when)[11:] if when else ''})")
        sheet = A.contact_sheet(sheet_imgs, sheet_caps)
        sheet_path = _imwrite(os.path.join(edir, "contact_sheet.jpg"), sheet, 85)

        # ---------------- VLM description
        vlm_res = None
        want_vlm = cfg.use_vlm and (cfg.vlm_scope == "all" or (cfg.vlm_scope == "people" and persons) or
                                    (cfg.vlm_scope == "flagged" and (prelim >= cfg.suspicion_alert_threshold * 0.5 or
                                                                     (persons and R.is_after_hours(ev_dt, cfg)))))
        if want_vlm and self._vlm_count < cfg.vlm_max_events and self.vlm is not None:
            try:
                ctx = f"{len(persons)} person track(s); objects: {', '.join(sorted(objects))}"
                if plates:
                    ctx += "; plates: " + ", ".join(p["text"] for p in plates)
                vlm_res = self.vlm.describe_event(A.contact_sheet(raw_imgs, sheet_caps, tile_w=448), ctx)
                self._vlm_count += 1
                self._vlm_errors = 0
            except Exception as ex:
                self._vlm_failure(ex)

        score, flags = R.score_event(ev, summaries, cfg, vlm_res, semantic, tamper)
        level = "HIGH" if score >= 70 else "REVIEW" if score >= cfg.suspicion_alert_threshold else \
            "LOW" if score > 0 else "NORMAL"

        # ---------------- clip
        clip = ""
        if cfg.save_clips and (cfg.clip_scope == "all" or (cfg.clip_scope == "people" and (persons or score > 0)) or
                               (cfg.clip_scope == "flagged" and score >= cfg.suspicion_alert_threshold)):
            cp = os.path.join(edir, "clip.mp4")
            lab = f"{camera} {eid} start {_iso(ev_dt) or V.fmt_offset(s)}" if cfg.clip_overlay else ""
            if V.export_clip(path, s, min(dur, cfg.clip_max_seconds), cp, cfg.clip_width, lab):
                clip = cp

        # ---------------- persist
        for sm in summaries:
            self.store.insert("tracks", {
                "event_id": eid, "track": sm["track"], "cls": sm["cls"], "first_off": round(sm["first"], 2),
                "last_off": round(sm["last"], 2), "first_dt": _at(base, sm["first"]),
                "duration": round(sm["duration"], 2), "frames": sm["frames"], "max_conf": round(sm["max_conf"], 3),
                "mean_conf": round(sm["mean_conf"], 3), "max_speed": round(sm["max_speed"], 2),
                "zones": {z: round(v, 1) for z, v in sm["zones"].items()}, "identity": sm["identity"],
                "identity_sim": sm["identity_sim"], "known_name": sm["known_name"], "face_crop": sm["face_crop"],
                "person_crop": sm["person_crop"], "hidden_face_s": round(sm["hidden_face_s"], 1),
                "posture": {"hands_up_s": round(sm["hands_up_s"], 1), "crouch_s": round(sm["crouch_s"], 1)},
                "faces_found": sm["faces_found"], "path": sm["path"], "watchlist": sm["watchlist"],
                "crossings": sm["crossings"]})
        self.store.insert_many("detections", det_rows)
        idents = sorted({(x["known_name"] or x["identity"]) for x in persons if x["identity"]})
        self.store.insert("events", {
            "event_id": eid, "video_id": vid, "video_path": path, "rel_path": rel, "camera": camera,
            "start_off": round(s, 2), "end_off": round(e, 2), "start_dt": _iso(ev_dt),
            "end_dt": _at(base, e), "duration": round(dur, 2),
            "kind": kind, "motion_peak": round(ev["motion_peak"], 4), "motion_mean": round(ev["motion_mean"], 4),
            "max_persons": max_persons, "n_tracks": len(summaries), "n_person_tracks": len(persons),
            "objects": objects, "faces_detected": faces_total, "identities": idents,
            "best_conf": round(max((x["max_conf"] for x in summaries), default=0.0), 3),
            "score": round(score, 1), "level": level, "flags": flags,
            "semantic": semantic[:K], "vlm": vlm_res, "snapshots": snaps, "contact_sheet": sheet_path,
            "clip_path": clip, "folder": edir, "after_hours": int(R.is_after_hours(ev_dt, cfg)),
            "frames_analyzed": nframes, "run_id": self.run_id, "time_source": ev_src,
            "luma_p99": round(luma, 1), "plates": [p["text"] for p in plates], "crossings": dict(crossings)})
        self.new_days.add(ev_dt.strftime("%Y-%m-%d") if ev_dt else "unknown-date")
        if any(f["flag"] == "unauthorized_person" for f in flags):
            for sm in persons:
                if not sm.get("known_name"):
                    self.store.insert("face_alerts", {
                        "run_id": self.run_id, "kind": "unauthorized", "identity": sm.get("identity") or "",
                        "dt": _at(base, sm["first"]), "camera": camera, "event_id": eid, "video": rel,
                        "t_off": round(sm["first"], 2), "face_crop": sm.get("face_crop", ""),
                        "person_crop": sm.get("person_crop", ""),
                        "note": "not a known person on an authorised-only camera"}, replace=False)
        log.info("  event %s %s→%s %s persons=%d tracks=%d faces=%d plates=%s score=%.0f %s", eid, V.fmt_offset(s),
                 V.fmt_offset(e), kind, len(persons), len(summaries), faces_total,
                 ",".join(p["text"] for p in plates) or "-", score, ",".join(f["flag"] for f in flags))
        return True


def run(cfg: Config, progress=None, build_reports=True, cancel=None) -> dict:
    return Analyzer(cfg, progress, cancel).run(build_reports)

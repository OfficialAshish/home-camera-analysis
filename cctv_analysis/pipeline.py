"""Orchestrates the whole analysis: discovery -> motion scan -> per-event deep
analysis -> storage -> reports. Safe to re-run: finished videos are skipped."""
from __future__ import annotations

import datetime as dt
import gc
import json
import logging
import os
import re
import time
import traceback
from collections import Counter, defaultdict
from typing import Callable, Optional

import cv2
import numpy as np

from . import annotate as A
from . import rules as R
from . import video_io as V
from .config import Config
from .motion import scan_motion
from .storage import Store

log = logging.getLogger("cctv")
PEOPLE_LIKE = {"person"}
VEHICLES = {"bicycle", "car", "motorcycle", "bus", "truck"}
ANIMALS = {"dog", "cat", "bird", "horse", "cow", "sheep"}


def setup_logging(out_dir: str, verbose: bool = True):
    os.makedirs(out_dir, exist_ok=True)
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    fh = logging.FileHandler(os.path.join(out_dir, "run_log.txt"), encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if verbose:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)
    log.propagate = False


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)[:80]


def _iso(d: Optional[dt.datetime]) -> Optional[str]:
    return d.strftime("%Y-%m-%d %H:%M:%S") if d else None


def _imwrite(path: str, img: np.ndarray, q: int = 88) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cv2.imwrite(path, img, [cv2.IMWRITE_JPEG_QUALITY, q])
    return path


class Analyzer:
    def __init__(self, cfg: Config, progress: Optional[Callable] = None):
        self.cfg = cfg
        self.progress = progress
        os.makedirs(cfg.output_dir, exist_ok=True)
        os.makedirs(cfg.work_dir, exist_ok=True)
        setup_logging(cfg.output_dir)
        cfg.save(os.path.join(cfg.output_dir, "config_used.json"))
        self.store = Store(os.path.join(cfg.work_dir, "analysis.db"), os.path.join(cfg.output_dir, "analysis.db"))
        from .detector import resolve_device
        self.device = resolve_device(cfg.device)
        log.info("Device: %s", self.device)
        self._det = self._faces = self._sem = self._vlm = None
        self._vlm_failed = False
        self.idb = None
        self.sem_keys, self.sem_embs = [], []
        idx = os.path.join(cfg.output_dir, "semantic_index.npz")
        if os.path.exists(idx):
            try:
                z = np.load(idx, allow_pickle=True)
                self.sem_keys, self.sem_embs = list(z["keys"]), list(z["emb"])
            except Exception:
                pass

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
            from .faces import FaceEngine, IdentityDB
            try:
                self._faces = FaceEngine(self.cfg)
                self.idb = IdentityDB(os.path.join(self.cfg.output_dir, "identities"),
                                      self.cfg.face_match_threshold, self.cfg.known_face_threshold)
                self.idb.enroll_known(self._faces, self.cfg.known_faces_dir)
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
                from .vlm import VLM
                log.info("Loading VLM %s ...", self.cfg.vlm_model)
                self._vlm = VLM(self.cfg, self.device)
            except Exception as e:
                log.warning("VLM disabled: %s", e)
                self._vlm_failed = True
        return self._vlm

    def _osd_reader(self, path, meta):
        if not self.cfg.use_vlm or self.vlm is None:
            return None
        f = V.read_frame_at(path, min(1.0, (meta.get("duration") or 2) / 2), 1280, (meta["width"], meta["height"]))
        return self.vlm.read_clock(f) if f is not None else None

    # ------------------------------------------------------------------- run
    def run(self, build_reports: bool = True) -> dict:
        cfg = self.cfg
        videos = V.find_videos(cfg)
        log.info("Found %d video file(s) under %s", len(videos), cfg.input_dir)
        todo = [p for p in videos if cfg.reprocess or not self.store.video_done(p, V.file_fingerprint(p))]
        log.info("%d to process (%d already done)", len(todo), len(videos) - len(todo))
        t_all = time.time()
        stats = Counter()
        for i, p in enumerate(todo, 1):
            log.info("[%d/%d] %s", i, len(todo), p)
            try:
                n = self.process_video(p)
                stats["videos_ok"] += 1
                stats["events"] += n
            except KeyboardInterrupt:
                raise
            except Exception as e:
                stats["videos_failed"] += 1
                log.error("FAILED %s: %s\n%s", p, e, traceback.format_exc())
                try:
                    self.store.db.execute("UPDATE videos SET status='error', error=? WHERE path=?", (str(e)[:2000], p))
                    if not self.store.db.execute("SELECT 1 FROM videos WHERE path=?", (p,)).fetchone():
                        self.store.insert("videos", {"path": p, "rel_path": os.path.relpath(p, cfg.input_dir),
                                                     "status": "error", "error": str(e)[:2000],
                                                     "fingerprint": V.file_fingerprint(p)})
                    self.store.commit()
                except Exception:
                    pass
            if self.progress:
                self.progress(i, len(todo), p)
            gc.collect()
        self._save_side_data()
        self.store.commit()
        log.info("Analysis finished in %.1f min: %s", (time.time() - t_all) / 60, dict(stats))
        if build_reports:
            from .report import build_reports as br
            br(self.store, cfg)
        self.store.commit()
        return dict(stats)

    def _save_side_data(self):
        if self.idb is not None:
            self.idb.save()
        if self.sem_embs:
            np.savez_compressed(os.path.join(self.cfg.output_dir, "semantic_index.npz"),
                                emb=np.stack(self.sem_embs).astype(np.float16), keys=np.array(self.sem_keys, dtype=object))

    # --------------------------------------------------------- single video
    def process_video(self, path: str) -> int:
        cfg = self.cfg
        t0 = time.time()
        meta = V.probe(path)
        if not meta["width"] or not meta["duration"]:
            raise RuntimeError(f"unreadable video (probe: {meta})")
        camera = V.camera_name(path, cfg)
        start_dt, tsrc = V.infer_start_time(path, meta, cfg, vlm_reader=self._osd_reader)
        rel = os.path.relpath(path, cfg.input_dir) if os.path.isdir(cfg.input_dir) else os.path.basename(path)
        vrow = {"path": path, "rel_path": rel, "fingerprint": V.file_fingerprint(path), "camera": camera,
                "start_dt": _iso(start_dt), "time_source": tsrc,
                "end_dt": _iso(start_dt + dt.timedelta(seconds=meta["duration"])) if start_dt else None,
                "duration": meta["duration"], "fps": meta["fps"], "width": meta["width"],
                "height": meta["height"], "codec": meta["codec"], "size_mb": os.path.getsize(path) / 1e6,
                "status": "running", "error": None, "processed_at": dt.datetime.now().isoformat(timespec="seconds"),
                "drive_link": V.drive_link(path) if cfg.drive_links else ""}
        vid = self.store.start_video(vrow)
        log.info("  %s | %dx%d %.1ffps %s | %s | start %s (%s)", camera, meta["width"], meta["height"],
                 meta["fps"], V.fmt_offset(meta["duration"]), meta["codec"], _iso(start_dt), tsrc)

        mr = scan_motion(path, meta, cfg, camera)
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
                                         "start_dt": _iso(start_dt + dt.timedelta(seconds=tp["start"])) if start_dt else None,
                                         "kind": tp["kind"]}, replace=False)
        n_saved = 0
        for k, ev in enumerate(mr.events, 1):
            try:
                if self.analyze_event(path, meta, vid, rel, camera, start_dt, ev, k, vdir, mr.tamper):
                    n_saved += 1
            except Exception as e:
                log.error("  event %d failed: %s\n%s", k, e, traceback.format_exc())
            if k % 10 == 0:
                self.store.commit(sync=False)
        # tamper intervals without motion still deserve an event row
        motion_s = float(sum(e["end"] - e["start"] for e in mr.events))
        self.store.update_video(vid, status="done", n_events=n_saved, motion_seconds=motion_s,
                                proc_seconds=time.time() - t0, heatmap_path=heat_path,
                                lighting_changes=len(mr.lighting_changes),
                                osd_masked=int(mr.osd_mask is not None),
                                models=json.dumps({"detector": self._det.model_name if self._det else "",
                                                   "pose": self._det.pose_name if self._det else "",
                                                   "semantic": cfg.semantic_model if self._sem else "",
                                                   "vlm": cfg.vlm_model if self._vlm else ""}))
        self._save_side_data()
        self.store.commit()
        log.info("  done: %d event(s) in %.1fs (%.1fx real-time)", n_saved, time.time() - t0,
                 meta["duration"] / max(1e-3, time.time() - t0))
        return n_saved

    # --------------------------------------------------------- single event
    def analyze_event(self, path, meta, vid, rel, camera, start_dt, ev, k, vdir, tamper) -> bool:
        cfg = self.cfg
        s, e = ev["start"], ev["end"]
        dur = max(0.5, e - s)
        ev_dt = start_dt + dt.timedelta(seconds=s) if start_dt else None
        ev["start_dt"] = ev_dt
        eid = f"{_safe(camera or 'cam')}-{ev_dt:%Y%m%d-%H%M%S}-{vid:04d}{k:03d}" if ev_dt else \
            f"{_safe(os.path.splitext(os.path.basename(path))[0])}-{vid:04d}{k:03d}"
        edir = os.path.join(vdir, eid)
        zones = R.zones_for(cfg, camera)
        det = self.det
        det.reset()
        fe = self.faces
        K = max(1, cfg.snapshots_per_event)
        slots = [None] * K
        tracks = {}
        frame_people = []
        det_rows = []
        last_logged = {}
        cls_counter = defaultdict(set)
        untracked = Counter()
        faces_total = 0
        H = W = None
        nframes = 0
        for t, frame in V.iter_frames(path, cfg.detect_fps, cfg.detect_width, s, dur,
                                      src_size=(meta["width"], meta["height"])):
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
                    untracked[d["cls"]] += 1
                    continue
                tr = tracks.setdefault(tid, {"track": tid, "cls": d["cls"], "obs": [], "votes": Counter(),
                                             "zone_frames": Counter(), "hidden_frames": 0, "hands_up": 0,
                                             "crouch": 0, "waist": 0, "faces": [], "best_crop": None,
                                             "faces_found": 0})
                tr["votes"][d["cls"]] += d["conf"]
                tr["cls"] = tr["votes"].most_common(1)[0][0]
                tr["obs"].append((t, d["conf"], d["xyxy"]))
                cls_counter[d["cls"]].add(tid)
                x1, y1, x2, y2 = d["xyxy"]
                if d["cls"] == "person":
                    fx, fy = ((x1 + x2) / 2) / W, y2 / H
                    for zn, poly in zones.items():
                        if R.point_in_poly(fx, fy, poly):
                            tr["zone_frames"][zn] += 1
                    from .detector import posture
                    ps = posture(d["kpts"])
                    tr["hands_up"] += ps["hands_up"]
                    tr["crouch"] += ps["crouching"]
                    tr["waist"] += ps["hand_at_waist"]
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
                            emb = fe.embed(frame, f)
                            tr["faces"].append((q, t, f, emb, fe.crop(frame, f["box"])))
                            tr["faces"].sort(key=lambda x: -x[0])
                            del tr["faces"][cfg.faces_per_track:]
                # sampled detection log: one row per track per second
                if t - last_logged.get(tid, -9) >= 1.0:
                    last_logged[tid] = t
                    det_rows.append({"event_id": eid, "t_off": round(t, 2),
                                     "dt": _iso(start_dt + dt.timedelta(seconds=t)) if start_dt else None,
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
        for tid, tr in tracks.items():
            if len(tr["obs"]) < 2 and max(o[1] for o in tr["obs"]) < 0.6:
                continue                                  # one-off noise
            sm = R.summarize_track(tr, fps, H)
            sm.update({"identity": "", "identity_sim": None, "known_name": "", "face_crop": "",
                       "person_crop": "", "identity_visits": 0})
            if tr["best_crop"] is not None and tr["best_crop"][1].size:
                sm["person_crop"] = _imwrite(os.path.join(edir, "people", f"track{tid:03d}_{tr['cls'].replace(' ', '_')}.jpg"),
                                             tr["best_crop"][1], cfg.snapshot_jpeg_quality)
            embs = [x[3] for x in tr["faces"] if x[3] is not None]
            if tr["faces"]:
                q, ft, f, _, crop = tr["faces"][0]
                if crop.size:
                    sm["face_crop"] = _imwrite(os.path.join(edir, "faces", f"track{tid:03d}_best.jpg"), crop, 92)
            if embs and self.idb is not None:
                m = np.mean(embs, 0)
                m /= np.linalg.norm(m) + 1e-9
                when = _iso(start_dt + dt.timedelta(seconds=sm["first"])) if start_dt else rel
                pid, sim, kn = self.idb.assign(m, when or "", sm["face_crop"])
                if sm["face_crop"]:
                    self.idb.set_sample(pid, sm["face_crop"])
                sm.update({"identity": pid, "identity_sim": round(sim, 3), "known_name": kn})
                sm["identity_visits"] = next((r["count"] for r in self.idb.ids if r["id"] == pid), 1)
                id_map[tid] = kn or pid
            for j, (q, ft, f, _, crop) in enumerate(tr["faces"]):
                cp = sm["face_crop"] if j == 0 else (
                    _imwrite(os.path.join(edir, "faces", f"track{tid:03d}_{j}.jpg"), crop, 90) if crop.size else "")
                self.store.insert("faces", {"event_id": eid, "track": tid, "t_off": round(ft, 2),
                                            "dt": _iso(start_dt + dt.timedelta(seconds=ft)) if start_dt else None,
                                            "score": round(f["score"], 3), "quality": round(q, 3),
                                            "box": [round(v, 1) for v in f["box"]], "crop_path": cp,
                                            "identity": sm["identity"], "similarity": sm["identity_sim"],
                                            "known_name": sm["known_name"]}, replace=False)
            summaries.append(sm)

        persons = [x for x in summaries if x["cls"] == "person"]
        objects = {c: len(ids) for c, ids in cls_counter.items()}
        max_persons = max((n for _, n in frame_people), default=0)
        ev["max_persons"] = max_persons
        kinds = set(objects)
        kind = "person" if kinds & PEOPLE_LIKE else "vehicle" if kinds & VEHICLES else \
            "animal" if kinds & ANIMALS else ("object" if kinds else "motion-only")
        if cfg.require_person_for_event and kind in ("motion-only", "object"):
            return False

        # ---------------- keyframes, semantic tags
        slots = [x for x in slots if x is not None]
        semantic = []
        if self.sem is not None and slots:
            try:
                embs = self.sem.encode_images([x[2] for x in slots])
                semantic = self.sem.classify(embs)
                for x, emb in zip(slots, embs):
                    self.sem_keys.append(f"{eid}|{x[1]:.2f}")
                    self.sem_embs.append(emb.astype(np.float16))
            except Exception as ex:
                log.warning("  semantic failed: %s", ex)
        prelim, _ = R.score_event(ev, summaries, cfg, None, semantic, tamper)

        # ---------------- snapshots
        snaps = []
        sheet_imgs, sheet_caps, raw_imgs = [], [], []
        for j, (_, t, fr, dets, ffs) in enumerate(slots, 1):
            when = start_dt + dt.timedelta(seconds=t) if start_dt else None
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
        vlm_count = getattr(self, "_vlm_count", 0)
        if want_vlm and vlm_count < cfg.vlm_max_events and self.vlm is not None:
            try:
                ctx = f"{len(persons)} person track(s); objects: {', '.join(sorted(objects))}"
                vlm_res = self.vlm.describe_event(A.contact_sheet(raw_imgs, sheet_caps, tile_w=448), ctx)
                self._vlm_count = vlm_count + 1
            except Exception as ex:
                log.warning("  VLM failed: %s", ex)

        score, flags = R.score_event(ev, summaries, cfg, vlm_res, semantic, tamper)
        level = "HIGH" if score >= 70 else "REVIEW" if score >= cfg.suspicion_alert_threshold else \
            "LOW" if score > 0 else "NORMAL"

        # ---------------- clip
        clip = ""
        if cfg.save_clips and (cfg.clip_scope == "all" or (cfg.clip_scope == "people" and (persons or score > 0)) or
                               (cfg.clip_scope == "flagged" and score >= cfg.suspicion_alert_threshold)):
            cp = os.path.join(edir, "clip.mp4")
            lab = f"{camera} {eid} start {_iso(ev_dt) or V.fmt_offset(s)}"
            if V.export_clip(path, s, min(dur, cfg.clip_max_seconds), cp, cfg.clip_width, lab):
                clip = cp

        # ---------------- persist
        for sm in summaries:
            self.store.insert("tracks", {
                "event_id": eid, "track": sm["track"], "cls": sm["cls"], "first_off": round(sm["first"], 2),
                "last_off": round(sm["last"], 2),
                "first_dt": _iso(start_dt + dt.timedelta(seconds=sm["first"])) if start_dt else None,
                "duration": round(sm["duration"], 2), "frames": sm["frames"], "max_conf": round(sm["max_conf"], 3),
                "mean_conf": round(sm["mean_conf"], 3), "max_speed": round(sm["max_speed"], 2),
                "zones": {z: round(v, 1) for z, v in sm["zones"].items()}, "identity": sm["identity"],
                "identity_sim": sm["identity_sim"], "known_name": sm["known_name"], "face_crop": sm["face_crop"],
                "person_crop": sm["person_crop"], "hidden_face_s": round(sm["hidden_face_s"], 1),
                "posture": {"hands_up_s": round(sm["hands_up_s"], 1), "crouch_s": round(sm["crouch_s"], 1),
                            "hand_at_waist_s": round(sm["waist_s"], 1)},
                "faces_found": sm["faces_found"], "path": sm["path"]})
        self.store.insert_many("detections", det_rows)
        idents = sorted({(x["known_name"] or x["identity"]) for x in persons if x["identity"]})
        self.store.insert("events", {
            "event_id": eid, "video_id": vid, "video_path": path, "rel_path": rel, "camera": camera,
            "start_off": round(s, 2), "end_off": round(e, 2), "start_dt": _iso(ev_dt),
            "end_dt": _iso(start_dt + dt.timedelta(seconds=e)) if start_dt else None, "duration": round(dur, 2),
            "kind": kind, "motion_peak": round(ev["motion_peak"], 4), "motion_mean": round(ev["motion_mean"], 4),
            "max_persons": max_persons, "n_tracks": len(summaries), "n_person_tracks": len(persons),
            "objects": objects, "faces_detected": faces_total, "identities": idents,
            "best_conf": round(max((x["max_conf"] for x in summaries), default=0.0), 3),
            "score": round(score, 1), "level": level, "flags": flags,
            "semantic": semantic[:K], "vlm": vlm_res, "snapshots": snaps, "contact_sheet": sheet_path,
            "clip_path": clip, "folder": edir, "after_hours": int(R.is_after_hours(ev_dt, cfg)),
            "frames_analyzed": nframes})
        log.info("  event %s %s→%s %s persons=%d tracks=%d faces=%d score=%.0f %s", eid, V.fmt_offset(s),
                 V.fmt_offset(e), kind, len(persons), len(summaries), faces_total, score,
                 ",".join(f["flag"] for f in flags))
        return True


def run(cfg: Config, progress=None, build_reports=True) -> dict:
    return Analyzer(cfg, progress).run(build_reports)

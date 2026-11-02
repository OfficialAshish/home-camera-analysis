"""Evidence packages for police / insurers / legal review.

``export_evidence`` writes one ZIP containing, per selected event:

* ``original_segment.<ext>`` – the event cut from the source recording by **stream copy**
  (no re-encoding: picture data identical to the recording; the cut can start up to one
  keyframe early, which is recorded in event.json),
* the event clip, raw + annotated snapshots, contact sheet, face / person / plate crops,
* ``event.json`` – times, time source, camera, source file + offsets, flags with reasons, AI
  description, plates, identities,

plus ``incident_report.html`` (printable), ``manifest.json`` (SHA-256 of every file in the
package and of each *full* source recording, tool version, operator, case id, export time)
and ``manifest.sha256`` (the manifest's own hash, to quote in correspondence).

``redact=True`` blurs faces (and number plates when ``redact_plates``) in every image and clip
and leaves out the unredacted original segments – for sharing with people who must not see
bystanders.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from typing import Iterable, List, Optional

import cv2
import numpy as np

from . import __version__
from . import video_io as V

log = logging.getLogger("cctv")


def sha256_file(path: str, chunk: int = 4 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _cut_original(src: str, start: float, end: float, out: str) -> bool:
    if not V.FFMPEG:
        return False
    r = subprocess.run([V.FFMPEG, "-nostdin", "-y", "-v", "error", "-ss", f"{max(0.0, start):.3f}",
                        *V._input_args(src), "-i", src, "-t", f"{max(0.5, end - start):.3f}", "-map", "0",
                        "-c", "copy", "-avoid_negative_ts", "make_zero", out], capture_output=True, timeout=1800)
    return r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0


class Redactor:
    """Blurs faces (YuNet) and optionally number plates (plate detector) in images and videos."""

    def __init__(self, cfg, plates: bool = False):
        from .faces import FaceEngine
        self.fe = FaceEngine(cfg)
        self.pd = None
        if plates:
            try:
                from .plates import YoloPlateDetector
                self.pd = YoloPlateDetector()
            except Exception as e:
                from .plates import ClassicPlateDetector
                log.warning("plate detector unavailable for redaction (%s) – using OpenCV plate finder", e)
                self.pd = ClassicPlateDetector()

    def boxes(self, img: np.ndarray) -> List[List[int]]:
        out = []
        for f in self.fe.detect_full(img):
            x1, y1, x2, y2 = f["box"]
            w, h = x2 - x1, y2 - y1
            out.append([int(x1 - 0.25 * w), int(y1 - 0.35 * h), int(x2 + 0.25 * w), int(y2 + 0.25 * h)])
        if self.pd is not None:
            out += [[int(v) for v in b] for b, _ in self.pd.find(img)]
        return out

    def image(self, img: np.ndarray) -> np.ndarray:
        img = img.copy()
        H, W = img.shape[:2]
        for x1, y1, x2, y2 in self.boxes(img):
            x1, y1, x2, y2 = max(0, x1), max(0, y1), min(W, x2), min(H, y2)
            if x2 > x1 and y2 > y1:
                roi = img[y1:y2, x1:x2]
                k = max(15, (max(roi.shape[:2]) // 3) | 1)
                img[y1:y2, x1:x2] = cv2.GaussianBlur(roi, (k, k), 0)
        return img

    def video(self, src: str, out: str, fps: float = 10.0, width: int = 960) -> bool:
        meta = V.probe(src)
        if not meta["width"]:
            return False
        w = min(width, meta["width"]) // 2 * 2
        h = int(round(meta["height"] * w / meta["width"])) // 2 * 2
        proc = subprocess.Popen([V.FFMPEG, "-nostdin", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                                 "-s", f"{w}x{h}", "-r", str(fps), "-i", "pipe:0", "-c:v", "libx264", "-preset",
                                 "veryfast", "-crf", "24", "-pix_fmt", "yuv420p", out],
                                stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        n = 0
        try:
            for _, f in V.iter_frames(src, fps, w, src_size=(meta["width"], meta["height"])):
                f = cv2.resize(f, (w, h)) if f.shape[1] != w or f.shape[0] != h else f
                proc.stdin.write(self.image(f).tobytes())
                n += 1
            proc.stdin.close()
            proc.wait(timeout=1800)
        except Exception as e:
            log.warning("redaction of %s failed: %s", src, e)
            proc.kill()
            return False
        return proc.returncode == 0 and n > 0


def _report_html(case: dict, events: List[dict], files: dict) -> str:
    E = html.escape
    rows = []
    for e in events:
        flags = "".join(f"<li><b>{E(f['flag'])}</b> (+{f['points']:g}) {E(f['reason'])}</li>" for f in e["flags"])
        snaps = "".join(f'<img src="{E(p)}" style="width:32%;margin:2px">' for p in e["images"][:6])
        vlm = (e.get("vlm") or {}).get("description", "")
        plates = ", ".join(f"{p['text']} ({p['confidence']:.2f})" for p in e["plates"]) or "–"
        rows.append(f"""<section><h2>{E(e['event_id'])} — {E(e['level'])} {e['score']:g}</h2>
<table><tr><th>Camera</th><td>{E(e['camera'] or '')}</td><th>Recorded</th><td>{E(e['start_dt'] or 'unknown')} → {E((e['end_dt'] or '')[11:])}</td></tr>
<tr><th>Time source</th><td>{E(e['time_source'] or '')}</td><th>Duration</th><td>{e['duration']:.0f} s</td></tr>
<tr><th>Source file</th><td colspan=3>{E(e['rel_path'])} @ {V.fmt_offset(e['start_off'])}–{V.fmt_offset(e['end_off'])}<br>
<small>SHA-256 {E(files.get(e['video_path'], ''))}</small></td></tr>
<tr><th>Number plates</th><td>{E(plates)}</td><th>People / identities</th><td>{e['n_person_tracks']} / {E(', '.join(e['identities'] or []))}</td></tr></table>
{f'<p><b>AI description:</b> {E(vlm)}</p>' if vlm else ''}<ul>{flags or '<li>no flags</li>'}</ul>{snaps}</section>""")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Incident report {E(case['case_id'] or '')}</title>
<style>body{{font-family:Arial,sans-serif;margin:24px;color:#111}} h1{{font-size:20px}} h2{{font-size:16px;border-bottom:1px solid #999}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0}} th,td{{border:1px solid #bbb;padding:4px;text-align:left;vertical-align:top}}
th{{background:#eee;width:14%}} section{{page-break-inside:avoid;margin-bottom:18px}} small{{color:#555}}
@media print {{ a{{color:#000}} }}</style></head><body>
<h1>CCTV incident report{(' — case ' + E(case['case_id'])) if case['case_id'] else ''}</h1>
<table><tr><th>Exported</th><td>{E(case['exported_at'])}</td><th>Operator</th><td>{E(case['operator'] or '')}</td></tr>
<tr><th>Tool</th><td>home-camera-analysis {E(__version__)}</td><th>Events</th><td>{len(events)}</td></tr>
<tr><th>Redacted</th><td>{'yes – faces' + (' and plates' if case['redact_plates'] else '') + ' blurred; originals not included' if case['redacted'] else 'no'}</td>
<th>Notes</th><td>{E(case['notes'] or '')}</td></tr></table>
<p><small>Integrity: every file in this package is listed with its SHA-256 in manifest.json; the manifest's own hash is in
manifest.sha256. Recording times come from the source named in "Time source" (filename, DVR clock via OCR, metadata, or an
approximate file time). Scores are a triage aid, not a finding of fact.</small></p>
{''.join(rows)}</body></html>"""


def export_evidence(out_dir: str, event_ids: Iterable[str], dest_zip: str, cfg=None, operator: str = "",
                    case_id: str = "", notes: str = "", redact: bool = False, redact_plates: bool = False,
                    progress=None) -> dict:
    from .config import Config
    cfg = cfg or Config(output_dir=out_dir)
    ids = list(dict.fromkeys(event_ids))
    if not ids:
        raise ValueError("no events selected")
    db = sqlite3.connect(f"file:{os.path.join(out_dir, 'analysis.db')}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    q = ",".join("?" * len(ids))
    evs = [dict(r) for r in db.execute(f"SELECT * FROM events WHERE event_id IN ({q})", ids)]
    found = {e["event_id"] for e in evs}
    missing = [i for i in ids if i not in found]
    if missing:
        raise KeyError(f"unknown event id(s): {', '.join(missing[:5])}")
    plates = {}
    for r in db.execute(f"SELECT * FROM plates WHERE event_id IN ({q})", ids):
        plates.setdefault(r["event_id"], []).append(dict(r))
    tracks = {}
    for r in db.execute(f"SELECT event_id, track, cls, identity, known_name, face_crop, person_crop FROM tracks "
                        f"WHERE event_id IN ({q})", ids):
        tracks.setdefault(r["event_id"], []).append(dict(r))
    db.close()
    red = Redactor(cfg, redact_plates) if redact else None
    exported_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    case = {"case_id": case_id, "operator": operator, "notes": notes, "exported_at": exported_at,
            "redacted": bool(redact), "redact_plates": bool(redact_plates)}
    src_hash = {}
    report_events = []
    tmp = tempfile.mkdtemp(prefix="evidence_")
    try:
        for n, e in enumerate(sorted(evs, key=lambda x: (x["start_dt"] or "", x["event_id"])), 1):
            if progress:
                progress(n, len(evs), e["event_id"])
            for k in ("flags", "vlm", "snapshots", "objects", "identities", "semantic", "plates", "crossings"):
                try:
                    e[k] = json.loads(e[k]) if isinstance(e.get(k), str) else e.get(k)
                except (TypeError, json.JSONDecodeError):
                    pass
            e["flags"] = e.get("flags") or []
            d = os.path.join(tmp, "events", e["event_id"])
            os.makedirs(d, exist_ok=True)
            src = e["video_path"]
            if os.path.exists(src) and src not in src_hash:
                src_hash[src] = sha256_file(src)
            note = {}
            if not redact and os.path.exists(src):
                ext = os.path.splitext(src)[1] or ".mp4"
                if _cut_original(src, e["start_off"], e["end_off"], os.path.join(d, "original_segment" + ext)):
                    note["original_segment"] = ("stream copy of the recording (not re-encoded); it may begin up to "
                                                "one keyframe before the event start")
            images = []

            def put(path, name):
                if not path or not os.path.exists(path):
                    return None
                dst = os.path.join(d, name)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if red is not None and name.lower().endswith((".jpg", ".png")):
                    img = cv2.imread(path)
                    if img is None:
                        return None
                    cv2.imwrite(dst, red.image(img), [cv2.IMWRITE_JPEG_QUALITY, 92])
                elif red is not None and name.lower().endswith(".mp4"):
                    if not red.video(path, dst):
                        return None
                else:
                    shutil.copyfile(path, dst)
                return os.path.relpath(dst, tmp).replace(os.sep, "/")

            for i, s in enumerate(e.get("snapshots") or [], 1):
                p = put(s.get("raw"), f"snapshots/snap{i:02d}_raw.jpg")
                a = put(s.get("annotated"), f"snapshots/snap{i:02d}_annotated.jpg")
                images += [x for x in (a or p,) if x]
            put(e.get("contact_sheet"), "contact_sheet.jpg")
            put(e.get("clip_path"), "clip.mp4")
            for t in tracks.get(e["event_id"], []):
                put(t["face_crop"], f"faces/track{t['track']:03d}.jpg")
                put(t["person_crop"], f"people/track{t['track']:03d}.jpg")
            pl = plates.get(e["event_id"], [])
            for p in pl:
                put(p["crop_path"], f"plates/track{p['track']:03d}_plate.jpg")
                put(p["enhanced_path"], f"plates/track{p['track']:03d}_enhanced.jpg")
            meta = {k: e.get(k) for k in ("event_id", "camera", "start_dt", "end_dt", "duration", "time_source",
                                         "rel_path", "start_off", "end_off", "kind", "level", "score", "flags",
                                         "identities", "max_persons", "n_person_tracks", "objects", "crossings")}
            meta["ai_description"] = (e.get("vlm") or {}).get("description", "") if isinstance(e.get("vlm"), dict) else ""
            meta["plates"] = [{k: p[k] for k in ("text", "confidence", "valid_format", "vehicle", "t_off", "dt",
                                                  "method", "watchlist")} for p in pl]
            meta["source_sha256"] = src_hash.get(src, "")
            meta["notes"] = note
            with open(os.path.join(d, "event.json"), "w", encoding="utf-8") as fh:
                json.dump(meta, fh, indent=1, default=str)
            report_events.append({**e, "plates": [p for p in meta["plates"] if p["text"]],
                                   "images": [f"events/{e['event_id']}/" + x.split(f"events/{e['event_id']}/", 1)[-1]
                                              for x in images]})
        with open(os.path.join(tmp, "incident_report.html"), "w", encoding="utf-8") as fh:
            fh.write(_report_html(case, report_events, src_hash))
        files = {}
        for root, _, fs in os.walk(tmp):
            for f in fs:
                p = os.path.join(root, f)
                files[os.path.relpath(p, tmp).replace(os.sep, "/")] = sha256_file(p)
        manifest = {"tool": f"home-camera-analysis {__version__}", **case,
                    "events": [e["event_id"] for e in report_events],
                    "sources": [{"path": os.path.relpath(p, cfg.input_dir) if cfg.input_dir and os.path.isdir(cfg.input_dir)
                                 else os.path.basename(p), "sha256": h, "bytes": os.path.getsize(p)}
                                for p, h in src_hash.items()],
                    "files": files}
        mtext = json.dumps(manifest, indent=1, sort_keys=True)
        with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as fh:
            fh.write(mtext)
        mhash = hashlib.sha256(mtext.encode()).hexdigest()
        with open(os.path.join(tmp, "manifest.sha256"), "w", encoding="utf-8") as fh:
            fh.write(f"{mhash}  manifest.json\n")
        os.makedirs(os.path.dirname(os.path.abspath(dest_zip)), exist_ok=True)
        part = dest_zip + ".part"
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, fs in os.walk(tmp):
                for f in sorted(fs):
                    p = os.path.join(root, f)
                    z.write(p, os.path.relpath(p, tmp))
        os.replace(part, dest_zip)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"zip": dest_zip, "zip_sha256": sha256_file(dest_zip), "manifest_sha256": mhash,
            "events": len(report_events), "files": len(files) + 2, "redacted": bool(redact)}


def verify_package(zip_path: str) -> dict:
    """Re-hash every file of an evidence ZIP against its manifest."""
    with zipfile.ZipFile(zip_path) as z:
        manifest_text = z.read("manifest.json").decode()
        manifest = json.loads(manifest_text)
        expect = z.read("manifest.sha256").decode().split()[0]
        bad = [n for n, h in manifest["files"].items() if hashlib.sha256(z.read(n)).hexdigest() != h]
        extra = [n for n in z.namelist() if n not in manifest["files"] and n not in ("manifest.json", "manifest.sha256")]
    ok_manifest = hashlib.sha256(manifest_text.encode()).hexdigest() == expect
    return {"ok": ok_manifest and not bad and not extra, "manifest_ok": ok_manifest, "modified": bad, "unlisted": extra,
            "files": len(manifest["files"])}

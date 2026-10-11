"""Reports built from the SQLite store: Excel workbook (with thumbnails,
clickable citations, review columns, charts), HTML dashboard, PNG charts and a
highlight reel of the most suspicious events."""
from __future__ import annotations

import base64
import csv
import datetime as dt
import html
import json
import logging
import os
import subprocess
from collections import Counter, defaultdict

import cv2

from . import video_io as V
from .rules import WEIGHTS

log = logging.getLogger("cctv")
LEVEL_COLORS = {"HIGH": "#F8696B", "REVIEW": "#FFB347", "LOW": "#FFF2A8", "NORMAL": "#E2EFDA"}
EXCEL_MAX_ROWS = 1_000_000


def _j(s, default=None):
    if s in (None, ""):
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def _rel(path: str, base: str) -> str:
    if not path:
        return ""
    try:
        return os.path.relpath(path, base)
    except ValueError:
        return path


def _thumb(src: str, out_dir: str, w: int = 200) -> str:
    if not src or not os.path.exists(src):
        return ""
    dst = os.path.join(out_dir, "thumbs", os.path.relpath(src, out_dir).replace(os.sep, "__"))
    dst = os.path.splitext(dst)[0] + ".jpg"
    if os.path.exists(dst):
        return dst
    img = cv2.imread(src)
    if img is None:
        return ""
    h = max(1, int(img.shape[0] * w / img.shape[1]))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    cv2.imwrite(dst, cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA), [cv2.IMWRITE_JPEG_QUALITY, 80])
    return dst


def _flags_text(flags) -> str:
    return "; ".join(f"{f['flag']} (+{f['points']:g}): {f['reason']}" for f in flags or [])


def _vlm_text(v) -> str:
    if not v:
        return ""
    d = v.get("description") or ""
    if v.get("suspicious"):
        d = f"[SUSPICIOUS: {v.get('category') or 'yes'}] " + d + (f" — {v['reason']}" if v.get("reason") else "")
    return d


def load(store):
    events = store.query("SELECT * FROM events ORDER BY COALESCE(start_dt, ''), camera, start_off")
    for e in events:
        for k in ("flags", "semantic", "vlm", "snapshots", "objects", "identities"):
            e[k] = _j(e[k], [] if k != "vlm" and k != "objects" else ({} if k == "objects" else None))
    videos = store.query("SELECT * FROM videos ORDER BY camera, start_dt, path")
    tracks = store.query("SELECT t.*, e.camera, e.video_path, e.rel_path, e.start_dt AS ev_dt FROM tracks t "
                         "JOIN events e USING(event_id) ORDER BY t.first_dt, t.event_id, t.track")
    faces = store.query("SELECT f.*, e.camera, e.rel_path, e.video_path FROM faces f JOIN events e USING(event_id) "
                        "ORDER BY f.dt, f.event_id")
    tamper = store.query("SELECT t.*, v.rel_path, v.camera FROM tamper t JOIN videos v ON v.id=t.video_id "
                         "ORDER BY t.start_dt")
    return events, videos, tracks, faces, tamper


def identities_table(tracks):
    ids = defaultdict(lambda: {"tracks": 0, "events": set(), "cameras": set(), "first": None, "last": None,
                               "name": "", "sample": "", "best_sim": 0})
    for t in tracks:
        if not t["identity"]:
            continue
        r = ids[t["identity"]]
        r["tracks"] += 1
        r["events"].add(t["event_id"])
        r["cameras"].add(t["camera"])
        when = t["first_dt"] or ""
        r["first"] = min(filter(None, [r["first"], when]), default=when)
        r["last"] = max(filter(None, [r["last"], when]), default=when)
        if t["known_name"]:
            r["name"] = t["known_name"]
        if t["face_crop"] and (t["identity_sim"] or 0) >= r["best_sim"]:
            r["sample"], r["best_sim"] = t["face_crop"], t["identity_sim"] or 0
    return dict(sorted(ids.items(), key=lambda kv: -len(kv[1]["events"])))


# ====================================================================== Excel
def build_excel(store, cfg, path):
    import xlsxwriter
    out = cfg.output_dir
    events, videos, tracks, faces, tamper = load(store)
    idt = identities_table(tracks)
    wb = xlsxwriter.Workbook(path, {"strings_to_urls": False, "nan_inf_to_errors": True})
    F = {
        "h": wb.add_format({"bold": True, "bg_color": "#1F3864", "font_color": "white", "text_wrap": True,
                            "valign": "vcenter", "border": 1}),
        "title": wb.add_format({"bold": True, "font_size": 16, "font_color": "#1F3864"}),
        "b": wb.add_format({"bold": True}),
        "wrap": wb.add_format({"text_wrap": True, "valign": "top"}),
        "top": wb.add_format({"valign": "top"}),
        "link": wb.add_format({"font_color": "blue", "underline": 1, "valign": "top"}),
        "num": wb.add_format({"num_format": "0.00", "valign": "top"}),
        "int": wb.add_format({"num_format": "0", "valign": "top"}),
        "pct": wb.add_format({"num_format": "0.0%", "valign": "top"}),
    }
    for lv, c in LEVEL_COLORS.items():
        F[lv] = wb.add_format({"bg_color": c, "bold": True, "valign": "top", "align": "center"})

    def link(ws, r, c, target, text):
        if target:
            ws.write_url(r, c, target if target.startswith("http") else "external:" + target, F["link"], text)
        else:
            ws.write_blank(r, c, None)

    thumbs_used = [0]

    def image(ws, r, c, src, w=150):
        if not cfg.excel_thumbnails or thumbs_used[0] >= cfg.excel_max_thumbnails:
            return
        t = _thumb(src, out, 200)
        if t:
            img = cv2.imread(t)
            if img is None:
                return
            sc = w / img.shape[1]
            ws.insert_image(r, c, t, {"x_scale": sc, "y_scale": sc, "x_offset": 2, "y_offset": 2,
                                      "object_position": 1})
            thumbs_used[0] += 1

    # ---------------------------------------------------------- Read Me
    ws = wb.add_worksheet("Read Me")
    ws.set_column(0, 0, 28)
    ws.set_column(1, 1, 110, F["wrap"])
    ws.write(0, 0, "CCTV Analysis Report", F["title"])
    ws.write(1, 0, "Generated")
    ws.write(1, 1, dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    rows = [
        ("How to review", "Start with the 'Events' sheet. It is sorted by time; filter the Level column to HIGH/REVIEW "
                          "or sort by Score. Each row cites the exact video file, the offset inside the video "
                          "(HH:MM:SS) and the real clock time. Click 'Clip' to watch the event, 'Snapshot' or "
                          "'Contact sheet' for stills, 'Video (Drive)' to open the original recording."),
        ("Links", "File links are relative to this workbook. They work when the workbook is opened from the "
                  "output folder (Google Drive for desktop, or after downloading the whole folder). "
                  "Drive links open in the browser."),
        ("Seek in VLC", "Open the original file in VLC and jump to 'Video offset start' (Ctrl+T)."),
        ("Score / Level", "Score 0-100 is the sum of explainable flags (see 'Flags'). HIGH ≥ 70, REVIEW ≥ "
                          f"{cfg.suspicion_alert_threshold:g}, LOW > 0, NORMAL = 0. It is a triage aid, not proof."),
        ("Identities", "P#### are anonymous face clusters that persist between runs (same person ≈ same ID). "
                       "Put photos in the known-faces folder (one sub-folder per person) to label staff/owner."),
        ("Review columns", "'Review status' and 'Notes' are for you; they are kept only in this file."),
        ("Shop hours", f"{cfg.open_hour}–{cfg.close_hour}, timezone {cfg.timezone}. Activity outside = after_hours."),
    ]
    for i, (a, b) in enumerate(rows, 3):
        ws.write(i, 0, a, F["b"])
        ws.write(i, 1, b)
    r0 = len(rows) + 4
    ws.write(r0, 0, "Flag", F["h"])
    ws.write(r0, 1, "Points (max) / meaning", F["h"])
    meaning = {
        "after_hours": "movement/people outside opening hours", "camera_tamper": "lens covered, blackout, blur or camera moved",
        "weapon_like_object": "knife / scissors / bat detected", "restricted_zone": "person inside a configured zone (e.g. cash counter)",
        "loitering": f"person stayed ≥ {cfg.loiter_seconds:g}s", "running": "very fast movement",
        "crowd": f"≥ {cfg.crowd_size} people at once", "face_hidden": "person facing camera but face not visible (mask/helmet)",
        "hands_up": "both hands above shoulders", "crouching": "crouching / bending low",
        "lights_after_hours": "lights switched on/off while the shop is closed", "semantic_match": "SigLIP-2 image-text match with a suspicious description",
        "vlm_suspicious": "the vision-language model judged the event suspicious",
        "unknown_person_after_hours": "unrecognised face after hours", "repeat_visitor": "same unknown face in ≥ 5 events",
        "long_event": "event ≥ 5 min with people"}
    for i, (k, v) in enumerate(WEIGHTS.items(), r0 + 1):
        ws.write(i, 0, k)
        ws.write(i, 1, f"{v} — {meaning.get(k, '')}")

    # ---------------------------------------------------------- Summary
    ws = wb.add_worksheet("Summary")
    ws.set_column(0, 0, 34)
    ws.set_column(1, 8, 16)
    ws.write(0, 0, "Summary", F["title"])
    tot_dur = sum(v["duration"] or 0 for v in videos)
    tot_motion = sum(v["motion_seconds"] or 0 for v in videos)
    lv = Counter(e["level"] for e in events)
    kpis = [
        ("Videos analysed", sum(1 for v in videos if v["status"] == "done")),
        ("Videos failed", sum(1 for v in videos if v["status"] not in ("done",))),
        ("Footage analysed (hours)", round(tot_dur / 3600, 2)),
        ("Motion time (hours)", round(tot_motion / 3600, 2)),
        ("Footage you can skip", f"{(1 - tot_motion / tot_dur) * 100:.1f}%" if tot_dur else "-"),
        ("Events", len(events)),
        ("Events with people", sum(1 for e in events if e["n_person_tracks"])),
        ("HIGH", lv.get("HIGH", 0)), ("REVIEW", lv.get("REVIEW", 0)), ("LOW", lv.get("LOW", 0)),
        ("NORMAL", lv.get("NORMAL", 0)),
        ("Person tracks", sum(1 for t in tracks if t["cls"] == "person")),
        ("Face samples", len(faces)),
        ("Distinct face identities", len(idt)),
        ("Tamper incidents", len(tamper)),
        ("Date range", f"{min((e['start_dt'] for e in events if e['start_dt']), default='-')} → "
                       f"{max((e['end_dt'] for e in events if e['end_dt']), default='-')}"),
    ]
    for i, (a, b) in enumerate(kpis, 2):
        ws.write(i, 0, a, F["b"])
        ws.write(i, 1, b)
    r = len(kpis) + 3
    ws.write(r, 0, "Top suspicious events", F["b"])
    hdr = ["Event ID", "Level", "Score", "Camera", "Start", "Video", "Offset", "Main reasons"]
    for c, h in enumerate(hdr):
        ws.write(r + 1, c, h, F["h"])
    top = sorted(events, key=lambda e: -(e["score"] or 0))[:25]
    for i, e in enumerate(top, r + 2):
        ws.write_url(i, 0, f"internal:'Events'!A{events.index(e) + 2}", F["link"], e["event_id"])
        ws.write(i, 1, e["level"], F.get(e["level"]))
        ws.write(i, 2, e["score"])
        ws.write(i, 3, e["camera"])
        ws.write(i, 4, e["start_dt"] or "")
        ws.write(i, 5, e["rel_path"])
        ws.write(i, 6, V.fmt_offset(e["start_off"]))
        ws.write(i, 7, ", ".join(f["flag"] for f in e["flags"]))
    ws.set_column(7, 7, 60)
    # hourly table + native chart
    hr = r + 3 + len(top) + 1
    ws.write(hr, 0, "Events by hour of day", F["b"])
    ws.write(hr + 1, 0, "Hour", F["h"])
    ws.write(hr + 1, 1, "Events", F["h"])
    ws.write(hr + 1, 2, "With people", F["h"])
    ws.write(hr + 1, 3, "REVIEW+HIGH", F["h"])
    byh = defaultdict(lambda: [0, 0, 0])
    for e in events:
        if e["start_dt"]:
            h = int(e["start_dt"][11:13])
            byh[h][0] += 1
            byh[h][1] += bool(e["n_person_tracks"])
            byh[h][2] += e["level"] in ("HIGH", "REVIEW")
    for h in range(24):
        ws.write(hr + 2 + h, 0, f"{h:02d}:00")
        for c in range(3):
            ws.write(hr + 2 + h, 1 + c, byh[h][c])
    ch = wb.add_chart({"type": "column"})
    for c, name in ((1, "Events"), (3, "REVIEW+HIGH")):
        ch.add_series({"name": name, "categories": ["Summary", hr + 2, 0, hr + 25, 0],
                       "values": ["Summary", hr + 2, c, hr + 25, c]})
    ch.set_title({"name": "Activity by hour"})
    ch.set_size({"width": 720, "height": 300})
    ws.insert_chart(2, 3, ch)
    # per day per camera
    byd = defaultdict(Counter)
    for e in events:
        byd[(e["start_dt"] or "unknown")[:10]][e["camera"]] += 1
    cams = sorted({e["camera"] for e in events})
    dr = hr + 28
    ws.write(dr, 0, "Events per day and camera", F["b"])
    ws.write(dr + 1, 0, "Date", F["h"])
    for c, cam in enumerate(cams, 1):
        ws.write(dr + 1, c, cam, F["h"])
    for i, (d, cnt) in enumerate(sorted(byd.items()), dr + 2):
        ws.write(i, 0, d)
        for c, cam in enumerate(cams, 1):
            ws.write(i, c, cnt.get(cam, 0))

    # ---------------------------------------------------------- Events
    ws = wb.add_worksheet("Events")
    cols = [("Event ID", 26), ("Level", 9), ("Score", 7), ("Camera", 10), ("Date", 11), ("Start time", 10),
            ("End time", 10), ("Duration (s)", 9), ("Video file", 34), ("Video offset start", 10),
            ("Video offset end", 10), ("Seek (s)", 8), ("Kind", 11), ("Max people at once", 8),
            ("Person tracks", 8), ("Objects (unique tracks)", 26), ("Faces detected", 8), ("Identities", 18),
            ("Best detection conf", 9), ("Flags / reasons", 60), ("AI description (VLM)", 60),
            ("Scene tag (SigLIP-2)", 34), ("Snapshot", 24), ("Snapshot file", 12), ("Contact sheet", 12),
            ("Clip", 8), ("Video (Drive)", 10), ("Event folder", 12), ("After hours", 7), ("Motion peak", 8),
            ("Frames analysed", 8), ("Time source", 14), ("Review status", 14), ("Notes", 30)]
    for c, (h, w) in enumerate(cols):
        ws.write(0, c, h, F["h"])
        ws.set_column(c, c, w)
    vmap = {v["id"]: v for v in videos}
    snap_col = [c for c, (h, _) in enumerate(cols) if h == "Snapshot"][0]
    for i, e in enumerate(events, 1):
        v = vmap.get(e["video_id"], {})
        snaps = e["snapshots"] or []
        best = max(snaps, key=lambda s: s.get("persons", 0)) if snaps else {}
        sem = max(e["semantic"] or [{}], key=lambda s: s.get("suspicious_score", 0)) if e["semantic"] else {}
        row = [e["event_id"], e["level"], e["score"], e["camera"], (e["start_dt"] or "")[:10],
               (e["start_dt"] or "")[11:], (e["end_dt"] or "")[11:], e["duration"], e["rel_path"],
               V.fmt_offset(e["start_off"]), V.fmt_offset(e["end_off"]), round(e["start_off"], 1), e["kind"],
               e["max_persons"], e["n_person_tracks"],
               ", ".join(f"{k}×{n}" for k, n in sorted((e["objects"] or {}).items(), key=lambda kv: -kv[1])),
               e["faces_detected"], ", ".join(e["identities"] or []), e["best_conf"], _flags_text(e["flags"]),
               _vlm_text(e["vlm"]),
               f"{sem.get('top_label', '')} ({sem.get('top_score', 0):.2f})" if sem else ""]
        for c, val in enumerate(row):
            if c == 1:
                ws.write(i, c, val, F.get(val, F["top"]))
            elif c in (19, 20):
                ws.write(i, c, val, F["wrap"])
            else:
                ws.write(i, c, val, F["top"])
        c = snap_col
        image(ws, i, c, best.get("annotated", ""), 160)
        link(ws, i, c + 1, _rel(best.get("annotated", ""), out), "open")
        link(ws, i, c + 2, _rel(e["contact_sheet"], out), "sheet")
        link(ws, i, c + 3, _rel(e["clip_path"], out), "clip" if e["clip_path"] else "")
        link(ws, i, c + 4, v.get("drive_link") or "", "drive" if v.get("drive_link") else "")
        link(ws, i, c + 5, _rel(e["folder"], out), "folder")
        ws.write(i, c + 6, "yes" if e["after_hours"] else "")
        ws.write(i, c + 7, e["motion_peak"])
        ws.write(i, c + 8, e["frames_analyzed"])
        ws.write(i, c + 9, v.get("time_source", ""))
        ws.set_row(i, 92)
    n = len(events)
    if n:
        ws.autofilter(0, 0, n, len(cols) - 1)
        ws.conditional_format(1, 2, n, 2, {"type": "3_color_scale", "min_color": "#63BE7B",
                                           "mid_color": "#FFEB84", "max_color": "#F8696B"})
        ws.data_validation(1, len(cols) - 2, n, len(cols) - 2, {
            "validate": "list", "source": ["Not reviewed", "OK / normal", "Suspicious", "Theft confirmed",
                                           "Reported", "False alarm"]})
    ws.freeze_panes(1, 1)

    # ---------------------------------------------------------- People / tracks
    ws = wb.add_worksheet("People & Objects")
    cols = [("Event ID", 26), ("Track", 6), ("Class", 10), ("Camera", 9), ("First seen", 19), ("Seen for (s)", 8),
            ("Video file", 30), ("Video offset", 10), ("Max conf", 7), ("Mean conf", 7), ("Max speed (body-h/s)", 9),
            ("Zones (s)", 18), ("Identity", 9), ("Known name", 12), ("Match similarity", 8), ("Faces found", 7),
            ("Face hidden (s)", 8), ("Posture (s)", 26), ("Person crop", 14), ("Face", 12), ("Crop file", 8),
            ("Face file", 8)]
    for c, (h, w) in enumerate(cols):
        ws.write(0, c, h, F["h"])
        ws.set_column(c, c, w)
    for i, t in enumerate(tracks[:EXCEL_MAX_ROWS - 1], 1):
        z = _j(t["zones"], {}) or {}
        p = _j(t["posture"], {}) or {}
        vals = [t["event_id"], t["track"], t["cls"], t["camera"], t["first_dt"] or "", t["duration"], t["rel_path"],
                V.fmt_offset(t["first_off"]), t["max_conf"], t["mean_conf"], t["max_speed"],
                ", ".join(f"{k}: {v:g}" for k, v in z.items()), t["identity"] or "", t["known_name"] or "",
                t["identity_sim"], t["faces_found"], t["hidden_face_s"],
                ", ".join(f"{k}={v:g}" for k, v in p.items() if v)]
        for c, val in enumerate(vals):
            ws.write(i, c, val, F["top"])
        big = i <= cfg.excel_max_thumbnails and (t["person_crop"] or t["face_crop"])
        if big:
            image(ws, i, 18, t["person_crop"], 60)
            image(ws, i, 19, t["face_crop"], 60)
            ws.set_row(i, 90)
        link(ws, i, 20, _rel(t["person_crop"], out), "crop" if t["person_crop"] else "")
        link(ws, i, 21, _rel(t["face_crop"], out), "face" if t["face_crop"] else "")
    if tracks:
        ws.autofilter(0, 0, min(len(tracks), EXCEL_MAX_ROWS - 1), len(cols) - 1)
    ws.freeze_panes(1, 1)

    # ---------------------------------------------------------- Identities
    ws = wb.add_worksheet("Face Identities")
    cols = [("Identity", 10), ("Known name", 14), ("Sample face", 14), ("Events", 8), ("Appearances", 8),
            ("Cameras", 14), ("First seen", 19), ("Last seen", 19), ("Event IDs", 80)]
    for c, (h, w) in enumerate(cols):
        ws.write(0, c, h, F["h"])
        ws.set_column(c, c, w)
    for i, (pid, r_) in enumerate(idt.items(), 1):
        vals = [pid, r_["name"], "", len(r_["events"]), r_["tracks"], ", ".join(sorted(r_["cameras"])),
                r_["first"], r_["last"], ", ".join(sorted(r_["events"]))]
        for c, val in enumerate(vals):
            ws.write(i, c, val, F["wrap"] if c == 8 else F["top"])
        image(ws, i, 2, r_["sample"], 80)
        ws.set_row(i, 85)
    ws.freeze_panes(1, 1)

    # ---------------------------------------------------------- Faces
    ws = wb.add_worksheet("Faces")
    cols = [("Event ID", 26), ("Track", 6), ("Time", 19), ("Camera", 9), ("Video file", 30), ("Video offset", 10),
            ("Detector score", 8), ("Quality", 7), ("Identity", 9), ("Known name", 12), ("Similarity", 8),
            ("Face", 12), ("Face file", 8)]
    for c, (h, w) in enumerate(cols):
        ws.write(0, c, h, F["h"])
        ws.set_column(c, c, w)
    for i, f in enumerate(faces[:EXCEL_MAX_ROWS - 1], 1):
        vals = [f["event_id"], f["track"], f["dt"] or "", f["camera"], f["rel_path"], V.fmt_offset(f["t_off"]),
                f["score"], f["quality"], f["identity"] or "", f["known_name"] or "", f["similarity"]]
        for c, val in enumerate(vals):
            ws.write(i, c, val, F["top"])
        if i <= 1500:
            image(ws, i, 11, f["crop_path"], 60)
            ws.set_row(i, 70)
        link(ws, i, 12, _rel(f["crop_path"], out), "face")
    if faces:
        ws.autofilter(0, 0, min(len(faces), EXCEL_MAX_ROWS - 1), len(cols) - 1)
    ws.freeze_panes(1, 1)

    # ---------------------------------------------------------- Detections
    ws = wb.add_worksheet("Detections (1 per s)")
    cols = ["Event ID", "Video offset (s)", "Time", "Class", "Confidence", "Track", "x1", "y1", "x2", "y2"]
    for c, h in enumerate(cols):
        ws.write(0, c, h, F["h"])
    ws.set_column(0, 0, 26)
    ws.set_column(2, 2, 19)
    cur = store.db.execute("SELECT event_id,t_off,dt,cls,conf,track,x1,y1,x2,y2 FROM detections ORDER BY dt, event_id, t_off")
    csv_path = os.path.join(out, "detections_full.csv")
    i = 0
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for row in cur:
            row = list(row)
            w.writerow(row)
            i += 1
            if i < EXCEL_MAX_ROWS:
                ws.write_row(i, 0, row)
    if i:
        ws.autofilter(0, 0, min(i, EXCEL_MAX_ROWS - 1), len(cols) - 1)
    ws.freeze_panes(1, 0)

    # ---------------------------------------------------------- Videos
    ws = wb.add_worksheet("Videos")
    cols = [("Video file", 40), ("Camera", 9), ("Recording start", 19), ("Recording end", 19), ("Time source", 16),
            ("Duration", 10), ("Resolution", 11), ("FPS", 6), ("Codec", 8), ("Size (MB)", 9), ("Events", 7),
            ("Motion (s)", 9), ("Motion %", 8), ("Lighting changes", 8), ("Clock masked", 7),
            ("Processing (s)", 9), ("Speed (x real-time)", 9), ("Status", 8), ("Error", 40), ("Heatmap", 10),
            ("Drive link", 10), ("Models", 40)]
    for c, (h, w) in enumerate(cols):
        ws.write(0, c, h, F["h"])
        ws.set_column(c, c, w)
    for i, v in enumerate(videos, 1):
        d = v["duration"] or 0
        vals = [v["rel_path"], v["camera"], v["start_dt"] or "", v["end_dt"] or "", v["time_source"],
                V.fmt_offset(d), f"{v['width']}x{v['height']}", round(v["fps"] or 0, 2), v["codec"],
                round(v["size_mb"] or 0, 1), v["n_events"], round(v["motion_seconds"] or 0, 1)]
        for c, val in enumerate(vals):
            ws.write(i, c, val, F["top"])
        ws.write(i, 12, (v["motion_seconds"] or 0) / d if d else 0, F["pct"])
        ws.write(i, 13, v["lighting_changes"])
        ws.write(i, 14, "yes" if v["osd_masked"] else "")
        ws.write(i, 15, round(v["proc_seconds"] or 0, 1))
        ws.write(i, 16, round(d / v["proc_seconds"], 1) if v["proc_seconds"] else "")
        ws.write(i, 17, v["status"])
        ws.write(i, 18, v["error"] or "", F["wrap"])
        link(ws, i, 19, _rel(v["heatmap_path"], out), "heatmap" if v["heatmap_path"] else "")
        link(ws, i, 20, v["drive_link"] or "", "drive" if v["drive_link"] else "")
        ws.write(i, 21, v["models"] or "")
    ws.freeze_panes(1, 1)

    # ---------------------------------------------------------- Tamper
    ws = wb.add_worksheet("Tamper & Camera")
    cols = [("Video file", 40), ("Camera", 9), ("Start", 19), ("Video offset", 10), ("Duration (s)", 9), ("Kind", 30)]
    for c, (h, w) in enumerate(cols):
        ws.write(0, c, h, F["h"])
        ws.set_column(c, c, w)
    for i, t in enumerate(tamper, 1):
        ws.write_row(i, 0, [t["rel_path"], t["camera"], t["start_dt"] or "", V.fmt_offset(t["start_off"]),
                            round(t["end_off"] - t["start_off"], 1), t["kind"]])

    # ---------------------------------------------------------- Hourly heat table
    ws = wb.add_worksheet("Activity Grid")
    ws.write(0, 0, "People-events per date × hour (colour = busier)", F["b"])
    ws.write(1, 0, "Date", F["h"])
    for h in range(24):
        ws.write(1, 1 + h, f"{h:02d}", F["h"])
    grid = defaultdict(Counter)
    for e in events:
        if e["start_dt"] and e["n_person_tracks"]:
            grid[e["start_dt"][:10]][int(e["start_dt"][11:13])] += 1
    for i, d in enumerate(sorted(grid), 2):
        ws.write(i, 0, d)
        for h in range(24):
            ws.write(i, 1 + h, grid[d].get(h, 0))
    if grid:
        ws.conditional_format(2, 1, 1 + len(grid), 24, {"type": "2_color_scale", "min_color": "#FFFFFF",
                                                         "max_color": "#F8696B"})
    ws.set_column(0, 0, 12)
    ws.set_column(1, 24, 4)

    # ---------------------------------------------------------- Config
    ws = wb.add_worksheet("Config")
    ws.set_column(0, 0, 28)
    ws.set_column(1, 1, 120, F["wrap"])
    for i, (k, v) in enumerate(cfg.to_dict().items()):
        ws.write(i, 0, k, F["b"])
        ws.write(i, 1, json.dumps(v) if isinstance(v, (list, dict)) else str(v))
    wb.close()
    log.info("Excel report: %s (%d events, %d thumbnails)", path, len(events), thumbs_used[0])


# ====================================================================== HTML
def build_html(store, cfg, path, max_embedded=1500):
    out = cfg.output_dir
    events, videos, tracks, faces, tamper = load(store)
    idt = identities_table(tracks)
    by_score = sorted(events, key=lambda e: -(e["score"] or 0))
    embed_ids = {e["event_id"] for e in by_score[:max_embedded]}

    def b64(src, w=360):
        t = _thumb(src, out, w)
        if not t:
            return ""
        with open(t, "rb") as fh:
            return "data:image/jpeg;base64," + base64.b64encode(fh.read()).decode()

    cards = []
    for e in events:
        snaps = e["snapshots"] or []
        best = max(snaps, key=lambda s: s.get("persons", 0)) if snaps else {}
        img = b64(best.get("annotated", "")) if e["event_id"] in embed_ids else ""
        flags = "".join(f"<li><b>{html.escape(f['flag'])}</b> +{f['points']:g}: {html.escape(f['reason'])}</li>"
                        for f in e["flags"])
        vlm = html.escape(_vlm_text(e["vlm"]))
        links = [f'<a href="{html.escape(_rel(e["contact_sheet"], out))}" target="_blank">contact sheet</a>']
        if e["clip_path"]:
            links.append(f'<a href="{html.escape(_rel(e["clip_path"], out))}" target="_blank">clip</a>')
        links.append(f'<a href="{html.escape(_rel(e["folder"], out))}" target="_blank">folder</a>')
        objs = ", ".join(f"{k}×{n}" for k, n in (e["objects"] or {}).items())
        cards.append(f"""
<div class="card" data-level="{e['level']}" data-cam="{html.escape(e['camera'] or '')}" data-date="{(e['start_dt'] or '')[:10]}"
 data-score="{e['score']}" data-people="{e['n_person_tracks']}" data-text="{html.escape((e['event_id'] + ' ' + objs + ' ' + vlm + ' ' + ' '.join(e['identities'] or [])).lower())}">
 <div class="hd"><span class="lv {e['level']}">{e['level']} {e['score']:g}</span> <b>{html.escape(e['event_id'])}</b></div>
 {'<a href="' + html.escape(_rel(best.get('annotated', ''), out)) + '" target="_blank"><img loading="lazy" src="' + img + '"></a>' if img else ''}
 <div class="meta">🕒 {e['start_dt'] or '?'} → {(e['end_dt'] or '')[11:]} ({e['duration']:.0f}s)<br>
 🎞 {html.escape(e['rel_path'])} @ <b>{V.fmt_offset(e['start_off'])}</b>–{V.fmt_offset(e['end_off'])}<br>
 👥 {e['max_persons']} max · {e['n_person_tracks']} tracks · faces {e['faces_detected']} {html.escape(', '.join(e['identities'] or []))}<br>
 📦 {html.escape(objs)}</div>
 {'<div class="vlm">🤖 ' + vlm + '</div>' if vlm else ''}
 {'<ul class="flags">' + flags + '</ul>' if flags else ''}
 <div class="links">{' · '.join(links)}</div></div>""")
    cams = sorted({e["camera"] for e in events})
    idcards = "".join(
        f'<div class="id"><img src="{b64(r_["sample"], 120)}"><br><b>{pid}</b> {html.escape(r_["name"])}'
        f'<br>{len(r_["events"])} events</div>' for pid, r_ in list(idt.items())[:200] if r_["sample"])
    tot = sum(v["duration"] or 0 for v in videos)
    mot = sum(v["motion_seconds"] or 0 for v in videos)
    lv = Counter(e["level"] for e in events)
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>CCTV Analysis Report</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:system-ui,Segoe UI,Arial;margin:0;background:#f4f6fa;color:#222}}
header{{background:#1f3864;color:#fff;padding:14px 20px}} h1{{margin:0;font-size:22px}}
.kpi{{display:flex;flex-wrap:wrap;gap:10px;padding:12px 20px}} .kpi div{{background:#fff;border-radius:8px;padding:10px 14px;box-shadow:0 1px 3px #0002}}
.kpi b{{display:block;font-size:20px}} .bar{{padding:6px 20px;display:flex;gap:10px;flex-wrap:wrap;align-items:center;position:sticky;top:0;background:#f4f6faee;z-index:2}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:12px;padding:12px 20px}}
.card{{background:#fff;border-radius:10px;box-shadow:0 1px 4px #0002;padding:10px;font-size:13px}} .card img{{width:100%;border-radius:6px}}
.lv{{padding:2px 8px;border-radius:10px;font-weight:bold;font-size:12px}} .HIGH{{background:{LEVEL_COLORS['HIGH']}}} .REVIEW{{background:{LEVEL_COLORS['REVIEW']}}}
.LOW{{background:{LEVEL_COLORS['LOW']}}} .NORMAL{{background:{LEVEL_COLORS['NORMAL']}}} .meta{{margin:6px 0;line-height:1.5}}
.vlm{{background:#eef3ff;border-radius:6px;padding:6px;margin:4px 0}} .flags{{margin:4px 0;padding-left:18px;color:#a33}}
.ids{{display:flex;flex-wrap:wrap;gap:8px;padding:0 20px 20px}} .id{{background:#fff;border-radius:8px;padding:6px;text-align:center;font-size:12px}} .id img{{width:90px;border-radius:6px}}
h2{{padding:0 20px;font-size:18px}}</style></head><body>
<header><h1>CCTV Analysis Report</h1><div>Generated {dt.datetime.now():%Y-%m-%d %H:%M} · shop hours {cfg.open_hour}–{cfg.close_hour}</div></header>
<div class="kpi"><div><b>{len(videos)}</b>videos</div><div><b>{tot / 3600:.1f} h</b>footage</div>
<div><b>{mot / 3600:.1f} h</b>with motion</div><div><b>{len(events)}</b>events</div>
<div><b>{lv.get('HIGH', 0)}</b>HIGH</div><div><b>{lv.get('REVIEW', 0)}</b>REVIEW</div><div><b>{len(idt)}</b>face IDs</div><div><b>{len(tamper)}</b>tamper</div></div>
<div class="bar">Level <select id="lv"><option value="">all</option><option>HIGH</option><option>REVIEW</option><option>LOW</option><option>NORMAL</option></select>
Camera <select id="cam"><option value="">all</option>{''.join(f'<option>{html.escape(c)}</option>' for c in cams)}</select>
Date <input id="date" type="date"> <label><input id="ppl" type="checkbox"> people only</label>
Search <input id="q" placeholder="person, P0003, bag, theft…"> Sort <select id="sort"><option value="time">time</option><option value="score">score</option></select>
<span id="cnt"></span></div>
<div class="grid" id="grid">{''.join(cards)}</div>
<h2>Face identities</h2><div class="ids">{idcards}</div>
<script>
const g=document.getElementById('grid'),cards=[...g.children];
function f(){{const lv=document.getElementById('lv').value,cam=document.getElementById('cam').value,d=document.getElementById('date').value,
q=document.getElementById('q').value.toLowerCase(),p=document.getElementById('ppl').checked,s=document.getElementById('sort').value;let n=0;
let arr=cards.slice(); if(s=='score') arr.sort((a,b)=>b.dataset.score-a.dataset.score); arr.forEach(c=>g.appendChild(c));
cards.forEach(c=>{{const ok=(!lv||c.dataset.level==lv)&&(!cam||c.dataset.cam==cam)&&(!d||c.dataset.date==d)&&(!p||c.dataset.people>0)&&(!q||c.dataset.text.includes(q));
c.style.display=ok?'':'none'; n+=ok;}}); document.getElementById('cnt').textContent=n+' events';}}
document.querySelectorAll('.bar select,.bar input').forEach(e=>e.addEventListener('input',f)); f();
</script></body></html>"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    log.info("HTML report: %s", path)


# ====================================================================== charts
def build_charts(store, cfg):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    events = load(store)[0]
    ev = [e for e in events if e["start_dt"]]
    if not ev:
        return []
    os.makedirs(os.path.join(cfg.output_dir, "charts"), exist_ok=True)
    paths = []
    cams = sorted({e["camera"] for e in ev})
    days = sorted({e["start_dt"][:10] for e in ev})[-31:]
    fig, ax = plt.subplots(figsize=(14, max(3, 0.45 * len(days) * max(1, len(cams)) + 1)))
    ylabels = []
    y = 0
    colors = {"HIGH": "#d62728", "REVIEW": "#ff7f0e", "LOW": "#bcbd22", "NORMAL": "#2ca02c"}
    for d in days:
        for c in cams:
            for e in ev:
                if e["start_dt"][:10] == d and e["camera"] == c:
                    h0 = int(e["start_dt"][11:13]) + int(e["start_dt"][14:16]) / 60 + int(e["start_dt"][17:19]) / 3600
                    ax.barh(y, max(e["duration"] / 3600, 0.01), left=h0, color=colors.get(e["level"], "grey"), height=0.7)
            ylabels.append(f"{d} {c}")
            y += 1
    ax.set_yticks(range(len(ylabels)))
    ax.set_yticklabels(ylabels, fontsize=8)
    ax.set_xlim(0, 24)
    ax.set_xticks(range(0, 25, 2))
    ax.set_xlabel("hour of day")
    o = [int(x) for x in cfg.open_hour.split(":")]
    c_ = [int(x) for x in cfg.close_hour.split(":")]
    ax.axvspan(0, o[0] + o[1] / 60, color="#000", alpha=0.06)
    ax.axvspan(c_[0] + c_[1] / 60, 24, color="#000", alpha=0.06)
    ax.set_title("Event timeline (colour = level, shaded = closed hours)")
    ax.invert_yaxis()
    fig.tight_layout()
    p = os.path.join(cfg.output_dir, "charts", "timeline.png")
    fig.savefig(p, dpi=110)
    plt.close(fig)
    paths.append(p)
    fig, ax = plt.subplots(figsize=(10, 3.5))
    hours = Counter(int(e["start_dt"][11:13]) for e in ev if e["n_person_tracks"])
    ax.bar(range(24), [hours.get(h, 0) for h in range(24)], color="#1f77b4")
    ax.set_xticks(range(24))
    ax.set_title("Events with people by hour of day")
    fig.tight_layout()
    p = os.path.join(cfg.output_dir, "charts", "people_by_hour.png")
    fig.savefig(p, dpi=110)
    plt.close(fig)
    paths.append(p)
    return paths


# ================================================================ highlight reel
def build_highlights(store, cfg) -> str:
    ff = V.FFMPEG
    if not ff:
        return ""
    events = [e for e in load(store)[0] if e["clip_path"] and os.path.exists(e["clip_path"])
              and e["level"] in ("HIGH", "REVIEW")]
    events = sorted(events, key=lambda e: -(e["score"] or 0))[: cfg.highlight_max_events]
    events.sort(key=lambda e: e["start_dt"] or "")
    if not events:
        return ""
    tmp = os.path.join(cfg.work_dir, "hl")
    os.makedirs(tmp, exist_ok=True)
    parts = []
    for i, e in enumerate(events):
        p = os.path.join(tmp, f"{i:04d}.mp4")
        txt = f"{e['level']} {e['score']:g} {e['camera']} {e['start_dt'] or ''}".replace(":", r"\:")
        vf = ("scale=960:540:force_original_aspect_ratio=decrease,pad=960:540:(ow-iw)/2:(oh-ih)/2,fps=15,"
              f"drawtext=text='{txt}':x=10:y=10:fontsize=22:fontcolor=yellow:box=1:boxcolor=black@0.6")
        cmd = [ff, "-nostdin", "-y", "-v", "error", "-i", e["clip_path"], "-t", "30", "-vf", vf, "-an",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p", p]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0:      # drawtext unavailable -> plain
            cmd[cmd.index("-vf") + 1] = vf.split(",drawtext")[0]
            r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0:
            parts.append(p)
    if not parts:
        return ""
    lst = os.path.join(tmp, "list.txt")
    with open(lst, "w") as fh:
        fh.writelines(f"file '{p}'\n" for p in parts)
    out = os.path.join(cfg.output_dir, "highlight_reel.mp4")
    subprocess.run([ff, "-nostdin", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
                    "-movflags", "+faststart", out], capture_output=True)
    for p in parts:
        os.remove(p)
    log.info("Highlight reel: %s (%d events)", out, len(parts))
    return out if os.path.exists(out) else ""


def build_reports(store, cfg) -> dict:
    out = {}
    try:
        out["excel"] = os.path.join(cfg.output_dir, "CCTV_Report.xlsx")
        tmp = os.path.join(cfg.work_dir, "CCTV_Report.xlsx")
        build_excel(store, cfg, tmp)
        import shutil
        try:
            shutil.copyfile(tmp, out["excel"])
        except PermissionError:      # workbook open elsewhere -> timestamped copy
            out["excel"] = out["excel"].replace(".xlsx", f"_{dt.datetime.now():%Y%m%d_%H%M%S}.xlsx")
            shutil.copyfile(tmp, out["excel"])
    except Exception as e:
        log.exception("Excel report failed: %s", e)
    if cfg.make_html:
        try:
            out["html"] = os.path.join(cfg.output_dir, "report.html")
            build_html(store, cfg, out["html"])
        except Exception as e:
            log.exception("HTML report failed: %s", e)
    try:
        out["charts"] = build_charts(store, cfg)
    except Exception as e:
        log.exception("charts failed: %s", e)
    if cfg.make_highlight_reel:
        try:
            out["highlights"] = build_highlights(store, cfg)
        except Exception as e:
            log.exception("highlight reel failed: %s", e)
    return out

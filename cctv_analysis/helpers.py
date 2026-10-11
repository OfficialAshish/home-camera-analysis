"""Notebook helpers: camera/zone preview, in-notebook review of the top
events (images + playable clips), free-text search and search-by-photo."""
from __future__ import annotations

import base64
import json
import os
from collections import defaultdict
from typing import Optional

import cv2
import numpy as np

from . import annotate as A
from . import video_io as V
from .config import Config
from .motion import detect_osd_mask, poly_mask
from .rules import zones_for
from .storage import Store


def _show(img_bgr, width=900):
    try:
        from IPython.display import Image, display
        ok, buf = cv2.imencode(".jpg", img_bgr)
        display(Image(data=buf.tobytes(), width=width))
    except Exception:
        pass


def _html(s):
    try:
        from IPython.display import HTML, display
        display(HTML(s))
    except Exception:
        print(s)


def grid_overlay(img):
    h, w = img.shape[:2]
    out = img.copy()
    for i in range(1, 10):
        x, y = int(w * i / 10), int(h * i / 10)
        cv2.line(out, (x, 0), (x, h), (255, 255, 255), 1)
        cv2.line(out, (0, y), (w, y), (255, 255, 255), 1)
        cv2.putText(out, f"{i / 10:.1f}", (x + 2, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)
        cv2.putText(out, f"{i / 10:.1f}", (2, y - 2), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)
    return out


def preview_cameras(cfg: Config, per_camera: int = 1, width: int = 960):
    """List videos, then show one frame per camera with a 0.1 grid (to write zone
    coordinates), configured zones (red), ignore regions (blue) and the
    auto-detected clock mask (magenta)."""
    vids = V.find_videos(cfg)
    by_cam = defaultdict(list)
    for p in vids:
        by_cam[V.camera_name(p, cfg)].append(p)
    tot = 0.0
    print(f"{len(vids)} video(s) in {len(by_cam)} camera group(s)")
    for cam, ps in by_cam.items():
        print(f"\n=== camera '{cam}': {len(ps)} file(s)")
        for p in ps[:per_camera]:
            meta = V.probe(p)
            tot += meta["duration"]
            st, src = V.infer_start_time(p, meta, cfg)
            print(f"  {os.path.relpath(p, cfg.input_dir)} | {meta['width']}x{meta['height']} {meta['fps']:.1f}fps "
                  f"{V.fmt_offset(meta['duration'])} {meta['codec']} | start {st} ({src})")
            f = V.read_frame_at(p, min(5.0, meta["duration"] / 2), width, (meta["width"], meta["height"]))
            if f is None:
                print("   !! could not decode a frame")
                continue
            img = f.copy()
            z = zones_for(cfg, cam)
            if z:
                A.draw_zones(img, z)
            ign = list(cfg.ignore_regions.get("*", [])) + list(cfg.ignore_regions.get(cam, []))
            if ign:
                m = poly_mask(img.shape[:2], ign)
                img[m > 0] = (img[m > 0] * 0.5 + np.array([255, 0, 0]) * 0.5).astype(np.uint8)
            if cfg.auto_mask_osd:
                osd = detect_osd_mask(p, meta, cfg.motion_width, 30)
                if osd is not None:
                    osd = cv2.resize(osd, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_NEAREST)
                    img[osd > 0] = (img[osd > 0] * 0.5 + np.array([255, 0, 255]) * 0.5).astype(np.uint8)
            _show(grid_overlay(img), width)
        for p in ps[per_camera:]:
            tot += V.probe(p)["duration"]
    print(f"\nTotal footage: {tot / 3600:.2f} h")


def _store(cfg):
    return Store(os.path.join(cfg.work_dir, "analysis.db"), os.path.join(cfg.output_dir, "analysis.db"))


def _video_tag(path, width=480):
    if not path or not os.path.exists(path) or os.path.getsize(path) > 40e6:
        return ""
    with open(path, "rb") as fh:
        b = base64.b64encode(fh.read()).decode()
    return f'<video width="{width}" controls preload="none" src="data:video/mp4;base64,{b}"></video>'


def show_events(cfg: Config, where: str = "level IN ('HIGH','REVIEW')", order: str = "score DESC",
                limit: int = 10, clips: bool = True):
    """Display events (contact sheet, reasons, AI description, clip) inside the notebook."""
    st = _store(cfg)
    rows = st.query(f"SELECT * FROM events WHERE {where} ORDER BY {order} LIMIT {int(limit)}")
    if not rows:
        print("No matching events.")
    for e in rows:
        flags = json.loads(e["flags"] or "[]")
        vlm = json.loads(e["vlm"]) if e["vlm"] else None
        _html(f"<h3 style='margin-bottom:2px'>{e['level']} {e['score']:g} — {e['event_id']}</h3>"
              f"<div>🕒 {e['start_dt']} → {e['end_dt']} ({e['duration']:.0f}s) · 🎞 {e['rel_path']} "
              f"@ <b>{V.fmt_offset(e['start_off'])}</b> · 👥 {e['max_persons']} · objects {e['objects']} · "
              f"IDs {e['identities']}</div>"
              + (f"<div style='background:#eef3ff;padding:4px'>🤖 {vlm.get('description', '')} "
                 f"{'⚠️ ' + str(vlm.get('reason', '')) if vlm.get('suspicious') else ''}</div>" if vlm else "")
              + "<ul>" + "".join(f"<li><b>{f['flag']}</b> +{f['points']:g}: {f['reason']}</li>" for f in flags) + "</ul>")
        img = cv2.imread(e["contact_sheet"]) if e["contact_sheet"] else None
        if img is not None:
            _show(img, 1000)
        if clips and e["clip_path"]:
            _html(_video_tag(e["clip_path"]))
    return rows


_SEM_CACHE = {}


def search_text(cfg: Config, query: str, k: int = 12, show: bool = True):
    """Free-text search over all analysed keyframes (SigLIP-2), e.g.
    'person in a red shirt', 'man with a helmet', 'someone carrying a big bag'."""
    from .detector import resolve_device
    from .semantic import Semantic, search_index
    key = (cfg.semantic_model, tuple(cfg.suspicious_prompts), tuple(cfg.normal_prompts))
    sem = _SEM_CACHE.get(key)
    if sem is None:
        sem = _SEM_CACHE[key] = Semantic(cfg, resolve_device(cfg.device))
    hits = search_index(sem, os.path.join(cfg.output_dir, "semantic_index.npz"), query, k)
    st = _store(cfg)
    results = []
    for key, score in hits:
        eid, t = key.split("|")
        e = st.query("SELECT * FROM events WHERE event_id=?", (eid,))
        if not e:
            continue
        e = e[0]
        snaps = json.loads(e["snapshots"] or "[]")
        snap = min(snaps, key=lambda s: abs(s["t_off"] - float(t))) if snaps else None
        results.append({"event_id": eid, "score": score, "time": snap and snap["dt"], "video": e["rel_path"],
                        "offset": V.fmt_offset(float(t)), "snapshot": snap and snap["annotated"]})
    if show:
        for r in results:
            print(f"{r['score']:.3f}  {r['event_id']}  {r['time']}  {r['video']} @ {r['offset']}")
            if r["snapshot"]:
                im = cv2.imread(r["snapshot"])
                if im is not None:
                    _show(im, 640)
    return results


def search_face(cfg: Config, image_path: str, min_sim: Optional[float] = None, show: int = 12):
    """Search-by-photo: find every appearance of the person in `image_path`."""
    from .faces import FaceEngine
    fe = FaceEngine(cfg)
    img = cv2.imread(image_path)
    if img is None:
        raise FileNotFoundError(image_path)
    faces = sorted(fe.detect_full(img), key=lambda f: -(f["box"][2] - f["box"][0]))
    if not faces:
        print("No face found in the photo.")
        return []
    q = fe.embed(img, faces[0])
    path = os.path.join(cfg.output_dir, "identities")
    ids = json.load(open(path + ".json"))
    emb = np.load(path + ".npy")
    sims = emb @ q
    thr = cfg.face_match_threshold - 0.05 if min_sim is None else min_sim
    order = [i for i in np.argsort(-sims) if sims[i] >= thr]
    st = _store(cfg)
    out = []
    for i in order:
        pid = ids[i]["id"]
        rows = st.query("SELECT t.event_id, t.first_dt, t.face_crop, e.rel_path, t.first_off, e.camera FROM tracks t "
                        "JOIN events e USING(event_id) WHERE t.identity=? ORDER BY t.first_dt", (pid,))
        print(f"\n{pid} similarity {sims[i]:.3f} — {len(rows)} appearance(s)")
        for r in rows:
            print(f"   {r['first_dt']}  {r['camera']}  {r['event_id']}  {r['rel_path']} @ {V.fmt_offset(r['first_off'])}")
            out.append({**r, "identity": pid, "similarity": float(sims[i])})
        if rows and rows[0]["face_crop"] and show:
            im = cv2.imread(rows[0]["face_crop"])
            if im is not None:
                _show(im, 120)
    if not out:
        print("No matching identity found.")
    return out


def summary(cfg: Config):
    st = _store(cfg)
    q = lambda s: st.query(s)[0]
    v = q("SELECT COUNT(*) n, SUM(duration) d, SUM(motion_seconds) m FROM videos WHERE status='done'")
    e = st.query("SELECT level, COUNT(*) n FROM events GROUP BY level")
    print(f"Videos: {v['n']}  footage: {(v['d'] or 0) / 3600:.2f} h  motion: {(v['m'] or 0) / 3600:.2f} h")
    print("Events by level:", {r["level"]: r["n"] for r in e})
    dup = st.query("SELECT rel_path, duplicate_of FROM videos WHERE status='duplicate'")
    if dup:
        print(f"Duplicates skipped: {len(dup)}")
        for d in dup[:20]:
            print("  ", d["rel_path"], "= copy of", d["duplicate_of"])
    nf = st.query("SELECT COUNT(*) n FROM face_alerts WHERE kind='new_face'")[0]["n"]
    ua = st.query("SELECT COUNT(*) n FROM face_alerts WHERE kind='unauthorized'")[0]["n"]
    print(f"New-face alerts (all runs): {nf}   unauthorised-person alerts: {ua}")
    bad = st.query("SELECT rel_path, error FROM videos WHERE status NOT IN ('done','duplicate')")
    if bad:
        print("Problems:")
        for b in bad:
            print("  ", b["rel_path"], (b["error"] or "")[:200])


def _q(v: str) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def show_new_faces(cfg: Config, run_id: Optional[str] = None, limit: int = 60):
    """Faces never seen before (default: in the latest run), with where/when they appeared."""
    st = _store(cfg)
    if run_id is None:
        r = st.query("SELECT run_id FROM face_alerts WHERE kind='new_face' ORDER BY id DESC LIMIT 1")
        run_id = r[0]["run_id"] if r else ""
    rows = st.query("SELECT * FROM face_alerts WHERE kind='new_face' AND run_id=? ORDER BY dt LIMIT ?",
                    (run_id, int(limit)))
    if not rows:
        print("No new faces" + (f" in run {run_id}" if run_id else "") + ".")
        return rows
    print(f"{len(rows)} new face(s) in run {run_id}:")
    tiles = []
    for a in rows:
        im = cv2.imread(a["face_crop"]) if a["face_crop"] else None
        src = ""
        if im is not None:
            ok, buf = cv2.imencode(".jpg", cv2.resize(im, (112, int(112 * im.shape[0] / im.shape[1]))))
            src = "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()
        tiles.append(f"<div style='display:inline-block;width:150px;margin:4px;padding:6px;background:#fff8c4;"
                     f"border-radius:8px;text-align:center;font-size:12px;vertical-align:top'>"
                     f"{'<img src=' + chr(34) + src + chr(34) + ' width=112><br>' if src else ''}<b>{a['identity']}</b>"
                     f"<br>{a['dt'] or ''}<br>{a['camera']}<br>{a['video']} @ {V.fmt_offset(a['t_off'] or 0)}</div>")
    _html("".join(tiles))
    return rows


def show_highlights(cfg: Config, day: Optional[str] = None, play: bool = True):
    """List the daily highlight reels (or one day's reel with its timing table)."""
    st = _store(cfg)
    if day is None:
        rows = st.query("SELECT day, COUNT(*) n, MAX(reel_end) d, reel_path FROM reel_segments GROUP BY day ORDER BY day DESC")
        for r in rows:
            print(f"{r['day']}: {r['n']:4d} movement(s), {V.fmt_offset(r['d'] or 0)}  ->  {r['reel_path']}")
        if not rows:
            print("No highlight reels yet.")
        return rows
    rows = st.query("SELECT r.*, e.camera, e.start_dt, e.level, e.score, e.rel_path, e.start_off FROM reel_segments r "
                    "JOIN events e USING(event_id) WHERE r.day=? ORDER BY r.idx", (day,))
    if not rows:
        print("No reel for", day)
        return rows
    _html("<table><tr><th>#</th><th>Reel time</th><th>Camera</th><th>Clock</th><th>Level</th><th>Video @ offset</th></tr>" +
          "".join(f"<tr><td>{r['idx']}</td><td>{V.fmt_offset(r['reel_start'])}</td><td>{r['camera']}</td>"
                  f"<td>{(r['start_dt'] or '')[11:]}</td><td>{r['level']} {r['score']:g}</td>"
                  f"<td>{r['rel_path']} @ {V.fmt_offset(r['start_off'])}</td></tr>" for r in rows) + "</table>")
    if play:
        _html(_video_tag(rows[0]["reel_path"], 720) or f"Reel: {rows[0]['reel_path']} (too large to embed – open it from Drive)")
    return rows


def review_dashboard(cfg: Config):
    """Interactive filter panel (date range, camera, level, person ID, text, new faces) for the notebook."""
    try:
        import ipywidgets as W
        from IPython.display import display, clear_output
    except Exception:
        print("ipywidgets not available – use show_events(cfg, where=...) instead.")
        return
    st = _store(cfg)
    cams = [r["camera"] for r in st.query("SELECT DISTINCT camera FROM events ORDER BY camera") if r["camera"]]
    days = [r["d"] for r in st.query("SELECT DISTINCT substr(start_dt,1,10) d FROM events WHERE start_dt IS NOT NULL ORDER BY d")]
    import datetime as _dt
    d1 = W.DatePicker(description="From", value=_dt.date.fromisoformat(days[0]) if days else None)
    d2 = W.DatePicker(description="To", value=_dt.date.fromisoformat(days[-1]) if days else None)
    cam = W.Dropdown(options=["(all)"] + cams, description="Camera")
    lvl = W.SelectMultiple(options=["HIGH", "REVIEW", "LOW", "NORMAL"], value=("HIGH", "REVIEW"), description="Level", rows=4)
    pid = W.Text(description="Person ID", placeholder="P0003")
    txt = W.Text(description="Contains", placeholder="flag / object / AI text, e.g. running")
    ppl = W.Checkbox(description="people only", value=False)
    nwf = W.Checkbox(description="new faces only", value=False)
    srt = W.Dropdown(options=[("score", "score DESC"), ("time", "start_dt")], description="Sort")
    lim = W.IntSlider(description="Max", value=10, min=1, max=100)
    clips = W.Checkbox(description="embed clips", value=True)
    btn = W.Button(description="Show events", button_style="primary")
    out = W.Output()

    def go(_=None):
        cond = ["1=1"]
        if d1.value:
            cond.append(f"substr(start_dt,1,10) >= {_q(d1.value.isoformat())}")
        if d2.value:
            cond.append(f"substr(start_dt,1,10) <= {_q(d2.value.isoformat())}")
        if cam.value != "(all)":
            cond.append(f"camera = {_q(cam.value)}")
        if lvl.value:
            cond.append("level IN (" + ",".join(_q(v) for v in lvl.value) + ")")
        if pid.value.strip():
            cond.append(f"identities LIKE {_q('%' + pid.value.strip().upper() + '%')}")
        if txt.value.strip():
            t = _q("%" + txt.value.strip() + "%")
            cond.append(f"(flags LIKE {t} OR objects LIKE {t} OR vlm LIKE {t} OR semantic LIKE {t})")
        if ppl.value:
            cond.append("n_person_tracks > 0")
        if nwf.value:
            cond.append("event_id IN (SELECT event_id FROM face_alerts WHERE kind='new_face')")
        with out:
            clear_output()
            n = st.query("SELECT COUNT(*) n FROM events WHERE " + " AND ".join(cond))[0]["n"]
            print(f"{n} matching event(s); showing up to {lim.value}")
            show_events(cfg, where=" AND ".join(cond), order=srt.value, limit=lim.value, clips=clips.value)

    btn.on_click(go)
    display(W.VBox([W.HBox([d1, d2, cam]), W.HBox([lvl, W.VBox([pid, txt, W.HBox([ppl, nwf])])]),
                    W.HBox([srt, lim, clips, btn]), out]))

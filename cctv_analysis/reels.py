"""Daily highlight reels: every movement of a day (all cameras, all videos) cut
out of the original recordings and joined in time order, with a running clock
and the source citation burnt in. Segments are encoded once and cached, so a
day's reel is cheaply re-assembled whenever new videos for that day arrive."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import subprocess

from . import video_io as V

log = logging.getLogger("cctv")


def _duration(path: str) -> float:
    try:
        r = subprocess.run([V.FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
                           capture_output=True, text=True, timeout=60)
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def _letterbox(frame, W, H):
    import cv2
    import numpy as np
    h, w = frame.shape[:2]
    sc = min(W / w, H / h)
    nw, nh = max(2, int(w * sc)), max(2, int(h * sc))
    img = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
    out = np.zeros((H, W, 3), np.uint8)
    y0, x0 = (H - nh) // 2, (W - nw) // 2
    out[y0:y0 + nh, x0:x0 + nw] = img
    return out


def _bar(img, text, y, scale, color):
    import cv2
    (tw, th), bl = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
    cv2.rectangle(img, (4, y - th - 8), (12 + tw, y + bl), (0, 0, 0), -1)
    cv2.putText(img, text, (8, y - 4), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 2, cv2.LINE_AA)


def encode_segment(e: dict, out: str, cfg) -> bool:
    """Decode the event from the original video, draw a running clock + citation
    with OpenCV (works with any ffmpeg build) and encode a normalised H.264 part."""
    src = e["video_path"]
    if not os.path.exists(src):
        log.warning("reel: source missing for %s (%s)", e["event_id"], src)
        return False
    dur = e["end_off"] - e["start_off"]
    if cfg.reel_max_event_seconds:
        dur = min(dur, cfg.reel_max_event_seconds)
    W = int(cfg.reel_width) // 2 * 2
    H = int(round(W * 9 / 16)) // 2 * 2
    fps = int(cfg.reel_fps)
    meta = V.probe(src)
    t0 = dt.datetime.strptime(e["start_dt"], "%Y-%m-%d %H:%M:%S") if e.get("start_dt") else None
    info = f"{e['camera']} | {e['level']} {e['score']:g} | {e['event_id']} | {e['rel_path']}"
    sc = W / 1100
    os.makedirs(os.path.dirname(out), exist_ok=True)
    part = out + ".part.mp4"
    proc = subprocess.Popen([V.FFMPEG, "-nostdin", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                             "-s", f"{W}x{H}", "-r", str(fps), "-i", "pipe:0", "-c:v", "libx264", "-preset",
                             "veryfast", "-crf", "28", "-pix_fmt", "yuv420p", "-video_track_timescale", "15360",
                             part], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    n = 0
    try:
        for t, fr in V.iter_frames(src, fps, W, e["start_off"], max(0.5, dur),
                                   src_size=(meta["width"], meta["height"])):
            img = _letterbox(fr, W, H)
            clock = (t0 + dt.timedelta(seconds=t - e["start_off"])).strftime("%Y-%m-%d %H:%M:%S") if t0 else ""
            _bar(img, f"{clock}   {e['camera']}", int(34 * sc) + 8, 0.9 * sc, (0, 255, 255))
            _bar(img, f"{info} @ {V.fmt_offset(t)}", H - 6, 0.55 * sc, (255, 255, 255))
            proc.stdin.write(img.tobytes())
            n += 1
        proc.stdin.close()
        proc.wait(timeout=600)
    except Exception as ex:
        log.warning("reel: encode failed for %s: %s", e["event_id"], ex)
        proc.kill()
        return False
    if proc.returncode == 0 and n and os.path.getsize(part) > 1000:
        os.replace(part, out)
        return True
    log.warning("reel: could not encode %s (%d frames)", e["event_id"], n)
    return False


def _events_for_day(store, day: str, scope: str):
    if day == "unknown-date":
        rows = store.query("SELECT * FROM events WHERE start_dt IS NULL ORDER BY video_path, start_off")
    else:
        rows = store.query("SELECT * FROM events WHERE start_dt LIKE ? ORDER BY start_dt, camera", (day + "%",))
    if scope == "people":
        rows = [r for r in rows if r["n_person_tracks"]]
    elif scope == "flagged":
        rows = [r for r in rows if r["level"] in ("HIGH", "REVIEW")]
    return rows


def build_day_reel(store, cfg, day: str) -> str:
    if not V.FFMPEG:
        return ""
    events = _events_for_day(store, day, cfg.reel_scope)
    if not events:
        return ""
    seg_dir = os.path.join(cfg.output_dir, "highlights", ".segments")
    year = day[:4] if day[:4].isdigit() else "unknown"
    reel = os.path.join(cfg.output_dir, "highlights", year, f"{day}_highlights.mp4")
    os.makedirs(os.path.dirname(reel), exist_ok=True)
    parts = []
    for e in events:
        seg = os.path.join(seg_dir, f"{e['event_id']}.mp4")
        if not (os.path.exists(seg) and os.path.getsize(seg) > 1000):
            if not encode_segment(e, seg, cfg):
                continue
        d = _duration(seg)
        if d > 0:
            parts.append((e, seg, d))
    if not parts:
        return ""
    lst = os.path.join(cfg.work_dir, f"reel_{day}.txt")
    os.makedirs(cfg.work_dir, exist_ok=True)
    with open(lst, "w") as fh:
        fh.writelines("file '" + p.replace("'", r"'\''") + "'\n" for _, p, _ in parts)
    tmp = os.path.join(cfg.work_dir, f"{day}_highlights.mp4")
    r = subprocess.run([V.FFMPEG, "-nostdin", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst,
                        "-c", "copy", "-movflags", "+faststart", tmp], capture_output=True, text=True)
    if r.returncode != 0:
        log.warning("reel concat failed for %s: %s", day, r.stderr[-300:])
        return ""
    import shutil
    shutil.copyfile(tmp, reel)
    os.remove(tmp)
    store.db.execute("DELETE FROM reel_segments WHERE day=?", (day,))
    t = 0.0
    for i, (e, seg, d) in enumerate(parts, 1):
        store.insert("reel_segments", {"day": day, "idx": i, "event_id": e["event_id"], "reel_start": round(t, 2),
                                       "reel_end": round(t + d, 2), "seg_path": seg, "reel_path": reel})
        t += d
    store.commit(sync=False)
    log.info("Highlight reel %s: %d movement(s), %s long -> %s", day, len(parts), V.fmt_offset(t), reel)
    return reel


def build_reels(store, cfg, days=None) -> list:
    """(Re)build reels for `days` (default: every day that has events but no reel yet)."""
    if not cfg.make_highlight_reel:
        return []
    all_days = {(r["d"] or "unknown-date") for r in
                store.query("SELECT DISTINCT substr(start_dt,1,10) d FROM events")}
    if days is None:
        have = {r["day"] for r in store.query("SELECT DISTINCT day FROM reel_segments")}
        days = all_days - have
    out = []
    for d in sorted(set(days) & all_days):
        try:
            p = build_day_reel(store, cfg, d)
            if p:
                out.append(p)
        except Exception as e:
            log.exception("reel for %s failed: %s", d, e)
    return out

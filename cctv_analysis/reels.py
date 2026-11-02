"""Daily highlight reels: every movement of a day (all cameras, all videos) cut out of the
original recordings and joined in true time order.

* Picture is left untouched by default (``reel_overlay=False``): no added text; only scaled /
  letter-boxed to a common size so the parts can be joined.
* Layout: ``highlights/<YYYY>/<MM-Mon>/<YYYY-MM-DD>_highlights.mp4``, plus
  ``<YYYY-MM-DD>_index.csv`` and ``<YYYY-MM-DD>_timeline.srt`` next to it (reel time → camera,
  clock time, source video, offset). The .srt is deliberately *not* named like the video, so
  players do not auto-display it; drag it onto the player to see the clock.
* Order: events with an exact recording time by clock time; events whose time is only
  approximate afterwards, in natural file-name order (never by download time).
* Segments are encoded once and cached under ``highlights/.segments`` keyed by a render
  signature, so a day is cheaply re-assembled when new videos arrive and stale renders
  (e.g. from an older version with burnt-in text) are never reused.
"""
from __future__ import annotations

import csv
import datetime as dt
import glob
import hashlib
import json
import logging
import os
import shutil
import subprocess

from . import timeline as T
from . import video_io as V

log = logging.getLogger("cctv")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
UNKNOWN = "unknown-date"


def _duration(path: str) -> float:
    try:
        r = subprocess.run([V.FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
                           capture_output=True, text=True, timeout=60)
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def reel_dir(output_dir: str, day: str) -> str:
    """highlights/2026/02-Feb for '2026-02-04'; highlights/unknown-date otherwise."""
    root = os.path.join(output_dir, "highlights")
    try:
        d = dt.date.fromisoformat(day)
    except ValueError:
        return os.path.join(root, UNKNOWN)
    return os.path.join(root, f"{d.year:04d}", f"{d.month:02d}-{MONTHS[d.month - 1]}")


def reel_path(output_dir: str, day: str) -> str:
    return os.path.abspath(os.path.join(reel_dir(output_dir, day), f"{day}_highlights.mp4"))


def _size(cfg):
    W = int(cfg.reel_width) // 2 * 2
    return W, int(round(W * 9 / 16)) // 2 * 2


def _signature(e: dict, cfg) -> str:
    W, H = _size(cfg)
    key = json.dumps([e["video_path"], e.get("fingerprint"), e["start_off"], e["end_off"], W, H, int(cfg.reel_fps),
                      cfg.reel_max_event_seconds, bool(cfg.reel_overlay), e.get("start_dt") if cfg.reel_overlay else ""])
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def _encode_clean(e: dict, out: str, cfg, dur: float) -> bool:
    W, H = _size(cfg)
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
          f"setsar=1,fps={int(cfg.reel_fps)}")
    cmd = [V.FFMPEG, "-nostdin", "-y", "-v", "error", "-ss", f"{max(0.0, e['start_off']):.3f}",
           *V._input_args(e["video_path"]), "-i", e["video_path"], "-t", f"{max(0.5, dur):.3f}", "-map", "0:v:0",
           "-an", "-sn", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
           "-video_track_timescale", "15360", out]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        log.warning("reel: ffmpeg failed for %s: %s", e["event_id"], r.stderr[-300:])
    return r.returncode == 0


def _encode_overlay(e: dict, out: str, cfg, dur: float) -> bool:
    """Optional (reel_overlay=True): running clock + citation drawn with OpenCV."""
    import cv2
    import numpy as np
    W, H = _size(cfg)
    fps = int(cfg.reel_fps)
    meta = V.probe(e["video_path"])
    t0 = dt.datetime.strptime(e["start_dt"], "%Y-%m-%d %H:%M:%S") if e.get("start_dt") else None
    info = f"{e['camera']} | {e['level']} {e['score']:g} | {e['event_id']} | {e['rel_path']}"
    sc = W / 1100
    proc = subprocess.Popen([V.FFMPEG, "-nostdin", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                             "-s", f"{W}x{H}", "-r", str(fps), "-i", "pipe:0", "-c:v", "libx264", "-preset",
                             "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-video_track_timescale", "15360",
                             out], stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def bar(img, text, y, scale, color):
        (tw, th), bl = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
        cv2.rectangle(img, (4, y - th - 8), (12 + tw, y + bl), (0, 0, 0), -1)
        cv2.putText(img, text, (8, y - 4), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 2, cv2.LINE_AA)

    n = 0
    try:
        for t, fr in V.iter_frames(e["video_path"], fps, W, e["start_off"], max(0.5, dur),
                                   src_size=(meta["width"], meta["height"])):
            h, w = fr.shape[:2]
            s = min(W / w, H / h)
            img = np.zeros((H, W, 3), np.uint8)
            rs = cv2.resize(fr, (max(2, int(w * s)), max(2, int(h * s))), interpolation=cv2.INTER_AREA)
            y0, x0 = (H - rs.shape[0]) // 2, (W - rs.shape[1]) // 2
            img[y0:y0 + rs.shape[0], x0:x0 + rs.shape[1]] = rs
            clock = (t0 + dt.timedelta(seconds=t - e["start_off"])).strftime("%Y-%m-%d %H:%M:%S") if t0 else ""
            bar(img, f"{clock}   {e['camera']}", int(34 * sc) + 8, 0.9 * sc, (0, 255, 255))
            bar(img, f"{info} @ {V.fmt_offset(t)}", H - 6, 0.55 * sc, (255, 255, 255))
            proc.stdin.write(img.tobytes())
            n += 1
        proc.stdin.close()
        proc.wait(timeout=600)
    except Exception as ex:
        log.warning("reel: encode failed for %s: %s", e["event_id"], ex)
        proc.kill()
        return False
    return proc.returncode == 0 and n > 0


def encode_segment(e: dict, out: str, cfg) -> bool:
    if not os.path.exists(e["video_path"]):
        log.warning("reel: source missing for %s (%s)", e["event_id"], e["video_path"])
        return False
    dur = e["end_off"] - e["start_off"]
    if cfg.reel_max_event_seconds:
        dur = min(dur, cfg.reel_max_event_seconds)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    part = out + ".part.mp4"
    ok = (_encode_overlay if cfg.reel_overlay else _encode_clean)(e, part, cfg, dur)
    if ok and os.path.exists(part) and os.path.getsize(part) > 1000:
        os.replace(part, out)
        return True
    if os.path.exists(part):
        os.remove(part)
    log.warning("reel: could not encode %s", e["event_id"])
    return False


def is_blank(e: dict) -> bool:
    """Nothing detected and the picture centre is black (night clip with no IR, lens covered
    without a tamper flag ...) – not worth watching in a highlight reel."""
    lp = e.get("luma_p99")
    return (e.get("kind") in ("motion-only", "lighting-change") and e.get("level") == "NORMAL"
            and lp is not None and lp < 40)


def events_for_day(store, day: str, scope: str, skip_blank: bool = True) -> list:
    sel = ("SELECT e.*, v.time_source AS video_time_source, v.fingerprint FROM events e "
           "LEFT JOIN videos v ON v.id = e.video_id ")
    if day == UNKNOWN:
        rows = store.query(sel + "WHERE e.start_dt IS NULL")
    else:
        rows = store.query(sel + "WHERE substr(e.start_dt,1,10) = ?", (day,))
    if scope == "people":
        rows = [r for r in rows if r["n_person_tracks"]]
    elif scope == "flagged":
        rows = [r for r in rows if r["level"] in ("HIGH", "REVIEW")]
    if skip_blank:
        rows = [r for r in rows if not is_blank(r)]
    def key(r):
        src = r.get("time_source") or r.get("video_time_source")
        if r["start_dt"] and T.is_exact(src):
            return (0, r["start_dt"], [], r["camera"] or "", r["start_off"])
        return (1, "", V.natural_key(r["rel_path"] or r["video_path"] or ""), r["camera"] or "", r["start_off"])
    return sorted(rows, key=key)


def _srt_time(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _write_index(reel: str, day: str, parts) -> None:
    base = os.path.join(os.path.dirname(reel), day)
    with open(base + "_index.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["#", "reel_start", "reel_end", "camera", "clock_start", "clock_end", "video", "offset_start",
                    "offset_end", "level", "score", "event_id", "time_source"])
        t = 0.0
        for i, (e, _, d) in enumerate(parts, 1):
            w.writerow([i, V.fmt_offset(t), V.fmt_offset(t + d), e["camera"], e["start_dt"] or "", e["end_dt"] or "",
                        e["rel_path"], V.fmt_offset(e["start_off"]), V.fmt_offset(e["end_off"]), e["level"],
                        e["score"], e["event_id"], e.get("time_source") or e.get("video_time_source") or ""])
            t += d
    with open(base + "_timeline.srt", "w", encoding="utf-8") as fh:
        t = 0.0
        for i, (e, _, d) in enumerate(parts, 1):
            fh.write(f"{i}\n{_srt_time(t)} --> {_srt_time(t + d)}\n"
                     f"{e['start_dt'] or 'time unknown'}  {e['camera']}  {e['rel_path']} @ {V.fmt_offset(e['start_off'])}\n\n")
            t += d


def _publish(tmp: str, final: str) -> str:
    os.makedirs(os.path.dirname(final), exist_ok=True)
    try:
        shutil.copyfile(tmp, final)
    except PermissionError:      # reel open in a player (Windows) -> timestamped copy
        root, ext = os.path.splitext(final)
        final = f"{root}_{dt.datetime.now():%Y%m%d_%H%M%S}{ext}"
        shutil.copyfile(tmp, final)
    os.remove(tmp)
    return final


def remove_day(store, cfg, day: str) -> None:
    """Delete a day's reel + index files (its events are gone or moved to another day)."""
    p = reel_path(cfg.output_dir, day)
    base = os.path.join(os.path.dirname(p), day)
    for f in (p, base + "_index.csv", base + "_timeline.srt"):
        if os.path.exists(f):
            os.remove(f)
    _remove_legacy(cfg, day)
    store.db.execute("DELETE FROM reel_segments WHERE day=?", (day,))


def _remove_legacy(cfg, day: str) -> None:
    """Reels written by earlier versions at highlights/<year>/<day>_highlights.mp4."""
    old = os.path.join(cfg.output_dir, "highlights", day[:4] if day[:4].isdigit() else "unknown",
                       f"{day}_highlights.mp4")
    if os.path.abspath(old) != reel_path(cfg.output_dir, day) and os.path.exists(old):
        os.remove(old)
        log.info("removed old-layout reel %s", old)


def build_day_reel(store, cfg, day: str) -> str:
    if not V.FFMPEG:
        log.warning("ffmpeg not found – highlight reels cannot be built")
        return ""
    events = events_for_day(store, day, cfg.reel_scope, cfg.reel_skip_blank)
    _remove_legacy(cfg, day)
    if not events:
        remove_day(store, cfg, day)
        return ""
    seg_dir = os.path.abspath(os.path.join(cfg.output_dir, "highlights", ".segments"))
    reel = reel_path(cfg.output_dir, day)
    parts = []
    for e in events:
        seg = os.path.join(seg_dir, f"{e['event_id']}-{_signature(e, cfg)}.mp4")
        if not (os.path.exists(seg) and os.path.getsize(seg) > 1000):
            if not encode_segment(e, seg, cfg):
                continue
        d = _duration(seg)
        if d > 0:
            parts.append((e, seg, d))
    if not parts:
        remove_day(store, cfg, day)
        return ""
    os.makedirs(cfg.work_dir, exist_ok=True)
    lst = os.path.abspath(os.path.join(cfg.work_dir, f"reel_{day}.txt"))
    with open(lst, "w", encoding="utf-8") as fh:
        fh.writelines("file '" + p.replace("'", r"'\''") + "'\n" for _, p, _ in parts)
    tmp = os.path.join(cfg.work_dir, f"{day}_highlights.mp4")
    r = subprocess.run([V.FFMPEG, "-nostdin", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst,
                        "-c", "copy", "-movflags", "+faststart", tmp], capture_output=True, text=True)
    if r.returncode != 0:
        log.warning("reel concat failed for %s: %s", day, r.stderr[-300:])
        return ""
    reel = _publish(tmp, reel)
    _write_index(reel, day, parts)
    store.db.execute("DELETE FROM reel_segments WHERE day=?", (day,))
    t = 0.0
    for i, (e, seg, d) in enumerate(parts, 1):
        store.insert("reel_segments", {"day": day, "idx": i, "event_id": e["event_id"], "reel_start": round(t, 2),
                                       "reel_end": round(t + d, 2), "seg_path": seg, "reel_path": reel})
        t += d
    store.commit(sync=False)
    log.info("Highlight reel %s: %d movement(s), %s long -> %s", day, len(parts), V.fmt_offset(t), reel)
    return reel


def _prune_segments(store, cfg) -> int:
    used = {r["seg_path"] for r in store.query("SELECT seg_path FROM reel_segments")}
    n = 0
    for f in glob.glob(os.path.join(os.path.abspath(cfg.output_dir), "highlights", ".segments", "*.mp4")):
        if f not in used:
            os.remove(f)
            n += 1
    return n


def build_reels(store, cfg, days=None) -> list:
    """(Re)build reels for `days` (default: every day with events but no reel yet). Days that have a
    reel but no longer any events (times corrected, videos removed) lose their reel."""
    if not cfg.make_highlight_reel:
        return []
    all_days = {(r["d"] or UNKNOWN) for r in store.query("SELECT DISTINCT substr(start_dt,1,10) d FROM events")}
    have = {r["day"] for r in store.query("SELECT DISTINCT day FROM reel_segments")}
    for d in have - all_days:
        remove_day(store, cfg, d)
        log.info("removed highlight reel of %s (no events left on that day)", d)
    if days is None:
        days = all_days - have
    out = []
    for d in sorted(set(days) & all_days):
        try:
            p = build_day_reel(store, cfg, d)
            if p:
                out.append(p)
        except Exception as e:
            log.exception("reel for %s failed: %s", d, e)
    n = _prune_segments(store, cfg)
    if n:
        log.info("pruned %d unused reel segment(s)", n)
    store.commit(sync=False)
    return out

"""Acquisition of a seized source: SHA-256 of every recording, and lossless playback copies.

``ingest_source`` walks a source folder (read only) and, for every video file:

1. computes its **SHA-256** (the acquisition hash quoted in the Section 63 certificate) and
   records size and modification time; files unchanged since the last ingest are not re-hashed
   unless ``verify=True`` (which re-hashes everything and reports any file that changed);
2. for formats a browser cannot play (``.dav``, ``.264``/``.h264``, ``.265``/``.h265``/``.hevc``,
   ``.ts``, ``.mkv``, ``.avi`` …) writes a **lossless remux** ``playback/<rel>.mp4`` with
   ``ffmpeg -c copy`` – the compressed picture data is copied, never re-encoded. The copy is
   checked: same video codec and the same number of video packets as the original; anything
   else is recorded as a failed remux and the original remains the only copy. Raw elementary
   streams (``.264``/``.265``) carry no timestamps, so the probed frame rate is passed to ffmpeg
   (``-r``) to give the packets presentation times.

Results go to ``<source output>/ingest.json`` with a Merkle root over all file hashes.
The original files are never modified; the analysis always reads the originals.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import subprocess
from typing import Callable, Dict, List, Optional

from . import merkle
from . import video_io as V
from .evidence import sha256_file

log = logging.getLogger("cctv")
# Containers/streams the browser's <video> cannot open directly; MP4/MOV/M4V/WEBM play as they are.
REMUX_EXT = {".dav", ".264", ".h264", ".265", ".h265", ".hevc", ".ts", ".mts", ".m2ts", ".mkv", ".avi", ".flv",
             ".asf", ".wmv", ".3gp", ".mpg", ".mpeg", ".vob", ".ps", ".mpeg4"}
MP4_VIDEO = {"h264", "hevc", "mpeg4", "av1", "vp9", "mjpeg"}
MP4_AUDIO = {"aac", "mp3", "ac3", "eac3", "opus", "alac"}
RAW = {".264", ".h264", ".265", ".h265", ".hevc"}


def _streams(path: str, raw_rate: float = 0.0) -> dict:
    """codec of the first video and audio stream and the number of video packets (no decoding)."""
    out = {"video": "", "audio": "", "packets": 0}
    if not V.FFPROBE:
        return out
    extra = list(V._input_args(path))
    if raw_rate and os.path.splitext(path)[1].lower() in RAW:
        extra = ["-framerate", f"{raw_rate:g}", *extra]
    try:
        r = subprocess.run([V.FFPROBE, "-v", "error", *extra, "-count_packets", "-show_entries",
                            "stream=codec_type,codec_name,nb_read_packets", "-of", "json", path],
                           capture_output=True, text=True, timeout=600)
        for s in json.loads(r.stdout or "{}").get("streams", []):
            if s.get("codec_type") == "video" and not out["video"]:
                out["video"], out["packets"] = s.get("codec_name", ""), int(s.get("nb_read_packets") or 0)
            elif s.get("codec_type") == "audio" and not out["audio"]:
                out["audio"] = s.get("codec_name", "")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return out


def needs_remux(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in REMUX_EXT


def remux(src: str, dest: str, fps: float = 0.0) -> dict:
    """Lossless MP4 copy of `src` for playback. Returns {"ok", "reason", "video", "packets", ...}."""
    if not V.FFMPEG:
        return {"ok": False, "reason": "ffmpeg not found"}
    ext = os.path.splitext(src)[1].lower()
    raw_rate = fps if fps and 0 < fps < 241 else 25.0
    before = _streams(src, raw_rate if ext in RAW else 0.0)
    if before["video"] not in MP4_VIDEO:
        return {"ok": False, "reason": f"video codec {before['video'] or 'unknown'} cannot be copied into MP4 "
                                       "without re-encoding – play the original in VLC", **before}
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part.mp4"
    inp = ["-fflags", "+genpts", *(["-r", f"{raw_rate:g}"] if ext in RAW else []), *V._input_args(src), "-i", src]
    maps = ["-map", "0:v:0"] + (["-map", "0:a:0"] if before["audio"] in MP4_AUDIO else [])
    cmd = [V.FFMPEG, "-nostdin", "-y", "-v", "error", *inp, *maps, "-c", "copy",
           *(["-tag:v", "hvc1"] if before["video"] == "hevc" else []), "-movflags", "+faststart", part]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "ffmpeg timed out", **before}
    if r.returncode != 0 or not os.path.exists(part):
        if os.path.exists(part):
            os.remove(part)
        return {"ok": False, "reason": (r.stderr.strip().splitlines() or ["ffmpeg failed"])[-1][:300], **before}
    after = _streams(part)
    same = after["video"] == before["video"] and after["packets"] == before["packets"] and before["packets"] > 0
    if not same:
        os.remove(part)
        return {"ok": False, "reason": f"copy check failed: {before['video']}/{before['packets']} packets in the "
                                       f"original, {after['video']}/{after['packets']} in the copy", **before}
    os.replace(part, dest)
    return {"ok": True, "reason": "", "video": before["video"], "packets": before["packets"],
            "audio": before["audio"] if before["audio"] in MP4_AUDIO else "",
            "audio_dropped": before["audio"] if before["audio"] and before["audio"] not in MP4_AUDIO else ""}


def ingest_source(src_dir: str, out_dir: str, extensions=None, remux_files: bool = True, verify: bool = False,
                  progress: Optional[Callable[[dict], None]] = None, cancel=None) -> dict:
    """Hash (and remux) every recording of one seized source. Returns the ingest record."""
    from .config import VIDEO_EXTENSIONS
    exts = {e.lower() for e in (extensions or VIDEO_EXTENSIONS)}
    path = os.path.join(out_dir, "ingest.json")
    old: Dict[str, dict] = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                old = {f["rel"]: f for f in json.load(fh).get("files", [])}
        except (OSError, ValueError, KeyError, TypeError):
            old = {}
    files: List[str] = []
    out_abs = os.path.realpath(out_dir)
    for root, dirs, fs in os.walk(src_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and os.path.realpath(os.path.join(root, d)) != out_abs)
        files += [os.path.join(root, f) for f in sorted(fs, key=V.natural_key)
                  if os.path.splitext(f)[1].lower() in exts and not f.startswith(".")]
    rows, changed = [], []
    for i, p in enumerate(files, 1):
        if cancel is not None and cancel.is_set():
            break
        rel = os.path.relpath(p, src_dir).replace(os.sep, "/")
        st = os.stat(p)
        if progress:
            progress({"stage": "hashing", "index": i, "total": len(files), "path": rel,
                      "percent": round(100 * (i - 1) / max(1, len(files)), 1)})
        prev = old.get(rel)
        same_stat = prev and prev.get("bytes") == st.st_size and prev.get("mtime") == int(st.st_mtime)
        if same_stat and not verify:
            digest = prev["sha256"]
        else:
            digest = sha256_file(p)
            if prev and prev.get("sha256") and prev["sha256"] != digest:
                changed.append(rel)
        row = {"rel": rel, "bytes": st.st_size, "mtime": int(st.st_mtime), "sha256": digest,
               "hashed_at": (prev or {}).get("hashed_at") if same_stat and not verify and prev else _now()}
        if remux_files and needs_remux(p):
            dest = os.path.join(out_dir, "playback", rel + ".mp4")
            if same_stat and prev.get("playback") and os.path.exists(os.path.join(out_dir, prev["playback"])) and \
                    prev.get("sha256") == digest:
                row.update({k: prev[k] for k in ("playback", "playback_sha256", "remux") if k in prev})
            else:
                if progress:
                    progress({"stage": "remuxing", "index": i, "total": len(files), "path": rel,
                              "percent": round(100 * (i - 0.5) / max(1, len(files)), 1)})
                meta = V.probe(p)
                res = remux(p, dest, meta.get("fps") or 0.0)
                row["remux"] = res
                if res["ok"]:
                    row["playback"] = os.path.relpath(dest, out_dir).replace(os.sep, "/")
                    row["playback_sha256"] = sha256_file(dest)
                else:
                    log.warning("remux of %s not possible: %s", rel, res["reason"])
        rows.append(row)
    record = {"source_dir": src_dir, "ingested_at": _now(), "files": rows,
              "merkle_root": merkle.root(merkle.file_lines({r["rel"]: r["sha256"] for r in rows})),
              "total_bytes": sum(r["bytes"] for r in rows), "changed_since_last_ingest": changed,
              "verified": bool(verify)}
    os.makedirs(out_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=1)
    os.replace(tmp, path)
    return record


def load_ingest(out_dir: str) -> Optional[dict]:
    try:
        with open(os.path.join(out_dir, "ingest.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")

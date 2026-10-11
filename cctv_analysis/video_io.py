"""Video discovery, probing, fast frame reading (ffmpeg pipe with OpenCV
fallback), recording-time inference, clip export and Google-Drive links."""
from __future__ import annotations

import datetime as dt
import fnmatch
import json
import os
import re
import shutil
import subprocess
import time
from typing import Iterator, List, Optional, Tuple

import cv2
import numpy as np

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
HW_DECODE = "auto"          # auto | cuda | none   (set from Config.hw_decode)
_HW_STATE = {}              # path-independent cache: {"cuda": True/False}


def _cuda_present() -> bool:
    return shutil.which("nvidia-smi") is not None or os.path.exists("/dev/nvidia0")


def _hw_args() -> List[str]:
    if HW_DECODE == "none" or not FFMPEG:
        return []
    if HW_DECODE == "auto" and not _cuda_present():
        return []
    if _HW_STATE.get("cuda") is False:
        return []
    return ["-hwaccel", "cuda"]

# --------------------------------------------------------------------- files

def find_videos(cfg) -> List[str]:
    exts = {e.lower() for e in cfg.extensions}
    out = []
    if os.path.isfile(cfg.input_dir):
        return [cfg.input_dir]
    cutoff = time.time() - cfg.only_newer_than_days * 86400 if cfg.only_newer_than_days else None
    for root, dirs, files in os.walk(cfg.input_dir):
        # never descend into our own output folder
        dirs[:] = sorted(d for d in dirs
                         if os.path.abspath(os.path.join(root, d)) != os.path.abspath(cfg.output_dir))
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() not in exts:
                continue
            p = os.path.join(root, f)
            if cfg.include_glob and not fnmatch.fnmatch(f, cfg.include_glob):
                continue
            if cfg.exclude_glob and fnmatch.fnmatch(f, cfg.exclude_glob):
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_size < 10_000:
                continue
            if cutoff and st.st_mtime < cutoff:
                continue
            out.append(p)
        if not cfg.recursive:
            break
    if cfg.max_videos:
        out = out[: cfg.max_videos]
    return out


def file_fingerprint(path: str) -> str:
    st = os.stat(path)
    return f"{st.st_size}-{int(st.st_mtime)}"

# ------------------------------------------------------------------- probing

def _input_args(path: str) -> List[str]:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".dav":
        return ["-f", "dhav"]          # Dahua container
    if ext in (".h264", ".264"):
        return ["-f", "h264"]
    if ext in (".h265", ".265", ".hevc"):
        return ["-f", "hevc"]
    return []


def probe(path: str) -> dict:
    info = {"duration": 0.0, "fps": 0.0, "width": 0, "height": 0, "codec": "",
            "creation_time": None, "nb_frames": 0, "format": ""}
    if FFPROBE:
        for extra in (_input_args(path), []):
            try:
                r = subprocess.run(
                    [FFPROBE, "-v", "error", *extra, "-print_format", "json",
                     "-show_format", "-show_streams", path],
                    capture_output=True, text=True, timeout=120)
                j = json.loads(r.stdout or "{}")
            except Exception:
                j = {}
            vs = [s for s in j.get("streams", []) if s.get("codec_type") == "video"]
            if not vs:
                continue
            s = vs[0]
            fmt = j.get("format", {})
            info["codec"] = s.get("codec_name", "")
            info["width"] = int(s.get("width") or 0)
            info["height"] = int(s.get("height") or 0)
            info["format"] = fmt.get("format_name", "")
            for key in ("avg_frame_rate", "r_frame_rate"):
                try:
                    n, d = s.get(key, "0/1").split("/")
                    v = float(n) / float(d) if float(d) else 0
                    if 0 < v < 241:
                        info["fps"] = v
                        break
                except Exception:
                    pass
            for src in (fmt, s):
                try:
                    if float(src.get("duration") or 0) > 0:
                        info["duration"] = float(src["duration"])
                        break
                except Exception:
                    pass
            info["nb_frames"] = int(s.get("nb_frames") or 0)
            tags = {**fmt.get("tags", {}), **s.get("tags", {})}
            info["creation_time"] = tags.get("creation_time")
            break
    if not info["width"] or not info["duration"]:
        cap = cv2.VideoCapture(path)
        if cap.isOpened():
            info["width"] = info["width"] or int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            info["height"] = info["height"] or int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = cap.get(cv2.CAP_PROP_FPS) or 0
            n = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
            info["fps"] = info["fps"] or (fps if 0 < fps < 241 else 0)
            if not info["duration"] and fps and n > 0:
                info["duration"] = n / fps
        cap.release()
    if not info["duration"] and FFMPEG:
        # last resort: decode-count (slow but reliable for raw streams)
        try:
            r = subprocess.run([FFMPEG, "-v", "error", *_input_args(path), "-i", path,
                                "-map", "0:v:0", "-c", "copy", "-f", "null", "-"],
                               capture_output=True, text=True, timeout=1800)
            m = re.findall(r"time=(\d+):(\d+):([\d.]+)", r.stderr)
            if m:
                h, mi, s = m[-1]
                info["duration"] = int(h) * 3600 + int(mi) * 60 + float(s)
        except Exception:
            pass
    return info

# ------------------------------------------------------------- frame reading

def iter_frames(path: str, fps: float, width: int, start: float = 0.0,
                duration: Optional[float] = None, gray: bool = False,
                src_size: Tuple[int, int] = (0, 0)) -> Iterator[Tuple[float, np.ndarray]]:
    """Yield (timestamp_seconds, frame) sampled at `fps`, resized to `width`."""
    sw, sh = src_size
    if width and sw and width >= sw:
        width = sw
    produced = False
    if FFMPEG and sw and sh:
        w = int(width or sw) // 2 * 2
        h = int(round(sh * w / sw)) // 2 * 2
        ch = 1 if gray else 3
        hw = _hw_args()
        cmd = [FFMPEG, "-nostdin", "-v", "error", *hw, "-threads", "0"]
        if start > 0:
            cmd += ["-ss", f"{start:.3f}"]
        cmd += [*_input_args(path), "-i", path]
        if duration:
            cmd += ["-t", f"{duration:.3f}"]
        cmd += ["-map", "0:v:0", "-an", "-sn", "-vf",
                f"fps={fps},scale={w}:{h}:flags=area", "-pix_fmt",
                "gray" if gray else "bgr24", "-f", "rawvideo", "pipe:1"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                bufsize=w * h * ch * 4)
        nbytes = w * h * ch
        i = 0
        try:
            while True:
                buf = proc.stdout.read(nbytes)
                if len(buf) < nbytes:
                    if i == 0 and hw:
                        proc.kill()
                        prev = _HW_STATE.get("cuda")
                        _HW_STATE["cuda"] = False      # retry on CPU
                        got = 0
                        for item in iter_frames(path, fps, width, start, duration, gray, src_size):
                            got += 1
                            yield item
                        if not got:                    # empty range, not a GPU problem
                            if prev is None:
                                _HW_STATE.pop("cuda", None)
                            else:
                                _HW_STATE["cuda"] = prev
                        return
                    break
                if i == 0 and hw:
                    _HW_STATE["cuda"] = True
                frame = np.frombuffer(buf, np.uint8).reshape((h, w) if gray else (h, w, 3))
                produced = True
                yield start + i / fps, frame
                i += 1
        finally:
            proc.stdout.close()
            proc.kill()
            proc.wait()
    if produced:
        return
    # ---- OpenCV fallback
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return
    nfps = cap.get(cv2.CAP_PROP_FPS) or 25
    if not (0 < nfps < 241):
        nfps = 25
    if start > 0:
        cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000)
    step = max(1, int(round(nfps / fps)))
    idx = 0
    end = start + duration if duration else None
    while True:
        ok = cap.grab()
        if not ok:
            break
        t = start + idx / nfps
        if end and t > end:
            break
        if idx % step == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            if width and frame.shape[1] != width:
                h = int(round(frame.shape[0] * width / frame.shape[1]))
                frame = cv2.resize(frame, (width, h), interpolation=cv2.INTER_AREA)
            if gray:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            yield t, frame
        idx += 1
    cap.release()


def read_frame_at(path: str, t: float, width: int = 0, src_size=(0, 0)) -> Optional[np.ndarray]:
    for _, f in iter_frames(path, 1.0, width, start=max(0.0, t), duration=1.0, src_size=src_size):
        return f.copy()
    return None

# ------------------------------------------------------------ time inference

_BUILTIN_PATTERNS = [
    # 20261011143000, 2026-10-11_14-30-00, 2026.10.11 14.30.00, 20261011T143000
    r"(?P<Y>20\d{2})[-_.]?(?P<m>[01]\d)[-_.]?(?P<d>[0-3]\d)[-_ T.]?(?P<H>[0-2]\d)[-_.:hH]?(?P<M>[0-5]\d)[-_.:mM]?(?P<S>[0-5]\d)",
    # 11-10-2026_14-30-00 (day first)
    r"(?P<d>[0-3]\d)[-_.](?P<m>[01]\d)[-_.](?P<Y>20\d{2})[-_ T.]?(?P<H>[0-2]\d)[-_.:]?(?P<M>[0-5]\d)[-_.:]?(?P<S>[0-5]\d)",
    # date only + hour-minute
    r"(?P<Y>20\d{2})[-_.]?(?P<m>[01]\d)[-_.]?(?P<d>[0-3]\d)[-_ T.]?(?P<H>[0-2]\d)[-_.:]?(?P<M>[0-5]\d)(?!\d)",
]
_DATE_ONLY = r"(?P<Y>20\d{2})[-_.]?(?P<m>[01]\d)[-_.]?(?P<d>[0-3]\d)(?!\d)"
_TIME_ONLY = r"(?<!\d)(?P<H>[0-2]\d)[.:_-](?P<M>[0-5]\d)[.:_-](?P<S>[0-5]\d)"   # Dahua 14.30.00-14.45.00


def _tz(name: str):
    if ZoneInfo is None:
        return dt.timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        return dt.timezone.utc


def _mk(g: dict, tz) -> Optional[dt.datetime]:
    try:
        d = dt.datetime(int(g["Y"]), int(g["m"]), int(g["d"]), int(g.get("H") or 0),
                        int(g.get("M") or 0), int(g.get("S") or 0), tzinfo=tz)
        if 2000 <= d.year <= 2100:
            return d
    except (ValueError, TypeError, KeyError):
        pass
    return None


def time_from_name(path: str, root: str, patterns: List[str], tz) -> Optional[dt.datetime]:
    name = os.path.basename(path)
    rel = os.path.relpath(path, root) if root and os.path.isdir(root) else path
    for pat in list(patterns) + _BUILTIN_PATTERNS:
        for text in (name, rel):
            m = re.search(pat, text)
            if m:
                d = _mk(m.groupdict(), tz)
                if d:
                    return d
    # date somewhere in path + time in file name (Dahua / many NVR exports)
    md = re.search(_DATE_ONLY, rel)
    mt = re.search(_TIME_ONLY, name)
    if md and mt:
        return _mk({**md.groupdict(), **mt.groupdict()}, tz)
    return None


def infer_start_time(path: str, meta: dict, cfg, vlm_reader=None) -> Tuple[Optional[dt.datetime], str]:
    """Return (aware datetime of first frame, source label)."""
    tz = _tz(cfg.timezone)
    base = os.path.basename(path)
    rel = os.path.relpath(path, cfg.input_dir) if os.path.isdir(cfg.input_dir) else base
    for key in (rel, base):
        if key in cfg.time_overrides:
            return dt.datetime.fromisoformat(cfg.time_overrides[key]).replace(tzinfo=tz), "override"
    for src in cfg.time_sources:
        if src == "filename":
            d = time_from_name(path, cfg.input_dir, cfg.filename_time_patterns, tz)
            if d:
                return d, "filename"
        elif src == "metadata" and meta.get("creation_time"):
            try:
                d = dt.datetime.fromisoformat(meta["creation_time"].replace("Z", "+00:00"))
                if d.tzinfo is None:
                    d = d.replace(tzinfo=dt.timezone.utc)
                if 2000 <= d.year <= 2100 and d.year != 1970:
                    return d.astimezone(tz), "metadata"
            except Exception:
                pass
        elif src == "vlm_osd" and vlm_reader is not None:
            try:
                d = vlm_reader(path, meta)
                if d:
                    return d.replace(tzinfo=tz) if d.tzinfo is None else d, "osd(vlm)"
            except Exception:
                pass
        elif src == "mtime":
            st = os.stat(path)
            d = dt.datetime.fromtimestamp(st.st_mtime, tz) - dt.timedelta(seconds=meta.get("duration") or 0)
            return d, "file-modified(approx)"
    return None, "unknown"


def camera_name(path: str, cfg) -> str:
    if cfg.camera_name_source == "none":
        return ""
    base = os.path.basename(path)
    m = re.search(r"(?i)(?:^|[^a-z])((?:ch|cam|camera|channel|d)[ _-]?\d{1,3})(?![0-9])", base)
    if cfg.camera_name_source in ("auto", "filename") and m:
        return re.sub(r"[ _-]", "", m.group(1)).lower()
    if cfg.camera_name_source in ("auto", "folder"):
        parent = os.path.dirname(os.path.abspath(path))
        if os.path.abspath(parent) != os.path.abspath(cfg.input_dir):
            rel = os.path.relpath(parent, cfg.input_dir)
            first = rel.split(os.sep)[0]
            if first and first != ".." and not re.fullmatch(r"[\d\-_.]+", first):
                return first
    return "cam"

# ---------------------------------------------------------------- clip export

def export_clip(path: str, start: float, duration: float, out_path: str,
                width: int = 960, label: str = "") -> bool:
    if not FFMPEG:
        return False
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    vf = f"scale='min({width},iw)':-2"
    if label:
        safe = re.sub(r"[^A-Za-z0-9 _:./-]", "", label).replace(":", r"\:")
        vf += (f",drawtext=text='{safe}':x=10:y=h-th-10:fontsize=18:fontcolor=white:"
               "box=1:boxcolor=black@0.5")
    base = [FFMPEG, "-nostdin", "-y", "-v", "error", "-ss", f"{max(0, start):.3f}",
            *_input_args(path), "-i", path, "-t", f"{duration:.3f}", "-map", "0:v:0", "-an"]
    tail = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "27", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", out_path]
    for filt in (vf, f"scale='min({width},iw)':-2"):     # retry without drawtext
        try:
            r = subprocess.run(base + ["-vf", filt] + tail, capture_output=True, timeout=900)
            if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
                return True
        except Exception:
            pass
    return False

# --------------------------------------------------------------- drive links

def drive_link(path: str) -> str:
    """Google-Drive web link for a file on a Colab-mounted Drive ('' if unknown)."""
    try:
        fid = os.getxattr(path, "user.drive.id").decode()
        if fid:
            return f"https://drive.google.com/file/d/{fid}/view"
    except Exception:
        pass
    return ""


def fmt_offset(sec: float) -> str:
    sec = max(0.0, float(sec))
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

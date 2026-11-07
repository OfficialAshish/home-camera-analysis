"""Video discovery, probing, fast frame reading (ffmpeg pipe with OpenCV
fallback), recording-time inference, clip export and Google-Drive links."""
from __future__ import annotations

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

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
HW_DECODE = "auto"          # auto | cuda | none   (set from Config.hw_decode)
_HW_STATE: dict = {}              # path-independent cache: {"cuda": True/False}


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

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def natural_key(s: str):
    """'clip2' < 'clip10' (numbers compared as numbers)."""
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", s)]


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", t.lower())


def day_month(text: str):
    """(day, month) from things like '6-oct', '06Oct26', 'Oct 6', '2026-10-06', '06-10-2026', '20261006'."""
    t = text.lower()
    m = re.search(r"(?<!\d)(\d{1,2})\s*[-_. ]?\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*", t)
    if m:
        return int(m.group(1)), _MONTHS[m.group(2)]
    m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*[-_. ]?\s*(\d{1,2})(?!\d)", t)
    if m:
        return int(m.group(2)), _MONTHS[m.group(1)]
    m = re.search(r"(?<!\d)(20\d{2})[-_.]?([01]\d)[-_.]?([0-3]\d)(?!\d)", t)
    if m:
        return int(m.group(3)), int(m.group(2))
    m = re.search(r"(?<!\d)([0-3]?\d)[-_.]([01]?\d)[-_.](20\d{2}|\d{2})(?!\d)", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def folder_matches(name: str, keyword: str) -> bool:
    kw = keyword.strip()
    if not kw:
        return False
    n, k = _norm(name), _norm(kw)
    if k and k in n:
        i = n.find(k)
        # avoid '6oct' matching '16oct'
        if not (k[0].isdigit() and i > 0 and n[i - 1].isdigit()):
            return True
    dk, dn = day_month(kw), day_month(name)
    return bool(dk and dn and dk == dn)


def find_videos(cfg) -> List[str]:
    import logging
    log = logging.getLogger("cctv")
    exts = {e.lower() for e in cfg.extensions}
    out = []
    if os.path.isfile(cfg.input_dir):
        return [cfg.input_dir]
    if not os.path.isdir(cfg.input_dir):
        raise FileNotFoundError(f"input folder not found: {cfg.input_dir}")
    kws = [k for k in (cfg.folder_keywords or []) if k and k.strip()]
    cutoff = time.time() - cfg.only_newer_than_days * 86400 if cfg.only_newer_than_days else None
    out_abs = os.path.abspath(cfg.output_dir)
    seen_dirs = set()
    for root, dirs, files in os.walk(cfg.input_dir):
        dirs[:] = sorted((d for d in dirs if os.path.abspath(os.path.join(root, d)) != out_abs
                         and not d.startswith(".")), key=natural_key)
        rel_parts = [] if os.path.abspath(root) == os.path.abspath(cfg.input_dir) else \
            os.path.relpath(root, cfg.input_dir).split(os.sep)
        if rel_parts:
            seen_dirs.add(rel_parts[-1])
        if kws and not any(folder_matches(part, k) for part in rel_parts for k in kws):
            if not cfg.recursive:
                break
            continue
        for f in sorted(files, key=natural_key):
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
    if kws and not out:
        log.warning("No sub-folder matched %s. Folders found: %s", kws, ", ".join(sorted(seen_dirs)[:60]))
    if cfg.max_videos:
        out = out[: cfg.max_videos]
    return out


def quick_hash(path: str, chunk: int = 2 << 20) -> str:
    """Content fingerprint of size + first/middle/last 2 MB (fast even on Drive);
    identical copies under other names/folders get the same value."""
    import hashlib
    size = os.path.getsize(path)
    h = hashlib.sha1(str(size).encode())
    with open(path, "rb") as fh:
        for off in sorted({0, max(0, size // 2 - chunk // 2), max(0, size - chunk)}):
            fh.seek(off)
            h.update(fh.read(chunk))
    return h.hexdigest()


def file_fingerprint(path: str) -> str:
    st = os.stat(path)
    return f"{st.st_size}-{int(st.st_mtime)}"

# ------------------------------------------------------------------- probing

# Raw elementary streams have no index or timestamps: ffmpeg cannot seek in them before decoding.
RAW_FORMATS = ("h264", "hevc")
# original path -> lossless MP4 remux of it (same packets, same timing; cctv_analysis.ingest). Frame
# reading decodes the proxy, which can seek; hashes and evidence cuts always use the original.
_PROXY: dict = {}


def register_proxy(original: str, proxy: str) -> None:
    if proxy and os.path.isfile(proxy):
        _PROXY[os.path.abspath(original)] = proxy


def decode_path(path: str) -> str:
    return _PROXY.get(os.path.abspath(path), path)


def is_raw(path: str) -> bool:
    a = _input_args(path)
    return bool(a) and a[1] in RAW_FORMATS


def seek_args(path: str, t: float, copy: bool = False, fps: float = 0.0):
    """(arguments before -i, arguments after -i) that start reading at `t` seconds.
    Containers seek on input (fast, and exact with decoding); raw streams cannot, so they seek on
    output (decode and discard up to t – exact, slower); a stream copy of a raw stream also needs
    generated timestamps (-fflags +genpts -r <fps>) or nothing could be cut."""
    t = max(0.0, float(t))
    if not is_raw(path):
        return (["-ss", f"{t:.3f}"] if t > 0 else []), []
    pre = ["-fflags", "+genpts", "-r", f"{fps or probe(path)['fps'] or 25:g}"] if copy else []
    return pre, (["-ss", f"{t:.3f}"] if t > 0 else [])


def _input_args(path: str) -> List[str]:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".dav":
        return ["-f", "dhav"]          # Dahua container
    if ext in (".h264", ".264"):
        return ["-f", "h264"]
    if ext in (".h265", ".265", ".hevc"):
        return ["-f", "hevc"]
    return []


def _raw_timing(path: str):
    """(fps, frames) of a raw H.264/H.265 stream: frames = video packets (demux only, no
    decoding); fps from the timestamps the decoder gives the first frames (the stream's own VUI
    timing, else ffmpeg's 25 fps default – the same timing every later decode of the file uses)."""
    if not (FFPROBE and FFMPEG):
        return 0.0, 0
    try:
        r = subprocess.run([FFPROBE, "-v", "error", *_input_args(path), "-count_packets", "-select_streams", "v:0",
                            "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", path],
                           capture_output=True, text=True, timeout=1800)
        n = int((r.stdout.strip().splitlines() or ["0"])[0].strip(",") or 0)
        r = subprocess.run([FFMPEG, "-nostdin", "-v", "info", *_input_args(path), "-i", path, "-map", "0:v:0",
                            "-frames:v", "16", "-vf", "showinfo", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=120)
        pts = [float(x) for x in re.findall(r"pts_time:([0-9.]+)", r.stderr)]
        steps = sorted(b - a for a, b in zip(pts, pts[1:]) if b > a)
        if not steps or not n:
            return 0.0, n
        fps = 1.0 / steps[len(steps) // 2]
        return (round(fps, 3) if 0 < fps < 241 else 0.0), n
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0, 0


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
    if info["width"] and not info["duration"] and _input_args(path) and _input_args(path)[1] in ("h264", "hevc"):
        # Raw elementary streams (.264/.265 DVR dumps) have no container duration and their
        # probed frame rate is the demuxer's default; time them as the decoder will.
        fps, n = _raw_timing(path)
        if fps and n:
            info["fps"], info["nb_frames"], info["duration"] = fps, n, n / fps
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
        src = decode_path(path)
        pre, post = seek_args(src, start)
        cmd += [*pre, *_input_args(src), "-i", src, *post]
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
    """The frame shown at time t (first frame with pts >= t), resized to `width` (0 = full size).
    Accurate seek + '-frames:v 1' – an fps filter here would return the *last* frame of the
    first output interval (~0.5 s late), which mis-crops moving objects."""
    sw, sh = src_size
    if FFMPEG and sw and sh:
        w = int(min(width, sw) if width else sw) // 2 * 2
        h = int(round(sh * w / sw)) // 2 * 2
        src = decode_path(path)
        pre, post = seek_args(src, t)
        cmd = [FFMPEG, "-nostdin", "-v", "error", *pre, *_input_args(src), "-i", src, *post,
               "-map", "0:v:0", "-an", "-sn", "-frames:v", "1", "-vf", f"scale={w}:{h}:flags=area",
               "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=120)
            if len(r.stdout) == w * h * 3:
                return np.frombuffer(r.stdout, np.uint8).reshape((h, w, 3)).copy()
        except Exception:
            pass
    for _, f in iter_frames(path, 1.0, width, start=max(0.0, t), duration=1.0, src_size=src_size):
        return f.copy()
    return None


def read_frames(path: str, t: float, n: int, src_size=(0, 0)) -> List[np.ndarray]:
    """`n` consecutive decoded frames at full size, the first being the frame shown at time t
    (no fps filter: every frame of the recording, none duplicated or dropped)."""
    sw, sh = src_size
    if not (FFMPEG and sw and sh) or n < 1:
        return []
    w, h = sw // 2 * 2, sh // 2 * 2
    src = decode_path(path)
    pre, post = seek_args(src, t)
    cmd = [FFMPEG, "-nostdin", "-v", "error", *pre, *_input_args(src), "-i", src, *post,
           "-map", "0:v:0", "-an", "-sn", "-frames:v", str(int(n)), "-vf", f"scale={w}:{h}:flags=area",
           "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return []
    size = w * h * 3
    return [np.frombuffer(r.stdout[i:i + size], np.uint8).reshape((h, w, 3)).copy()
            for i in range(0, len(r.stdout) - size + 1, size)]

# ------------------------------------------------------------- cameras

def _device_serial(base: str) -> str:
    """Camera serial prefix of cloud exports, e.g. 'G225087WSFW' in 'G225087WSFW-cloud-18056409.mp4'."""
    m = re.match(r"([A-Z0-9]{8,20})[-_]", base)
    if m and re.search(r"[A-Z]", m.group(1)) and re.search(r"\d", m.group(1)):
        return m.group(1)
    return ""


def camera_name(path: str, cfg) -> str:
    """Camera label: 'ch01'-style token in the file name, else the first sub-folder (unless that
    folder is a date such as '31Oct26New' or '2026-10-11'), else a device serial prefix, else 'cam'."""
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
            if first and first != ".." and not re.fullmatch(r"[\d\-_.]+", first) and not day_month(first):
                return first
    if cfg.camera_name_source in ("auto", "filename"):
        serial = _device_serial(base)
        if serial:
            return serial
    return "cam"

# ------------------------------------------------------------- encoding

_ENCODER: dict = {}


def h264_args(crf: int = 23) -> List[str]:
    """Video encoder arguments for the available ffmpeg build: libx264 (GPL builds), else
    libopenh264 (LGPL builds, Cisco's BSD codec), else MPEG-4 Part 2 (plays in VLC, not browsers)."""
    if "args" not in _ENCODER:
        enc = ""
        try:
            r = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30)
            enc = r.stdout
        except Exception:
            pass
        if " libx264 " in enc:
            _ENCODER["name"], _ENCODER["args"] = "libx264", ["-c:v", "libx264", "-preset", "veryfast"]
        elif " libopenh264 " in enc:
            _ENCODER["name"], _ENCODER["args"] = "libopenh264", ["-c:v", "libopenh264", "-allow_skip_frames", "1"]
        else:
            _ENCODER["name"], _ENCODER["args"] = "mpeg4", ["-c:v", "mpeg4"]
    args = list(_ENCODER["args"])
    if _ENCODER["name"] == "libx264":
        args += ["-crf", str(crf)]
    elif _ENCODER["name"] == "libopenh264":
        args += ["-b:v", "3M"]
    else:
        args += ["-q:v", "4"]
    return args + ["-pix_fmt", "yuv420p"]

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
    src = decode_path(path)
    pre, post = seek_args(src, start)
    base = [FFMPEG, "-nostdin", "-y", "-v", "error", *pre, *_input_args(src), "-i", src, *post,
            "-t", f"{duration:.3f}", "-map", "0:v:0", "-an"]
    tail = [*h264_args(27), "-movflags", "+faststart", out_path]
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

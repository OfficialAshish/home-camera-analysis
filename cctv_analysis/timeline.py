"""Recording-time resolution – the single place that decides *when* a frame was recorded.

Sources, tried in ``Config.time_sources`` order (default filename → osd → metadata → mtime):

* ``filename`` – time encoded in the file name / path (Hikvision, Dahua, NVR exports …)
* ``osd``      – the DVR's burnt-in clock, read by OCR at several offsets; the readings must
                 agree with each other (clock(t) − t = constant) and with the folder date, so a
                 single misread digit cannot shift a whole video
* ``metadata`` – container ``creation_time``; rejected when it contradicts the folder date
                 (cloud/phone exports stamp the *download* time here)
* ``mtime``    – file modified time minus duration; always marked approximate

A date in the folder or file name (``31Oct26New``, ``2026-10-31``, ``31-10-2026``) is used as a
plausibility check and, for approximate sources, as the day.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from .video_io import read_frame_at

log = logging.getLogger("cctv")

TIME_SOURCES = ("filename", "osd", "metadata", "mtime")
# labels written to videos.time_source that count as exact recording times
EXACT_PREFIXES = ("override", "filename", "osd")

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def is_exact(time_source: Optional[str]) -> bool:
    return bool(time_source) and time_source.startswith(EXACT_PREFIXES)


def tz(name: str):
    try:
        return ZoneInfo(name)
    except Exception:
        return dt.timezone.utc

# ------------------------------------------------------------------ file names

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


def _mk(g: dict, tzinfo) -> Optional[dt.datetime]:
    try:
        d = dt.datetime(int(g["Y"]), int(g["m"]), int(g["d"]), int(g.get("H") or 0),
                        int(g.get("M") or 0), int(g.get("S") or 0), tzinfo=tzinfo)
        if 2000 <= d.year <= 2100:
            return d
    except (ValueError, TypeError, KeyError):
        pass
    return None


def _rel(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root) if root and os.path.isdir(root) else path
    except ValueError:
        return path


def time_from_name(path: str, root: str, patterns: List[str], tzinfo) -> Optional[dt.datetime]:
    name = os.path.basename(path)
    rel = _rel(path, root)
    for pat in list(patterns) + _BUILTIN_PATTERNS:
        for text in (name, rel):
            m = re.search(pat, text)
            if m:
                d = _mk(m.groupdict(), tzinfo)
                if d:
                    return d
    # date somewhere in path + time in file name (Dahua / many NVR exports)
    md = re.search(_DATE_ONLY, rel)
    mt = re.search(_TIME_ONLY, name)
    if md and mt:
        return _mk({**md.groupdict(), **mt.groupdict()}, tzinfo)
    return None


def date_from_text(text: str) -> Optional[dt.date]:
    """A full calendar date in a folder/file name: 31Oct26, 31-Oct-2026, Oct31_2026, 2026-10-31,
    20261031, 31-10-2026, 31.10.26. Returns None when no unambiguous full date is present."""
    t = text.lower()
    cands = []
    for m in re.finditer(r"(?<!\d)([0-3]?\d)\s*[-_. ]?\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
                         r"\s*[-_. ]?\s*(20\d{2}|\d{2})(?!\d)", t):
        cands.append((m.group(3), _MONTHS[m.group(2)], m.group(1)))
    for m in re.finditer(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*[-_. ]?\s*([0-3]?\d)"
                         r"\s*[-_.,' ]+\s*(20\d{2})(?!\d)", t):
        cands.append((m.group(3), _MONTHS[m.group(1)], m.group(2)))
    for m in re.finditer(r"(?<!\d)(20\d{2})[-_.]?([01]\d)[-_.]?([0-3]\d)(?!\d)", t):
        cands.append((m.group(1), int(m.group(2)), m.group(3)))
    for m in re.finditer(r"(?<!\d)([0-3]?\d)[-_.]([01]?\d)[-_.](20\d{2}|\d{2})(?!\d)", t):
        cands.append((m.group(3), int(m.group(2)), m.group(1)))
    for y, mo, d in cands:
        y = int(y)
        y = y + 2000 if y < 100 else y
        try:
            return dt.date(y, int(mo), int(d))
        except ValueError:
            continue
    return None


def path_date_hint(path: str, root: str) -> Optional[dt.date]:
    """Date written in the file name or the nearest folder that carries one."""
    rel = _rel(path, root)
    parts = rel.replace("\\", "/").split("/")
    for part in [parts[-1]] + parts[-2::-1]:
        d = date_from_text(os.path.splitext(part)[0] if part == parts[-1] else part)
        if d:
            return d
    return None

# ------------------------------------------------------------ clock-text parsing

_DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "l": "1", "I": "1", "|": "1", "i": "1",
                            "Z": "2", "z": "2", "S": "5", "s": "5", "B": "8", "g": "9", "q": "9"})
_DIGIT_FIX_CHARS = set("OoDQlI|iZzSsBgq")
_MON_RE = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"


def _fix_digits(text: str) -> str:
    """Repair OCR letter/digit confusions only where a letter sits inside a number
    ('2O26', '05:3B', 'O5:38') – month and weekday names ('31Oct', 'Sat') are kept."""
    out = list(text)
    n = len(text)
    for i, c in enumerate(text):
        if c not in _DIGIT_FIX_CHARS:
            continue
        prev = text[i - 1] if i else ""
        nxt = text[i + 1] if i + 1 < n else ""
        num_prev, num_next = prev.isdigit(), nxt.isdigit()
        sep_prev, sep_next = prev in ":-/." or prev == "", nxt in ":-/." or nxt == ""
        if (num_prev and (num_next or sep_next)) or (num_next and (num_prev or sep_prev or prev == " ")):
            out[i] = c.translate(_DIGIT_FIX)
    return "".join(out)


def _time_part(s: str):
    # no "not preceded by a digit" guard: OCR often glues the date to the time ('2026-10-3105:38:27')
    m = re.search(r"([0-2]?\d)\s*[:.]\s*([0-5]\d)\s*[:.]\s*([0-5]\d)(?!\d)(?:\s*([ap])\.?m)?", s, re.I)
    if not m:
        return None
    h, mi, se = int(m.group(1)), int(m.group(2)), int(m.group(3))
    ap = (m.group(4) or "").lower()
    if ap == "p" and h < 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    if h > 23:
        return None
    return h, mi, se, m.start(), m.end()


def parse_clock_text(text: str, order: str = "auto", hint: Optional[dt.date] = None) -> Optional[dt.datetime]:
    """Parse a DVR clock such as '2026-10-31 05:38:27', '2026-10-3105:38:27' (OCR dropped the
    space), '31/10/2026 Sat 05:38:27 PM', '10-31-2026 05:38:27', '31 Oct 2026 05:38:27'.
    ``order`` = auto | ymd | dmy | mdy; for ambiguous day/month ``auto`` prefers the reading that
    matches ``hint`` (folder date), else day-first. Returns a naive datetime or None."""
    if not text:
        return None
    s = _fix_digits(text)
    tp = _time_part(s)
    if not tp:
        return None
    H, M, S, ts, te = tp
    date_txt = s[:ts] + " " + s[te:]
    cands = []
    m = re.search(r"(?<!\d)(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})", date_txt)
    if m and order in ("auto", "ymd"):
        cands.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    m = re.search(r"(?<!\d)(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(20\d{2}|\d{2})(?!\d)", date_txt)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = y + 2000 if y < 100 else y
        if order in ("auto", "dmy"):
            cands.append((y, b, a))
        if order in ("auto", "mdy"):
            cands.append((y, a, b))
    m = re.search(rf"(?<!\d)(\d{{1,2}})\s*[-/. ]?\s*({_MON_RE})[a-z]*\s*[-/., ]?\s*(20\d{{2}})", date_txt, re.I)
    if m:
        cands.append((int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1))))
    m = re.search(rf"({_MON_RE})[a-z]*\s*[-/. ]?\s*(\d{{1,2}})\s*[-/., ]+\s*(20\d{{2}})", date_txt, re.I)
    if m:
        cands.append((int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2))))
    valid = []
    for y, mo, d in cands:
        try:
            valid.append(dt.datetime(y, mo, d, H, M, S))
        except ValueError:
            continue
    if not valid:
        return None
    if hint and len(valid) > 1:
        valid.sort(key=lambda x: abs((x.date() - hint).days))
    return valid[0]

# ---------------------------------------------------------------- OSD reading

def osd_bands(frame) -> list:
    """Regions where DVRs print the clock: top and bottom strips, upscaled for small text."""
    import cv2
    h, w = frame.shape[:2]
    out = []
    for y0, y1 in ((0, int(h * 0.35)), (int(h * 0.65), h)):
        band = frame[y0:y1]
        if band.size == 0:
            continue
        if w < 1200:
            sc = 1200 / w
            band = cv2.resize(band, None, fx=sc, fy=sc, interpolation=cv2.INTER_CUBIC)
        out.append(band)
    return out


def read_clock(frame, ocr, order: str = "auto", hint: Optional[dt.date] = None) -> Optional[dt.datetime]:
    """OCR the clock printed on one frame (naive datetime) or None."""
    if frame is None or not ocr:
        return None
    for band in osd_bands(frame):
        try:
            lines = ocr.read(band)
        except Exception as e:
            log.debug("OCR failed: %s", e)
            continue
        texts = [t for t, _ in lines]
        for cand in texts + [" ".join(texts)]:
            d = parse_clock_text(cand, order, hint)
            if d and 2005 <= d.year <= 2100:
                return d
    return None


def _plausible(d: dt.datetime, hint: Optional[dt.date]) -> bool:
    # a DVR whose clock was never set prints 2000-01-01 etc.; a folder date must match within a day
    if d.year < 2005:
        return False
    return hint is None or abs((d.date() - hint).days) <= 1


def osd_start_time(path: str, meta: dict, ocr, order: str = "auto", hint: Optional[dt.date] = None,
                   samples: int = 3) -> Tuple[Optional[dt.datetime], str]:
    """Video start from the burnt-in clock; start = clock(t) - t.

    Readings at 0.5 s and 1.5 s near the start, the middle and the end are grouped by agreement;
    the biggest group wins and ties go to the group with the earliest reading, so a file stitched
    from several recordings still starts at its first frame. One group = 'consensus'; several =
    'discontinuous' (events then use their own clock reading).
    Returns (naive start, detail) - detail is 'consensus', 'single', 'discontinuous' or a reason."""
    dur = float(meta.get("duration") or 0)
    if not ocr or dur <= 0:
        return None, "no-ocr" if not ocr else "no-duration"
    offs = [min(0.5, dur / 4)]
    if dur > 3:
        offs.append(1.5)
    if samples >= 3 and dur > 8:
        offs.append(dur / 2)
    if samples >= 2 and dur > 4:
        offs.append(dur - 1.5)
    reads: List[tuple] = []
    for n, t in enumerate(offs):
        if n == 2 and not reads:
            break                     # no clock in the first two frames: footage without an OSD, stop here
        f = read_frame_at(path, t, 1280, (meta.get("width") or 0, meta.get("height") or 0))
        d = read_clock(f, ocr, order, hint)
        if d and _plausible(d, hint):
            # the clock shows the current whole second; mid-second is the unbiased estimate
            reads.append((t, d + dt.timedelta(seconds=0.5 - t)))
    if not reads:
        return None, "unreadable"
    if len(reads) == 1:
        return _round(reads[0][1]), "single"

    def close(a, b):
        return abs((a - b).total_seconds()) <= 2.5

    # Group agreeing readings. The biggest group wins; on a tie the group holding the earliest
    # reading wins - a file stitched from several recordings starts at its first frame.
    groups: List[list] = []
    for t, s in reads:
        for g in groups:
            if close(s, g[0][1]):
                g.append((t, s))
                break
        else:
            groups.append([(t, s)])
    groups.sort(key=lambda g: (-len(g), g[0][0]))
    if len(groups[0]) >= 2:
        return _round(groups[0][0][1]), "consensus" if len(groups) == 1 else "discontinuous"
    return _round(reads[0][1]), "discontinuous"


def _round(d: dt.datetime) -> dt.datetime:
    return (d + dt.timedelta(microseconds=500_000)).replace(microsecond=0)

# ----------------------------------------------------------------- resolution

def resolve_start_time(path: str, meta: dict, cfg, ocr=None) -> Tuple[Optional[dt.datetime], str]:
    """Return (aware datetime of the first frame, source label)."""
    zone = tz(cfg.timezone)
    base = os.path.basename(path)
    rel = _rel(path, cfg.input_dir)
    for key in (rel, base):
        if key in cfg.time_overrides:
            return dt.datetime.fromisoformat(cfg.time_overrides[key]).replace(tzinfo=zone), "override"
    hint = path_date_hint(path, cfg.input_dir) if cfg.use_path_date_hint else None
    for src in cfg.time_sources:
        if src == "filename":
            d = time_from_name(path, cfg.input_dir, cfg.filename_time_patterns, zone)
            if d:
                return d, "filename"
        elif src == "osd" and ocr:
            d, detail = osd_start_time(path, meta, ocr, cfg.osd_date_order, hint, cfg.osd_samples)
            if d:
                return d.replace(tzinfo=zone), f"osd({ocr.name},{detail})"
        elif src == "metadata" and meta.get("creation_time"):
            try:
                d = dt.datetime.fromisoformat(meta["creation_time"].replace("Z", "+00:00"))
                if d.tzinfo is None:
                    d = d.replace(tzinfo=dt.timezone.utc)
                d = d.astimezone(zone)
                if 2000 <= d.year <= 2100 and d.year != 1970:
                    # exact day: cloud/phone exports stamp the download time, often the next day
                    if hint and d.date() != hint:
                        log.info("  metadata time %s contradicts folder date %s – ignored", d, hint)
                    else:
                        return d, "metadata"
            except Exception:
                pass
        elif src == "mtime":
            st = os.stat(path)
            d = dt.datetime.fromtimestamp(st.st_mtime, zone) - dt.timedelta(seconds=meta.get("duration") or 0)
            if hint and d.date() != hint:
                return d.replace(year=hint.year, month=hint.month, day=hint.day), "folder-date+file-modified(approx)"
            return d, "file-modified(approx)"
    return None, "unknown"


def event_clock(path: str, meta: dict, start_dt: Optional[dt.datetime], tsrc: str, s: float, cfg, ocr):
    """(base, source) for one event starting at offset `s`: base + offset = clock time.

    Only when the video time came from the DVR clock: the clock is read again at the event start
    (files stitched from several recordings jump in time) and a disagreement larger than
    ``osd_event_tolerance`` means the event's own reading is the truth. Footage whose clock could
    not be read is not re-read per event (each failed read costs seconds)."""
    if not cfg.osd_verify_events or not tsrc.startswith("osd") or not ocr:
        return start_dt, tsrc
    f = read_frame_at(path, s + 0.1, 1280, (meta["width"], meta["height"]))
    hint = start_dt.date() if (start_dt and is_exact(tsrc)) else \
        (path_date_hint(path, cfg.input_dir) if cfg.use_path_date_hint else None)
    d = read_clock(f, ocr, cfg.osd_date_order, hint)
    if d is None or (hint and abs((d.date() - hint).days) > 1):
        return start_dt, tsrc
    zone = start_dt.tzinfo if start_dt else tz(cfg.timezone)
    clock = d.replace(tzinfo=zone) + dt.timedelta(seconds=0.5)
    if start_dt is not None and is_exact(tsrc):
        diff = (clock - (start_dt + dt.timedelta(seconds=s + 0.1))).total_seconds()
        if abs(diff) <= cfg.osd_event_tolerance:
            return start_dt, tsrc
        log.info("  clock at offset %.0fs reads %s (%.0fs from the file start time) – using the event's own clock",
                 s, d, diff)
    base = clock - dt.timedelta(seconds=s + 0.1)
    return base.replace(microsecond=0), f"osd({ocr.name},event)"

"""Cases: one investigation, several seized recording sources, one calibrated timeline.

A **case** (``case_id``, investigating officer, FIR number, police station, incident time, …)
holds any number of **sources** – one per seized DVR/NVR/pen drive folder (``ShopA_DVR/``,
``TrafficCam_North/``…). Each source is analysed by the ordinary pipeline into its own output
folder, so every existing view, report and evidence export works per source unchanged::

    <cases_root>/<case_id>/
        case.json                 case details + sources (the only file this module edits)
        audit_log.jsonl           hash-chained case audit log (who changed what, when)
        sources/<source_id>/      analysis output of that source (analysis.db, events/, …)
            ingest.json           acquisition record: SHA-256 of every seized file, playback copies
            playback/             lossless MP4 remuxes for the browser (cctv_analysis.ingest)
        enhancements/             Forensic Studio outputs + their filter logs
        exports/                  evidence bundles with the Section 63 certificate

**Clock calibration never edits recorded times.** DVR clocks are often minutes or hours wrong.
The officer enters the error as an offset (``-00:11:00`` = "this DVR runs 11 minutes fast")
for a source, or for one camera of it; every case-level view shows both the DVR time (what
the recording says) and the calibrated time (``DVR time + offset``) and sorts by the latter.
Changing an offset is audited and takes effect immediately, without re-analysis.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field, fields
from typing import Dict, List, Optional

from .audit import AuditLog

CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
DEVICE_KINDS = ("DVR", "NVR", "Computer / Storage Media", "Mobile", "Flash Drive", "CD/DVD", "Server", "Cloud",
                "Other")
STORAGE_MEDIA = ("HDD", "SSD", "Pen Drive", "SD Card", "CD/DVD", "Cloud export", "Other", "")
MAX_OFFSET = 7 * 24 * 3600          # a week: anything larger is a wrong date, not a clock error
_LOCK = threading.Lock()
PHOTO_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
MAX_REFERENCE_PHOTOS = 500
MAX_PHOTO_BYTES = 25 << 20


def person_name(text: str) -> str:
    """A reference person's name as a folder name: what the officer wrote, without characters a
    file system refuses (any script – Devanagari names stay as they are)."""
    n = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", str(text or "")).strip().strip(".")
    n = re.sub(r"\s+", " ", n)[:60].strip()
    if not n:
        raise ValueError(f"not a usable person name: {text!r}")
    return n


def reference_photos(folder: str) -> List[tuple]:
    """(person, photo path) of a folder of reference photos: one sub-folder per person
    (folder/<Name>/*.jpg) or photos directly in it (one person per photo, named by the file).
    ValueError when there is none, too many or a file is too large."""
    folder = os.path.abspath(os.path.expanduser(folder or ""))
    if not os.path.isdir(folder):
        raise ValueError(f"folder not found: {folder}")
    items = []
    for entry in sorted(os.listdir(folder)):
        p = os.path.join(folder, entry)
        if os.path.isdir(p):
            items += [(person_name(entry), os.path.join(p, f)) for f in sorted(os.listdir(p))
                      if f.lower().endswith(PHOTO_EXT) and os.path.isfile(os.path.join(p, f))]
        elif entry.lower().endswith(PHOTO_EXT):
            items.append((person_name(os.path.splitext(entry)[0]), p))
    if not items:
        raise ValueError(f"no photos (.jpg, .png) in {folder} or its sub-folders – one sub-folder per person")
    if len(items) > MAX_REFERENCE_PHOTOS:
        raise ValueError(f"at most {MAX_REFERENCE_PHOTOS} reference photos")
    big = [p for _, p in items if os.path.getsize(p) > MAX_PHOTO_BYTES]
    if big:
        raise ValueError(f"photo larger than 25 MB: {big[0]}")
    return items


def parse_offset(text) -> float:
    """Clock correction in seconds from what an officer types.

    Accepted: ``-00:11:00`` / ``+1:05:30`` (H:M:S), ``-11:00`` (M:S), ``-11m``, ``+2h``,
    ``90s``, ``-660`` (seconds), ``0``. The sign says which way to move the recording's times:
    a DVR 11 minutes fast needs ``-00:11:00``."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        v = float(text)
    else:
        s = str(text or "").strip().replace(" ", "")
        if not s:
            return 0.0
        sign = -1.0 if s.startswith("-") else 1.0
        s = s.lstrip("+-")
        m = re.fullmatch(r"(\d+(?:\.\d+)?)([hms])", s, re.I)
        if m:
            v = float(m[1]) * {"h": 3600, "m": 60, "s": 1}[m[2].lower()]
        elif re.fullmatch(r"\d+(?:\.\d+)?", s):
            v = float(s)
        else:
            parts = s.split(":")
            if not 2 <= len(parts) <= 3 or not all(re.fullmatch(r"\d{1,3}(?:\.\d+)?", p) for p in parts):
                raise ValueError(f"clock offset {text!r}: use -HH:MM:SS (for example -00:11:00 for a DVR "
                                 "running 11 minutes fast)")
            nums = [float(p) for p in parts]
            if len(nums) == 2:
                nums = [0.0] + nums
            h, mi, se = nums
            if mi >= 60 or se >= 60:
                raise ValueError(f"clock offset {text!r}: minutes and seconds must be below 60")
            v = h * 3600 + mi * 60 + se
        v *= sign
    if abs(v) > MAX_OFFSET:
        raise ValueError(f"clock offset {text!r} is more than 7 days – correct the date of the recording instead")
    return round(v, 3)


def format_offset(sec: float) -> str:
    sign = "-" if sec < 0 else "+"
    s = abs(float(sec))
    h, rem = divmod(int(round(s)), 3600)
    m, se = divmod(rem, 60)
    return f"{sign}{h:02d}:{m:02d}:{se:02d}"


def calibrate(dvr_time: Optional[str], offset: float) -> Optional[str]:
    """'YYYY-MM-DD HH:MM:SS' (DVR clock) shifted by the offset; None stays None."""
    if not dvr_time:
        return None
    try:
        t = dt.datetime.strptime(dvr_time[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return (t + dt.timedelta(seconds=offset)).strftime("%Y-%m-%d %H:%M:%S")


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:40]
    return s or "source"


@dataclass
class Device:
    """Source device particulars (they go into the Section 63 certificate)."""
    kind: str = "DVR"                 # one of DEVICE_KINDS (the certificate's tick boxes)
    make: str = ""
    model: str = ""
    serial: str = ""
    color: str = ""
    identifier: str = ""              # IMEI / UIN / UID / MAC / cloud ID, as applicable
    storage_medium: str = ""          # HDD / SSD / Pen Drive …
    operating_system: str = ""
    custodian: str = ""               # person in lawful control of the device (Part A declarant)
    control: List[str] = field(default_factory=lambda: ["Operated"])   # Owned / Maintained / Managed / Operated
    notes: str = ""


@dataclass
class Source:
    source_id: str
    label: str
    path: str                         # the seized folder (read only; never written to)
    clock_offset: float = 0.0         # seconds added to the DVR clock to get true time
    camera_offsets: Dict[str, float] = field(default_factory=dict)   # per-camera override (seconds)
    device: Device = field(default_factory=Device)
    location: Dict[str, float] = field(default_factory=dict)          # optional {"lat":…, "lon":…} or {"x":…, "y":…}
    declarant: Dict[str, str] = field(default_factory=dict)           # Part A of this device's certificate
    added_at: str = ""

    def offset_for(self, camera: Optional[str]) -> float:
        if camera and camera in self.camera_offsets:
            return float(self.camera_offsets[camera])
        return float(self.clock_offset)

    def recording(self, rel_path: str) -> str:
        """The seized recording `rel_path` (relative to the source folder) where the folder is now –
        never the absolute path stored at analysis time, which a new drive letter makes stale."""
        return os.path.join(self.path, *[x for x in re.split(r"[\\/]+", rel_path or "") if x])


CASE_FIELDS = ("investigating_officer", "fir_number", "police_station", "under_sections", "district", "state",
               "incident_time", "notes")


@dataclass
class Case:
    case_id: str
    investigating_officer: str = ""
    fir_number: str = ""
    police_station: str = ""
    under_sections: str = ""          # BNS sections, as written in the FIR
    district: str = ""
    state: str = ""
    incident_time: str = ""           # 'YYYY-MM-DD HH:MM' (local)
    notes: str = ""
    created_at: str = ""
    expert: Dict[str, str] = field(default_factory=dict)              # Part B of every certificate
    sources: List[Source] = field(default_factory=list)
    root: str = field(default="", repr=False)

    # ------------------------------------------------------------- paths
    @property
    def path(self) -> str:
        return os.path.join(self.root, "case.json")

    def source_dir(self, sid: str) -> str:
        return os.path.join(self.root, "sources", sid)

    @property
    def audit(self) -> AuditLog:
        return AuditLog(os.path.join(self.root, "audit_log.jsonl"))

    def source(self, sid: str) -> Source:
        for s in self.sources:
            if s.source_id == sid:
                return s
        raise KeyError(f"no source {sid!r} in case {self.case_id}")

    # ------------------------------------------------------------- persistence
    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("root")
        return d

    def save(self) -> None:
        os.makedirs(self.root, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=1)
        os.replace(tmp, self.path)

    @classmethod
    def load(cls, root: str) -> "Case":
        with open(os.path.join(root, "case.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        sources = []
        for s in d.pop("sources", []):
            dev = Device(**{k: v for k, v in (s.pop("device", {}) or {}).items() if k in {f.name for f in fields(Device)}})
            sources.append(Source(**{k: v for k, v in s.items() if k in {f.name for f in fields(Source)}}, device=dev))
        known = {f.name for f in fields(cls)} - {"sources", "root"}
        return cls(**{k: v for k, v in d.items() if k in known}, sources=sources, root=root)

    # ------------------------------------------------------------- edits (all audited)
    def update(self, user: str = "", **values) -> None:
        bad = set(values) - set(CASE_FIELDS)
        if bad:
            raise ValueError(f"unknown case field(s): {sorted(bad)}")
        for k, v in values.items():
            if not isinstance(v, str) or len(v) > 2000:
                raise ValueError(f"{k} must be text (at most 2000 characters)")
        if values.get("incident_time"):
            _check_time(values["incident_time"])
        changed = {k: v for k, v in values.items() if getattr(self, k) != v}
        if not changed:
            return
        for k, v in changed.items():
            setattr(self, k, v)
        with _LOCK:
            self.save()
        self.audit.append("case_updated", case_id=self.case_id, fields=changed, by=user)

    def add_source(self, path: str, label: str = "", clock_offset="0", user: str = "", device: Optional[dict] = None,
                   source_id: str = "") -> Source:
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(path):
            raise ValueError(f"folder not found: {path}")
        root_r = os.path.realpath(self.root)
        if os.path.commonpath([root_r, os.path.realpath(path)]) == root_r:
            raise ValueError("a source folder must not be inside the case folder")
        if any(os.path.realpath(s.path) == os.path.realpath(path) for s in self.sources):
            raise ValueError(f"this folder is already a source of the case: {path}")
        label = (label or os.path.basename(path.rstrip("\\/")) or "source").strip()[:80]
        sid = source_id or slug(label)
        base, n = sid, 2
        while any(s.source_id == sid for s in self.sources):
            sid, n = f"{base}-{n}", n + 1
        if not SOURCE_ID.fullmatch(sid):
            raise ValueError(f"bad source id {sid!r}")
        src = Source(source_id=sid, label=label, path=path, clock_offset=parse_offset(clock_offset),
                     device=_device(device or {}), added_at=_now())
        self.sources.append(src)
        with _LOCK:
            self.save()
        os.makedirs(self.source_dir(sid), exist_ok=True)
        self.audit.append("source_added", case_id=self.case_id, source=sid, path=path, label=label,
                          clock_offset=format_offset(src.clock_offset), by=user)
        return src

    def set_expert(self, values: dict, user: str = "") -> None:
        """Part B (the expert) of the case's Section 63 certificates."""
        d = parse_declarant(values)
        if d == self.expert:
            return
        self.expert = d
        with _LOCK:
            self.save()
        self.audit.append("expert_updated", case_id=self.case_id, expert=d, by=user)

    def update_source(self, sid: str, user: str = "", label: Optional[str] = None, clock_offset=None,
                      camera_offsets: Optional[dict] = None, device: Optional[dict] = None,
                      location: Optional[dict] = None, declarant: Optional[dict] = None) -> Source:
        s = self.source(sid)
        before = {"label": s.label, "clock_offset": format_offset(s.clock_offset),
                  "camera_offsets": {k: format_offset(v) for k, v in s.camera_offsets.items()}}
        if label is not None:
            if not isinstance(label, str) or not label.strip() or len(label) > 80:
                raise ValueError("label must be 1-80 characters")
            s.label = label.strip()
        if clock_offset is not None:
            s.clock_offset = parse_offset(clock_offset)
        if camera_offsets is not None:
            if not isinstance(camera_offsets, dict) or len(camera_offsets) > 200:
                raise ValueError("camera_offsets must be an object {camera: offset}")
            s.camera_offsets = {str(k)[:80]: parse_offset(v) for k, v in camera_offsets.items() if str(v).strip()}
        if device is not None:
            s.device = _device(device)
        if location is not None:
            s.location = _location(location)
        if declarant is not None:
            s.declarant = parse_declarant(declarant)
        with _LOCK:
            self.save()
        after = {"label": s.label, "clock_offset": format_offset(s.clock_offset),
                 "camera_offsets": {k: format_offset(v) for k, v in s.camera_offsets.items()}}
        self.audit.append("source_updated", case_id=self.case_id, source=sid, before=before, after=after,
                          device=asdict(s.device) if device is not None else None,
                          location=s.location if location is not None else None,
                          declarant=s.declarant if declarant is not None else None, by=user)
        return s

    def relink_source(self, sid: str, path: str, user: str = "", full_hash: bool = True,
                      progress=None) -> dict:
        """Point a source at the new location of its seized folder (the pen drive is now F: instead
        of E:, the folder was copied to the forensic workstation). Every recording of the
        acquisition record must be there with the same size and – with `full_hash` – the same
        SHA-256; otherwise nothing changes and the differences are reported."""
        from .evidence import sha256_file
        from .ingest import load_ingest
        s = self.source(sid)
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(path):
            raise ValueError(f"folder not found: {path}")
        root_r = os.path.realpath(self.root)
        if os.path.commonpath([root_r, os.path.realpath(path)]) == root_r:
            raise ValueError("a source folder must not be inside the case folder")
        rec = load_ingest(self.source_dir(sid))
        if not rec or not rec.get("files"):
            raise ValueError("this source was never ingested: there is no acquisition record to compare the folder "
                             "with – run the case first")
        problems: List[str] = []
        files = rec["files"]
        for i, f in enumerate(files, 1):
            p = os.path.join(path, *f["rel"].split("/"))
            if progress:
                progress({"stage": "verifying", "index": i, "total": len(files), "path": f["rel"],
                          "percent": round(100 * (i - 1) / len(files), 1)})
            if not os.path.isfile(p):
                problems.append(f"{f['rel']}: missing")
            elif os.path.getsize(p) != f["bytes"]:
                problems.append(f"{f['rel']}: size differs")
            elif full_hash and sha256_file(p) != f["sha256"]:
                problems.append(f"{f['rel']}: SHA-256 differs from the acquisition hash")
        if problems:
            raise ValueError(f"this folder does not hold the acquired recordings of {s.label} "
                             f"({len(problems)} problem(s)): " + "; ".join(problems[:5]))
        old = s.path
        s.path = path
        with _LOCK:
            self.save()
        verified = "sha256" if full_hash else "size"
        self.audit.append("source_relinked", case_id=self.case_id, source=sid, old_path=old, new_path=path,
                          files=len(files), verified=verified, by=user)
        return {"source_id": sid, "path": path, "files": len(files), "verified": verified}

    def remove_source(self, sid: str, user: str = "") -> None:
        s = self.source(sid)
        self.sources = [x for x in self.sources if x.source_id != sid]
        with _LOCK:
            self.save()
        # the analysis output stays on disk (it may already be cited); it just leaves the case
        self.audit.append("source_removed", case_id=self.case_id, source=sid, path=s.path, by=user)

    def calibrated(self, sid: str, camera: Optional[str], dvr_time: Optional[str]) -> Optional[str]:
        return calibrate(dvr_time, self.source(sid).offset_for(camera))

    # ------------------------------------------------------------- reference persons (face search)
    @property
    def references_dir(self) -> str:
        return os.path.join(self.root, "references")

    def references(self) -> List[dict]:
        """The reference persons: [{"name", "photos": [file, …]}] (``references/<Name>/``)."""
        d = self.references_dir
        out = []
        for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            p = os.path.join(d, name)
            photos = sorted(f for f in os.listdir(p) if f.lower().endswith(PHOTO_EXT)) if os.path.isdir(p) else []
            if photos:
                out.append({"name": name, "photos": photos})
        return out

    def add_reference_photos(self, folder: str, user: str = "") -> List[dict]:
        """Copy reference photos (``reference_photos``) into ``references/<Name>/``. Every copy's
        SHA-256 and where it came from go into the case audit log: which pictures a face search
        looked for is part of the record. A photo already there (same content) is not copied twice."""
        import hashlib
        import shutil
        added = []
        for name, src in reference_photos(folder):
            with open(src, "rb") as fh:
                data = fh.read()
            sha = hashlib.sha256(data).hexdigest()
            dst_dir = os.path.join(self.references_dir, name)
            os.makedirs(dst_dir, exist_ok=True)
            base, ext = os.path.splitext(os.path.basename(src))
            dst, n = os.path.join(dst_dir, base + ext.lower()), 2
            while os.path.exists(dst):
                with open(dst, "rb") as fh:
                    if hashlib.sha256(fh.read()).hexdigest() == sha:
                        break
                dst, n = os.path.join(dst_dir, f"{base}_{n}{ext.lower()}"), n + 1
            else:
                shutil.copyfile(src, dst)
                added.append({"name": name, "photo": os.path.basename(dst), "sha256": sha, "from": src})
        self.audit.append("reference_photos_added", case_id=self.case_id, folder=os.path.abspath(folder),
                          photos=added, by=user)
        return added

    def remove_reference(self, name: str, user: str = "") -> None:
        import shutil
        if name not in {r["name"] for r in self.references()}:
            raise KeyError(f"no reference person {name!r}")
        shutil.rmtree(os.path.join(self.references_dir, name))
        self.audit.append("reference_person_removed", case_id=self.case_id, name=name, by=user)


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _check_time(v: str) -> None:
    for f in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt.datetime.strptime(v.strip(), f)
            return
        except ValueError:
            pass
    raise ValueError(f"incident_time {v!r}: use YYYY-MM-DD HH:MM")


def _device(d: dict) -> Device:
    if not isinstance(d, dict):
        raise ValueError("device must be an object")
    names = {f.name for f in fields(Device)}
    bad = set(d) - names
    if bad:
        raise ValueError(f"unknown device field(s): {sorted(bad)}")
    dev = Device(**{k: v for k, v in d.items()})
    if dev.kind not in DEVICE_KINDS:
        raise ValueError(f"device kind must be one of {list(DEVICE_KINDS)}")
    if dev.storage_medium not in STORAGE_MEDIA:
        raise ValueError(f"storage medium must be one of {[x for x in STORAGE_MEDIA if x]}")
    if not isinstance(dev.control, list) or not set(dev.control) <= {"Owned", "Maintained", "Managed", "Operated"}:
        raise ValueError("control must list Owned / Maintained / Managed / Operated")
    for k in names - {"control"}:
        v = getattr(dev, k)
        if not isinstance(v, str) or len(v) > 200:
            raise ValueError(f"device {k} must be text (at most 200 characters)")
    return dev


DECLARANT_FIELDS = ("name", "relation_name", "address", "designation", "place")


def parse_declarant(d: Optional[dict]) -> Dict[str, str]:
    """A certificate declarant (Part A: the person in charge of the device; Part B: the expert).
    Only what the officer typed – the tool never fills in a person's particulars."""
    if d is None:
        return {}
    if not isinstance(d, dict):
        raise ValueError("declarant must be an object")
    bad = set(d) - set(DECLARANT_FIELDS)
    if bad:
        raise ValueError(f"unknown declarant field(s): {sorted(bad)}")
    out = {}
    for k in DECLARANT_FIELDS:
        v = d.get(k, "")
        if v is None:
            v = ""
        if not isinstance(v, str) or len(v) > 200:
            raise ValueError(f"declarant {k} must be text (at most 200 characters)")
        if v.strip():
            out[k] = v.strip()
    return out


def _location(d: dict) -> Dict[str, float]:
    if not isinstance(d, dict):
        raise ValueError("location must be an object")
    out = {}
    for k in ("lat", "lon", "x", "y"):
        if d.get(k) not in (None, ""):
            try:
                out[k] = float(d[k])
            except (TypeError, ValueError):
                raise ValueError(f"location {k} must be a number")
    if "lat" in out and not -90 <= out["lat"] <= 90 or "lon" in out and not -180 <= out["lon"] <= 180:
        raise ValueError("latitude/longitude out of range")
    return out


class CaseStore:
    """All cases under one folder (``<data>/cases`` by default)."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    def list(self) -> List[dict]:
        out = []
        for name in sorted(os.listdir(self.root)):
            p = os.path.join(self.root, name, "case.json")
            if os.path.isfile(p):
                try:
                    c = Case.load(os.path.join(self.root, name))
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
                out.append({"case_id": c.case_id, "fir_number": c.fir_number, "police_station": c.police_station,
                            "investigating_officer": c.investigating_officer, "sources": len(c.sources),
                            "created_at": c.created_at})
        return out

    def create(self, case_id: str, user: str = "", **values) -> Case:
        if not CASE_ID.fullmatch(case_id or ""):
            raise ValueError("case_id: letters, digits, '-' and '_' only (for example FIR-112-2026)")
        root = os.path.join(self.root, case_id)
        if os.path.exists(os.path.join(root, "case.json")):
            raise FileExistsError(f"case {case_id} already exists")
        c = Case(case_id=case_id, created_at=_now(), root=root)
        os.makedirs(root, exist_ok=True)
        c.save()
        c.audit.append("case_created", case_id=case_id, by=user)
        if values:
            c.update(user=user, **values)
        return c

    def get(self, case_id: str) -> Case:
        if not CASE_ID.fullmatch(case_id or ""):
            raise KeyError("bad case id")
        root = os.path.join(self.root, case_id)
        if not os.path.isfile(os.path.join(root, "case.json")):
            raise KeyError(f"no case {case_id}")
        return Case.load(root)


__all__ = ["Case", "CaseStore", "Device", "Source", "calibrate", "format_offset", "parse_declarant", "parse_offset"]

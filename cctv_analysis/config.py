"""All tunable settings live here. Every field has a sensible default, so a run
only needs `input_dir` and `output_dir`."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields
from typing import Dict, List, Optional

VIDEO_EXTENSIONS = [
    ".mp4", ".avi", ".mkv", ".mov", ".m4v", ".dav", ".h264", ".264", ".h265",
    ".265", ".hevc", ".ts", ".mts", ".m2ts", ".flv", ".asf", ".wmv", ".3gp",
    ".mpg", ".mpeg", ".webm", ".vob", ".ps", ".mpeg4",
]

# Zero-shot prompts used by SigLIP-2 to tag keyframes. Prompts in the
# "suspicious" group raise the suspicion score.
DEFAULT_SUSPICIOUS_PROMPTS = [
    "a person hiding a product inside their clothes or pocket",
    "a person putting merchandise into a bag without paying",
    "a person reaching into the cash register or cash drawer",
    "a person taking money from a counter",
    "two people fighting or pushing each other",
    "a person threatening someone with a knife or gun",
    "a person wearing a mask or helmet covering their face indoors",
    "a person breaking or damaging property",
    "a person covering or tampering with the security camera",
    "a person climbing over the counter",
    "a person lying on the floor",
    "a person looking around nervously while touching products",
]
DEFAULT_NORMAL_PROMPTS = [
    "an empty shop with no people",
    "a customer browsing products on a shelf",
    "a customer paying at the counter",
    "a shopkeeper standing behind the counter",
    "people talking calmly in a shop",
    "a person cleaning the floor",
    "a delivery person carrying boxes",
    "a street outside a shop with vehicles passing",
]

DEFAULT_VLM_PROMPT = (
    "You are a security analyst reviewing CCTV footage from a retail shop. "
    "The image is a grid of frames from one motion event, in time order "
    "(left to right, top to bottom). Describe briefly what the people are "
    "doing. Then decide if anything looks suspicious (theft, concealing items, "
    "taking cash, violence, vandalism, tampering with the camera, entering a "
    "restricted area). Answer ONLY with JSON of the form "
    '{"description": "...", "suspicious": true/false, "category": "none|theft|'
    'cash_handling|violence|vandalism|tampering|intrusion|other", '
    '"confidence": 0.0-1.0, "reason": "..."}'
)


# Speed presets (field overrides). Used by the notebook, the CLI (--preset) and the desktop app.
PRESETS = {
    "fast":     dict(motion_fps=3, detect_fps=3, detector_model="yolo26n.pt", detector_fallbacks=["yolo11n.pt"],
                     use_pose=False, det_imgsz=640, detect_width=960, semantic_model="google/siglip2-base-patch16-224"),
    "balanced": dict(motion_fps=4, detect_fps=5, detector_model="yolo26s.pt", detector_fallbacks=["yolo11s.pt"],
                     use_pose=True, det_imgsz=960, detect_width=1280, semantic_model="google/siglip2-base-patch16-224"),
    "accurate": dict(motion_fps=5, detect_fps=8, detector_model="yolo26m.pt", detector_fallbacks=["yolo11m.pt"],
                     use_pose=True, pose_model="yolo26s-pose.pt", det_imgsz=1280, detect_width=1920,
                     semantic_model="google/siglip2-so400m-patch14-384", vlm_model="Qwen/Qwen3-VL-4B-Instruct"),
}


@dataclass
class Config:
    # ---------------------------------------------------------------- paths
    input_dir: str = "/content/drive/MyDrive/CCTV"
    output_dir: str = "/content/drive/MyDrive/CCTV_Analysis"
    work_dir: str = "/content/cctv_work"          # fast local scratch (DB, temp)
    recursive: bool = True
    extensions: List[str] = field(default_factory=lambda: list(VIDEO_EXTENSIONS))
    include_glob: str = ""                       # e.g. "*ch01*" to limit files
    exclude_glob: str = ""
    max_videos: int = 0                          # 0 = no limit
    only_newer_than_days: float = 0              # 0 = all files
    reprocess: bool = False                      # ignore the "already done" cache
    max_runtime_minutes: float = 0               # stop cleanly (and still build reports) after this; 0 = no limit
    known_faces_dir: str = ""                    # folder/<Name>/*.jpg (staff, owner)
    # Only analyse sub-folders whose name matches one of these (any depth), e.g.
    # ["6-oct", "12oct"]; date-like keywords also match "2026-10-06", "06Oct26", "Oct 6"...
    # Empty = every video under input_dir.
    folder_keywords: List[str] = field(default_factory=list)
    dedupe: bool = True                          # skip identical copies (content hash), even renamed/moved
    model_cache_dir: str = "/content/drive/MyDrive/CCTV_models"   # models saved here once, reused next runs

    # ------------------------------------------------------------ time info
    timezone: str = "Asia/Kolkata"
    # Order in which the recording start time is resolved (see timeline.py):
    # filename | osd (burnt-in DVR clock read by OCR) | metadata (container creation_time) | mtime (approx)
    time_sources: List[str] = field(
        default_factory=lambda: ["filename", "osd", "metadata", "mtime"])
    ocr_engine: str = "auto"                     # auto | rapidocr | tesseract | vlm | none  (reads the DVR clock)
    osd_date_order: str = "auto"                 # auto | ymd | dmy | mdy  (how the DVR prints dates)
    osd_samples: int = 3                         # clock readings per video that must agree
    osd_verify_events: bool = True               # re-read the clock at every event start (fixes gaps inside a file)
    osd_event_tolerance: float = 5.0             # seconds; a larger disagreement uses the event's own clock reading
    use_path_date_hint: bool = True              # a date in the folder/file name ('31Oct26') validates/fixes times
    # Extra regexes with named groups Y m d H M S (optional) tried before built-ins.
    filename_time_patterns: List[str] = field(default_factory=list)
    # Manual overrides: {"file name or relative path": "2026-10-11 09:30:00"}
    time_overrides: Dict[str, str] = field(default_factory=dict)
    camera_name_source: str = "auto"             # auto | folder | filename | none

    # ------------------------------------------------------------ shop rules
    open_hour: str = ""                          # "" = open 24h (no after-hours flag); e.g. "09:00"
    close_hour: str = ""
    closed_weekdays: List[int] = field(default_factory=list)  # 0=Mon .. 6=Sun
    # Restricted zones per camera, normalised polygon coords (0..1).
    # {"*": {"cash_counter": [[0.6,0.5],[1,0.5],[1,1],[0.6,1]]}, "ch02": {...}}
    zones: Dict[str, Dict[str, List[List[float]]]] = field(default_factory=dict)
    # Cameras where only known (enrolled) people are allowed, e.g. ["warehouse"] or ["*"].
    # Anyone else (unknown face or no face visible) is flagged "unauthorized_person".
    authorized_only_cameras: List[str] = field(default_factory=list)
    # Areas to ignore for motion (timestamp overlay, swaying plants, TV screens).
    ignore_regions: Dict[str, List[List[List[float]]]] = field(default_factory=dict)
    auto_mask_osd: bool = True                   # auto-detect burnt-in clock text

    # ---------------------------------------------------------- motion pass
    motion_fps: float = 4.0
    motion_width: int = 384
    motion_sensitivity: float = 0.5              # 0 (least) .. 1 (most sensitive)
    min_motion_area: float = 0.0015              # fraction of frame
    global_change_ratio: float = 0.65            # > this = lighting change, ignore
    event_pre_roll: float = 2.0                  # seconds added before motion
    event_post_roll: float = 3.0
    event_merge_gap: float = 6.0                 # merge events closer than this
    event_min_duration: float = 1.0
    event_max_duration: float = 600.0            # split very long events
    detect_tamper: bool = True

    # -------------------------------------------------------- detection pass
    device: str = "auto"                         # auto | cuda | cpu
    hw_decode: str = "auto"                      # auto | cuda | none  (NVDEC video decoding on Colab GPUs)
    detector_model: str = "yolo26s.pt"           # falls back to yolo11s.pt
    detector_fallbacks: List[str] = field(default_factory=lambda: ["yolo11s.pt", "yolov8s.pt"])
    pose_model: str = "yolo26n-pose.pt"
    use_pose: bool = True
    tracker: str = "bytetrack.yaml"
    detect_fps: float = 6.0
    detect_width: int = 1280
    det_conf: float = 0.30
    det_imgsz: int = 960
    # COCO classes that matter in a shop (None = everything).
    keep_classes: Optional[List[str]] = field(default_factory=lambda: [
        "person", "bicycle", "car", "motorcycle", "bus", "truck", "dog", "cat",
        "backpack", "umbrella", "handbag", "suitcase", "bottle", "cup", "knife",
        "scissors", "baseball bat", "cell phone", "laptop", "book", "chair",
    ])
    require_person_for_event: bool = False       # drop motion with no person/vehicle/animal

    # ----------------------------------------------------------------- faces
    use_faces: bool = True                       # detect + save face crops
    face_identities: bool = True                 # group faces into persistent identities (unique faces, P0001…)
    face_watchlist_dir: str = ""                 # folder/<Name>/*.jpg – people who raise a "watchlist_face" alert
    face_score_threshold: float = 0.75
    face_min_size: int = 20                      # px, in detection-resolution frame
    face_match_threshold: float = 0.42           # SFace cosine similarity
    known_face_threshold: float = 0.45
    faces_per_track: int = 3
    face_min_quality: float = 0.30               # below this a face is kept as evidence but not used for identity

    # --------------------------------------------------- number plates (ANPR)
    use_plates: bool = False                     # read vehicle number plates (slower; off by default)
    plate_enhance: bool = False                  # super-resolve / sharpen / multi-frame vote hard plates (slowest)
    plate_vlm_read: bool = False                 # ask the VLM when OCR cannot read a plate
    plate_detector: str = "auto"                 # auto | yolo-plate (open-image-models ONNX) | classic (OpenCV)
    plate_ocr: str = "auto"                      # auto | fast-plate-ocr | generic (ocr_engine)
    plate_region: str = "IN"                     # IN (Indian formats, incl. BH series) | any
    plate_min_vehicle_px: int = 48               # skip vehicles smaller than this (box height, detection frame)
    plate_reads_per_track: int = 6               # plate crops read per vehicle track
    plate_watchlist: List[str] = field(default_factory=list)   # plates that raise a "watchlist_plate" alert
    plate_watchlist_max_edits: int = 1           # tolerated OCR differences when matching the watchlist

    # ------------------------------------------------------ footfall counting
    # Virtual counting lines per camera, normalised coords: {"*": {"entrance": [[0.1,0.6],[0.9,0.6]]}}
    # People crossing a line are counted in (left→right of the line direction) / out.
    count_lines: Dict[str, Dict[str, List[List[float]]]] = field(default_factory=dict)

    # ------------------------------------------------- suspicious heuristics
    loiter_seconds: float = 90.0
    crowd_size: int = 4
    running_speed: float = 1.6                   # body-heights per second
    weapon_classes: List[str] = field(default_factory=lambda: ["knife", "scissors", "baseball bat"])
    hidden_face_seconds: float = 8.0
    suspicion_alert_threshold: float = 40.0      # score >= this = "REVIEW"

    # ------------------------------------------------------ semantic models
    use_semantic: bool = True
    semantic_model: str = "google/siglip2-base-patch16-224"
    suspicious_prompts: List[str] = field(default_factory=lambda: list(DEFAULT_SUSPICIOUS_PROMPTS))
    normal_prompts: List[str] = field(default_factory=lambda: list(DEFAULT_NORMAL_PROMPTS))
    semantic_alert_score: float = 0.10           # SigLIP sigmoid; also needs lift >= 0.6

    use_vlm: bool = True                         # AI text description of events (slow on CPU)
    vlm_provider: str = "transformers"           # transformers (local HF model) | openai | nvidia
    vlm_model: str = "Qwen/Qwen3-VL-2B-Instruct" # HF repo id, or the model name the server knows (e.g. "qwen3-vl:2b")
    vlm_base_url: str = ""                       # openai provider: e.g. http://127.0.0.1:11434/v1 (Ollama), :1234/v1 (LM Studio)
    vlm_api_key_env: str = "CCTV_VLM_API_KEY"    # name of the environment variable holding the API key (never stored)
    # more providers tried in order when the one above fails: [{"provider", "model", "base_url", "api_key_env"}]
    vlm_fallbacks: List[dict] = field(default_factory=list)
    vlm_timeout: float = 120.0                   # seconds per request (openai provider)
    vlm_scope: str = "flagged"                   # flagged | people | all
    vlm_max_events: int = 300
    vlm_prompt: str = DEFAULT_VLM_PROMPT
    vlm_max_new_tokens: int = 180

    # --------------------------------------------------------------- outputs
    snapshots_per_event: int = 6
    save_raw_snapshots: bool = True
    snapshot_jpeg_quality: int = 88
    save_clips: bool = True
    clip_scope: str = "people"                   # all | people | flagged
    clip_max_seconds: float = 180.0
    clip_width: int = 960
    clip_overlay: bool = False                   # burn an event label into clips (off = untouched picture)
    excel_thumbnails: bool = True
    excel_max_thumbnails: int = 3000
    make_html: bool = True
    master_report: bool = True                   # all-time CCTV_Report.xlsx (in addition to weekly files)
    weekly_reports: bool = True                  # reports/<year>/11-17-Oct-2026-W41.xlsx
    week_start: str = "sunday"                   # sunday (US week numbers, W41 = 11-17 Oct 2026) | monday (ISO)
    make_highlight_reel: bool = True             # one reel per day with every movement of all cameras
    reel_scope: str = "all"                      # all | people | flagged
    reel_max_event_seconds: float = 0            # 0 = whole event; else cap each event's part of the reel
    reel_overlay: bool = False                   # burn clock/camera text into reels (off = original picture only)
    reel_skip_blank: bool = True                 # leave black/blank, nothing-detected motion out of reels
    reel_width: int = 1280
    reel_fps: int = 15
    drive_links: bool = True                     # resolve Google Drive file links in Colab

    def validate(self) -> List[str]:
        """Human-readable problems with this configuration (empty list = valid)."""
        from .ocr import ENGINES
        from .timeline import TIME_SOURCES
        p = []
        bad = [s for s in self.time_sources if s not in TIME_SOURCES]
        if bad:
            p.append(f"time_sources: unknown {bad}; valid: {list(TIME_SOURCES)}")
        choices = {"ocr_engine": ENGINES, "osd_date_order": ("auto", "ymd", "dmy", "mdy"),
                   "vlm_scope": ("flagged", "people", "all"),
                   "clip_scope": ("all", "people", "flagged"), "reel_scope": ("all", "people", "flagged"),
                   "plate_detector": ("auto", "yolo-plate", "classic"),
                   "plate_ocr": ("auto", "fast-plate-ocr", "generic"), "plate_region": ("IN", "any"),
                   "week_start": ("sunday", "monday"), "camera_name_source": ("auto", "folder", "filename", "none"),
                   "hw_decode": ("auto", "cuda", "none")}
        for k, ok in choices.items():
            if getattr(self, k) not in ok:
                p.append(f"{k}: {getattr(self, k)!r} is not one of {list(ok)}")
        if self.use_vlm:
            from .vlm import check_spec, provider_specs
            for i, spec in enumerate(provider_specs(self)):
                p += [f"{'vlm' if i == 0 else f'vlm_fallbacks[{i - 1}]'}: {x}" for x in check_spec(spec)]
        for k in ("open_hour", "close_hour"):
            v = (getattr(self, k) or "").strip()
            if v and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", v):
                p.append(f"{k}: {v!r} must look like 09:00")
        if bool((self.open_hour or "").strip()) != bool((self.close_hour or "").strip()):
            p.append("open_hour/close_hour: set both or neither")
        for k in ("motion_fps", "detect_fps", "reel_fps"):
            if not getattr(self, k) or getattr(self, k) <= 0:
                p.append(f"{k}: must be > 0")
        if not 0 <= self.motion_sensitivity <= 1:
            p.append("motion_sensitivity: must be between 0 and 1")
        return p

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        names = {f.name for f in fields(cls)}
        unknown = set(d) - names
        if unknown:
            raise ValueError(f"Unknown config keys: {sorted(unknown)}")
        return cls(**d)

    @classmethod
    def from_json(cls, path: str) -> "Config":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2)

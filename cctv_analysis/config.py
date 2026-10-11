"""All tunable settings live here. Every field has a sensible default, so a run
only needs `input_dir` and `output_dir`."""
from __future__ import annotations

import json
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
    known_faces_dir: str = ""                    # folder/<Name>/*.jpg (staff, owner)

    # ------------------------------------------------------------ time info
    timezone: str = "Asia/Kolkata"
    # Order in which the recording start time is resolved.
    time_sources: List[str] = field(
        default_factory=lambda: ["filename", "metadata", "vlm_osd", "mtime"])
    # Extra regexes with named groups Y m d H M S (optional) tried before built-ins.
    filename_time_patterns: List[str] = field(default_factory=list)
    # Manual overrides: {"file name or relative path": "2026-10-11 09:30:00"}
    time_overrides: Dict[str, str] = field(default_factory=dict)
    camera_name_source: str = "auto"             # auto | folder | filename | none

    # ------------------------------------------------------------ shop rules
    open_hour: str = "09:00"                     # activity outside -> after-hours
    close_hour: str = "21:00"
    closed_weekdays: List[int] = field(default_factory=list)  # 0=Mon .. 6=Sun
    # Restricted zones per camera, normalised polygon coords (0..1).
    # {"*": {"cash_counter": [[0.6,0.5],[1,0.5],[1,1],[0.6,1]]}, "ch02": {...}}
    zones: Dict[str, Dict[str, List[List[float]]]] = field(default_factory=dict)
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
    use_faces: bool = True
    face_score_threshold: float = 0.75
    face_min_size: int = 24                      # px, in detection-resolution frame
    face_match_threshold: float = 0.42           # SFace cosine similarity
    known_face_threshold: float = 0.45
    faces_per_track: int = 3
    face_min_quality: float = 0.30               # below this a face is kept as evidence but not used for identity

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

    use_vlm: bool = True
    vlm_model: str = "Qwen/Qwen3-VL-2B-Instruct"
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
    excel_thumbnails: bool = True
    excel_max_thumbnails: int = 3000
    make_html: bool = True
    make_highlight_reel: bool = True
    highlight_max_events: int = 40
    drive_links: bool = True                     # resolve Google Drive file links in Colab

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

"""Generates cctv-analysis.ipynb from the package sources (single source of truth)."""
import glob
import os

import nbformat as nbf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
nb = nbf.v4.new_notebook()
nb.metadata = {"accelerator": "GPU", "colab": {"provenance": [], "gpuType": "T4", "toc_visible": True},
               "kernelspec": {"name": "python3", "display_name": "Python 3"},
               "language_info": {"name": "python"}}
C = []
md = lambda s: C.append(nbf.v4.new_markdown_cell(s.strip("\n")))
code = lambda s, hide=False: C.append(nbf.v4.new_code_cell(s.strip("\n"), metadata={"cellView": "form"} if hide else {}))

md("""
# 🎥 CCTV Footage Analysis — find what matters without watching hours of video

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/OfficialAshish/home-camera-analysis/blob/main/cctv-analysis.ipynb)

This notebook scans every recording in a Google Drive folder and produces a **reviewable, cited index of everything that happened**:

| What you get | Where |
|---|---|
| Every **motion event**: start/end clock time, the video file, and offset inside the video (HH:MM:SS) | `CCTV_Report.xlsx` → *Events* |
| **Snapshots** (annotated + raw), a contact sheet and a short **clip** for each event | `events/<camera>/<date>/<event-id>/` |
| **People & objects** tracked (YOLO26 + ByteTrack): confidence, how long they stayed, speed, restricted zones | *People & Objects* |
| **Faces**: detected, quality-scored, cropped and **grouped into identities across days** (P0001, P0002 …); known staff can be labelled | *Faces*, *Face Identities* |
| **Suspicion score 0–100** with plain-English reasons: after-hours activity, camera tampering, loitering, running, crowding, hidden face, hands up, restricted zone, weapon-like objects, AI scene match | *Events* (sortable) |
| **AI description** of each flagged event (Qwen3-VL vision-language model) | *Events* → *AI description* |
| Activity **heatmaps**, timeline charts, an interactive **HTML dashboard** and a **highlight reel** of the top events | output folder |
| **Search**: free text (*"man in a helmet"*) or **by photo** (find every visit of a person) | cells at the end |

**How to use:** `Runtime → Change runtime type → T4 GPU`, check the settings in **Step 3**, then `Runtime → Run all`.
Re-running is safe and incremental: finished videos are skipped and reports always cover everything analysed so far.
""")

md("## Step 1 · GPU check & Google Drive")
code("""
#@title Check GPU and mount Google Drive
import subprocess, os
print(subprocess.run("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader", shell=True,
                     capture_output=True, text=True).stdout or "⚠️ No GPU – use Runtime › Change runtime type › T4 GPU (CPU works, but slowly)")
try:
    from google.colab import drive
    drive.mount('/content/drive')
except ImportError:
    print("Not running in Colab – using local paths.")
""", hide=True)

md("## Step 2 · Install dependencies (~1–2 min)")
code("""
#@title Install packages
!pip -q install -U "ultralytics>=8.3.200" lap xlsxwriter "transformers>=4.57" accelerate sentencepiece protobuf
!apt-get -qq install -y ffmpeg > /dev/null 2>&1 || true
import importlib, ultralytics, transformers, torch, cv2
print("ultralytics", ultralytics.__version__, "| transformers", transformers.__version__,
      "| torch", torch.__version__, "| opencv", cv2.__version__, "| CUDA", torch.cuda.is_available())
""", hide=True)

md("""
### Analysis code
The next cells write the `cctv_analysis` package to `/content` (same code as the GitHub repo, so the notebook is fully self-contained).
You don't need to read or edit them — collapse the section and move on.
""")
order = ["__init__", "config", "video_io", "motion", "detector", "faces", "semantic", "vlm", "rules",
         "annotate", "storage", "report", "pipeline", "helpers", "__main__"]
for name in order:
    src = open(os.path.join(ROOT, "cctv_analysis", name + ".py"), encoding="utf-8").read()
    code(f"%%writefile /content/cctv_analysis/{name}.py\n" + src)
code("""
import sys, importlib, os
os.makedirs('/content/cctv_analysis', exist_ok=True)
if '/content' not in sys.path:
    sys.path.insert(0, '/content')
for m in [k for k in list(sys.modules) if k.startswith('cctv_analysis')]:
    del sys.modules[m]          # pick up edits when re-running
import cctv_analysis
print("cctv_analysis", cctv_analysis.__version__, "ready")
""")
# fix: %%writefile needs the folder first
C.insert(C.index(next(c for c in C if c.source.startswith("%%writefile"))),
         nbf.v4.new_code_cell("import os; os.makedirs('/content/cctv_analysis', exist_ok=True)"))

md("""
## Step 3 · Settings
Recommended defaults are already filled in. The only must-change items are the two folders.

* **INPUT_DIR** – the Drive folder your DVR/NVR syncs to (sub-folders are scanned; a sub-folder name or `ch01`/`cam2` in the file name becomes the camera name).
* **SPEED_PRESET** – `balanced` is recommended on a T4. `fast` ≈ 2–3× quicker (lighter models, no pose); `accurate` for small faces / far cameras.
* **Shop hours** – activity outside them is flagged as *after hours*.
* **KNOWN_FACES_DIR** *(optional)* – `known_faces/Owner/*.jpg`, `known_faces/Staff_Ravi/*.jpg` … these people get named instead of P-numbers and lower the score of their events.
""")
code("""
#@title Main settings { run: "auto" }
INPUT_DIR = "/content/drive/MyDrive/CCTV"            #@param {type:"string"}
OUTPUT_DIR = "/content/drive/MyDrive/CCTV_Analysis"  #@param {type:"string"}
KNOWN_FACES_DIR = "/content/drive/MyDrive/CCTV_known_faces"  #@param {type:"string"}
SPEED_PRESET = "balanced"  #@param ["fast", "balanced", "accurate"]
TIMEZONE = "Asia/Kolkata"  #@param {type:"string"}
OPEN_HOUR = "09:00"  #@param {type:"string"}
CLOSE_HOUR = "21:30"  #@param {type:"string"}
MOTION_SENSITIVITY = 0.5  #@param {type:"slider", min:0.1, max:0.9, step:0.05}
USE_FACES = True  #@param {type:"boolean"}
USE_AI_DESCRIPTIONS = True  #@param {type:"boolean"}
AI_DESCRIBE = "flagged"  #@param ["flagged", "people", "all"]
SAVE_CLIPS = "people"  #@param ["all", "people", "flagged", "none"]
ONLY_NEWER_THAN_DAYS = 0  #@param {type:"number"}
MAX_VIDEOS = 0  #@param {type:"integer"}
REPROCESS_ALL = False  #@param {type:"boolean"}
""", hide=True)
code("""
#@title Advanced settings (zones, ignore areas, file naming) – optional
# Coordinates are fractions of the frame (0..1); use the grid shown in Step 4 to read them.
# "*" applies to every camera; use the camera name (e.g. "ch01") for one camera.
ZONES = {
    # "*":    {"cash_counter": [[0.60, 0.45], [1.00, 0.45], [1.00, 1.00], [0.60, 1.00]]},
    # "ch02": {"stock_room_door": [[0.05, 0.10], [0.25, 0.10], [0.25, 0.80], [0.05, 0.80]]},
}
IGNORE_REGIONS = {
    # "ch01": [[[0.0, 0.0], [0.35, 0.0], [0.35, 0.07], [0.0, 0.07]]],   # e.g. a TV screen / fan / road
}
CLOSED_WEEKDAYS = []          # e.g. [6] = closed on Sunday (0=Mon … 6=Sun)
FILENAME_TIME_PATTERNS = []   # extra regexes with named groups Y m d H M S if your file names are unusual
TIME_OVERRIDES = {}           # {"file.mp4": "2026-10-11 09:30:00"} for files without any time info
EXTRA = {}                    # any other Config field, e.g. {"loiter_seconds": 120, "crowd_size": 5}
""", hide=True)
code("""
#@title Build configuration
from cctv_analysis.config import Config
PRESETS = {
    "fast":     dict(motion_fps=3, detect_fps=3, detector_model="yolo26n.pt", detector_fallbacks=["yolo11n.pt"],
                     use_pose=False, det_imgsz=640, detect_width=960, semantic_model="google/siglip2-base-patch16-224"),
    "balanced": dict(motion_fps=4, detect_fps=5, detector_model="yolo26s.pt", detector_fallbacks=["yolo11s.pt"],
                     use_pose=True, det_imgsz=960, detect_width=1280, semantic_model="google/siglip2-base-patch16-224"),
    "accurate": dict(motion_fps=5, detect_fps=8, detector_model="yolo26m.pt", detector_fallbacks=["yolo11m.pt"],
                     use_pose=True, pose_model="yolo26s-pose.pt", det_imgsz=1280, detect_width=1920,
                     semantic_model="google/siglip2-so400m-patch14-384", vlm_model="Qwen/Qwen3-VL-4B-Instruct"),
}
cfg = Config(
    input_dir=INPUT_DIR, output_dir=OUTPUT_DIR, work_dir="/content/cctv_work",
    known_faces_dir=KNOWN_FACES_DIR if os.path.isdir(KNOWN_FACES_DIR) else "",
    timezone=TIMEZONE, open_hour=OPEN_HOUR, close_hour=CLOSE_HOUR, closed_weekdays=CLOSED_WEEKDAYS,
    motion_sensitivity=MOTION_SENSITIVITY, use_faces=USE_FACES, use_vlm=USE_AI_DESCRIPTIONS, vlm_scope=AI_DESCRIBE,
    save_clips=SAVE_CLIPS != "none", clip_scope=SAVE_CLIPS if SAVE_CLIPS != "none" else "flagged",
    only_newer_than_days=ONLY_NEWER_THAN_DAYS, max_videos=MAX_VIDEOS, reprocess=REPROCESS_ALL,
    zones=ZONES, ignore_regions=IGNORE_REGIONS, filename_time_patterns=FILENAME_TIME_PATTERNS,
    time_overrides=TIME_OVERRIDES, **PRESETS[SPEED_PRESET])
for k, v in EXTRA.items():
    setattr(cfg, k, v)
assert os.path.isdir(cfg.input_dir), f"INPUT_DIR not found: {cfg.input_dir}"
print("Config ready →", cfg.output_dir)
""", hide=True)

md("""
## Step 4 · Preview (recommended once)
Shows every camera found, how the recording start time was worked out, and one frame per camera with a **0.1 grid** (to write zone coordinates), your **zones** (red), **ignored areas** (blue) and the **auto-detected DVR clock** (magenta, ignored for motion).
If a start time says `file-modified(approx)`, add a pattern or override in *Advanced settings*.
""")
code("""
#@title Preview cameras
from cctv_analysis.helpers import preview_cameras
preview_cameras(cfg)
""", hide=True)

md("""
## Step 5 · Run the analysis
Progress is printed per video and saved after every file, so if Colab disconnects just run this cell again — it continues where it stopped.
Typical speed on a T4 (`balanced`): idle footage ≈ 20–60× real-time, busy footage ≈ 2–6× real-time.
""")
code("""
#@title ▶️ Analyse all videos
from cctv_analysis.pipeline import Analyzer
analyzer = Analyzer(cfg)
stats = analyzer.run(build_reports=True)
print(stats)
""", hide=True)

md("## Step 6 · Review results")
code("""
#@title Summary + top suspicious events (snapshots, reasons, AI description, playable clip)
from cctv_analysis.helpers import summary, show_events
summary(cfg)
TOP_N = 10  #@param {type:"integer"}
_ = show_events(cfg, where="level IN ('HIGH','REVIEW')", order="score DESC", limit=TOP_N)
""", hide=True)
code("""
#@title Browse any events (SQL filter)
# Examples:  "after_hours=1"   "camera='ch01' AND start_dt LIKE '2026-10-11%'"   "identities LIKE '%P0003%'"
#            "objects LIKE '%handbag%'"   "start_dt BETWEEN '2026-10-11 20:00' AND '2026-10-11 23:59'"
WHERE = "n_person_tracks > 0"  #@param {type:"string"}
ORDER = "start_dt"  #@param ["start_dt", "score DESC", "duration DESC", "max_persons DESC"]
LIMIT = 10  #@param {type:"integer"}
_ = show_events(cfg, where=WHERE, order=ORDER, limit=LIMIT)
""", hide=True)
code("""
#@title 🔎 Free-text search over every snapshot
QUERY = "a person wearing a helmet"  #@param {type:"string"}
from cctv_analysis.helpers import search_text
_ = search_text(cfg, QUERY, k=8)
""", hide=True)
code("""
#@title 🧑 Search by photo – find every appearance of a person
PHOTO = "/content/drive/MyDrive/suspect.jpg"  #@param {type:"string"}
from cctv_analysis.helpers import search_face
_ = search_face(cfg, PHOTO)
""", hide=True)
code("""
#@title Show outputs (charts, files)
from IPython.display import Image, display
for p in ["charts/timeline.png", "charts/people_by_hour.png"]:
    f = os.path.join(cfg.output_dir, p)
    if os.path.exists(f):
        display(Image(f, width=1000))
print("Excel report :", os.path.join(cfg.output_dir, "CCTV_Report.xlsx"))
print("HTML report  :", os.path.join(cfg.output_dir, "report.html"), "(download the output folder to open it with images)")
print("Highlights   :", os.path.join(cfg.output_dir, "highlight_reel.mp4"))
print("Event folders:", os.path.join(cfg.output_dir, "events"))
""", hide=True)
code("""
#@title (Optional) Rebuild reports only / download a zip of the reports
from cctv_analysis.report import build_reports
from cctv_analysis.storage import Store
st = Store(os.path.join(cfg.work_dir, "analysis.db"), os.path.join(cfg.output_dir, "analysis.db"))
print(build_reports(st, cfg)); st.commit()
MAKE_ZIP = False  #@param {type:"boolean"}
if MAKE_ZIP:
    import shutil
    z = shutil.make_archive("/content/cctv_report", "zip", cfg.output_dir)
    from google.colab import files; files.download(z)
""", hide=True)
md("""
## Tips & troubleshooting
* **Too many events?** Lower `MOTION_SENSITIVITY`, add `IGNORE_REGIONS` for TVs/fans/roads, or set `EXTRA = {"require_person_for_event": True}`.
* **Missed movement?** Raise `MOTION_SENSITIVITY` (0.6–0.7) or `EXTRA = {"min_motion_area": 0.0008}`.
* **Wrong clock time?** Check Step 4. Supported names include Hikvision `ch01_20261011143000`, Dahua `2026-10-11/…/14.30.00-14.45.00.dav`, `NVR_ch2_main_20261011093015_…`, `11-10-2026_08-00-00`. Otherwise the AI reads the burnt-in DVR clock (when AI descriptions are on), then file time is used.
* **Same person gets several IDs?** Lower `EXTRA = {"face_match_threshold": 0.38}`; **different people merged?** raise it to 0.48.
* **Faster runs:** `SPEED_PRESET="fast"`, `AI_DESCRIBE="flagged"`, `SAVE_CLIPS="flagged"`.
* **`.dav` files** (Dahua) are read natively by ffmpeg; if one fails, export from the DVR as MP4.
* The score is a triage aid: always confirm by watching the cited clip / original video at the given offset.
* Face recognition: use only in line with local privacy law and inform customers that CCTV is in use.
""")
nb.cells = C
out = os.path.join(ROOT, "cctv-analysis.ipynb")
nbf.write(nb, out)
print("wrote", out, len(C), "cells")

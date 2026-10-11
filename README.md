# 🎥 home-camera-analysis — CCTV footage triage for a shop

Stop scrubbing through hours of recordings. Point this at the Google Drive folder your DVR/NVR syncs to, run one Colab notebook, and get an **Excel workbook + HTML dashboard that cites every movement**: when it happened, which video file, the exact offset in that file, snapshots, a short clip, who/what was seen, faces, and an **explainable suspicion score**.

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/OfficialAshish/home-camera-analysis/blob/main/cctv-analysis.ipynb)

## What it does

| Capability | How |
|---|---|
| **Motion events** with start / end clock time, duration, video file and in-video offset | MOG2 background subtraction + frame differencing on a low-res stream (fast full-video pass) |
| Ignores the **DVR's burnt-in clock**, lighting jumps (lights on/off, IR switch) and user-defined areas (TV, fan, road) | automatic OSD-mask detection, global-change rejection, polygon masks |
| **People / vehicles / bags / phones / knives…** with confidence and persistent track IDs | Ultralytics **YOLO26** (falls back to YOLO11) + ByteTrack, run only on motion segments |
| **Posture cues** – hands raised (hold-up), crouching; face-visible check | YOLO26-pose keypoints |
| **Faces** detected, quality-scored, cropped, and **grouped into identities across days** (P0001…); known staff/owner labelled from photos | OpenCV **YuNet** + **SFace** (Apache-2.0), persistent identity DB |
| **Suspicion score 0–100 with reasons** – after hours, camera tamper (covered / blackout / blur / moved), lights switched after hours, restricted zones (cash counter), loitering, running, crowding, hidden face, hands up, crouching, weapon-like object, repeat unknown visitor | `rules.py` – every flag has points and a human-readable reason |
| **AI scene tags** and **free-text search** ("man in a helmet near the counter") | Google **SigLIP-2** zero-shot embeddings |
| **AI description + verdict** of flagged events; reads the DVR clock when file names carry no time | **Qwen3-VL-2B-Instruct** (4B in *accurate* preset) |
| **Search by photo** – every appearance of a person | face embedding vs identity DB |
| Reports | `CCTV_Report.xlsx` (10 sheets, thumbnails, clickable links, review drop-down), `report.html` dashboard, timeline charts, activity heatmaps, `highlight_reel.mp4` |
| Robust for daily use | resumable & incremental (SQLite), per-video error isolation, skip already-processed files, Drive links |

## Quick start (Google Colab)

1. Open **`cctv-analysis.ipynb`** in Colab (badge above) → `Runtime › Change runtime type › T4 GPU`.
2. In **Step 3** set `INPUT_DIR` (your CCTV folder in Drive) and `OUTPUT_DIR`, plus shop hours.
3. `Runtime › Run all`. Use **Step 4** once to check cameras, start times and zone coordinates.
4. Open `OUTPUT_DIR/CCTV_Report.xlsx` → *Events* sheet → filter **Level = HIGH / REVIEW** or sort by **Score**.

Re-run any time: only new videos are analysed; reports always include everything so far.

## Output folder

```
CCTV_Analysis/
├── CCTV_Report.xlsx          # Read Me · Summary · Events · People & Objects · Face Identities · Faces
│                             # Detections (1/s) · Videos · Tamper & Camera · Activity Grid · Config
├── report.html               # filterable dashboard (level, camera, date, text)
├── highlight_reel.mp4        # top HIGH/REVIEW events back-to-back
├── charts/                   # timeline.png, people_by_hour.png
├── heatmaps/                 # where movement happens, per video
├── events/<camera>/<date>/<event-id>/
│   ├── snapNN_HHMMSS_annotated.jpg / _raw.jpg
│   ├── contact_sheet.jpg     # 6 keyframes in one image
│   ├── clip.mp4              # H.264 clip with burnt-in event label
│   ├── people/ track###_person.jpg     faces/ track###_best.jpg
├── analysis.db               # SQLite – the source of truth (query it if you like)
├── identities.json/.npy      # persistent face clusters
├── semantic_index.npz        # SigLIP-2 embeddings for text search
├── detections_full.csv       # every sampled detection
└── run_log.txt, config_used.json
```

### Events sheet columns
Event ID · Level · Score · Camera · Date · Start/End time · Duration · **Video file** · **Video offset start/end** · Seek (s) · Kind · Max people at once · Person tracks · Objects · Faces detected · Identities · Best confidence · **Flags / reasons** · **AI description** · Scene tag · Snapshot (thumbnail) · links to snapshot / contact sheet / clip / original video on Drive / event folder · After hours · Motion peak · Frames analysed · Time source · **Review status** (drop-down) · Notes.

## Recording time
Resolved in this order (configurable): file name → container metadata → AI read of the burnt-in clock → file modified time (marked *approx*). Built-in patterns cover Hikvision (`ch01_20261011143000`), Dahua (`2026-10-11/001/dav/14/14.30.00-14.45.00[M][0@0][0].dav`), generic NVR (`NVR_ch2_main_20261011093015_…`), day-first (`11-10-2026_08-00-00`) and more. Add regexes or per-file overrides in the notebook.

## Presets (Colab T4)

| preset | detector | pose | detect fps | VLM | use when |
|---|---|---|---|---|---|
| fast | yolo26n @640 | – | 3 | Qwen3-VL-2B (flagged) | lots of footage, close cameras |
| **balanced** | yolo26s @960 | yolo26n-pose | 5 | Qwen3-VL-2B (flagged) | recommended |
| accurate | yolo26m @1280 | yolo26s-pose | 8 | Qwen3-VL-4B | far cameras / small faces |

Pass 1 (motion) runs at ~50–150× real time; pass 2 only touches motion segments, so idle night footage costs almost nothing.

## Local / command-line use

```bash
pip install -r requirements.txt
python -m cctv_analysis --input /path/videos --output /path/results use_vlm=false detect_fps=4 \
       'zones={"*":{"cash_counter":[[0.6,0.5],[1,0.5],[1,1],[0.6,1]]}}'
python -m cctv_analysis --input ... --output ... --reports-only     # rebuild reports
python -m pytest -q tests                                             # unit tests
python tools/build_notebook.py                                        # regenerate the notebook from the package
```

All settings live in [`cctv_analysis/config.py`](cctv_analysis/config.py) (documented inline).

## Project layout

| module | role |
|---|---|
| `video_io.py` | discovery, ffprobe, fast ffmpeg frame pipe (OpenCV fallback, `.dav/.h264/.h265` support), time inference, clip export, Drive links |
| `motion.py` | pass 1: motion, clock mask, lighting rejection, tamper, heatmap, event segmentation |
| `detector.py` | YOLO26 detection + tracking, pose & posture cues |
| `faces.py` | YuNet/SFace, quality, persistent identity clustering, known-face enrolment |
| `semantic.py` | SigLIP-2 tagging + text-search index |
| `vlm.py` | Qwen3-VL event description, verdict JSON parsing, clock reading |
| `rules.py` | explainable scoring |
| `pipeline.py` | orchestration (resumable) |
| `report.py` | Excel, HTML, charts, highlight reel |
| `helpers.py` | notebook review / search utilities |

## Research notes – what exists and what this adds
Existing open-source projects each solve a slice: frame-diff timestamp reporters ([JAIJANYANI/Video-Analysis-for-Surveillance](https://github.com/JAIJANYANI/Video-Analysis-for-Surveillance)), YOLO person-event extractors ([makemecker/video-event-detection](https://github.com/makemecker/video-event-detection)), VLM summarisers ([Ravi-Teja-konda/Surveillance_Video_Summarizer](https://github.com/Ravi-Teja-konda/Surveillance_Video_Summarizer), Florence-2), and live AI-NVR platforms ([SharpAI/DeepCamera](https://github.com/SharpAI/DeepCamera), Frigate). None produce an offline, **cited, review-ready Excel index of recorded footage** on free Colab. This project combines the two-pass approach (cheap motion first, deep models only on motion) with tracking, face identities that persist across days, posture/zone/tamper heuristics, open-vocabulary tagging (SigLIP-2), VLM descriptions (Qwen3-VL), and reports designed for a shop owner's manual review.

## Models & licences
YOLO26/YOLO11 – Ultralytics, **AGPL-3.0** (fine for personal use; commercial redistribution needs an Ultralytics licence) · YuNet / SFace – OpenCV Zoo, Apache-2.0 / MIT · SigLIP-2 – Apache-2.0 · Qwen3-VL – Apache-2.0. InsightFace `buffalo_l` was deliberately not used because its recognition weights are not licensed for commercial use.

## Privacy
Face grouping is local to your Colab session and Drive. Inform people that CCTV is in use and follow local law (e.g. India's DPDP Act 2023) when storing or sharing face data. The score is a triage aid, not proof — always confirm with the cited footage.

# Decision log

Status: **current** · Owner: maintainers · Each entry: context → decision → consequence.

## D1 — Recording time comes from the DVR clock before container metadata
*Context.* Cloud/phone exports (`31Oct26New/G225087WSFW-cloud-18056409.mp4`) carry no time in the
name; their `creation_time` and file mtime are the **download** time (next day, scrambled order).
The old order `filename → metadata → vlm_osd → mtime` gave every clip ≈ the same bogus time, so
daily reels were in download order (the reported 05:38 → 22:00 → 20:38 jumps).
*Decision.* `filename → osd → metadata → mtime` (`timeline.py`). The burnt-in clock is read by OCR
(RapidOCR, Tesseract or the VLM) at 3–4 offsets; two early readings define the start, later ones
confirm it or flag a discontinuity. A date in the folder/file name (`31Oct26`) must match an OSD
reading (±1 day) and must match metadata **exactly** (exports are often stamped the next day).
*Consequence.* ~4 OCR reads per untimed file (≈0.3 s each on CPU). `vlm_osd` was removed; configs
that still list it fail validation with a clear message.

## D2 — Every event re-reads its own clock when the file time came from the clock
*Context.* A single file can be stitched from several recordings (the clock jumps inside it).
*Decision.* `timeline.event_clock`: one OCR read at each event start; if it disagrees with
`file start + offset` by more than `osd_event_tolerance` (5 s), the event uses its own reading
(`time_source = osd(...,event)`). Skipped for times from file names/overrides.

## D3 — Process videos in chronological order
A planning pass (probe + time) sorts videos by real recording time (approximate/unknown last,
natural file-name order). Face IDs (P0001 = first person seen), "new face" alerts and logs follow
the real timeline instead of the file-system order.

## D4 — Highlight reels keep the original picture
No burnt-in text by default (`reel_overlay=False`, `clip_overlay=False`); segments are cut and
scaled/letter-boxed by ffmpeg directly. The clock/camera mapping lives in
`<day>_index.csv`, `<day>_timeline.srt` (deliberately not named like the video so players do
not auto-show it) and the Excel *Highlights* sheet. Layout `highlights/YYYY/MM-Mon/`.
Segment cache keys include a render signature, so renders from older versions are never reused.

## D5 — Feature switches skip work, not just output
`use_faces`, `face_identities` (embedding + clustering), `use_pose`, `use_semantic`, `use_vlm`,
`use_plates`, `plate_enhance`, `plate_vlm_read`, `save_clips`, `make_highlight_reel`. With
`face_identities=False` and no watchlist, SFace embeddings are not computed at all.

## D6 — Number plates: learned detector + plate OCR, strict format fit, opt-in deblur
Plate detector `open-image-models` YOLOv9 (MIT) with an OpenCV fallback; reader `fast-plate-ocr`
(MIT). Indian format correction allows **at most one** letter/digit substitution — a truncated
read must never be "corrected" into a plausible wrong plate. `plate_enhance` adds blind motion
deblurring along the track direction (Wiener, blur length searched coarse-to-fine, scored by the
plate OCR, best three re-read by RapidOCR), multi-frame fusion, and saves the enhanced image for a
human. Measured results and limits: `docs/PLATES.md`. Off by default (≈5× slower per vehicle).

## D7 — VLM is a provider, not a hard-coded model
`vlm_provider = transformers | openai`. `openai` speaks OpenAI Chat Completions with an
`image_url` data URL, which Ollama, LM Studio, vLLM and llama.cpp accept (Ollama source checked:
`openai/openai.go`, case `"image_url"`). The API key is read from the environment variable named
in `vlm_api_key_env`; the app stores it in `secrets.json` (owner-only permissions), never in
`config_used.json`. Three consecutive failures disable the VLM for the run (circuit breaker).

## D8 — Desktop app = local web UI + PyInstaller bundle
A browser UI served on 127.0.0.1 by waitress works on Windows/macOS/Linux, needs no UI toolkit
and is testable with Playwright. The packaged `CCTVAnalysis.exe` opens the browser at a private
link that carries a per-launch token (see `docs/DESKTOP-APP.md` for the security model).

## D9 — Licences gate the packaged app, not the open-source library
Ed25519-signed, time-limited keys (optional machine binding, clock-rollback detection) are
verified offline against `cctv_analysis/license_public_key.pem`. No valid licence → trial mode
(5 videos per job) — fail closed. The Python package / CLI / notebook are the developer interface
of a public repository and are not licence-gated; anyone with the source can remove a check, so
the licence is a commercial control for non-technical customers, not DRM.

## D10 — Reviews live outside analysis.db
The analyzer replaces `<output>/analysis.db` with its working copy after every video, so anything
another process writes there is lost. Review status/notes are kept in `<output>/reviews.db` and
merged into reports.

## D11 — Fix old outputs in place (`--retime`)
Already analysed folders can be corrected without re-analysis: times are re-resolved, every stored
timestamp is recomputed as `base + offset` in SQL, reels/weekly workbooks of affected days are
rebuilt and those without events removed. Event IDs and folders keep their names (identifiers).

## D12 — Video encoder chosen from the ffmpeg build
`libx264` (GPL builds) → `libopenh264` (LGPL builds) → `mpeg4`. The Windows bundle ships an LGPL
ffmpeg so the distributed app does not contain GPL x264 (see `docs/COMMERCIAL-READINESS.md`).

## D13 — Footfall = counting lines, not track counts
Track counts over-count people (ByteTrack splits identities; measured 96 tracks on a clip with
roughly a dozen people). The Footfall sheet labels track counts as an upper bound and uses
counting-line crossings (`count_lines`) as the footfall figure.

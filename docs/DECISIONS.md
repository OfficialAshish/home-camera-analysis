# Decision log

Status: **current** · Owner: maintainers · Each entry: context → decision → consequence.

## D1 — Recording time comes from the DVR clock before container metadata
*Context.* Cloud/phone exports (`31Oct26New/G225087WSFW-cloud-18056409.mp4`) carry no time in the
name; their `creation_time` and file mtime are the **download** time (next day, scrambled order).
The old order `filename → metadata → vlm_osd → mtime` gave every clip ≈ the same bogus time, so
daily reels were in download order (the reported 05:38 → 22:00 → 20:38 jumps).
*Decision.* `filename → osd → metadata → mtime` (`timeline.py`). The burnt-in clock is read by OCR
(RapidOCR, Tesseract or the VLM) at 0.5 s, 1.5 s, the middle and the end; agreeing readings are
grouped, the biggest group wins and ties go to the group with the earliest reading (a stitched file
starts at its first frame); more than one group flags a discontinuity. A date in the folder/file name (`31Oct26`) must match an OSD
reading (±1 day) and must match metadata **exactly** (exports are often stamped the next day).
*Consequence.* ~4 OCR reads per untimed file (≈0.3 s each on CPU). `vlm_osd` was removed; configs
that still list it fail validation with a clear message.

## D2 — Every event re-reads its own clock when the file time came from the clock
*Context.* A single file can be stitched from several recordings (the clock jumps inside it).
*Decision.* `timeline.event_clock`: one OCR read at each event start; if it disagrees with
`file start + offset` by more than `osd_event_tolerance` (5 s), the event uses its own reading
(`time_source = osd(...,event)`). Only for files whose time came from the clock; footage without a
readable clock is not re-read per event, and its file-level search stops after two empty frames
(an empty read costs up to ~5 s on a busy CPU-only scene, measured on `vtest.avi`).

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

## D7 — VLM is a provider, not a hard-coded model; several providers fall back in order
`vlm_provider = transformers | openai | nvidia`, plus `vlm_fallbacks` (more providers tried in
order; `vlm.ChainVLM`). `openai`/`nvidia` speak OpenAI Chat Completions with an `image_url` data
URL, which Ollama, LM Studio, vLLM, llama.cpp and NVIDIA's `integrate.api.nvidia.com` accept
(sources in `docs/VLM.md`). The desktop app configures the list only in *Settings › AI model
providers* (`app/ai_providers.py`): one place, so the Set up page and the Studio cannot disagree.
Keys are stored per provider in `secrets.json` (owner-only permissions) and handed to the
analysis as environment variables whose names are in the configuration – never in
`config_used.json`. Temperature 0 always: readings are evidence aids and must be repeatable.
Three consecutive failures of the whole chain disable the VLM for the run (circuit breaker).

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

## D14 — A case analyses each seized source into its own output folder
*Context.* An investigation brings 10–20 unrelated DVR exports. Folding them into one analysis
would mix camera names, file-relative paths and clocks of different devices.
*Decision.* `cases.py`/`investigation.py` run the ordinary pipeline once per source into
`<case>/sources/<id>/`; cross-source questions (timeline, sightings, appearance search, journey,
certificate) are answered by reading those folders. *Consequence.* Every existing per-folder view,
report and export keeps working per source; the pipeline is unchanged by cases.

## D15 — Clock corrections never edit recorded times
*Decision.* The officer's correction is stored per source (and optionally per camera) in
`case.json`; calibrated time = DVR time + correction is computed when shown. Both times appear
everywhere; the change is audited and immediate. *Why.* The DVR's own time is part of the record
(it is what the picture shows); rewriting it would make the evidence disagree with the footage.

## D16 — Appearance search uses a built-in descriptor; a learned model is optional
*Decision.* `reid.py` ships a colour/texture/shape descriptor (OpenCV/NumPy). Any OSNet/FastReID
ONNX export can be added at `<models>/reid/reid.onnx` and is blended in. *Why.* Public re-ID
weights are trained on research-only datasets (MSMT17, Market-1501); bundling them would put a
licence problem into a product sold to government. The built-in descriptor is measured
(docs/REID.md) and its limits are stated.

## D17 — Raw `.264/.265` streams are timed by their frames and decoded through their remux
*Context.* They have no duration, index or timestamps (probe returned 0 s; every seek returned the
first frame). *Decision.* Duration from packet count ÷ decoder frame rate; output-side seeking;
in case runs decode the lossless remux (identical frames) via `video_io.register_proxy`; hashes
and evidence cuts always use the original; copy cuts of raw streams start 3 s early.

## D18 — The certificate is generated, the declaration is not
*Decision.* `section63.py` fills in everything the software knows (case, devices, tick boxes,
algorithm, the hash report, acquisition hashes, Merkle root) and leaves signatures – and names,
addresses and places unless typed by the officer – as blank lines. The statutory wording follows
the Schedule of the Bharatiya Sakshya (Second) Bill, 2023 as enacted (source and hash in
`docs/legal/BSA-2023-SECTION-63.md`); the Gazette text could not be retrieved and must be compared
by the legal cell (UNVERIFIED). *Why.* A Section 63 certificate is a statement by a person; the
tool must not put words in a declarant's mouth.


## D19 — One Section 63 certificate per source device
*Context.* Part A is signed by "a person in charge of the computer or communication device"; a
shop owner can vouch for his DVR and for nothing else. *Decision.* `build_bundle` writes one
certificate per source that contributes a record, listing only that device, its records and its
acquisition hashes; Part B is the same expert on each. Declarants are stored (Part A per source,
Part B per case) and audited. *Consequence.* A three-DVR bundle carries three certificates.

## D20 — Face identities are per source; across sources a face is matched by similarity
*Context.* Each source's pipeline clusters faces into P0001, P0002 … independently; the first
version of `sightings` treated the same number in two sources as the same person (proved wrong by
`tests/test_case_sightings.py`). *Decision.* A P-number is always qualified by its source; other
sources are searched by SFace cosine ≥ `face_match_threshold` against their identities. Enrolled
names stay global.

## D21 — Stored paths are resolved at read time, recordings from the source's current folder
*Context.* `analysis.db` stores absolute paths; drive letters change and cases move.
*Decision.* Readers use `storage.resolve`/`rel_to` (longest existing trailing part under the
current output folder) and `Source.recording(rel_path)`; a moved seized folder is relinked only
after its recordings match the acquisition hashes. *Why not rewrite the database?* The analysis
output is evidence-adjacent and shared with the single-folder screens; read-time resolution
changes nothing on disk.

## D22 — The trial limit counts videos of the whole case run
*Decision.* `run_case(max_videos_total=TRIAL_MAX_VIDEOS)` spends one budget across sources; the
per-source `apply_limits` hook was removed so the limit has one meaning ("videos per job").

## D23 — Offer the system folder dialog only where it can open
*Decision.* `server.native_dialog()` (tkinter importable, and a display on Linux) is reported by
`/api/fs/roots`; the UI goes straight to the built-in folder browser otherwise and `/api/fs/pick`
answers 501 without starting a helper process.

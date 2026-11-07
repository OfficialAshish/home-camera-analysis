# Testing

Status: **current**

## Gate (CI: `.github/workflows/ci.yml`)
| Step | Command | Notes |
|---|---|---|
| Lint | `ruff check .` | config in `pyproject.toml` |
| Type check | `mypy` | `cctv_analysis/`, lenient optional handling |
| Notebook in sync | `python tools/build_notebook.py && git diff --exit-code cctv-analysis.ipynb` | notebook is generated from the package (deterministic cell ids) |
| Tests | `python -m pytest -q` | everything below; ~12 min on 4 CPU cores |
| App self-test | `python -m cctv_analysis.app --self-test` | imports, UI, ffmpeg, torchvision NMS, offline OCR, case API, certificate PDF, forensic filters, appearance vector |
| Windows build | `packaging/build_windows.ps1` | builds `CCTVAnalysis.exe` and runs its self-test |
| One-click launcher | `run_app.bat --self-test` (windows-app job) | the launcher finds a Python with the packages, puts the bundled ffmpeg on PATH, self-test passes |
Release-only checks (need owner credentials): `python -m pytest -m release` → `docs/CREDENTIAL-TASKS.md`.

## Suites
| File | What it proves |
|---|---|
| `test_core.py` | file-name times, camera names, hours, zones, scoring, VLM verdict parsing, motion on a synthetic clip, folder keywords, week labels, duplicate hashing, every encoder fallback produces a playable file |
| `test_timeline.py` | clock-text formats, folder dates, **the reported bug**: cloud exports get their time from the DVR clock (and the negative control reproduces the bogus download time), folder-date rejection, stitched files flagged |
| `test_pipeline_e2e.py` | real pipeline (YOLO, OCR): reel chronological and in `highlights/YYYY/MM-Mon`, **DVR clock read off the rendered reel matches the index at every movement**, picture unmodified (+ positive control with overlay on), stitched file events use their own clock (+ control with the check off), cancel/resume, broken detector fails videos instead of reporting success, `--retime` repairs a folder analysed with the old rules |
| `test_plates.py` | format fit never "corrects" truncated reads, voting, watchlist, exact-frame seek regression, plates off by default, sharp plate + watchlist alert, 9 px motion-blurred plate unreadable without and read with enhancement |
| `test_footfall_reports.py` | counting lines on real pedestrian footage (OpenCV `vtest.avi`), event totals = sum of tracks, Excel sheets and contents |
| `test_vlm_providers.py` | OpenAI-compatible wire format (fake server checks every request), API key header, NOT_CONFIGURED, full pipeline using the server for descriptions and plate reading, circuit breaker after 3 failures, built-in transformers provider with SmolVLM-256M |
| `test_licensing.py` | valid / expired / edited / other-vendor / other-machine / clock-rollback / fail-closed states, using the real vendor tool |
| `test_app_api.py` | token, cookie, Host, path-confinement (incl. symlink), validation, request size, double submit, one job at a time, scan → estimate → analysis → results, plates search, seekable video (206), reviews survive the analyzer's DB sync, evidence export + tamper detection, audit log, retime endpoint, rate limit, secrets never returned |
| `test_guards_unit.py` | job exclusivity/idempotency, job failure reporting, audit chain edits/deletes/reorders, trial limit |
| `test_multi_folder_ingest.py` | a case with three seized folders (MP4, raw `.264`, MPEG-TS) whose DVR clocks are wrong: acquisition hashes, lossless playback copies (same packets, same pixels), DVR times from each wrong clock, calibration restores the true route (negative control without it), per-camera override, journey and reel in calibrated time (time read back by OCR), trial limit over the whole run, a moved case and new drive letters (relink refuses a wrong folder and a one-byte change), raw stream timing |
| `test_case_sightings.py` | face identities are per source (Lena is P0001 in one DVR, P0002 in another): following a face finds the same person by similarity, never the other source's same number (old rule reproduced as a mutation); enrolled names; plates exact/normalised/one edit across DVRs; journey of a face |
| `test_person_reid.py` | appearance descriptor: colour names, pose/light/scale/background changes, texture, unusable crops, real pedestrians (rank-1 measured), ONNX model path, case index and search |
| `test_forensic_enhancement.py` | every filter: validation, reproducibility, measured effect (PSNR/edge energy), perspective, multi-frame stacking noise reduction, saved records |
| `test_section63_bsa.py` | RFC 6962 Merkle root for 0–40 items, certificate wording and tick boxes, PDF content, `sha256sum -c`, tampering / added / removed files and forged roots detected, one certificate per device with its own declarant, refused inputs |
| `test_case_api.py` | the case endpoints with a real four-source Quick investigation: token/cookie/media rules, path confinement, validation before creation, statuses, timeline and immediate clock corrections, clothing colours and appearance search across NVRs, describe-it search (SigLIP-2) ranking the described scene first, playback copies and range requests, lossless frames, event detail with both times, relink, Forensic Studio preview/save/reproduce, highlight reel, court bundle with Part A/B + verification + certificate preview CSP, folder location by content, moving the cases folder, run validation, system dialog check, level names |
| `test_ui_cases_playwright.py` | an officer's whole investigation in headless Chromium: folders via the folder browser and drag & drop, clock corrections, run with progress per source, timeline order on the calibrated clock, describe-it and colour search, journey, event → Forensic Studio → saved still, court bundle with Part A/B → download → verify, certificate preview, case screen; fails on console errors / failed requests |
| `test_ui_playwright.py` | headless Chromium through the real UI: folder browser, options, check, scan, estimate, start (double click), progress, results, event detail, review, evidence export + download, audit, plate search, highlights, settings; fails on console errors / failed requests |
Model weights and test videos download once into `.test_models/` (cached in CI).

## Proving the tests guard something
`python tools/mutation_check.py` removes one rule at a time (token check, Host check, path
confinement, licence expiry/signature/rollback/binding, trial limit, job exclusivity, audit
chain, metadata/OSD date checks, first-frames start, plate correction limit, exact seek,
per-event clock, clean reel, reel order, VLM breaker, evidence tamper detection, one certificate
per device, bundle root verification, system-dialog check, case media routes, validation before
creating a case, per-source face identities, moved-folder pictures, relink hash check, trial limit
over a case run), runs the test that claims to cover it, and requires it to fail. Results of the last run: see `docs/STATUS.md`.

## Load test
`python tools/soak.py` – see `docs/PERFORMANCE.md`.

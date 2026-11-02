# Testing

Status: **current**

## Gate (CI: `.github/workflows/ci.yml`)
| Step | Command | Notes |
|---|---|---|
| Lint | `ruff check .` | config in `pyproject.toml` |
| Type check | `mypy` | `cctv_analysis/`, lenient optional handling |
| Notebook in sync | `python tools/build_notebook.py && git diff --exit-code cctv-analysis.ipynb` | notebook is generated from the package (deterministic cell ids) |
| Tests | `python -m pytest -q` | everything below; ~12 min on 4 CPU cores |
| App self-test | `python -m cctv_analysis.app --self-test` | imports, UI, ffmpeg, torchvision NMS, offline OCR |
| Windows build | `packaging/build_windows.ps1` | builds `CCTVAnalysis.exe` and runs its self-test |
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
| `test_ui_playwright.py` | headless Chromium through the real UI: folder browser, options, check, scan, estimate, start (double click), progress, results, event detail, review, evidence export + download, audit, plate search, highlights, settings; fails on console errors / failed requests |
Model weights and test videos download once into `.test_models/` (cached in CI).

## Proving the tests guard something
`python tools/mutation_check.py` removes one rule at a time (token check, Host check, path
confinement, licence expiry/signature/rollback/binding, trial limit, job exclusivity, audit
chain, metadata/OSD date checks, first-frames start, plate correction limit, exact seek,
per-event clock, clean reel, reel order, VLM breaker, evidence tamper detection), runs the
test that claims to cover it, and requires it to fail. Results of the last run: see `docs/STATUS.md`.

## Load test
`python tools/soak.py` – see `docs/PERFORMANCE.md`.

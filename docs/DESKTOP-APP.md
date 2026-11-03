# Desktop app

Status: **current** · Code: `cctv_analysis/app/` · Tests: `tests/test_app_api.py`, `tests/test_ui_playwright.py`,
`tests/test_guards_unit.py` · Packaging: `packaging/`

## Running
* From source: `pip install -r requirements.txt` then `python -m cctv_analysis.app`
  (`--port`, `--no-browser`, `--data-dir`, `--self-test`).
* Packaged: `CCTVAnalysis.exe` (built by `packaging/build_windows.ps1`, or the *windows-app* CI
  job artifact). A console window shows the private link; closing it stops the app.

## Screens
1. **Set up** – recordings folder and results folder (built-in folder browser; *System dialog*
   when tkinter is available), speed preset, feature switches with time-cost badges
   (rendered from `app/features.py`, keys = `Config` fields), *Check computer* (CPU, RAM, GPU,
   disk, ffmpeg, models, per-feature compatibility), *Scan recordings* (count, total length,
   cameras, files whose time must be read from the picture), *Measure speed* (1-minute benchmark
   on one of the user's videos), *Estimate time* (projection with listed assumptions), *Start*.
2. **Progress** – stage, video n of N, movement n of M, %, time left, live log, *Stop*
   (finished videos are kept; the rest continues next run).
3. **Results** – KPIs, filters (dates, camera, levels, people only, review status, text, person,
   plate), paginated event cards, event detail (flags with reasons, AI description, clip,
   snapshots, plate crops + enhanced images, people/face crops, review status + notes,
   *Add to evidence*), *Fix recording times*.
4. **Highlights** – day picker, video player, index (click → jump to that movement).
5. **Number plates** – search with 0–2 character tolerance.
6. **Faces** – one card per person; NEW ones outlined.
7. **Export evidence** – case id, operator, notes, face/plate blurring → ZIP; audit log view with
   integrity check.
8. **Settings** – licence (state, days left, this computer's ID, activate), AI server address/key +
   *Test connection*, models (installed / download), operator name.

## Security model (single user, own computer)
| Control | Where | Test |
|---|---|---|
| Listens on 127.0.0.1 only; `--allow-lan` required for anything else | `__main__.py` | – |
| Per-launch random token; link `/?t=` sets an HttpOnly SameSite=Strict cookie | `server.index` | `test_index_link_sets_cookie_and_page_carries_token` |
| Every API call needs header `X-CCTV-Token` (cookie alone is not enough → CSRF-safe) | `server.guard` | `test_api_requires_the_token`, `test_cookie_alone_cannot_call_the_api` |
| Cookie accepted only for GET media/download routes | `server.guard` | `test_cookie_can_load_media_only` |
| Host header must be 127.0.0.1/localhost:port (DNS rebinding) | `server.guard` | `test_foreign_host_header_is_rejected` |
| Files only from inside the requested output folder (realpath + commonpath; symlinks resolved) | `_inside` | `test_file_serving_cannot_escape_the_output_folder` |
| CSP `script-src 'self'`, no inline script/style; UI writes data with `textContent` only | `after_request`, `app.js` | page header asserted |
| 1 MB request limit, unknown config keys rejected, `Config.validate()` | `build_config` | `test_rejects_bad_config` |
| Rate limits (60 writes/min per endpoint group, 1200 reads/min) | `RateLimiter` | `test_rate_limit` |
| One analysis at a time; `request_id` makes start idempotent | `JobManager` | `test_one_analysis_at_a_time_and_idempotent_start` |
| API key stored in `secrets.json` (0600), never returned | `Settings` | `test_settings_never_return_the_api_key` |
| No stack traces to the browser | `errorhandler(Exception)` | – |
Each guard is also checked by `tools/mutation_check.py` (removing it must make its test fail).

## Data locations
`--data-dir` (default `%APPDATA%\CCTVAnalysis`, `~/Library/Application Support/CCTVAnalysis`,
`~/.local/share/cctv-analysis`): `settings.json`, `secrets.json`, `license/`, `models/`,
`work/<hash>/` (working database per results folder), `audit_log.jsonl` (licence actions).
Per results folder: `analysis.db`, `reviews.db`, `audit_log.jsonl`, `exports/`, reports.

## Packaging
`packaging/cctv_app.spec` (PyInstaller) bundles the UI, the licence public key, OCR models and an
LGPL ffmpeg (`packaging/ffmpeg/`). `CCTVAnalysis --self-test` imports every module, checks the
UI, ffmpeg/ffprobe, torchvision's compiled NMS operator and an offline OCR read; the build
script fails if it does not pass. Size ≈ 1.5 GB (CPU PyTorch). A GPU build needs the CUDA
PyTorch wheels instead of `--index-url …/cpu` in `build_windows.ps1`.

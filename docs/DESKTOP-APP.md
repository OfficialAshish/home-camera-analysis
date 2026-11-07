# Desktop app

Status: **current** · Code: `cctv_analysis/app/` (`server.py` single-folder screens and guards, `case_api.py` case
screens, `common.py` request helpers, `static/app.js` + `static/case.js`) · Tests: `tests/test_app_api.py`,
`tests/test_case_api.py`, `tests/test_ui_playwright.py`, `tests/test_ui_cases_playwright.py`, `tests/test_guards_unit.py`
· Packaging: `packaging/`, `run_app.bat`

## Running
* From source: `pip install -r requirements.txt` then `python -m cctv_analysis.app`
  (`--port`, `--no-browser`, `--data-dir`, `--self-test`).
* **One click (Windows, source checkout): `run_app.bat`.** It uses the first Python that has the
  packages (`.venv\` next to it, `python`, `py -3`), otherwise finds Python 3.10+ and offers to
  install everything into `.venv\` (CPU PyTorch + `requirements.txt`); puts `packaging\ffmpeg\`
  on PATH when present, warns when ffmpeg is missing, then starts the app (the browser opens on the
  private link). `run_app.bat --self-test` runs the installation check and exits with its result;
  `run_app.bat --install` installs into `.venv\` without asking (first start on a new computer).
  The *windows-app* CI job runs both: `--self-test` against the runner's Python, then
  `--install --self-test` (a fresh `.venv` with CPU PyTorch and `requirements.txt`).
* Packaged: `CCTVAnalysis.exe` (built by `packaging/build_windows.ps1`, or the *windows-app* CI
  job artifact). A console window shows the private link; closing it stops the app.

## Screens
The app opens on **Quick investigation**. Case screens (Drishti-AI, docs/CASES.md):
* **Quick investigation** – drop the seized DVR/NVR/pen-drive folders on the page, or *Choose
  folder…* (the built-in folder browser) or *Windows folder dialog…* (shown only where the app can
  open the system dialog: on Windows the Windows "Browse For Folder" dialog, opened by the app's
  helper process with an invisible top-most owner window so it comes up in front; elsewhere
  tkinter). A browser never tells a page where a dropped folder is: the app finds it by the names
  and sizes of its files, first next to the folders used before (a seized drive holds several
  device folders side by side), then in the user's folders and on every drive; when it is not
  found the page asks for the path (paste, folder browser or the system dialog). A clock
  correction per folder, optional FIR / police station / officer, *Balanced* or *Fast*,
  describe-it search / plates, the face mode (off / detect only / unique faces / only reference persons with their photos – docs/CASES.md), **Run investigation**: one request creates the case, adds
  every folder and starts the run (everything is validated first; nothing is created on error).
* **Cases** – list, new case, case details, one card per source (label, folder, availability,
  clock and per-camera corrections, status: files hashed, events, person tracks, playback copies,
  describe-it search; device particulars and Part A declarant for the certificate; map position;
  *Relink…* when the drive letter changed; *Remove*), *Analyse all sources*, the case audit log.
  The cases folder can be moved to another disk (*Change…*).
* **Timeline & search** – all events of all sources on the calibrated clock (lanes per source and
  cards with calibrated and DVR time), event detail (the recording played from its playback copy,
  people, plates, snapshots; *Find this person on all cameras*, *Follow face*, *Find this
  vehicle*, *Open in Forensic Studio at this moment*, *Add to court bundle*), and three searches
  over the whole case: *Describe it* (SigLIP-2), *Clothing colours* (top/bottom), *Number plate*.
* **Journey** – the sightings of the search in calibrated order with tick boxes (untick wrong
  matches → *Rebuild journey*), the route as a flow of camera stops with travel times, an offline
  map when the sources have positions (latitude/longitude or site-plan x/y; nothing is fetched
  from the internet), *Make highlight reel*, *Add ticked to court bundle*.
* **Forensic Studio** – the original frame decoded from the recording at the chosen time
  (lossless PNG), filters with live preview (debounced, latest result wins), four-corner
  perspective by clicking the original, multi-frame stacking (3–9 frames, ECC alignment), *Save*
  (PNG + filter log + hashes, checked to reproduce bit for bit), saved stills of the case.
* **Court bundle** – the chosen records and stills, Part A per device and Part B (kept for the next
  export, audited), *Preview certificate* per device (printable page), *Create court bundle*
  (one certificate per device), the case's exports with SHA-256 and *Verify*.

Single-folder screens (unchanged, for one DVR without a case):
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
8. **Settings** – licence (state, days left, this computer's ID, activate), *AI model providers*
   (ordered list with fallback: type, address, model name, API key, *Test* with a real picture –
   `docs/VLM.md`), models (installed / download), operator name.

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
| Case media (`/api/cases/<id>/file`, `/video`, `/frame`, `/certificate`, `/exports/<name>`) are the only case routes a cookie alone may load | `case_api.is_media`, `server.guard` | `test_case_routes_need_the_token_and_the_cookie_loads_media_only` |
| Case files only from inside the case folder; recordings only those listed in the source's acquisition record | `common.inside`, `case_video` | `test_case_files_cannot_escape_the_case_folder`, `test_recordings_play_from_their_lossless_copy_and_only_ingested_files_are_served` |
| Quick investigation validates every folder, offset, preset and option before creating anything | `case_quick`, `run_request` | `test_quick_investigation_checks_every_folder_before_creating_anything` |
| The printable certificate page runs no script at all (`default-src 'none'; style-src 'unsafe-inline'`) | `case_certificate` | `test_court_bundle_has_one_certificate_per_device_and_verifies` |
| A relinked source must hold the acquired recordings (same size and SHA-256) | `Case.relink_source` | `test_relink_moves_a_source_only_to_a_folder_with_the_same_recordings` |
| Responses carry case-relative paths, never absolute paths of the computer | `case_api._public_point`, `_case_rel` | `test_people_by_clothing_colour_and_appearance_search_across_cameras` |
| Forensic Studio previews have their own write budget (300/min) so sliders do not hit the 60/min limit | `server.guard` | – |
Each guard is also checked by `tools/mutation_check.py` (removing it must make its test fail).

## Data locations
`--data-dir` (default `%APPDATA%\CCTVAnalysis`, `~/Library/Application Support/CCTVAnalysis`,
`~/.local/share/cctv-analysis`): `settings.json`, `secrets.json`, `license/`, `models/`,
`work/<hash>/` (working database per results folder), `audit_log.jsonl` (licence actions, cases
folder changes), `cases/` (default cases folder; *Cases › Change…* moves new work to another disk,
e.g. a large `D:` – a case's playback copies are as large as its originals).
Per results folder: `analysis.db`, `reviews.db`, `audit_log.jsonl`, `exports/`, reports.

## Packaging
`packaging/cctv_app.spec` (PyInstaller) bundles the UI, the licence public key, OCR models and an
LGPL ffmpeg (`packaging/ffmpeg/`). `CCTVAnalysis --self-test` imports every module, checks the
UI, ffmpeg/ffprobe, torchvision's compiled NMS operator and an offline OCR read; the build
script fails if it does not pass. Size ≈ 1.5 GB (CPU PyTorch). A GPU build needs the CUDA
PyTorch wheels instead of `--index-url …/cpu` in `build_windows.ps1`.

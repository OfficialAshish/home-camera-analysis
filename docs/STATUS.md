# Round status – enterprise CCTV platform

Status: **current** · Branch `feature/enterprise-cctv-platform` (PR #1) · Base: `main` @ `bbc2e0b`
Environment of all local measurements: Linux, Intel Xeon 2.8 GHz, 4 vCPU, 15 GB RAM, no GPU,
Python 3.11.17, ffmpeg 6.1, torch 2.15 CPU, ultralytics 8.4.198.

## 1. Summary
The scrambled highlight timeline is fixed at its root cause. Recording times now come from the
DVR clock printed on the picture (OCR, cross-checked) rather than download timestamps, and videos
and reels follow real recording order. Reels keep the original picture and are filed under
`highlights/YYYY/MM-Mon/`. Optional analyses can be switched off and then really skip their work.
Added: number-plate reading (with motion-deblur enhancement and a watchlist), VLM providers
(built-in or any OpenAI-compatible local server), a desktop app (local web UI, packaged with
PyInstaller) with system check, scan, benchmark, estimate, progress, results, highlights, plates,
faces, search by photo, evidence export, audit log and settings; offline time-limited licences;
evidence packages with SHA-256 manifests and redaction; reviews; footfall counting lines; face and
plate watchlists; in-place `--retime` for folders analysed with the old version. CI runs lint,
type check, notebook sync, the full test suite (incl. a real-browser test) and a Windows build.

## 2. Issues found and how each was fixed (with evidence)
| # | Issue | Fix | Evidence |
|---|---|---|---|
| 1 | **Reel timeline scrambled** (reported): cloud clips had no time in the name; metadata/mtime = next-day download time → wrong day, reel in download order | `timeline.py`: filename → OSD (OCR) → metadata (must match folder date) → mtime; multi-sample consensus; folder-date checks | Original code on the reproduction: 0/5 reel positions show the clock they claim, order 22:00→05:38→13:47→20:38→01:12. New: 10/10 within 1.4 s, monotonic. `test_reel_shows_the_dvr_clock_it_claims`, `test_cloud_export_times_come_from_the_dvr_clock` (incl. negative control) |
| 2 | Date folder `31Oct26New` used as camera name | date-like folders skipped; device-serial prefix used | `test_reel_is_chronological_and_foldered` (camera = `G225087WSFW`) |
| 3 | Files stitched from several recordings got one time | per-event clock re-read (`event_clock`) | `test_stitched_file_event_uses_its_own_clock` (+ control with the check off → 5.5 h wrong) |
| 4 | Videos processed in file-system order | planning pass, chronological processing | `test_cancel_stops_cleanly_and_resumes` sees the planning stage; reel order tests |
| 5 | Reels had burnt-in text | clean ffmpeg cut (`reel_overlay=False`), index CSV/SRT | `test_reel_picture_is_unmodified` (diff < 3) + positive control with overlay (> 10) |
| 6 | Reels in `highlights/<year>/` | `highlights/YYYY/MM-Mon/`; old-layout and empty-day reels removed | e2e + retime tests |
| 7 | `read_frame_at` returned the frame ~0.4 s late (fps filter) | accurate seek + `-frames:v 1` | `test_read_frame_at_returns_the_exact_frame`; plate reads went from truncated (`MH12AB12`) to exact |
| 8 | Plate format correction could turn truncated reads into plausible wrong plates | at most one substitution | `test_indian_format_fit`; mutation killed |
| 9 | Motion-blurred plates unreadable | blind motion deblur scored by OCR + RapidOCR re-read + fusion (opt-in) | 9 px blur: plain `1` → enhanced `MH12AB1234`; `test_motion_blurred_plate_needs_enhancement` (+ negative control); full matrix in PLATES.md |
| 10 | **Silent success**: in the packaged app every event crashed (missing torchvision op) yet videos were `done` with 0 events and the job reported success | a video whose events all fail is `error`; same error on 3 videos stops the run; app job fails when nothing was analysed | `test_broken_detector_fails_videos_instead_of_reporting_success`; packaged run before fix: "done, 0 events"; after: "failed – no video could be analysed" |
| 11 | PyInstaller bundle lacked torchvision's `_C_stable.so` (YOLO NMS) | explicit binaries in the spec; self-test runs NMS and an offline OCR read | packaged binary: self-test exit 1 before, 0 after; real analysis through the packaged server: 5/5 videos, chronological reel |
| 12 | Reviews existed only as an Excel drop-down (lost on every rebuild) and would be overwritten if stored in `analysis.db` (the analyzer replaces it) | `reviews.db` | `test_review_persists_across_reanalysis_sync` |
| 13 | Stale weekly workbooks stayed after times changed | removed when their week has no events | `test_retime_fixes_already_analysed_footage` |
| 14 | VLM failures cost a timeout on every event | circuit breaker after 3 failures | `test_dead_server_disables_vlm_after_three_failures` (5 events → exactly 3 calls) |
| 15 | `libx264` hard-coded (absent in LGPL ffmpeg builds) | encoder auto-detect | `test_every_encoder_fallback_produces_a_playable_file` (libx264, mpeg4) |
| 16 | App hot paths unbounded: plate search scanned every row under the GIL, all reviews loaded per request, highlights returned every day's index, date filter defeated the index | see PERFORMANCE.md | soak: 16 → 2 731 requests/25 s, 0 errors; p50 event detail 15.7 s → 34 ms |
| 17 | Clock OCR on footage without a clock: up to ~22 s/file + seconds per event | stop after two empty frames; per-event reads only for clock-timed files | `test_footage_without_a_clock_costs_at_most_two_reads` |
| 18 | UI race (found by CI): older folder listing overwrote the folder the user chose | latest-request-wins in folder browser, event list, plate search | UI test injects a slow response; fails without the fix (`/root` instead of the chosen folder), passes with it |
| 19 | Footfall by track count over-counts (96 tracks on a clip with about a dozen people) | counting lines are the footfall figure; track counts labelled as an upper bound | `test_people_are_counted_crossing_the_line` |
| 20 | Search by photo only in the notebook | shared `faces.match_identities`, app endpoint + UI | `test_search_by_photo_finds_the_person` |
| 21 | Redaction untested | test | faces before ≥ 1, after 0; no original segment; package verifies |
| 22 | Old databases | migration tested | `test_old_database_migrates_and_works` (original schema verbatim) |

Guards proven by mutation (`tools/mutation_check.py`): **23/23 killed** (22/23 in the full run; the
survivor exposed weak test data, now fixed and killed). An earlier fast run showed a mutation
surviving because the code it targeted was redundant. That rule was removed and the mutation
re-targeted at the deciding rule.

## 3. Known limitations (stated plainly)
* **Selling requires an owner decision on YOLO (AGPL-3.0)**: AGPL for the whole app, an Ultralytics
  licence, or a different detector. No replacement detector was built this round.
  `docs/COMMERCIAL-READINESS.md`. The repository has no LICENSE file.
* Licences need the owner's signing key (`docs/CREDENTIAL-TASKS.md` #1). Until then the app runs in
  trial mode. Code signing of the exe needs a certificate (#2).
* Licences gate only the packaged app. The open-source package/CLI are not gated (decision D9).
* Plates: measured on synthetic blur of a real car photo, not on a real-world plate dataset (none
  available). Enhancement recovers 5–9 px blur at ~70–96 px plate width, not 15 px blur or ~56 px
  plates. One enhanced result was a confident-looking wrong plate (`MH22AB1234`, confidence 0.34).
  Low-confidence reads must be checked by eye.
* The OpenAI-compatible provider was tested against a fake server that checks the documented wire
  format. No real Ollama/LM Studio/vLLM server was run here. The default Qwen3-VL-2B was not run
  (CPU RAM); the built-in provider was verified with SmolVLM-256M.
* `libopenh264` encoder path (used with LGPL ffmpeg) was not available locally and is untested; the
  Windows build depends on it or falls back to MPEG-4 (plays in VLC; browsers may not play MPEG-4 Part 2).
* The Windows exe is built and self-tested only in CI (no Windows machine here). The Linux bundle
  from the same spec was built and run end to end.
* Headless Chromium in the UI test cannot decode H.264, so in-browser video *playback* is not
  covered by tests. Range requests (206) and the player wiring are.
* GPU paths (CUDA decode, GPU inference) were not exercised (no GPU).
* Footfall counts depend on tracking quality. Person-track counts are an upper bound.
* Search by photo and face identities use SFace similarity thresholds tuned by the original author.
  Not re-validated on real CCTV faces.

Where I was wrong during the round (corrected):
* My first deblur prototype used an odd-only blur-length grid, and the true length was even.
* I first concluded the deblur "does not work on compressed video". The real problem was the plate
  OCR's sensitivity to ringing; RapidOCR re-reads fixed it.
* My first exact-frame test assumed codec-exact pixel values.
* Twice I committed while ruff/mypy failed (exit codes masked by `| tail`). Each was fixed in the
  next commit, and commits now go through a script that stops on failure.
* The "two early readings" rule in the OSD start logic was redundant (shown by mutation testing).
* The first packaged-app self-test passed on a broken bundle (too weak). It now executes NMS and OCR.
* The first UI race test passed without the fix (the delay was too short to matter).
* The estimator's default rates first claimed a measurement that hadn't been made. They were then measured.

## 4. Infrastructure, process and code improvements
CI workflow (lint · mypy · notebook sync · full tests incl. Playwright · app self-test · Windows
build, artifact upload, failure summaries posted on the PR); ruff + mypy configuration, both clean;
deterministic notebook generation; `tools/mutation_check.py`; `tools/soak.py`; release-credential
tests; PyInstaller spec + Windows build script with self-test; single sources for presets,
time resolution, face enrolment, plate matching and the clock-reading rule; documentation set
with index and lifecycle status (`docs/INDEX.md`).

## 5. Commits (all on `origin/feature/enterprise-cctv-platform`)
See the PR for the final list; at the time of writing, in order:
ff668a2, 6b9ab07, 4f10092, c074864, 3a229b2, b05705a, b05c395, c278bad, 8b962fa, 73a8270,
97ab2a3, babea31, bd21208, 0554a4a, 31efd72, abe7dd0, 4907d13, and this status commit.

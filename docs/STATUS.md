# Round status – Drishti-AI police investigation workstation

Status: **current** · Branch `engineering/foundation` (draft PR #2) · Base: `main` @ `7e6d15f` · Previous
round: `docs/archive/STATUS-2026-11-02-enterprise-platform.md`.
Environment of all local measurements: Linux, Intel Xeon 2.8 GHz, 4 vCPU, 15 GB RAM, no GPU,
Python 3.11, ffmpeg with libx264, torch 2.15 CPU, ultralytics 8.4.204. `NODE_ENV` and other
`CCTV_*`/`HF_*` variables were checked unset before every local run (`env | grep`), and CI
prints them.

## 1. Implementation and audit summary
The single-folder CCTV triage app became **Drishti-AI**, an offline investigation workstation for
police, without changing the single-folder screens:
* **Cases** (`cases.py`, `investigation.py`): one case, any number of seized sources (DVR, NVR, pen
  drive), each analysed by the unchanged pipeline into its own folder; clock corrections per
  source and per camera that never edit the DVR time; every edit audited (hash chain).
* **Acquisition** (`ingest.py`, `merkle.py`): SHA-256 of every seized file, RFC 6962 Merkle root,
  lossless MP4 playback copies checked by codec and packet count; raw `.264/.265` DVR streams made
  analysable (they were not – finding 1).
* **Search across the case** (`journey.py`, `reid.py`, `case_api.py`): cross-camera timeline on the
  calibrated clock; person appearance search (built-in descriptor, optional ONNX model); clothing
  colours; describe-it search (SigLIP-2); face sightings across sources by similarity; plates
  exact or within one character; journey with travel times, offline route map, highlight reel with
  calibrated and DVR time burnt in.
* **Forensic Studio** (`enhance.py`): 10 filters + multi-frame ECC stacking, validated parameters,
  reproducible chain log, input/output hashes, audit entry; checked to reproduce bit for bit.
* **Court bundle** (`section63.py`): one Section 63 BSA 2023 certificate per device (PDF + HTML +
  JSON) with the hash report, stored Part A/Part B declarants, `checksums.sha256`, `README.txt`,
  verification. Wording follows the Schedule of the Bharatiya Sakshya (Second) Bill 2023 (primary
  source fetched and hashed; Gazette text UNVERIFIED).
* **App** (`app/case_api.py`, `static/case.js`): Quick investigation (drag & drop located by
  content, system dialog where it can open, presets, one-click run), Cases, Timeline & search,
  Journey, Forensic Studio, Court bundle; `run_app.bat` one-click launcher with `--self-test`.
* **Robustness**: stored paths follow a moved case folder; recordings come from the source's
  current folder; a moved seized folder is relinked only after its SHA-256 match; memory governor
  (85 %/93 %); trial limit per case run.
* **Documents**: CASES, REID, FORENSIC-STUDIO, legal/BSA-2023-SECTION-63, EVIDENCE, DESKTOP-APP,
  USER-GUIDE (Part A for officers), ARCHITECTURE, TESTING, DECISIONS D14–D23, COMMERCIAL-READINESS,
  and `docs/gem/` (technical specifications, Make in India declaration, terms, EULA, police
  handover runbook – templates with explicit seller blanks, no certification claims).

Gate on the final code (`42e3672`), each executed, not inferred:
* `pytest -q` in a clean worktree checkout: **155 passed, 2 deselected, EXIT 0** (10 min 16 s; the 2
  deselected are the `release` tests that need the owner's credentials, docs/CREDENTIAL-TASKS.md);
* `ruff check .` clean; `mypy` clean in a CI-equivalent environment; notebook regenerated and
  identical (`git diff --exit-code`); `python -m cctv_analysis.app --self-test` exit 0 with every check
  `True` (incl. case API, certificate PDF, forensic filters, appearance vector);
* `tools/mutation_check.py` (fixed tool, finding 27): result in section 5;
* GitHub CI on `42e3672`: lint, test (14 min on a clean runner, SigLIP-2 downloaded and cached) and
  windows-app green. The Windows job's own report (PR #2 comment 6705066144) shows
  `run_app.bat --self-test` and `run_app.bat --install --self-test` both ending with every check
  `True` – the second in a fresh `.venv` (Python 3.11.9, torch 2.15.0+cpu, app 1.0.0) built by the
  launcher itself, with the bundled LGPL ffmpeg on PATH (encoder `libopenh264`).

## 2. Issues found and how each was fixed (with evidence)
| # | Issue | Fix | Evidence |
|---|---|---|---|
| 1 | **Raw `.264/.265` DVR streams unanalysable**: probe gave duration 0 and every seek returned the first frame | duration = packet count ÷ decoder fps; output-side seek; decode through the lossless remux (D17) | `test_raw_h264_stream_without_timestamps_is_timed_by_its_frames`: (fps, frames, duration) = (10, 60, 6.0), frame at 4 s ≠ frame at 0 s; the raw TrafficCam source of the case tests gets OSD times within 3 s of its clock |
| 2 | Evidence copy cut of a raw stream began **after** the event start (copy starts at the next keyframe) | cut starts 3 s early; notes say so | `test_a_copy_cut_of_a_raw_stream_contains_the_event_start`: the event's first frame is in the cut; with the 3 s removed (mutation) it is not |
| 3 | One certificate listed every device, but Part A is signed by the person in charge of **one** device | one certificate per source, own records and acquisition hashes, stored declarants (D19) | `test_each_device_gets_its_own_certificate_with_its_own_declarant`; two mutations (all exhibits / all devices) fail it |
| 4 | **Face sightings across sources used the identity number**, but P-numbers are per source: "Shop P0001" found the Bank's P0001 – another person | match the picked face in other sources by SFace similarity ≥ 0.42 (D20) | `test_case_sightings.py`: Lena = Shop P0001 = Bank P0002, Bank P0001 = Messi; following Shop P0001 finds only Lena's window in the Bank; the old rule as a mutation fails the test |
| 5 | Plate sightings had a private edit-distance copy and a different normalisation (`isalnum` keeps non-ASCII) | `plates.normalize` / `plates.within_edits` | `test_a_plate_is_found_on_both_dvrs_exact_or_within_one_edit` (`mh 12 ab-1234` = exact) |
| 6 | **Stored absolute paths break when a drive letter changes or a case moves** (all pictures and recordings) | `storage.resolve`/`rel_to`, `Source.recording`, `Case.relink_source` with SHA-256 check (D21) | `test_a_moved_case_and_a_new_drive_letter_keep_working` (moved case + seized folders; wrong folder and one changed byte refused; reel from the new place); mutations of the fallback and of the hash check both fail it |
| 7 | A single-folder evidence package **silently** left out the original segment when the recording was missing | the event's notes and the incident report say NOT INCLUDED and why | `test_an_evidence_package_without_its_recording_says_so` |
| 8 | Quick investigation **created the case before validating** the run options (preset "turbo" left a half-made case) – found by my own test | `run_request` validates first | `test_quick_investigation_checks_every_folder_before_creating_anything` failed before, passes after; mutation of the duplicate-folder check fails it |
| 9 | The global `after_request` overwrote a route's stricter CSP (certificate preview) | `setdefault` | the bundle test failed on the CSP header before, passes after |
| 10 | Trial limit in case runs counted per source (5 × N videos) | one budget per run (`max_videos_total`), per-source hook removed (D22) | `test_the_trial_limit_counts_videos_of_the_whole_run`; mutation fails it |
| 11 | Case-run progress bar jumped backwards (job recomputed the percent from the current source's videos) | overall percent kept for case runs | `test_case_run_progress_is_the_overall_percent_not_one_source_s`; mutation fails it |
| 12 | Video route compared Windows `\` paths (events) with `/` paths (ingest): every recording 404 on Windows | separator normalised | `test_recordings_play_…` asks for `day1\ch01_copy.mp4` → 200 |
| 13 | Without a display every *Add folder…* spawned a helper process and the browser logged a 501 console error | `native_dialog()` reported by `/api/fs/roots`; UI goes straight to the built-in browser (D23) | `test_the_system_folder_dialog_is_offered_only_where_it_can_open` (no subprocess started); the UI test's console is clean; mutation fails the test |
| 14 | Timeline silently truncated at its limit | `total` returned, UI says what is left out | `test_timeline_levels_are_the_pipeline_levels` (limit 1 → total = all) |
| 15 | Re-ID descriptor weak under background and lighting changes | background suppression, exposure normalisation, soft binning (hand-v2) | `docs/REID.md`: rank-1 17/17 (0.2 s apart) and 17/23 = 74 % (2 s apart) on real pedestrians |
| 16 | Notebook would break when an embedded module imports a non-embedded one | build fails with the module named | positive control in the previous commit; notebook regenerated with every change |
| 17 | Duplicate helpers (`results.rel` / `journey._rel`); level names undefined (I first used `INFO`) | `storage.rel_to`; `rules.LEVELS` | grep: one definition each; level test |
| 18 | `run_app.bat` assumed `python` on PATH with packages, no ffmpeg, no check | rewritten: first Python with the packages (`.venv`, `python`, `py -3`), offer to install (or `--install`), bundled ffmpeg on PATH, `--self-test`; CRLF enforced by `.gitattributes` | CI windows job: `run_app.bat --self-test` and `run_app.bat --install --self-test` |
| 19 | Three mutation-check entries no longer matched the code after refactors (would have been SETUP-ERROR) | updated; new mutations for the new guards | `tools/mutation_check.py`: every original found exactly once (checked), results in section 5 |
| 20 | Describe-it search could not be turned on after a case was analysed: finished videos are skipped, so the index stayed empty and the advice "run again" did nothing | `semantic.backfill_index` embeds the saved keyframes of finished events during the next run | `test_describe_it_search_can_be_turned_on_later_without_analysing_again`: 0 videos re-analysed, ≥ 3 keyframes added, search answers; mutation fails it |
| 21 | A stack-only still was reported "reproducible" without any check (nothing to re-run) | every save decodes the frame(s) from the recording again, re-runs stack and filters and compares the hash; stored as `reproduced_from_recording` and audited | `test_a_saved_still_is_reproduced_from_the_recording_or_flagged` (a simulated drifting decode is reported, a 3-frame stack reproduces); mutation fails it |
| 22 | Text search re-implemented the ranking of `semantic.search_index` | reuses it | one ranking function |
| 23 | **Mutation check found the certificate-root check unguarded**: a forged root was caught only by the checksum list, so removing the root check survived | test adds a consistent forgery (checksum list rewritten to match): only the recomputed Merkle root exposes it | the mutation now fails `test_checksums_verify_with_sha256sum_and_tampering_is_detected` |
| 24 | Journey map labels near the right edge were cut off (seen in the screenshots) | labels anchored to the left of dots in the right part of the map | screenshot `5-journey.png` |
| 25 | **The memory governor (85 %/93 %) had no test at all** | `tests/test_resources.py`: batch sizes, waiting with a time limit, appearance indexing under 99 % pressure gives the same vectors in batches of one | 3 tests; mutation of the 85 % rule fails them |
| 26 | The hash report gave each original segment the event's span although a stream copy starts at a keyframe (up to a few seconds earlier) | footnote on the certificate (HTML and PDF) | `test_certificate_follows_the_schedule_and_lists_real_hashes` asserts the sentence |
| 27 | **`tools/mutation_check.py` could leave mutated bytecode behind**: a same-size mutation restored within the same second left a `.pyc` that still matched the restored source (whole-second mtime + size), so later runs executed the mutated code – found when a full run failed `test_indian_format_fit` after a mutation run (pyc 08:46:24.619, source restored 08:46:24.749, both 24 458 bytes) | mutated runs use `PYTHONDONTWRITEBYTECODE=1`; the file's bytecode is dropped before the base run and after the restore | positive control: with the old tool the next normal run of the test fails (reproduced at 09:19:25), with the new tool it passes; all `__pycache__` of both trees deleted |

## 3. Known limitations and where I was wrong
Limitations (not rounded up):
* **Legal text**: the certificate follows the Bill's Schedule; the Gazette text of the Act could not
  be retrieved (503) – UNVERIFIED, legal cell must compare. The Make in India order text could not
  be retrieved either; the declaration template states no percentage and marks the thresholds
  UNVERIFIED.
* **YOLO is AGPL-3.0**: selling needs the owner's decision (unchanged from the last round). No
  LICENSE file. The name "Drishti-AI" has not been searched for trademarks.
* **Re-ID** is colour/texture based: people dressed alike match; 74 % rank-1 at 2 s apart on one
  real clip; no cross-camera real-world dataset was available. The API test proves the cross-source
  mechanics on identical footage copied to two NVRs, not re-ID quality.
* **Describe-it search** was checked with two discriminative queries on synthetic + `vtest.avi`
  footage, not on real CCTV of Indian streets.
* **Drag & drop** of real OS folders is not automated: the UI test drops simulated
  FileSystemEntry objects (the server-side location by content is real). Location searches the
  parents of recent folders, the home folders and drive roots to depth 4 within 6 s.
* **`run_app.bat`**: CI runs `--self-test` and `--install --self-test` on Windows (detected Python,
  then a fresh `.venv` install); the interactive Y/N prompt itself is not automated. The system
  folder dialog on Windows is not exercised (CI is headless).
* UI in English only; the map is a plot of the cameras' positions, not a road map; GPU speeds not
  measured; playback copies double the disk use of non-MP4 sources; *Remove source* (browser
  confirm dialog) is not covered by the UI test.

Where I was wrong this round:
* My first certificate design (one certificate for all devices) was legally wrong (finding 3).
* My first `sightings` treated equal face numbers as the same person (finding 4) – caught by the
  test I wrote for it.
* My first Quick investigation endpoint validated after creating the case (finding 8).
* I used level `INFO` in the first timeline API; the pipeline's levels are HIGH/REVIEW/LOW/NORMAL.
* A CLAHE test expectation was wrong and one assertion was vacuous (`or True`); both corrected.
* My drag & drop test fake called its callback synchronously and recursed forever; Playwright's
  `wait_for_function` with a plain string violated the page's CSP – both test-code errors, fixed.
* Commit `ef9ede8` was pushed after the targeted suite only; its full run was confirmed by CI
  (lint, test, windows-app green) rather than locally first. One local background full run was
  killed by a sandbox restart and had to be repeated.
* Local mypy (in `.venv`) reports stub errors that CI's environment (ruff/mypy/nbformat only)
  does not; all type checks quoted here use a CI-equivalent environment.
* In the new governor test I expected batch 4 at 95 % RAM; the rule (one halving per 5 points over
  85 %, plus one) gives 2 – my arithmetic, test corrected. In one API test I used the source id
  `mall-gate` for a folder whose id is `mall-gate-nvr`.
* My first full mutation run (35/36 at `c2ddde3`) ran partly with the stale mutated `plates` bytecode
  of finding 27. Each verdict still compared a passing base run with a failing mutated run, but
  the run was repeated end to end with the fixed tool (section 5).

## 4. Infrastructure, process and code improvements
* CI: test-model cache key covers the new case tests (SigLIP-2 is downloaded once and cached).
* Self-test now also checks the case API, the certificate PDF, the forensic filters and the
  appearance vector (positive control: a broken PDF builder makes it exit 1).
* `app/common.py` holds the request helpers once; case endpoints live in their own module
  (`case_api.register`), registered by the server; UI split into `app.js` (helpers, single-folder
  screens) and `case.js` (case screens) with a `TAB_HOOKS` seam.
* Mutation harness: 3 entries repaired after refactors, 14 added for the new guards (23 → 37
  entries), and it can no longer leave mutated bytecode behind (finding 27).
* CI's Windows job now executes the launcher twice (detected Python; fresh `.venv` install) and
  posts its report on the pull request, so the result can be read and quoted.
* Documentation: index updated with lifecycle status, previous status archived, decisions D19–D23.

## 5. Mutation check and commits
`tools/mutation_check.py` on `42e3672` (clean worktree, fixed tool): **37/37 mutations killed**, exit 0 (each guard removed in turn makes its test fail; the test passes on the real code first). The worktree was clean afterwards (`git status`).

All commits are on `origin/engineering/foundation` (checked with `git ls-remote` after each push) and
`main` was fast-forwarded to the last one at round close. CI cancels a run when a newer commit is
pushed to the same branch; "superseded" commits are covered by the next green run, which tests the
cumulative state.
| commit | summary | CI |
|---|---|---|
| 20b06e3 | investigation core: cases, ingest, re-ID, forensic filters, journey, Section 63 bundle, raw-stream fix | superseded by the next push (concurrency cancel) |
| ef9ede8 | one Section 63 certificate per device, stored declarants, bundle README, every certificate verified | green |
| f441ea2 | case API and screens, Quick investigation, cross-source face/plate sightings, moved drives (relink), run_app.bat, self-test, docs/gem | green |
| c2ddde3 | describe-it search can be enabled after analysis (keyframe backfill); stills re-verified from the recording | superseded by the next push |
| 709190b | certificate-root and memory-governor tests, keyframe note on the certificate, map labels | superseded by the next push |
| b27ed1f | `run_app.bat --install`, executed by the Windows job | green |
| 6fcdb7a | Windows job posts the launcher evidence on the PR | superseded by the next push |
| 42e3672 | mutation_check never leaves mutated bytecode behind | green |
| (this commit) | round status | – |

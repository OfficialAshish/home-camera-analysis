# Round status – review fixes: Forensic Studio, plates, AI providers, face modes, filters, folders

Status: **current** · Branch `engineering/foundation` (draft PR #3) · Base: `main` @ `b147e0a` (PR #2
merged, plus the owner's commits `7651ce2` and `b147e0a`) · Previous round:
`docs/archive/STATUS-2026-11-05-drishti-workstation.md`.
Environment of all local measurements: Linux, 4 vCPU, 15 GB RAM, no GPU, Python 3.11, OpenCV
4.x (pip wheel), torch CPU. Local runs used `CCTV_TEST_MODEL_DIR` (the model cache) and
`PYTHONUNBUFFERED`; no `NODE_ENV`, `HF_*` or other `CCTV_*` variable was set (`env | grep`
before each full run). Every unit below was validated in a clean git worktree with only that
unit's changes applied before it was committed.

## 1. Implementation and audit summary
The owner's review asked for nine things; each is done end to end (backend, API, UI, tests, docs):
1. **Forensic Studio** (`enhance.py`, `app/case_api.py`, `static/case.js`): zoomable original and
   enhanced viewers (wheel at the pointer, drag, Fit / 1:1 / 2–16×, real pixels), a region dragged
   on the frame or given by *Enhance face / person / vehicle / plate* from an event (`GET …/roi`:
   the stored plate box or the track's detector box), picture from one frame, a region-following
   stack, or **multi-frame super-resolution** of the region (`enhance.super_resolve`), plate and
   night presets, perspective corners on the enhanced picture, *Read plate / text* (both readers
   with confidence and the format fit; save the examiner's reading), *Ask the AI* (providers with
   fallback; audited with the picture's SHA-256).
2. **Highlight reel** without repeats (`journey.reel_segments`).
3. **Filters at the top**: timeline by kind and score, *tick all shown*, bulk bundle / journey /
   reel; journey ticks by score; single-folder Results by score with *Add all matching events*.
4. **Plates**: an uncertain read is "not read" (`plate_min_confidence` 0.6, a Set up setting); the
   machine's guess and the plate box are kept; examiners type / correct / mark not readable
   (audited, counted in searches, sightings, evidence exports, Excel); *Plates not read* lists.
5. **Face mode before a run**: off / detect only / unique faces / only reference persons
   (`investigation.FACE_MODES`), reference photos copied into the case and hashed into the audit
   log; the recogniser is loaded only when faces are compared.
6. **The owner's two commits reviewed** and corrected (findings 5–13 below).
7. **Windows folder dialog** that comes up in front and returns the folder (UTF-8, waits for the
   officer), with a Windows-only UI Automation test in CI.
8. **AI model providers**: an ordered list with model names, per-provider keys, fallback, *Test*
   with a real picture (`app/ai_providers.py`, `vlm.ChainVLM`, `vlm.probe`).
9. **Tests and guards**: new suites `test_face_modes.py`, `test_native_dialog_windows.py`, new tests
   in eight files, 12 new mutation entries (49 in total), a guard that every Set up field is a
   Config field.

## 2. Issues found and how each was fixed (with the evidence)
| # | Issue | Fix | Evidence |
|---|---|---|---|
| 1 | Reel repeated the same seconds 2–3× (reported). Reproduced: 28 sightings of one 14 s recording became 28 parts. | `reel_segments` merges overlapping windows per recording; times anchored on the recording. | `test_a_reel_never_shows_the_same_seconds_twice` (no second twice, ≥ 95 % distinct frames, ≤ 16.5 s of that recording); with merging disabled it fails `28 == 28 and 28 < 28`; mutation killed. |
| 2 | A valid-format plate read was reported at **any** confidence; partial reads (`MH12AE` 0.514, car cut by the frame edge) were listed as plates in the table, search, Plates tab and Excel (reported "too vague"). | `is_reportable(conf ≥ plate_min_confidence)`; text '' + `ocr_text` guess + `box`. | Pipeline rows measured `('MH12AB1234', 0.999)`, `('', 'MH12AE', 0.514)`; `test_an_uncertain_read_is_not_the_plate…`; 2 mutations killed. |
| 3 | No way for an examiner to record the plate they read. | `reviews.plate_readings`, `PUT …/plates/…`, plate tiles, audit entries. | `test_an_examiner_types_a_plate_and_that_reading_counts`, `test_an_examiners_plate_reading_counts_in_the_case`, UI test types a not-read plate; 2 mutations killed. |
| 4 | Results written by an older version (no `ocr_text`/`box`, opened read-only, never migrated) would have failed every plate query. | `_plate_cols` selects missing columns as NULL; `/roi` says "select the plate". | The API test drops the column and the event and unread list still answer 200. |
| 5 | Super-resolution was **not bit-identical** for identical frames: OpenCV's float32 Sobel/Gaussian paths differ in the last bit with buffer alignment, so a saved still could be reported "not reproducible". | Alignment inputs and unsharp mask computed with integer Sobel and the 8-bit Gaussian. | Old float path differed in 36–38 of 60 trials, new in 0 (`/tmp/grad_trials.py`, 3 runs); `test_the_alignment_path_is_bit_exact…` fails 10/10 runs with the old code; mutation killed 3/3. |
| 6 | `7651ce2`: the dialog's owner form was **minimised**, so the dialog opened minimised / behind the browser. | Invisible, top-most, non-minimised owner; `-STA`; UTF-8. | Code review; UI Automation test in the windows-app job (result in §3). |
| 7 | `7651ce2`: 120 s timeout → 501 → the dialog button was disabled for good when an officer took longer. | Wait up to an hour; 502 with the helper's error; button hidden only on 501. | Code; `fs_pick` paths. |
| 8 | `7651ce2`: the paste-a-path prompt used inline `style` attributes – blocked by the app's CSP (`style-src 'self'`). | CSS classes. | Playwright check: a `style` attribute set by `el()` left colour default, border 0 px and logged "Applying inline style violates … 'style-src 'self''"; CSSOM `.style` works. |
| 9 | `7651ce2`: recent folders were searched themselves instead of their parent → **sibling device folders of a seized drive were no longer found**; the officer Playwright test failed (TrafficCam_North not added). cwd's parent then found identical copies all over the disk → a chooser instead of the folder. A folder could match by name alone. | Parent first, then the folder; staged search stopping at the first stage with a hit; content-only matching. | `test_a_dropped_sibling_of_a_folder_used_before_is_found`; officer Playwright flow passes; mutation killed. |
| 10 | `b147e0a`: `Config.validate()` **edited** the configuration (NVIDIA address). | `validate()` only reports. | `test_vlm_url_normalization_and_nvidia` asserts `vlm_base_url` stays empty. |
| 11 | `b147e0a`: temperature 0.2 and token budget chosen by **model-name substrings** (`"r1"`, `"mistral"`, `"reasoning"`). | Temperature 0 always; more tokens only when the answer ran out (`finish_reason`/reasoning only). | `test_reasoning_models_get_more_tokens…`. |
| 12 | `b147e0a`: `reasoning_content` was returned **as the answer**. | Never used as the answer. | Same test (fake server returns only reasoning first). |
| 13 | `b147e0a`: *Test connection* listed an invented model (`mistralai/mistral-large-4-1t-a52b`) and "(connected via /chat/completions)" as if they were models on the server. | `vlm.probe`: real model list when there is one + a vision question with a picture of "42". | `test_probe_works_without_a_model_list…`, app test with a fake server. NVIDIA format checked against its API reference (`docs/VLM.md`). |
| 14 | One provider, one key, no fallback. | Ordered providers, per-provider keys in `secrets.json`, `ChainVLM`. | `test_fallback_providers_answer_when_the_first_fails`, app test; mutation killed. |
| 15 | *Ask the AI* without any provider would have started loading (downloading) the 2B model inside a web request. | 409 `NOT_CONFIGURED` unless a provider is set or the built-in model is installed. | `test_the_studio_reads_text_and_asks…` (409). |
| 16 | `FaceEngine` loaded (and downloaded) SFace even when faces were only detected. | Lazy recogniser. | `test_detect_keeps_face_pictures_but_compares_nothing` (`recognizer_loaded False`, 0 embeddings); mutation killed. |
| 17 | One "Faces" tick box switched detection, recognition and grouping together. | Face mode before the run; the mode overrides any face option sent. | `test_face_modes.py` (4 modes on two real faces), `test_the_face_mode_decides…`; 2 mutations killed. |
| 18 | Sightings by name found enrolled (known) faces only, not reference persons. | `known_name OR watchlist` (column-checked for old results). | `test_reference_finds_only_the_reference_person`. |
| 19 | Studio worked on whole frames at screen size only. | Feature 1 above. | Super-resolution measured: PSNR 18.0 dB vs 16.6 dB (best single frame) vs 13.4 dB (median without alignment); RapidOCR reads `MH12AB1234` only after it. Studio API and Playwright tests. |
| 20 | Events and sightings had to be picked one by one. | Feature 3 above. | `test_the_timeline_filters_by_score_and_kind`, events `min_score` test, Playwright (tick all, score sliders); mutation killed. |
| 21 | A Set up field that is not a Config field would be noticed only on *Start*. | Guard test over `features.FIELDS`. | Fails when an unknown key is added (control run). |
| 22 | The plates commit `28efcfa` left `test_pipeline_uses_local_server_for_descriptions_and_plates` expecting a VLM read fused at 0.595 to be reported. | The AI read is the guess unless confident; test and docs updated (`60223be`). | Found by the full suite at `8ac3ebe` (1 failed, 176 passed). |
| 23 | CI at `8cb5c44`: the Windows dialog test did not find the dialog among the desktop's top-level windows (UI Automation can list an owned dialog under its owner). | `221e295`, then `4475ab6`: the helper process is found by its command line, its windows and their children are checked for the dialog class `#32770`, OK is pressed by its control id. | CI comment at `8cb5c44` ("NOT FOUND"); result in §3. |
| 24 | CI at `60223be` and `4475ab6`: the dialog **was found on the runner's screen** ("Browse For Folder", 334×327, `offscreen=False`), but pressing OK through the element with AutomationId 1 raised InvalidOperationException, so the dialog stayed open. | `da8fdc2`: the OK button by control type + id, else the dialog gets WM_COMMAND IDOK (what a click sends); daemon request thread and a cleanup step so a failure never leaves the job waiting. | CI comments at `60223be`, `4475ab6`, `48f8afb`; result in §3. |
| 25 | Found on screenshots of a real run: Read showed a confidence for an empty read ("– (0.961)"), prefilled the plate box with any read ("1"), and the zoom bar zoomed the original away from the selected plate. | `48f8afb`. | Screenshot `4-forensic-studio-plate-super-resolution.png`; Studio API and Playwright tests pass. |

## 3. Known limitations (stated as measured) and where I was wrong
* **Windows folder dialog**: proven by the windows-app CI job at `da8fdc2` (GitHub windows-latest
  runner): "FOUND 'Browse For Folder' 334x327 offscreen=False · 1 passed" – the dialog the app
  opens is on screen, OK returns the pre-selected folder whose name has spaces, brackets and
  Devanagari. No manual check on a physical desktop was possible here; the runner's desktop is the
  only Windows screen this proof reached.
* A correct deblurred plate read at 0.57 (9 px motion blur) is now "not read" with the guess shown
  – the examiner confirms it; an AI read of an unreadable plate usually ends as a guess too.
* Super-resolution was measured on a synthetic moving plate (uniform motion, Gaussian noise,
  JPEG); real footage with rolling shutter, blur and IR glare gains less. No learned SR is used.
* The face region for *Enhance face* is the top third of the person box (the face box is stored
  in detection-frame pixels whose size is not recorded); the officer can redraw it.
* Changing the face mode does not re-analyse finished videos unless *analyse again* is ticked.
* No NVIDIA key, Ollama or LM Studio was available: providers were tested against fake
  OpenAI-compatible servers that check every request; the NVIDIA wire format rests on its API
  reference.
* Where I was wrong this round: (a) my first locate fix still let the search from cwd's parent
  find identical copies (test failed, then the staged search); (b) two plate tests assumed one
  plate row per car (the cars have two tracks); (c) the UI test assumed the synthetic clip yields
  an unread plate under the fast preset (both tracks read 0.999 / 1.0); (d) the region-drag UI
  test used a bounding box from before the page scrolled; (e) the pipeline test with the limit at
  1.0 assumed every read stays below 1.0 (made per-row); (f) my first bit-exactness test fed
  offset frames but OpenCV allocates the float buffers itself – its mutation **survived**, the
  test was replaced; (g) I validated the plates commit without `test_vlm_providers.py` and it
  broke a test there (finding 22); (h) the first UI Automation test searched only top-level
  windows (finding 23); (i) I concluded from a check one minute too early that the windows job at
  `60223be` was hanging and wrote that into commit `4475ab6`; it had finished, with the dialog found
  and the OK press failing (finding 24).

## 4. Infrastructure, process and code improvements
* Each unit committed only after it passed lint, mypy, the notebook check and its suites in a
  clean worktree with only that unit applied (hunk-level staging); the full suite and the full
  mutation check ran on the final commit.
* Windows CI: the dialog test runs on the runner and its result (with the windows on screen on
  failure) is posted on the PR with the launcher evidence.
* Mutation check: 12 new rules (49 in total).
* Guard that the Set up form and Config cannot drift apart.
* Results opened read-only from older versions are covered for the new plate columns.

## Gate of record
* Full suite at `60223be` (clean worktree): **177 passed, 1 skipped** (the Windows-only dialog test),
  2 deselected (`-m release`), 0 failed, 602 s. The run at `8ac3ebe` had 1 failure (finding 22).
* Mutation check at `60223be`: **49/49 killed** (`tools/mutation_check.py`, all entries).
* CI at `60223be`: lint ✔, test ✔ (14.5 min); windows-app: launcher self-tests ✔, dialog opens on screen ✔, OK press ✘ (finding 24).
* Commits after `60223be` (`194aff8`, `48f8afb`: Studio API/UI; `4475ab6`, `da8fdc2`: Windows test only) were checked with their suites (Studio API, officer Playwright flow) and by CI's full test job.
* CI at `da8fdc2`: **lint ✔, test ✔ (the full suite), windows-app ✔** (launcher self-test from source and from a fresh `.venv`, folder dialog test passed).
* Lint (ruff), mypy, notebook check, `node --check`, `--self-test`: clean at every commit.

## 5. Commits (all on `origin/engineering/foundation`)
- `74e4b85` [AI] Providers: ordered AI model providers with model names and fallback — one provider and one key could not fall back, and Test listed an invented model
- `efb62cb` [APP] Folders: the Windows folder dialog comes up in front and dropped folders are found next to the ones used before — 7651ce2 hid the dialog behind a minimised owner and stopped searching beside recent folders
- `4d1e3f7` [CASES] Reel: every second of footage once — sightings sharing a recording each became a part, so a reel repeated the same seconds and time stamps 2-3 times
- `28efcfa` [PLATES] Confidence and examiner readings: an uncertain read is "not read" and an examiner types the plate — valid-format reads were reported at any confidence and partial reads were listed as plates
- `8cb5c44` [STUDIO] Forensic Studio: zoomable viewers, region focus, multi-frame super-resolution, opened from a face, person, vehicle or plate, OCR read and Ask the AI — enhancement worked only on whole frames at screen size
- `ffa61c0` [FACES] Face mode chosen before a run: off / detect only / unique faces / only reference persons — one "Faces" tick box switched on detection, recognition and grouping together, and the recogniser loaded even when only detecting
- `8c458c5` [APP] Set up: the plate confidence limit is a setting, and every setup field is checked against Config — a form key that is not a Config field was only noticed when an officer pressed Start
- `808b141` [CASES] Filters by score and kind at the top, tick all shown, bulk bundle / journey / reel; journey ticks by score — events and sightings had to be picked one by one
- `4abdc3b` [DOCS] Decisions D24-D26 and architecture for plate readings, face modes and super-resolution; the uncertain-plate test no longer depends on a read staying below 1.0
- `8ac3ebe` [TOOLS] Mutation check covers the 12 new rules; the bit-exactness guard now really fails on the float path — the first version survived its mutation
- `221e295` [CI] Windows dialog test: search the dialog among all UI Automation descendants — CI at 8cb5c44 reported NOT FOUND: an owned dialog is listed under its owner window, not as a top-level window
- `60223be` [PLATES] An AI plate read counts only when confident, like any reader — the plates commit left the VLM pipeline test expecting a 0.595 vote to be reported (caught by the full suite, not by that commit's checks)
- `194aff8` [STUDIO] /roi answers only t and roi — it also returned the frame size and the echoed kind, which the client never uses
- `4475ab6` [CI] Windows dialog test finds the dialog through the helper's process and never leaves the job waiting — the run at 60223be hung after the dialog search (a non-daemon request thread waited for the open dialog)
- `48f8afb` [STUDIO] Read shows "nothing read" instead of a confidence without text, the plate box is prefilled only with a format-fitting read, and zooming the original centres on the region — found on screenshots of a real run
- `da8fdc2` [CI] Windows dialog test presses OK reliably — CI now finds the dialog on screen ("Browse For Folder" 334x327, not off screen) but pressing the element with id 1 raised InvalidOperationException
- this document (`[DOCS] Round status …`)

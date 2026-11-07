# Drishti-AI — technical specifications (for GeM product listing)

Status: **current (template)** · Product version: `cctv_analysis.__version__` (1.0.0 on this branch) ·
Every figure below is either a property of the code (with the module that implements it) or a
measurement (with the test or document that measured it). Items in `[[SELLER: …]]` are filled in
by the seller; nothing here is a certification. Before listing, the seller's legal cell must
review this sheet together with `docs/legal/BSA-2023-SECTION-63.md`.

## 1. Identification
| Item | Value |
|---|---|
| Product name | Drishti-AI – offline CCTV investigation workstation (software) `[[SELLER: confirm the trade name is free to use – no trademark search has been made]]` |
| OEM / seller | `[[SELLER: legal name, address, GSTIN, GeM seller ID]]` |
| Category | Software – video analytics / digital-evidence triage (offline, on-premise) `[[SELLER: GeM category code]]` |
| Delivery | Windows installer folder (`CCTVAnalysis.exe`, PyInstaller build) or source install with the one-click `run_app.bat` |
| Licence model | Per-computer offline licence key (Ed25519-signed, optional machine binding, expiry) – `docs/LICENSING.md`; trial mode analyses at most 5 videos per run |

## 2. Deployment and hardware
| Item | Value |
|---|---|
| Operating system | Windows 10 / 11, 64-bit (packaged build); Linux and macOS from source |
| Network | **None required.** The app listens on 127.0.0.1 only; no footage, picture or result leaves the computer. Internet is needed once to download models, unless they are pre-loaded |
| CPU-only operation | Yes. Measured on 4 vCPU / 15 GB RAM / no GPU (`docs/PERFORMANCE.md`): motion pass 13.3× real time (fast preset), detector 14.1 frames/s (fast) and 2.9 frames/s (balanced) |
| GPU | Optional (NVIDIA CUDA build of PyTorch); not measured in this repository |
| Memory | 8 GB minimum `[[SELLER: confirm on the target laptop]]`; above 85 % RAM in use the app halves its batch sizes, above 93 % it processes one item at a time (`resources.MemoryGovernor`) |
| Disk | Analysis outputs plus lossless playback copies of non-browser formats (a playback copy is as large as its original) – the cases folder can be placed on any disk |

## 3. Input
| Item | Value |
|---|---|
| Video containers / streams | `.mp4 .avi .mkv .mov .m4v .dav .h264 .264 .h265 .265 .hevc .ts .mts .m2ts .flv .asf .wmv .3gp .mpg .mpeg .webm .vob .ps .mpeg4` (`config.VIDEO_EXTENSIONS`) |
| Raw DVR streams | `.264/.265` without container are timed by their frame count and decoded through a lossless remux (`video_io`, decision D17; `tests/test_multi_folder_ingest.py`) |
| Multiple seized devices | Any number of sources per case, one per DVR/NVR/pen drive, each with its own clock correction (`cases.py`) |
| Recording time | File name patterns (Hikvision, Dahua, generic NVR …), else the DVR clock printed on the picture (OCR, cross-checked), else metadata/mtime (marked approximate) – `docs/TIMELINE.md` |

## 4. Functions (each with the test that proves it)
| Function | Implementation | Proof |
|---|---|---|
| Acquisition hash of every seized file (SHA-256) + Merkle root (RFC 6962) | `ingest.py`, `merkle.py` | `test_multi_folder_ingest.py`, `test_section63_bsa.py::test_merkle_root_equals_rfc6962_for_every_size` |
| Lossless playback copies (`ffmpeg -c copy`, packet count checked) | `ingest.remux` | `test_proprietary_formats_get_a_lossless_playback_copy` |
| Clock calibration per source and per camera, never editing the DVR time | `cases.py` | `test_dvr_times_come_from_each_wrong_clock_and_calibration_restores_true_order` |
| Motion events, people/vehicle/object detection and tracking | `motion.py`, `detector.py` (Ultralytics YOLO) | `test_pipeline_e2e.py` |
| Faces: detection, grouping, search by photo, cross-source following | `faces.py`, `journey.face_identities` | `test_face_search.py`, `test_case_sightings.py` |
| Number plates (Indian formats), fuzzy search across sources | `plates.py`, `journey.sightings` | `test_plates.py`, `test_case_sightings.py` |
| Person appearance search across cameras | `reid.py` | `test_person_reid.py`: rank-1 17/17 at 0.2 s and 17/23 (74 %) at 2 s apart on real pedestrian footage (`docs/REID.md`) |
| Natural-language ("describe it") search | SigLIP-2 (`semantic.py`, `case_api.case_text_search`) | `test_case_api.py::test_natural_language_search_ranks_the_described_scene_first` |
| Cross-camera timeline, journey, highlight reel with burnt-in calibrated and DVR time | `journey.py` | `test_journey_and_highlight_reel_follow_calibrated_time` (time read back from the reel by OCR) |
| Forensic enhancement with reproducible filter log | `enhance.py` | `test_forensic_enhancement.py` (`docs/FORENSIC-STUDIO.md`) |
| Court bundle: one Section 63 BSA 2023 certificate per device (PDF + HTML), hash report, `checksums.sha256`, verification | `section63.py` | `test_section63_bsa.py`, `test_case_api.py::test_court_bundle_has_one_certificate_per_device_and_verifies` |
| Tamper-evident audit log (hash chain) | `audit.py` | `test_guards_unit.py::test_audit_chain_detects_edit_delete_and_reorder` |

## 5. Security
Local-only server with a per-launch token, Host-header check, path confinement, CSP without inline
script, request limits, rate limits, one analysis at a time, secrets never returned – the full
table with the test of every control is in `docs/DESKTOP-APP.md`; `tools/mutation_check.py` removes
each control in turn and requires its test to fail.

## 6. Standards referred to
| Standard | Use |
|---|---|
| SHA-256 (FIPS 180-4) | every hash in ingest records, bundles and certificates |
| RFC 6962 §2.1 Merkle tree hash | roots over acquisition and exhibit hashes |
| Bharatiya Sakshya Adhiniyam 2023, Section 63 and its Schedule | certificate wording (source and its limits: `docs/legal/BSA-2023-SECTION-63.md`) |
| Digital Personal Data Protection Act 2023 | local processing, redaction, audit log – the product supports, but cannot enforce, lawful use |

## 7. What the product does not do (stated for the evaluator)
* It does not certify anything: signatures and declarants' particulars are never filled in by software.
* Scores, similarities and AI descriptions rank footage for a human; they are not identification.
* The user interface is in English only `[[SELLER: Hindi / regional languages are not implemented]]`.
* No live camera streams: it works on recorded, exported footage.
* Accuracy figures above are from the stated test footage, not from a field trial
  `[[SELLER: attach field-trial results if any]]`.

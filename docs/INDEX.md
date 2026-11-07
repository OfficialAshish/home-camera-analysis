# Documentation index

Everything under `docs/` is meant to match the code; a mismatch is a defect. Lifecycle:
**current** = describes the code on this branch; **current (template)** = a document the seller
completes (blanks marked `[[SELLER: …]]`); **archived** = kept for history only (`docs/archive/`).

| Document | Status | For | Content |
|---|---|---|---|
| [STATUS.md](STATUS.md) | current | everyone | round summary (round of 2026-11-07: Studio super-resolution, plate readings, AI providers, face modes, filters, folder dialog): what was done, evidence, limitations, commits |
| [archive/STATUS-2026-11-05-drishti-workstation.md](archive/STATUS-2026-11-05-drishti-workstation.md) | archived | everyone | summary of the Drishti-AI workstation round (PR #2) |
| [archive/STATUS-2026-11-02-enterprise-platform.md](archive/STATUS-2026-11-02-enterprise-platform.md) | archived | everyone | summary of the previous round (enterprise CCTV platform, PR #1) |
| [USER-GUIDE.md](USER-GUIDE.md) | current | investigating officers, shop/mall/security staff | Drishti-AI case workflow and the single-folder screens, step by step |
| [TIMELINE.md](TIMELINE.md) | current | users, developers | how recording times are resolved, the reel-order defect, retime |
| [CASES.md](CASES.md) | current | investigators, developers | cases with several seized sources, acquisition hashes, playback copies, clock calibration, face modes and reference persons, cross-source face/plate/appearance sightings, moved drives (relink), app endpoints |
| [REID.md](REID.md) | current | investigators, developers | person appearance search: method, **measured accuracy and limits** |
| [FORENSIC-STUDIO.md](FORENSIC-STUDIO.md) | current | investigators, examiners | zoom viewers, region focus, multi-frame super-resolution, filters, read / ask the AI, the audit log, measured effect |
| [legal/BSA-2023-SECTION-63.md](legal/BSA-2023-SECTION-63.md) | current | legal reviewers | statutory text the certificate follows, source and what is filled in |
| [PLATES.md](PLATES.md) | current | users, developers | number plates: switches, method, confidence limit, examiner readings, **measured accuracy and limits** |
| [EVIDENCE.md](EVIDENCE.md) | current | investigators | evidence packages, the Section 63 case bundle, redaction, reviews, audit log |
| [VLM.md](VLM.md) | current | users, developers | AI model providers (OpenAI-compatible, NVIDIA, local) with model names, keys and fallback; wire format and sources |
| [DESKTOP-APP.md](DESKTOP-APP.md) | current | developers, IT | app screens, security model, data locations, packaging |
| [LICENSING.md](LICENSING.md) | current | vendor | creating and issuing licences |
| [COMMERCIAL-READINESS.md](COMMERCIAL-READINESS.md) | current | owner | third-party licences (AGPL YOLO, ffmpeg), owner decisions before selling |
| [CREDENTIAL-TASKS.md](CREDENTIAL-TASKS.md) | current | owner | the only tasks that need credentials the developer does not hold |
| [ARCHITECTURE.md](ARCHITECTURE.md) | current | developers | interfaces, pipeline, modules, storage |
| [DECISIONS.md](DECISIONS.md) | current | developers | decision log D1–D26 |
| [TESTING.md](TESTING.md) | current | developers | gate, suites, mutation check, load test |
| [PERFORMANCE.md](PERFORMANCE.md) | current | developers | measured speeds and load-test results |
| [gem/TECHNICAL-SPECIFICATIONS.md](gem/TECHNICAL-SPECIFICATIONS.md) | current (template) | procurement (GeM) | technical specification sheet of the product, measured figures only |
| [gem/MAKE-IN-INDIA-DECLARATION.md](gem/MAKE-IN-INDIA-DECLARATION.md) | current (template) | procurement (GeM) | local-content self-declaration template with the component bill of materials |
| [gem/TERMS-AND-CONDITIONS.md](gem/TERMS-AND-CONDITIONS.md) | current (template) | procurement (GeM) | supply, warranty and support terms (blanks for the seller) |
| [gem/END-USER-LICENSE-AGREEMENT.md](gem/END-USER-LICENSE-AGREEMENT.md) | current (template) | procurement, legal | EULA incl. third-party licences and data protection |
| [gem/HANDOVER-RUNBOOK-POLICE.md](gem/HANDOVER-RUNBOOK-POLICE.md) | current | police IT / district nodal officer | installation, daily use, evidence handling, backup, troubleshooting |

The repository `README.md` is the overview; `cctv-analysis.ipynb` is generated from the package by
`tools/build_notebook.py` (CI fails if it is out of date).

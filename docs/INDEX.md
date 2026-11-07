# Documentation index

Everything under `docs/` is meant to match the code; a mismatch is a defect. Lifecycle:
**current** = describes the code on this branch; **archived** = kept for history only
(`docs/archive/`, none yet).

| Document | Status | For | Content |
|---|---|---|---|
| [STATUS.md](STATUS.md) | current | everyone | round summary: what was done, evidence, limitations, commits |
| [USER-GUIDE.md](USER-GUIDE.md) | current | shop/mall/security staff | using the desktop app step by step |
| [TIMELINE.md](TIMELINE.md) | current | users, developers | how recording times are resolved, the reel-order defect, retime |
| [CASES.md](CASES.md) | current | investigators, developers | cases with several seized sources, acquisition hashes, playback copies, clock calibration |
| [REID.md](REID.md) | current | investigators, developers | person appearance search: method, **measured accuracy and limits** |
| [FORENSIC-STUDIO.md](FORENSIC-STUDIO.md) | current | investigators, examiners | enhancement filters, the audit log, measured effect |
| [legal/BSA-2023-SECTION-63.md](legal/BSA-2023-SECTION-63.md) | current | legal reviewers | statutory text the certificate follows, source and what is filled in |
| [PLATES.md](PLATES.md) | current | users, developers | number plates: switches, method, **measured accuracy and limits** |
| [EVIDENCE.md](EVIDENCE.md) | current | investigators | evidence packages, the Section 63 case bundle, redaction, reviews, audit log |
| [VLM.md](VLM.md) | current | users, developers | AI descriptions, using your own model server |
| [DESKTOP-APP.md](DESKTOP-APP.md) | current | developers, IT | app screens, security model, data locations, packaging |
| [LICENSING.md](LICENSING.md) | current | vendor | creating and issuing licences |
| [COMMERCIAL-READINESS.md](COMMERCIAL-READINESS.md) | current | owner | third-party licences (AGPL YOLO, ffmpeg), owner decisions before selling |
| [CREDENTIAL-TASKS.md](CREDENTIAL-TASKS.md) | current | owner | the only tasks that need credentials the developer does not hold |
| [ARCHITECTURE.md](ARCHITECTURE.md) | current | developers | interfaces, pipeline, modules, storage |
| [DECISIONS.md](DECISIONS.md) | current | developers | decision log D1–D18 |
| [TESTING.md](TESTING.md) | current | developers | gate, suites, mutation check, load test |
| [PERFORMANCE.md](PERFORMANCE.md) | current | developers | measured speeds and load-test results |

The repository `README.md` is the overview; `cctv-analysis.ipynb` is generated from the package by
`tools/build_notebook.py` (CI fails if it is out of date).

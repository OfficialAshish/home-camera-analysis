# Commercial readiness – third-party licences and owner decisions

Status: **current** · This is an engineering summary, not legal advice.

## Components inside the product
| Component | Licence | Used for | Selling implication |
|---|---|---|---|
| Ultralytics YOLO26/YOLO11 (`ultralytics` package + weights) | **AGPL-3.0** | person/vehicle/object detection, tracking, pose | Distributing the app means distributing AGPL code. Either (a) license the whole app under AGPL-3.0 and give every customer the complete source (licence keys can then legally be removed by customers), or (b) buy an **Ultralytics Enterprise licence**, or (c) replace the detector. **Owner decision required before selling.** |
| PyTorch, torchvision | BSD-3 | inference runtime | fine |
| OpenCV (YuNet, SFace models) | Apache-2.0 / MIT | faces | fine (attribution) |
| RapidOCR (PaddleOCR models) | Apache-2.0 | DVR clock, plate re-reads | fine |
| fast-plate-ocr, open-image-models | MIT | plate reading / detection | fine; check the model-weight terms on their release pages before selling |
| Hugging Face Transformers | Apache-2.0 | built-in VLM | fine; each model has its own licence (Qwen3-VL: Apache-2.0; SigLIP-2: Apache-2.0) |
| ffmpeg | LGPL-2.1+ (LGPL build) / GPL (with x264) | decoding, cutting, encoding | the Windows build bundles an **LGPL** build (BtbN `win64-lgpl`), so no GPL x264 is shipped; the app then encodes with OpenH264 or MPEG-4 (decision D12). Ship ffmpeg's licence text and a source offer. |
| Flask, waitress, cryptography, psutil, requests, xlsxwriter, matplotlib, numpy | BSD / MIT / ZPL / Apache / PSF | app, reports | fine |
| ReportLab | BSD | Section 63 certificate PDF | fine (attribution) |
| Person re-ID model (optional `<models>/reid/reid.onnx`) | depends on the weights | appearance search, blended with the built-in descriptor | **Not bundled.** Public OSNet/FastReID weights are trained on Market-1501 / MSMT17 / DukeMTMC, whose terms allow research use only (DukeMTMC was withdrawn by its authors). The built-in descriptor (`reid.py`, own code) needs no model. Use a learned model only with weights whose training data is licensed for the buyer's use. |

The repository itself has **no LICENSE file**. Choose one deliberately, and consistently with the
YOLO decision above.

## Privacy and lawful use
Face grouping, watchlists and plate reading process personal data. In India the DPDP Act 2023
applies, and elsewhere comparable laws do. Customers should display CCTV notices, limit
retention, restrict access and use redacted exports when sharing. The product supports this
(local processing only, redacted exports, audit log) but cannot enforce it.

## Government procurement (GeM)
Templates in `docs/gem/`: technical specifications (measured figures only), the Make in India
local-content self-declaration (with the component bill of materials; the percentage is the
seller's to compute and certify), supply/warranty/support terms, the end-user licence agreement
and the police handover runbook. None of them is a certification; every `[[SELLER: …]]` blank
must be filled and the set reviewed by the seller's legal cell. The trade name "Drishti-AI" has
not been searched for existing trademarks.

## Distribution checklist (owner)
1. Decide the YOLO route (above) and add a LICENSE file.
2. `python tools/license_tool.py init`, commit `cctv_analysis/license_public_key.pem`, keep the private key offline.
3. Optional: a Windows code-signing certificate, so SmartScreen doesn't warn on the unsigned exe (`docs/CREDENTIAL-TASKS.md`).
4. Build with the *windows-app* CI job or `packaging/build_windows.ps1`; run `CCTVAnalysis.exe --self-test`.
5. Ship with `THIRD-PARTY-NOTICES` (licences of the components above).

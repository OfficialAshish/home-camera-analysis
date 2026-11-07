# Make in India — local content self-declaration (template)

Status: **current (template)** · To be completed, signed and stamped by the seller. The software
cannot know the seller's cost structure, so **no local-content percentage is stated here**; the
bill of materials below lists what the product is made of, so that the seller's auditor can
compute it.

> UNVERIFIED – open question for the seller's legal cell: the current text of the *Public
> Procurement (Preference to Make in India) Order, 2017* (as revised) and of any notification of
> MeitY / the procuring ministry for **software** could not be retrieved while preparing this
> template (dpiit.gov.in and meity.gov.in serve script-rendered pages). The class thresholds
> commonly cited for the 2020 revision – *Class-I local supplier* ≥ 50 % local content,
> *Class-II local supplier* ≥ 20 % – and the rule that the declaration must name the location(s)
> where value is added must be confirmed against the order in force on the bid date before this
> declaration is signed.

---

**Self-declaration of local content**

I/We, `[[SELLER: authorised signatory name, designation]]`, on behalf of
`[[SELLER: company legal name, registered address, GSTIN]]`, hereby declare that the item
offered – **Drishti-AI, offline CCTV investigation workstation software, version
`[[version]]`** – has a local content of **`[[__]] %`**, computed as specified in the Public
Procurement (Preference to Make in India) Order in force, and that the supplier is a
**`[[Class-I / Class-II]]` local supplier**.

The local value addition is made at: `[[address(es) of development, build, support]]`.

The local content has been calculated on the basis of the bill of materials below and the
costs certified in `[[auditor's certificate reference, where the order requires one]]`.

Place: `[[ ]]`  Date: `[[DD/MM/YYYY]]`

Signature and seal: ______________________

---

## Bill of materials (software components, from this repository)
| Component | Origin / licence | Role in the product | Local / imported `[[SELLER]]` |
|---|---|---|---|
| Drishti-AI application code (`cctv_analysis/`: cases, ingest, timeline, journey, re-ID descriptor, Forensic Studio filters, Section 63 certificate, app, licensing) | developed for the seller `[[SELLER: where and by whom]]` | the product | `[[ ]]` |
| Ultralytics YOLO (AGPL-3.0 or commercial licence – see COMMERCIAL-READINESS) | open source / licensed | object detection and tracking | `[[ ]]` |
| PyTorch, torchvision (BSD-3) | open source | inference runtime | `[[ ]]` |
| OpenCV, YuNet/SFace models (Apache-2.0/MIT) | open source | image processing, faces | `[[ ]]` |
| RapidOCR / PaddleOCR models (Apache-2.0) | open source | reading DVR clocks | `[[ ]]` |
| fast-plate-ocr, open-image-models (MIT) | open source | number plates | `[[ ]]` |
| SigLIP-2 model via Hugging Face Transformers (Apache-2.0) | open source | describe-it search | `[[ ]]` |
| ffmpeg (LGPL build) | open source | decoding, lossless copies, cuts | `[[ ]]` |
| ReportLab (BSD) | open source | certificate PDF | `[[ ]]` |
| Flask, waitress, cryptography, numpy, others (BSD/MIT/Apache/PSF) | open source | app server, crypto, maths | `[[ ]]` |
| Installation, training, support services | `[[SELLER]]` | services | `[[ ]]` |

How open-source components that are not paid for are treated in the local-content calculation is
a question for the order in force and the seller's auditor; this template takes no position.

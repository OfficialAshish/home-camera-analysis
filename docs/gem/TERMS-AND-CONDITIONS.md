# Drishti-AI — supply, warranty and support terms (template)

Status: **current (template)** · Seller-specific values are in `[[SELLER: …]]`. These terms add to,
and never override, the GeM General Terms and Conditions and the bid's Additional Terms and
Conditions; where they conflict, the GeM terms prevail. Legal review by the seller is required
before use.

## 1. Scope of supply
1. Drishti-AI software, version `[[ ]]`, for `[[n]]` computers, with one licence key per computer
   (`docs/LICENSING.md`), valid for `[[period]]`.
2. Installation on the buyer's computers `[[on-site / remote / by the buyer with the runbook]]`,
   following `docs/gem/HANDOVER-RUNBOOK-POLICE.md`, including the model files so that the
   computers can work without internet.
3. Training: `[[n]]` sessions of `[[hours]]` for `[[n]]` officers, covering the case workflow, the
   Forensic Studio and the court bundle with the Section 63 certificate.
4. Documentation: the user guide, the runbook, the technical specifications and the end-user
   licence agreement, in English `[[and any other language the seller provides]]`.

## 2. Acceptance
The buyer accepts each installation when, on the delivered computer:
1. `CCTVAnalysis.exe --self-test` (or `run_app.bat --self-test`) ends with exit code 0 and reports
   every check as `True`;
2. a Quick investigation of a sample folder supplied by the buyer completes, its timeline opens
   and a court bundle is created and verifies as intact (*Court bundle › Exports › Verify*).

## 3. Warranty
1. For `[[period, e.g. 12 months]]` from acceptance the seller corrects, free of charge, defects
   that make the software deviate from `docs/gem/TECHNICAL-SPECIFICATIONS.md`.
2. The warranty does not cover: accuracy beyond what the specifications state (scores and
   similarities rank footage for a human and are not identification); footage the software
   cannot decode; changes made to the software by anyone other than the seller; hardware.
3. The software never modifies the seized recordings it reads; the warranty includes the
   correction of any defect that would.

## 4. Support
| Item | Value |
|---|---|
| Channels | `[[phone, e-mail, ticket portal]]` |
| Hours | `[[e.g. 09:30–18:00 IST, working days]]` |
| Response time | `[[e.g. next working day]]`; defects that stop the creation of a court bundle: `[[ ]]` |
| Updates | security and defect updates during the warranty / AMC; each update states its version and is delivered with its SHA-256 |
| Remote access | only with the buyer's written permission for each session; the seller never asks for footage to be sent outside the buyer's premises |

## 5. Data protection and confidentiality
1. All processing happens on the buyer's computers. The software sends no footage, image, result
   or telemetry anywhere (`docs/DESKTOP-APP.md`, security model).
2. The seller's staff treat any case information seen during installation or support as
   confidential and do not copy it.
3. The buyer is the data fiduciary for the personal data in its footage under the Digital
   Personal Data Protection Act, 2023.

## 6. Price, payment, penalties
`[[SELLER: as per the bid; licence, installation, training, AMC as separate line items]]`

## 7. Third-party components
The product contains open-source components under their own licences (list:
`docs/COMMERCIAL-READINESS.md`; notices shipped with the product). The object detector
(Ultralytics YOLO) is AGPL-3.0 unless the seller holds a commercial licence:
`[[SELLER: state which applies]]`.

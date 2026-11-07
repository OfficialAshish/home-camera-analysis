# Evidence packages, review and audit trail

Status: **current** · Code: `evidence.py`, `section63.py`, `merkle.py`, `audit.py`, `reviews.py` · Tests:
`tests/test_app_api.py` (export, tamper, audit), `tests/test_guards_unit.py` (audit chain),
`tests/test_section63_bsa.py` (certificate, bundle, Merkle tree)

## Evidence package (`exports/evidence_<case>_<timestamp>.zip`)
```
incident_report.html          printable report: case, operator, export time, per event: camera, recorded
                              time + time source, source file + SHA-256, offsets, plates, people, flags, stills
manifest.json                 SHA-256 of every file, SHA-256 + size of each full source recording, tool version,
                              case id, operator, notes, export time, redaction flag
manifest.sha256               hash of manifest.json (quote it in letters / FIRs)
events/<event-id>/
   original_segment.<ext>     cut from the recording by stream copy – NOT re-encoded (starts at a keyframe, so it
                              may begin a few seconds before the event)
   clip.mp4, contact_sheet.jpg, snapshots/, faces/, people/, plates/ (plate + enhanced image)
   event.json                 machine-readable facts of the event
```
`cctv_analysis.evidence.verify_package(zip)` re-hashes everything. Any modified, missing or
unlisted file is reported (tested by editing one byte of an `event.json`).

**Redacted packages** (`blur faces`, optionally `also blur number plates`) blur faces/plates in
every image and clip and leave the unredacted original segments out, for sharing with people who
must not see bystanders. The manifest records that the package is redacted.

## Case bundle with the Section 63 certificate (`<case>/exports/*.zip`)
For a case (docs/CASES.md), `section63.build_bundle` produces the court bundle:
```
Section63_Certificate.pdf / .html   the certificate of the Schedule to Section 63 BSA 2023 (Part A, Part B)
                                    and the enclosed hash report (docs/legal/BSA-2023-SECTION-63.md)
certificate.json                    everything printed, machine-readable
hash_report.csv                     file, kind, camera, timestamp span (calibrated and DVR clock), SHA-256
checksums.sha256                    SHA-256 of every file in the bundle (`sha256sum -c checksums.sha256`)
originals/<source>/<event>.<ext>    stream-copy cuts of the source recordings, not re-encoded
enhanced/<name>.png (+ _input.png, .json)   Forensic Studio stills with their filter log and hashes
events/<source>/<event>.json        DVR and calibrated times, clock correction, source file + its SHA-256
audit/                              case and source audit logs, ingest records (acquisition hashes)
```
The hash report also lists the acquisition SHA-256 of every seized source file, with a Merkle
root (RFC 6962, `merkle.py`) over the exhibits and one over the seized files; the root is printed
on the certificate. `section63.verify_bundle(zip)` re-hashes every file and recomputes the root:
a changed byte, an added file or a forged root is reported. The certificate fills in the case
details, device particulars, tick boxes, the SHA-256 algorithm and the hash report; it never
fills in signatures, and names or places of the declarants only when the officer typed them.
A recording that is no longer available (drive unplugged) stops the bundle with that message.

## Review status
Per event: *Not reviewed · OK / normal · Suspicious · Theft confirmed · Reported · False alarm* +
notes + reviewer (operator name), stored in `<output>/reviews.db`, shown in the app, written into
the Excel *Events* sheet, filterable. Separate from `analysis.db` (decision D10).

## Audit log (`<output>/audit_log.jsonl`)
Hash-chained JSON lines: analysis started/finished (job, licence state, features), reviews, retime,
evidence exports (zip + manifest hashes, events, operator, case). `AuditLog.verify()` /
*Export evidence › Show audit log* report the first edited, deleted or reordered line. This makes
tampering *evident*; it does not prevent someone with file access from deleting the whole log, so
keep exported manifests and their hashes outside the computer as well.

## Limits (stated plainly)
* Hashes prove a package has not changed since export. They don't prove the source recording is
  authentic; that depends on the DVR and how the footage was collected.
* Recording times are as good as their source (see `docs/TIMELINE.md`); every event states it.
* Scores, AI descriptions and plate reads are triage aids. Confirm against the original segment.

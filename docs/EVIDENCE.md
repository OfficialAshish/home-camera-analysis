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
certificates/<source>/Section63_Certificate.pdf / .html
                                    one certificate of the Schedule to Section 63 BSA 2023 PER SOURCE DEVICE
                                    (Part A, Part B, the enclosed hash report; docs/legal/BSA-2023-SECTION-63.md)
certificates/<source>/certificate.json   everything printed on that certificate, machine-readable
originals/<source>/<event>.<ext>    stream-copy cuts of the source recordings, not re-encoded
enhanced/<name>.png (+ _input.png, .json)   Forensic Studio stills with their filter log and hashes
events/<source>/<event>.json        DVR and calibrated times, clock correction, source file + its SHA-256
audit/                              case and source audit logs, ingest records (acquisition hashes)
hash_report.csv                     file, kind, certificate, camera, timestamp span (calibrated and DVR), SHA-256
README.txt                          what each folder is and how to verify the bundle
checksums.sha256                    SHA-256 of every file in the bundle (`sha256sum -c checksums.sha256`)
```
**One certificate per device.** Part A is signed by the person in charge of the device (the shop
owner for his DVR, the traffic-control operator for the junction camera); none of them can vouch
for another's recorder. So each source that contributes a record gets its own certificate listing
only that device, only that device's records (original segments and the enhanced stills made from
its frames) and only that device's acquisition hashes. Part B (the expert) is the same person on
every certificate of a bundle. The declarants are typed once per case – Part A on each source, Part
B on the case – and every change is in the case audit log; an export can override them.

Each certificate's hash report carries a Merkle root (RFC 6962, `merkle.py`) over its records and
one over the device's seized files; the roots are printed on the certificate.
`section63.verify_bundle(zip)` re-hashes every file and recomputes every certificate's root: a
changed byte, a missing or added file, or a forged root is reported. The certificate fills in the
case details, device particulars, tick boxes, the SHA-256 algorithm and the hash report; it never
fills in signatures, and names or places of the declarants only when the officer typed them. An
enhanced still that does not name a source of the case cannot be certified and stops the export,
as does a recording that is no longer available (drive unplugged).

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

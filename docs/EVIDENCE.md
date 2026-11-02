# Evidence packages, review and audit trail

Status: **current** · Code: `evidence.py`, `audit.py`, `reviews.py` · Tests: `tests/test_app_api.py`
(export, tamper, audit), `tests/test_guards_unit.py` (audit chain)

## Evidence package (`exports/evidence_<case>_<timestamp>.zip`)
```
incident_report.html          printable report: case, operator, export time, per event: camera, recorded
                              time + time source, source file + SHA-256, offsets, plates, people, flags, stills
manifest.json                 SHA-256 of every file, SHA-256 + size of each full source recording, tool version,
                              case id, operator, notes, export time, redaction flag
manifest.sha256               hash of manifest.json (quote it in letters / FIRs)
events/<event-id>/
   original_segment.<ext>     cut from the recording by stream copy – NOT re-encoded (may start up to one keyframe early)
   clip.mp4, contact_sheet.jpg, snapshots/, faces/, people/, plates/ (plate + enhanced image)
   event.json                 machine-readable facts of the event
```
`cctv_analysis.evidence.verify_package(zip)` re-hashes everything. Any modified, missing or
unlisted file is reported (tested by editing one byte of an `event.json`).

**Redacted packages** (`blur faces`, optionally `also blur number plates`) blur faces/plates in
every image and clip and leave the unredacted original segments out, for sharing with people who
must not see bystanders. The manifest records that the package is redacted.

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

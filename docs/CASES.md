# Cases: several seized sources, one calibrated timeline

Status: **current** · Code: `cases.py`, `ingest.py`, `investigation.py`, `journey.py`, `merkle.py`
Tests: `tests/test_multi_folder_ingest.py`, `tests/test_person_reid.py`, `tests/test_section63_bsa.py`

## Model
A **case** has `case_id` (letters, digits, `-`, `_`), investigating officer, FIR number, police
station, BNS sections, district, state, incident time and notes. It holds any number of
**sources**: one per seized folder (a DVR/NVR export, a pen drive, a traffic camera dump). Each
source has a label, its folder (read only, never written), a **clock correction**, optional
per-camera corrections, the device particulars for the Section 63 certificate (kind, make, model,
serial, colour, IMEI/UIN/UID/MAC/cloud ID, storage medium, operating system, custodian,
Owned/Maintained/Managed/Operated) and an optional map position (`lat`/`lon` or plan `x`/`y`).

```
<cases root>/<case_id>/
  case.json               the case and its sources (the only file case edits change)
  audit_log.jsonl         hash-chained log: created, updated, source added/updated/removed,
                          ingest results, runs, enhancements, bundles
  sources/<source_id>/    the analysis output of that source (analysis.db, events/, reports …)
      ingest.json         acquisition record (below)
      playback/           lossless MP4 copies for the browser
      appearance_index.npz  person appearance vectors (docs/REID.md)
  enhancements/           Forensic Studio outputs + filter logs (docs/FORENSIC-STUDIO.md)
  exports/                evidence bundles with the Section 63 certificate (docs/EVIDENCE.md)
```

## Running a case (`investigation.run_case`)
For each source in turn (one heavy analysis at a time – what an 8 GB laptop sustains):
1. **Ingest** (`ingest.ingest_source`): SHA-256 of every recording (size, mtime, time of hashing);
   unchanged files are not re-hashed (`verify=True` re-hashes and lists files that changed since
   the last ingest); a Merkle root (RFC 6962) over all file hashes. Formats a browser cannot play
   (`.dav .264 .h264 .265 .h265 .hevc .ts .mts .m2ts .mkv .avi .flv .asf .wmv .3gp .mpg .mpeg .vob
   .ps`) get a **lossless remux** `playback/<file>.mp4` (`ffmpeg -c copy`). The copy is checked:
   same video codec, same number of video packets. A codec MP4 cannot hold, or a failed check,
   is recorded with the reason and no copy is made. Audio a browser MP4 cannot carry (G.711 from
   DVRs) is left out of the playback copy and the record says so.
2. **Analysis**: the normal pipeline (`pipeline.Analyzer`) with the chosen preset, reading the
   **original** files; raw streams are decoded from their remux (same frames, can seek).
3. **Appearance index** (`reid.build_index`).

The run can be cancelled between sources, files and events; finished videos are skipped next time.

## Clock calibration
`parse_offset` accepts what an officer types: `-00:11:00`, `+1:05:30`, `-11:00`, `-11m`, `+2h`,
`90s`, `-660`, `0`; more than 7 days is refused (that is a wrong date, not a clock error).
Calibrated time = DVR time + correction (source correction, or the camera's if set). DVR times are
never edited. Every case view (`journey.timeline`, `sightings`, `journey`, the highlight reel, the
certificate's hash report) shows both and orders by the calibrated time.

## Measured on synthetic seized sources (`tests/make_case_sources.py`)
Three folders, file names without times, the time only in the burnt-in clock: Shop A (MP4, clock
11 min fast), Junction B (raw `.264`, 5 min slow), Alley C (MPEG-TS, correct). One case run
analyses all three; each DVR time is within 3 s of what that clock shows, each calibrated time
within 3 s of the truth. Without corrections the route is B → C → A; with `-00:11:00`,
`+00:05:00`, `0` it is A → B → C (21:14 → 21:18 → 21:23), and the journey reports 4–5 minutes
between cameras. The MP4 needs no copy; the `.264` and `.ts` remuxes keep all 350 packets and
decode to identical pixels.

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
Owned/Maintained/Managed/Operated), the **Part A declarant** of that device's certificate (name,
relation, address, place – only what the officer types) and an optional map position (`lat`/`lon`
or plan `x`/`y`). The case holds the **Part B declarant** (the expert: name, relation, address,
designation, place). Every edit is audited with the old and new values.

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
4. **Describe-it search index**, when scene search is on: the pipeline embeds the keyframes of the
   events it analyses; `semantic.backfill_index` also embeds the saved keyframes (raw snapshots)
   of events analysed earlier with scene search off – so it can be turned on later without
   analysing the videos again (`test_describe_it_search_can_be_turned_on_later_without_analysing_again`).

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

## Finding one person, face or vehicle across the case (`journey.sightings`)
* **Appearance** (clothing, build): the person re-ID index of every source (docs/REID.md); the
  officer picks a person track and gets every track above the score, optionally only those whose
  top/bottom colour names match.
* **Face**: face identity numbers (`P0001` …) are given **per source** by that source's own
  clustering, so `P0003` of one DVR and `P0003` of another are unrelated people. A face picked in
  one source is found in the others by face similarity (SFace cosine ≥ `face_match_threshold`,
  0.42) against their identities (`journey.face_identities`); each sighting carries that
  similarity. Names enrolled from a known-faces folder are the same everywhere. Measured in
  `tests/test_case_sightings.py`: Lena is the Shop DVR's P0001 and the Bank DVR's P0002 (Messi is
  the Bank's P0001); following the Shop's P0001 finds Lena in the Bank (similarity ≥ 0.42) and
  never Messi; the old "same number = same person" rule is reproduced as a mutation and fails.
* **Plate**: exact text, or within one character (`plates.within_edits`), normalised the same way
  as everywhere else (`plates.normalize`: `mh 12 ab-1234` = `MH12AB1234`).
* **List**: the sightings the officer kept (unticked wrong matches are left out).

## Drives change letters, cases move
A seized pen drive that was `E:` yesterday is `F:` today; a case folder is copied to the forensic
lab's computer. Paths stored at analysis time are then stale:
* pictures of the analysis (snapshots, crops) are found under the case's current folder
  (`storage.resolve`: the longest trailing part of the stored path that exists there);
* recordings are always taken from the source's **current** folder (`Source.recording(rel_path)`),
  never from the absolute path stored at analysis time;
* **Relink** (`Case.relink_source`, *Cases › source › Relink…*) points a source at its folder's new
  location only if every recording of the acquisition record is there with the same size and the
  same SHA-256 (or the same size only, when chosen); otherwise nothing changes and the differences
  are listed. Audited as `source_relinked` with the old and new path.
`tests/test_multi_folder_ingest.py::test_a_moved_case_and_a_new_drive_letter_keep_working` moves a
case and its seized folders, checks pictures, refuses a wrong folder and a one-byte change, relinks
and builds a highlight reel from the new place.

## Trial licence
Without a licence a run analyses at most `TRIAL_MAX_VIDEOS` (5) videos **in total over all sources
of the case** (`run_case(max_videos_total=…)`), not 5 per source; hashing and playback copies are
never limited. A source left out says so (`skipped`).

## App endpoints (`cctv_analysis/app/case_api.py`)
| Method and path | Purpose |
|---|---|
| `GET /api/cases` · `POST /api/cases` | list (and the cases folder) · create |
| `POST /api/cases/quick` | Quick investigation: folders + details → case, sources, run (all validated first) |
| `GET/PUT /api/cases/<id>` | case with per-source status · edit details, `expert` (Part B) |
| `POST /api/cases/<id>/sources` · `PUT/DELETE …/sources/<sid>` | add · edit label, offsets, device, Part A declarant, location · remove |
| `POST …/sources/<sid>/relink` | job: verify and relink a moved source |
| `POST /api/cases/<id>/run` | job: ingest + analyse + index every (or the chosen) source |
| `GET …/timeline` · `GET …/events/<sid>/<eid>` | cross-camera timeline · one event with both times |
| `POST …/sightings` · `GET …/people` · `POST …/text-search` | sightings + journey · people by clothing colour · describe-it search |
| `GET …/video` · `GET …/frame` · `GET …/file` | recording (playback copy) · original frame (PNG) · a file of the case |
| `POST …/enhance/preview` · `POST …/enhance/save` · `GET …/enhancements` · `GET /api/enhance/filters` | Forensic Studio |
| `POST …/reel` · `POST …/bundle` · `GET …/exports[/<name>]` · `POST …/exports/<name>/verify` | jobs: highlight reel (`journey.reel_segments`: overlapping sightings of one recording merged, no second twice; result `parts`, `sightings`, `index`), court bundle · downloads · verification |
| `GET …/certificate?source=` · `GET …/audit` | printable certificate preview · case audit log |
| `POST /api/fs/locate` | where a dropped folder is (by its name and the sizes of its files) |
Responses carry paths relative to the case folder; files are served by `…/file?p=`.

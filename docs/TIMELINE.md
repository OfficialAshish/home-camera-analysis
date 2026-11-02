# Recording times, ordering and highlight reels

Status: **current** · Code: `timeline.py`, `pipeline.py` (plan / event clock), `reels.py`, `retime.py`
Tests: `tests/test_timeline.py`, `tests/test_pipeline_e2e.py`

## The reported defect (root cause)
Highlight reel `2026-11-01_highlights.mp4` showed at 3:31 a DVR clock of 05:38:27, at 3:41
22:00:46, at 3:51 20:38:34, while the burnt-in reel clock said 2026‑11‑01 11:21:59 → 11:22:05.

The clips were cloud exports: `31Oct26New/G225087WSFW-cloud-18056409.mp4`. Nothing in the name is a
time. The old resolution order was `filename → metadata → vlm_osd → mtime`; container
`creation_time` (and mtime) held the **download** time, 2026‑11‑01 ~11:22, a few seconds apart in
download order. So every clip got the wrong day and a fake time, and the reel was sorted by
download order. The folder name was also taken as the camera name.

Reproduction: `python tests/make_cloud_clips.py /tmp/repro` builds the same situation
(date folder, cloud names, DVR clock top-right, next-day metadata and mtimes in scrambled order).
Running the **original** code (commit `bbc2e0b`) on it and reading the DVR clock off its reel with
OCR: 0 of 5 positions match the reel's own labels and the clock is not monotonic
(22:00 → 05:38 → 13:47 → 20:38 → 01:12). The current code: 10 of 10 checks match, monotonic.
Both checks are automated in `tests/test_pipeline_e2e.py::test_reel_shows_the_dvr_clock_it_claims`.

## How a time is resolved now
`Config.time_sources` (default `filename → osd → metadata → mtime`), first success wins:

| source | label in reports | notes |
|---|---|---|
| override | `override` | `time_overrides = {"file.mp4": "2026-10-11 09:30:00"}` |
| filename | `filename` | Hikvision, Dahua, NVR exports, day-first, date-folder + time-name; extra regexes in `filename_time_patterns` |
| osd | `osd(rapidocr,consensus)` / `…,discontinuous` / `…,single` | DVR clock read from the top/bottom bands at 0.5 s, 1.5 s, middle, end; start = clock(t) − t + 0.5 s |
| metadata | `metadata` | rejected when a date in the folder/file name exists and differs |
| mtime | `file-modified(approx)` / `folder-date+file-modified(approx)` | last resort; the folder date replaces the day |

Clock text parsing (`parse_clock_text`) handles `2026-10-31 05:38:27`, the OCR-glued
`2026-10-3105:38:27`, `31/10/2026 Sat 05:38:27 PM`, `10-31-2026`, `31 Oct 2026`, `Oct 31, 2026`,
CJK dates, and letter/digit confusions inside numbers (`2O26`, `3B`). Ambiguous day/month uses the
folder date, else `osd_date_order` (`auto|ymd|dmy|mdy`).

Safety checks: an OSD reading must be within ±1 day of the folder date and after 2005 (unset DVR
clocks print 2000‑01‑01); two early readings must agree; a later disagreement marks the file
*discontinuous* and each event then reads its own clock (`osd_verify_events`,
`osd_event_tolerance` = 5 s).

## Order of processing and of reels
* Videos are planned first (probe + time) and processed in recording order.
* Reels: events with exact times sorted by clock time; events with approximate times after them
  in natural file-name order (`clip2` before `clip10`), never by download time.
* Layout: `highlights/<YYYY>/<MM-Mon>/<YYYY-MM-DD>_highlights.mp4` (+ `_index.csv`, `_timeline.srt`).
  Unknown dates: `highlights/unknown-date/`.
* Picture untouched by default (`reel_overlay=False`). Black/blank nothing-detected events are left
  out of reels (`reel_skip_blank`) but remain in all reports.

## Fixing folders analysed with an older version
`python -m cctv_analysis --input <recordings> --output <results> --retime` (or *Fix recording
times* in the app / notebook). Re-resolves times, re-reads every event's clock, recomputes all
stored timestamps from their offsets, rebuilds reels/weekly workbooks of affected days and deletes
those that no longer have events. IDs and event folders keep their names.

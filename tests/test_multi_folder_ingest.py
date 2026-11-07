"""A case with three seized folders whose DVR clocks are wrong by different amounts."""
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import make_case_sources  # noqa: E402

from cctv_analysis import journey as J  # noqa: E402
from cctv_analysis import video_io as V  # noqa: E402
from cctv_analysis.audit import AuditLog  # noqa: E402
from cctv_analysis.cases import CaseStore, format_offset, parse_offset  # noqa: E402
from cctv_analysis.investigation import run_case  # noqa: E402

MODELS = os.environ.get("CCTV_TEST_MODEL_DIR", os.path.join(ROOT, ".test_models"))
OPTS = dict(use_vlm=False, use_semantic=False, use_faces=False, use_pose=False, detector_model="yolo11n.pt",
            detector_fallbacks=[], det_imgsz=640, detect_fps=3, save_clips=False, excel_thumbnails=False,
            make_highlight_reel=False, model_cache_dir=MODELS)
FMT = "%Y-%m-%d %H:%M:%S"


def test_officer_clock_corrections_are_parsed():
    for text, sec in [("-00:11:00", -660), ("+00:05:00", 300), ("0", 0), ("", 0), ("-11:00", -660), ("1:05:30", 3930),
                      ("-11m", -660), ("+2h", 7200), ("90s", 90), ("-660", -660), (" + 00:00:30 ", 30), (45.5, 45.5)]:
        assert parse_offset(text) == sec, text
    for bad in ("11 minutes", "00:61:00", "1:2:3:4", "-8d", "+200:00:00", "abc"):
        with pytest.raises(ValueError):
            parse_offset(bad)
    assert format_offset(-660) == "-00:11:00" and format_offset(3930) == "+01:05:30"


@pytest.fixture(scope="module")
def case_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("case")
    truth = make_case_sources.make(str(tmp / "seized"))
    store = CaseStore(str(tmp / "cases"))
    c = store.create("FIR-0451-2026", user="SI Sharma", fir_number="451/2026", police_station="Civil Lines",
                     investigating_officer="SI R. Sharma")
    offsets = {"ShopA_DVR": "-00:11:00", "TrafficCam_North": "+00:05:00", "Jeweler_NVR": "0"}
    for folder in ("ShopA_DVR", "TrafficCam_North", "Jeweler_NVR"):
        c.add_source(str(tmp / "seized" / folder), folder.replace("_", " "), offsets[folder], user="SI Sharma")
    res = run_case(c, OPTS, preset="fast", work_root=str(tmp / "work"), model_cache_dir=MODELS)
    return c, truth, res, tmp


def test_all_three_folders_are_ingested_and_analysed_in_one_run(case_run):
    c, truth, res, tmp = case_run
    assert set(res["sources"]) == {"shopa-dvr", "trafficcam-north", "jeweler-nvr"} and not res["cancelled"]
    for sid, r in res["sources"].items():
        assert r["stats"]["videos_ok"] == 1 and not r["stats"].get("videos_failed"), (sid, r)
        rec = json.load(open(os.path.join(c.source_dir(sid), "ingest.json")))
        f = rec["files"][0]
        src = os.path.join(c.source(sid).path, f["rel"])
        assert f["sha256"] == hashlib.sha256(open(src, "rb").read()).hexdigest()       # acquisition hash
        assert f["bytes"] == os.path.getsize(src)


def test_proprietary_formats_get_a_lossless_playback_copy(case_run):
    c, truth, res, tmp = case_run
    by = {sid: json.load(open(os.path.join(c.source_dir(sid), "ingest.json")))["files"][0]
          for sid in ("shopa-dvr", "trafficcam-north", "jeweler-nvr")}
    assert "playback" not in by["shopa-dvr"]                         # MP4 plays as it is
    for sid in ("trafficcam-north", "jeweler-nvr"):                  # raw .264 and MPEG-TS
        f = by[sid]
        assert f["remux"]["ok"] and f["remux"]["video"] == "h264", f["remux"]
        play = os.path.join(c.source_dir(sid), f["playback"])
        with open(play, "rb") as fh:
            assert fh.read(12)[4:8] == b"ftyp"                       # an MP4 container
        orig, copy = V.probe(os.path.join(c.source(sid).path, f["rel"])), V.probe(play)
        assert abs(orig["duration"] - copy["duration"]) < 0.2 and copy["codec"] == "h264"
        assert f["remux"]["packets"] == 350                         # every compressed frame, none re-encoded
        assert f["playback_sha256"] == hashlib.sha256(open(play, "rb").read()).hexdigest()
    # the copy's frames are the original's frames (lossless): identical decoded pixels
    f = by["trafficcam-north"]
    a = V.read_frame_at(os.path.join(c.source("trafficcam-north").path, f["rel"]), 7.0, 0, (640, 360))
    b = V.read_frame_at(os.path.join(c.source_dir("trafficcam-north"), f["playback"]), 7.0, 0, (640, 360))
    assert a is not None and b is not None and (a == b).all()


def test_dvr_times_come_from_each_wrong_clock_and_calibration_restores_true_order(case_run):
    c, truth, res, tmp = case_run
    tl = J.timeline(c)
    assert len(tl) == 3, tl
    by = {e["source_id"]: e for e in tl}
    expect = {"shopa-dvr": "ShopA_DVR", "trafficcam-north": "TrafficCam_North", "jeweler-nvr": "Jeweler_NVR"}
    for sid, folder in expect.items():
        t = truth[folder]
        walk = dt.timedelta(seconds=t["walk"][0])
        dvr_walk = dt.datetime.strptime(t["dvr_start"], FMT) + walk
        true_walk = dt.datetime.strptime(t["true_start"], FMT) + walk
        e = by[sid]
        assert e["time_source"].startswith("osd"), e                 # the clock burnt into the picture
        assert abs((dt.datetime.strptime(e["dvr_time"], FMT) - dvr_walk).total_seconds()) <= 3, (sid, e)
        assert abs((dt.datetime.strptime(e["calibrated_time"], FMT) - true_walk).total_seconds()) <= 3, (sid, e)
    assert [e["source_id"] for e in tl] == ["shopa-dvr", "trafficcam-north", "jeweler-nvr"]      # true route
    # negative control: without the corrections the DVR clocks give the wrong route
    assert sorted(tl, key=lambda e: e["dvr_time"])[0]["source_id"] == "trafficcam-north"
    for s in c.sources:
        c.update_source(s.source_id, clock_offset="0", user="test")
    raw = J.timeline(c)
    assert [e["source_id"] for e in raw] == ["trafficcam-north", "jeweler-nvr", "shopa-dvr"]
    # corrections back: the route is right again, and every change is in the audit log
    for sid, off in (("shopa-dvr", "-00:11:00"), ("trafficcam-north", "+00:05:00")):
        c.update_source(sid, clock_offset=off, user="SI Sharma")
    assert [e["source_id"] for e in J.timeline(c)] == ["shopa-dvr", "trafficcam-north", "jeweler-nvr"]
    log = AuditLog(os.path.join(c.root, "audit_log.jsonl"))
    ok, n, _ = log.verify()
    assert ok and n >= 9
    changes = [e["details"] for e in log.entries() if e["action"] == "source_updated"]
    assert changes[-1]["before"]["clock_offset"] == "+00:00:00" and changes[-1]["after"]["clock_offset"] == "+00:05:00"


def test_a_per_camera_correction_overrides_the_source(case_run):
    c, truth, res, tmp = case_run
    before = {e["source_id"]: e for e in J.timeline(c)}["jeweler-nvr"]["calibrated_time"]
    c.update_source("jeweler-nvr", camera_offsets={"ch07": "+00:01:00"})
    after = {e["source_id"]: e for e in J.timeline(c)}["jeweler-nvr"]
    assert (dt.datetime.strptime(after["calibrated_time"], FMT) - dt.datetime.strptime(before, FMT)).total_seconds() == 60
    assert after["dvr_time"] == {e["source_id"]: e for e in J.timeline(c)}["jeweler-nvr"]["dvr_time"]  # DVR time untouched
    c.update_source("jeweler-nvr", camera_offsets={})


def test_journey_and_highlight_reel_follow_calibrated_time(case_run):
    c, truth, res, tmp = case_run
    items = [{"source_id": e["source_id"], "event_id": e["event_id"]} for e in reversed(J.timeline(c))]
    pts = J.sightings(c, "list", query={"items": items})
    j = J.journey(pts)
    assert [s["source_id"] for s in j["stops"]] == ["shopa-dvr", "trafficcam-north", "jeweler-nvr"]
    assert j["stops"][0]["camera"] == "ch01" and j["stops"][1]["camera"] == "ch03"
    gaps = [s["to_next_seconds"] for s in j["stops"][:2]]
    assert all(200 <= g <= 300 for g in gaps), gaps                   # ~4-5 minutes between cameras
    assert j["summary"].startswith("ShopA DVR/ch01 @ 21:1")
    reel = J.highlight_reel(pts, str(tmp / "journey.mp4"), pre=0.5, post=0.5, width=640, fps=8)
    assert reel["parts"] == 3 and [x["source_id"] for x in reel["index"]] == ["shopa-dvr", "trafficcam-north",
                                                                             "jeweler-nvr"]
    m = V.probe(reel["path"])
    assert m["width"] == 640 and abs(m["duration"] - reel["seconds"]) < 1.0
    # the burnt-in calibrated time of the first part is the true time (read back by OCR)
    from cctv_analysis.ocr import get_ocr
    frame = V.read_frame_at(reel["path"], 0.3, 0, (m["width"], m["height"]))
    texts = "".join(t for t, _ in get_ocr("auto").read(frame[:60])).replace(" ", "")
    assert "ShopADVR/ch01" in texts and "2026-10-3121:14:0" in texts, texts     # true time, not the DVR's 21:25


def test_case_inputs_are_validated(case_run, tmp_path):
    c, truth, res, tmp = case_run
    with pytest.raises(ValueError, match="already a source"):
        c.add_source(c.source("shopa-dvr").path)
    with pytest.raises(ValueError, match="inside the case folder"):
        c.add_source(c.source_dir("shopa-dvr"))
    with pytest.raises(ValueError, match="not found"):
        c.add_source(str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="unknown case field"):
        c.update(colour="red")
    with pytest.raises(ValueError, match="incident_time"):
        c.update(incident_time="yesterday")
    store = CaseStore(os.path.dirname(c.root))
    with pytest.raises(FileExistsError):
        store.create("FIR-0451-2026")
    with pytest.raises(ValueError):
        store.create("../escape")
    assert [x["case_id"] for x in store.list()] == ["FIR-0451-2026"]
    assert store.list()[0]["sources"] == 3


def test_the_trial_limit_counts_videos_of_the_whole_run(case_run):
    """A trial job analyses at most N videos – across all sources of the case, not N per source."""
    c, truth, res, tmp = case_run
    again = run_case(c, OPTS, preset="fast", work_root=str(tmp / "work"), model_cache_dir=MODELS, max_videos_total=2)
    got = again["sources"]
    assert "stats" in got["shopa-dvr"] and "stats" in got["trafficcam-north"]
    assert "stats" not in got["jeweler-nvr"] and "trial limit" in got["jeweler-nvr"]["skipped"]
    assert got["jeweler-nvr"]["files"] == 1                       # hashing (ingest) is never limited
    assert run_case(c, OPTS, preset="fast", work_root=str(tmp / "work"), model_cache_dir=MODELS,
                    analyse=False)["sources"]["jeweler-nvr"].get("skipped") is None


def test_a_moved_case_and_a_new_drive_letter_keep_working(case_run, tmp_path):
    """The case folder moves to another disk and the seized drive comes back with another letter:
    stored absolute paths are stale. Pictures are found under the moved folder; the source is
    relinked only after every recording matches its acquisition SHA-256."""
    c, truth, res, tmp = case_run
    old_cases, old_seized = os.path.dirname(c.root), str(tmp / "seized")
    new_cases, new_seized = tmp_path / "E" / "cases", tmp_path / "F" / "seized"
    new_cases.parent.mkdir()
    new_seized.parent.mkdir()
    os.rename(old_cases, new_cases)
    os.rename(old_seized, new_seized)
    try:
        m = CaseStore(str(new_cases)).get(c.case_id)
        tl = J.timeline(m)
        assert len(tl) == 3 and all(e["snapshot"] for e in tl)              # pictures follow the moved folder
        for e in tl:
            assert os.path.isfile(os.path.join(m.source_dir(e["source_id"]), e["snapshot"]))
        assert not os.path.exists(m.source("shopa-dvr").recording(tl[0]["rel_path"]))     # old drive letter
        with pytest.raises(ValueError, match="missing"):                     # not the folder of this DVR
            m.relink_source("shopa-dvr", str(new_seized / "TrafficCam_North"), user="SI Sharma")
        bad = new_seized / "Jeweler_NVR" / "ch07_backup.ts"
        orig = bad.read_bytes()
        bad.write_bytes(orig[:-1] + bytes([orig[-1] ^ 1]))                  # one byte changed, same size
        with pytest.raises(ValueError, match="SHA-256 differs"):
            m.relink_source("jeweler-nvr", str(new_seized / "Jeweler_NVR"))
        bad.write_bytes(orig)
        assert m.source("jeweler-nvr").path == os.path.join(old_seized, "Jeweler_NVR")   # unchanged on failure
        for s in m.sources:
            r = m.relink_source(s.source_id, str(new_seized / os.path.basename(s.path)), user="SI Sharma")
            assert r["verified"] == "sha256" and r["files"] == 1
        m = CaseStore(str(new_cases)).get(c.case_id)
        pts = J.sightings(m, "list", query={"items": [{"source_id": e["source_id"], "event_id": e["event_id"]}
                                                      for e in tl]})
        assert all(p["video_path"].startswith(str(new_seized)) and os.path.exists(p["video_path"]) for p in pts)
        reel = J.highlight_reel(pts, str(tmp_path / "moved.mp4"), pre=0.2, post=0.2, width=320, fps=4)
        assert reel["parts"] == 3
        acts = [e["details"] for e in AuditLog(os.path.join(m.root, "audit_log.jsonl")).entries(1000)
                if e["action"] == "source_relinked"]
        assert len(acts) == 3 and acts[0]["old_path"].startswith(old_seized) and acts[0]["verified"] == "sha256"
    finally:
        os.rename(new_cases, old_cases)
        os.rename(new_seized, old_seized)
        for s in c.sources:                                                   # back to the original paths
            back = CaseStore(old_cases).get(c.case_id)
            if back.source(s.source_id).path != s.path:
                back.relink_source(s.source_id, s.path, full_hash=False)


def test_raw_h264_stream_without_timestamps_is_timed_by_its_frames(tmp_path):
    """Raw .264 dumps have no container duration; before the fix probe() returned 0 s and every
    seek returned the first frame (the clock read the same time everywhere)."""
    p = tmp_path / "ch05.264"
    subprocess.run([V.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10", "-t", "6",
                    "-c:v", "libx264" if " libx264 " in subprocess.run([V.FFMPEG, "-hide_banner", "-encoders"],
                                                                     capture_output=True, text=True).stdout else "mpeg4",
                    "-bsf:v", "h264_mp4toannexb", "-f", "h264", str(p)], check=True)
    m = V.probe(str(p))
    assert (m["fps"], m["nb_frames"], m["duration"]) == (10.0, 60, 6.0)
    a = V.read_frame_at(str(p), 0.0, 0, (320, 240))
    b = V.read_frame_at(str(p), 4.0, 0, (320, 240))
    assert a is not None and b is not None and not (a == b).all()  # seeking reaches a different frame
    frames = list(V.iter_frames(str(p), 5, 320, 4.0, 1.0, src_size=(320, 240)))
    assert len(frames) == 5 and (frames[0][1].astype(int) - b.astype(int)).__abs__().mean() < 8


def test_a_copy_cut_of_a_raw_stream_contains_the_event_start(tmp_path):
    """A stream copy can only start at a keyframe, and for a raw stream ffmpeg starts at the first
    keyframe AFTER the seek point: the evidence cut used to begin after the event had started."""
    import numpy as np

    from cctv_analysis.evidence import _cut_original
    p = tmp_path / "ch05.264"
    subprocess.run([V.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25", "-t", "12",
                    "-c:v", "libx264", "-g", "25", "-keyint_min", "25", "-sc_threshold", "0", "-bsf:v",
                    "h264_mp4toannexb", "-f", "h264", str(p)], check=True)
    start = V.read_frame_at(str(p), 7.52, 0, (320, 240))               # the event starts between two keyframes
    later = V.read_frame_at(str(p), 8.0, 0, (320, 240))
    assert start is not None and np.abs(start.astype(int) - later.astype(int)).mean() > 1   # distinct frames
    out = tmp_path / "cut.264"
    assert _cut_original(str(p), 7.52, 9.0, str(out))
    frames = V.read_frames(str(out), 0.0, 400, (320, 240))
    assert len(frames) >= int(1.48 * 25)                                # at least the event's length
    assert any((f == start).all() for f in frames)                       # the first frame of the event is in it

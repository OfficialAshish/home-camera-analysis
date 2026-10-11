"""Fast unit tests (no GPU / no model downloads): python -m pytest -q tests"""
import datetime as dt
import os
import subprocess
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cctv_analysis import rules as R  # noqa: E402
from cctv_analysis import video_io as V  # noqa: E402
from cctv_analysis.config import Config  # noqa: E402
from cctv_analysis.vlm import parse_clock, parse_verdict  # noqa: E402

TZ = V._tz("Asia/Kolkata")


def test_filename_times():
    cases = {
        "ch01_20261011143000.mp4": (2026, 10, 11, 14, 30, 0),
        "/x/2026-10-11/001/dav/14/14.30.00-14.45.00[M][0@0][0].dav": (2026, 10, 11, 14, 30, 0),
        "NVR_ch2_main_20261011093015_20261011094500.mp4": (2026, 10, 11, 9, 30, 15),
        "cam 11-10-2026_08-00-00.avi": (2026, 10, 11, 8, 0, 0),
        "2026-10-11 21.05.09.mp4": (2026, 10, 11, 21, 5, 9),
    }
    for name, exp in cases.items():
        d = V.time_from_name(name, "/x", [], TZ)
        assert d is not None and d.timetuple()[:6] == exp, (name, d)
    assert V.time_from_name("IMG_1234.mp4", "/x", [], TZ) is None


def test_camera_names():
    cfg = Config(input_dir="/in")
    assert V.camera_name("/in/x/ch01_2026.mp4", cfg) == "ch01"
    assert V.camera_name("/in/Front Door/rec.mp4", cfg) == "Front Door"
    assert V.camera_name("/in/rec.mp4", cfg) == "cam"


def test_after_hours():
    cfg = Config(open_hour="09:00", close_hour="21:00", closed_weekdays=[6])
    assert R.is_after_hours(dt.datetime(2026, 10, 12, 22, 0), cfg)          # Monday night
    assert not R.is_after_hours(dt.datetime(2026, 10, 12, 12, 0), cfg)
    assert R.is_after_hours(dt.datetime(2026, 10, 11, 12, 0), cfg)          # Sunday closed
    cfg2 = Config(open_hour="18:00", close_hour="02:00")                     # open across midnight
    assert not R.is_after_hours(dt.datetime(2026, 10, 12, 1, 0), cfg2)
    assert R.is_after_hours(dt.datetime(2026, 10, 12, 10, 0), cfg2)


def test_point_in_poly():
    sq = [[0, 0], [1, 0], [1, 1], [0, 1]]
    assert R.point_in_poly(0.5, 0.5, sq) and not R.point_in_poly(1.5, 0.5, sq)


def test_scoring_explainable():
    cfg = Config()
    ev = {"start": 0, "end": 30, "start_dt": dt.datetime(2026, 10, 12, 23, 0), "max_persons": 1,
          "motion_peak": 0.05, "lighting_changes": 1}
    tracks = [{"cls": "person", "duration": 120, "max_conf": 0.9, "zones": {"cash_counter": 12},
               "max_speed": 0.3, "hidden_face_s": 10, "hands_up_s": 0, "crouch_s": 0}]
    score, flags = R.score_event(ev, tracks, cfg)
    names = {f["flag"] for f in flags}
    assert {"after_hours", "restricted_zone", "loitering", "face_hidden", "lights_after_hours"} <= names
    assert 0 < score <= 100 and all(f["reason"] for f in flags)
    s2, f2 = R.score_event({**ev, "start_dt": dt.datetime(2026, 10, 12, 12, 0), "lighting_changes": 0},
                           [{**tracks[0], "zones": {}, "duration": 10, "hidden_face_s": 0}], cfg)
    assert s2 == 0 and not f2


def test_vlm_parsing():
    v = parse_verdict('Sure: {"description": "A man puts a bottle in his jacket", "suspicious": true, '
                      '"category": "theft", "confidence": 0.8, "reason": "concealing"}')
    assert v["suspicious"] is True and v["category"] == "theft" and v["confidence"] == 0.8
    assert parse_verdict("Nothing suspicious, a customer pays.")["suspicious"] is False
    assert parse_clock("2026-10-11 22:15:50") == dt.datetime(2026, 10, 11, 22, 15, 50)
    assert parse_clock("11/10/2026 08:01:02") == dt.datetime(2026, 10, 11, 8, 1, 2)
    assert parse_clock("NONE") is None


def test_motion_on_synthetic(tmp_path):
    from cctv_analysis.motion import scan_motion
    p = str(tmp_path / "ch01_20261011221500.mp4")
    subprocess.run([sys.executable, os.path.join(ROOT, "tests", "make_synthetic.py"), p], check=True)
    cfg = Config(input_dir=str(tmp_path), output_dir=str(tmp_path / "out"))
    r = scan_motion(p, V.probe(p), cfg, "ch01")
    spans = [(round(e["start"]), round(e["end"])) for e in r.events]
    assert len(spans) == 2, spans                       # two walkers, nothing during idle/clock ticks
    assert spans[0][0] <= 20 <= spans[0][1] and spans[1][0] <= 92 <= spans[1][1]
    assert any(t["kind"].startswith("blackout") for t in r.tamper)
    assert r.osd_mask is not None                       # burnt-in clock found
    assert any(69 <= t <= 73 for t in r.lighting_changes)

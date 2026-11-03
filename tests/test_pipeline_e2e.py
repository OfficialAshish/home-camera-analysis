"""End-to-end runs of the real pipeline (YOLO on CPU, OCR) on synthetic cloud-DVR footage.
Slow-ish (~2 min); needs requirements.txt installed. Models download once into tests' cache."""
import csv
import datetime as dt
import os
import sqlite3
import sys
import threading

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import make_cloud_clips  # noqa: E402

from cctv_analysis import timeline as T  # noqa: E402
from cctv_analysis import video_io as V  # noqa: E402
from cctv_analysis.config import Config  # noqa: E402
from cctv_analysis.ocr import get_ocr  # noqa: E402
from cctv_analysis.pipeline import Analyzer  # noqa: E402

MODELS = os.environ.get("CCTV_TEST_MODEL_DIR", os.path.join(ROOT, ".test_models"))
FAST = dict(use_vlm=False, use_semantic=False, use_faces=False, use_pose=False, detector_model="yolo26n.pt",
            detector_fallbacks=["yolo11n.pt"], det_imgsz=640, detect_fps=3, save_clips=False, excel_thumbnails=False)


def _cfg(tmp, inp, **kw):
    return Config(input_dir=str(inp), output_dir=str(tmp / "out"), work_dir=str(tmp / "work"),
                  model_cache_dir=MODELS, **{**FAST, **kw})


def _sec(s):
    h, m, x = map(int, s.split(":"))
    return h * 3600 + m * 60 + x


@pytest.fixture(scope="module")
def cloud_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("cloud")
    inp = tmp / "in"
    truth = make_cloud_clips.make(str(inp))
    cfg = _cfg(tmp, inp)
    stats = Analyzer(cfg).run()
    return cfg, truth, stats


def test_reel_is_chronological_and_foldered(cloud_run):
    cfg, truth, stats = cloud_run
    assert stats["videos_ok"] == 5 and stats.get("videos_failed", 0) == 0
    reel = os.path.join(cfg.output_dir, "highlights", "2026", "10-Oct", "2026-10-31_highlights.mp4")
    assert os.path.exists(reel)
    assert not os.path.exists(os.path.join(cfg.output_dir, "highlights", "2026", "11-Nov"))   # download day: gone
    rows = list(csv.DictReader(open(reel.replace("_highlights.mp4", "_index.csv"), encoding="utf-8")))
    assert len(rows) == 5                                           # every clip reached the reel
    clocks = [r["clock_start"] for r in rows]
    assert clocks == sorted(clocks)
    assert [os.path.basename(r["video"]) for r in rows] == sorted(truth, key=lambda k: truth[k])
    db = sqlite3.connect(os.path.join(cfg.output_dir, "analysis.db"))
    assert db.execute("SELECT COUNT(*) FROM reel_segments WHERE day='2026-10-31'").fetchone()[0] == 5
    assert {r[0] for r in db.execute("SELECT DISTINCT camera FROM events")} == {"G225087WSFW"}


def test_reel_shows_the_dvr_clock_it_claims(cloud_run):
    cfg, truth, _ = cloud_run
    reel = os.path.join(cfg.output_dir, "highlights", "2026", "10-Oct", "2026-10-31_highlights.mp4")
    rows = list(csv.DictReader(open(reel.replace("_highlights.mp4", "_index.csv"), encoding="utf-8")))
    rm = V.probe(reel)
    ocr = get_ocr("auto")
    checked, prev = 0, None
    for r in rows:
        t = (_sec(r["reel_start"]) + _sec(r["reel_end"])) / 2
        shown = T.read_clock(V.read_frame_at(reel, t, 0, (rm["width"], rm["height"])), ocr)
        expected = dt.datetime.fromisoformat(r["clock_start"]) + dt.timedelta(seconds=t - _sec(r["reel_start"]))
        assert shown is not None and abs((shown - expected).total_seconds()) <= 2, (t, shown, expected)
        assert prev is None or shown > prev
        prev = shown
        checked += 1
    assert checked == 5


def test_reel_picture_is_unmodified(cloud_run):
    cfg, truth, _ = cloud_run
    reel = os.path.join(cfg.output_dir, "highlights", "2026", "10-Oct", "2026-10-31_highlights.mp4")
    rows = list(csv.DictReader(open(reel.replace("_highlights.mp4", "_index.csv"), encoding="utf-8")))
    diff = _overlay_diff(reel, rows[0], cfg.input_dir)
    assert diff < 3, diff                      # top-left + bottom strips equal the source


def _overlay_diff(reel, row, inp):
    import cv2
    rm = V.probe(reel)
    src = os.path.join(inp, row["video"])
    sm = V.probe(src)
    a = V.read_frame_at(reel, _sec(row["reel_start"]) + 4, 0, (rm["width"], rm["height"]))
    b = cv2.resize(V.read_frame_at(src, _sec(row["offset_start"]) + 4, 0, (sm["width"], sm["height"])),
                   (rm["width"], rm["height"]))
    h, w = a.shape[:2]
    regions = [(slice(0, int(h * 0.12)), slice(0, int(w * 0.55))), (slice(int(h * 0.92), h), slice(0, w))]
    return float(np.mean([np.abs(a[r].astype(int) - b[r].astype(int)).mean() for r in regions]))


def test_overlay_option_is_a_real_switch(tmp_path):
    """Positive control for the 'unmodified' check: with reel_overlay=True the same measure is large."""
    inp = tmp_path / "in"
    make_cloud_clips.make(str(inp), 1)
    cfg = _cfg(tmp_path, inp, reel_overlay=True)
    Analyzer(cfg).run()
    reel = os.path.join(cfg.output_dir, "highlights", "2026", "10-Oct", "2026-10-31_highlights.mp4")
    rows = list(csv.DictReader(open(reel.replace("_highlights.mp4", "_index.csv"), encoding="utf-8")))
    assert _overlay_diff(reel, rows[0], cfg.input_dir) > 10


def test_stitched_file_event_uses_its_own_clock(tmp_path):
    inp = tmp_path / "in" / "31Oct26New"
    inp.mkdir(parents=True)
    make_cloud_clips.make_stitched(str(inp / "G225087WSFW-cloud-1.mp4"))
    cfg = _cfg(tmp_path, tmp_path / "in")
    Analyzer(cfg).run(build_reports=False)
    db = sqlite3.connect(os.path.join(cfg.output_dir, "analysis.db"))
    vsrc, vstart = db.execute("SELECT time_source, start_dt FROM videos").fetchone()
    assert vstart == "2026-10-31 10:00:00" and "discontinuous" in vsrc
    evs = db.execute("SELECT start_dt, start_off, time_source FROM events ORDER BY start_off").fetchall()
    assert evs, "the walker in the second recording must produce an event"
    start_dt, off, src = evs[-1]
    assert off >= 9 and "event" in src
    got = dt.datetime.fromisoformat(start_dt)
    exp = dt.datetime(2026, 10, 31, 15, 30, 0) + dt.timedelta(seconds=off - 12)
    assert abs((got - exp).total_seconds()) <= 2, (got, exp)
    # without per-event verification the same event would be stamped ~10:00 (5.5 h off)
    cfg2 = _cfg(tmp_path / "b", tmp_path / "in", osd_verify_events=False)
    Analyzer(cfg2).run(build_reports=False)
    db2 = sqlite3.connect(os.path.join(cfg2.output_dir, "analysis.db"))
    s2 = db2.execute("SELECT start_dt FROM events ORDER BY start_off DESC").fetchone()[0]
    assert s2.startswith("2026-10-31 10:00")


def test_cancel_stops_cleanly_and_resumes(tmp_path):
    inp = tmp_path / "in"
    make_cloud_clips.make(str(inp), 3)
    cfg = _cfg(tmp_path, inp)
    cancel = threading.Event()
    seen = []

    def progress(ev):
        seen.append(ev["stage"])
        if ev["stage"] == "video" and ev["index"] == 2:
            cancel.set()

    stats = Analyzer(cfg, progress, cancel).run(build_reports=False)
    assert stats["cancelled"] == 1 and stats["videos_ok"] == 1 and stats.get("videos_failed", 0) == 0
    assert "planning" in seen
    db = sqlite3.connect(os.path.join(cfg.output_dir, "analysis.db"))
    assert db.execute("SELECT COUNT(*) FROM videos WHERE status='done'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM videos WHERE status='error'").fetchone()[0] == 0
    stats2 = Analyzer(cfg).run(build_reports=False)                 # next run resumes the rest
    assert stats2["videos_ok"] == 2 and stats2["already_done"] == 1


def test_retime_fixes_already_analysed_footage(tmp_path):
    """Folders analysed before the fix carry download-day times; --retime corrects them in place."""
    from cctv_analysis.retime import retime
    inp = tmp_path / "in"
    truth = make_cloud_clips.make(str(inp), 3)
    old = _cfg(tmp_path, inp, time_sources=["filename", "metadata", "mtime"], use_path_date_hint=False)
    Analyzer(old).run()
    out = old.output_dir
    db = sqlite3.connect(os.path.join(out, "analysis.db"))
    before = db.execute("SELECT event_id, start_dt FROM events ORDER BY event_id").fetchall()
    db.close()
    assert all(s.startswith("2026-11-01") for _, s in before)                         # the broken state
    assert os.path.exists(os.path.join(out, "highlights", "2026", "11-Nov", "2026-11-01_highlights.mp4"))
    assert os.path.exists(os.path.join(out, "reports", "2026", "01-07-Nov-2026-W44.xlsx"))
    stats = retime(_cfg(tmp_path, inp))
    assert stats["videos_changed"] == 3 and stats["events_changed"] == 3
    db = sqlite3.connect(os.path.join(out, "analysis.db"))
    after = dict(db.execute("SELECT event_id, start_dt FROM events").fetchall())
    srcs = {r[0] for r in db.execute("SELECT time_source FROM videos")}
    tr = db.execute("SELECT COUNT(*) FROM detections WHERE dt LIKE '2026-11-01%'").fetchone()[0]
    db.close()
    assert set(after) == {e for e, _ in before}                                        # ids unchanged
    got = sorted(after.values())
    exp = sorted((dt.datetime.fromisoformat(t) + dt.timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
                 for t in truth.values())                                              # events start 1 s in
    assert all(abs((dt.datetime.fromisoformat(a) - dt.datetime.fromisoformat(b)).total_seconds()) <= 1
               for a, b in zip(got, exp)), (got, exp)
    assert all(s.startswith("osd(") for s in srcs) and tr == 0
    assert not os.path.exists(os.path.join(out, "highlights", "2026", "11-Nov", "2026-11-01_highlights.mp4"))
    assert os.path.exists(os.path.join(out, "highlights", "2026", "10-Oct", "2026-10-31_highlights.mp4"))
    assert not os.path.exists(os.path.join(out, "reports", "2026", "01-07-Nov-2026-W44.xlsx"))
    assert os.path.exists(os.path.join(out, "reports", "2026", "25-31-Oct-2026-W43.xlsx"))


def test_broken_detector_fails_videos_instead_of_reporting_success(tmp_path, monkeypatch):
    """A detector that cannot run must not yield 'done' videos with zero events (silent success)."""
    from cctv_analysis import detector as D
    inp = tmp_path / "in"
    make_cloud_clips.make(str(inp), 5)
    cfg = _cfg(tmp_path, inp)

    def boom(self, frame):
        raise RuntimeError("operator torchvision::nms does not exist")
    monkeypatch.setattr(D.Detector, "__call__", boom)
    stats = Analyzer(cfg).run(build_reports=False)
    assert stats.get("videos_ok", 0) == 0 and stats["videos_failed"] == 3 and stats["aborted"] == 1
    assert stats["videos_deferred"] == 2
    db = sqlite3.connect(os.path.join(cfg.output_dir, "analysis.db"))
    assert db.execute("SELECT COUNT(*) FROM videos WHERE status='done'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM videos WHERE status='error' AND error LIKE '%nms%'").fetchone()[0] == 3
    db.close()
    monkeypatch.undo()
    stats = Analyzer(cfg).run(build_reports=False)                     # fixed install: everything is retried
    assert stats["videos_ok"] == 5

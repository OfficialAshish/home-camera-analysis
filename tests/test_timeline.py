"""Recording-time resolution (the cause of the out-of-order highlight reels).
Needs an OCR engine (rapidocr-onnxruntime from requirements.txt); no detector models."""
import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import make_cloud_clips  # noqa: E402

from cctv_analysis import timeline as T  # noqa: E402
from cctv_analysis import video_io as V  # noqa: E402
from cctv_analysis.config import Config  # noqa: E402
from cctv_analysis.ocr import get_ocr  # noqa: E402

OCR = get_ocr("auto")


def test_an_ocr_engine_is_installed():
    # the timeline fix depends on it; a missing engine must fail loudly, not skip
    assert OCR, "install rapidocr-onnxruntime (requirements.txt) or tesseract"


def test_clock_text_formats():
    cases = {"2026-10-3105:38:27": (2026, 10, 31, 5, 38, 27),          # RapidOCR drops the space
             "2026-10-31 22:00:46": (2026, 10, 31, 22, 0, 46),
             "31/10/2026 Sat 05:38:27 PM": (2026, 10, 31, 17, 38, 27),
             "10-31-2026 05:38:27": (2026, 10, 31, 5, 38, 27),
             "31 Oct 2026 05:38:27": (2026, 10, 31, 5, 38, 27),
             "Oct 31, 2026 20:38:34": (2026, 10, 31, 20, 38, 34),
             "2O26-1O-31 O5:3B:27": (2026, 10, 31, 5, 38, 27),            # letter/digit confusions
             "2026年10月31日 星期六 05:38:27": (2026, 10, 31, 5, 38, 27)}
    for text, exp in cases.items():
        d = T.parse_clock_text(text)
        assert d is not None and d.timetuple()[:6] == exp, (text, d)
    # ambiguous day/month: folder date decides, explicit order overrides
    assert T.parse_clock_text("12-01-2026 10:00:00", hint=dt.date(2026, 12, 1)).month == 12
    assert T.parse_clock_text("12-01-2026 10:00:00").month == 1
    assert T.parse_clock_text("12-01-2026 10:00:00", order="mdy").month == 12
    assert T.parse_clock_text("no clock") is None and T.parse_clock_text("05:38:27") is None


def test_dates_in_folder_names():
    yes = {"31Oct26New": dt.date(2026, 10, 31), "2026-10-31": dt.date(2026, 10, 31), "31-10-2026": dt.date(2026, 10, 31),
           "Oct31_2026": dt.date(2026, 10, 31), "20261031": dt.date(2026, 10, 31), "backup 1 Nov 2026": dt.date(2026, 11, 1)}
    for name, exp in yes.items():
        assert T.date_from_text(name) == exp, name
    for name in ("Front Door", "ch01", "6-oct", "cam2026"):
        assert T.date_from_text(name) is None, name
    assert T.path_date_hint("/in/31Oct26New/G225087WSFW-cloud-1.mp4", "/in") == dt.date(2026, 10, 31)


def test_cloud_export_times_come_from_the_dvr_clock(tmp_path):
    """The reported bug: no time in the file name, metadata/mtime = next-day download time."""
    truth = make_cloud_clips.make(str(tmp_path))
    cfg = Config(input_dir=str(tmp_path))
    got = {}
    for p in V.find_videos(cfg):
        d, src = T.resolve_start_time(p, V.probe(p), cfg, OCR)
        assert src.startswith("osd("), (p, src)
        got[os.path.basename(p)] = d.strftime("%Y-%m-%d %H:%M:%S")
    assert got == truth
    # negative control: without the clock the same files fall back to the bogus download time
    no_osd = Config(input_dir=str(tmp_path), time_sources=["filename", "metadata", "mtime"])
    p = os.path.join(str(tmp_path), "31Oct26New", next(iter(truth)))
    d, src = T.resolve_start_time(p, V.probe(p), no_osd, OCR)
    assert d.date() == dt.date(2026, 10, 31) and "approx" in src     # metadata (11-01) rejected by folder date
    no_hint = Config(input_dir=str(tmp_path), time_sources=["filename", "metadata", "mtime"], use_path_date_hint=False)
    d, src = T.resolve_start_time(p, V.probe(p), no_hint, OCR)
    assert src == "metadata" and d.date() == dt.date(2026, 11, 1)    # exactly what produced the scrambled reel


def test_osd_rejects_reading_that_contradicts_folder_date(tmp_path):
    truth = make_cloud_clips.make(str(tmp_path), 1)
    p = os.path.join(str(tmp_path), "31Oct26New", next(iter(truth)))
    meta = V.probe(p)
    assert T.osd_start_time(p, meta, OCR, hint=dt.date(2026, 10, 31))[0] is not None
    assert T.osd_start_time(p, meta, OCR, hint=dt.date(2026, 3, 2)) == (None, "unreadable")


def test_stitched_file_is_flagged_discontinuous(tmp_path):
    p = str(tmp_path / "stitched.mp4")
    make_cloud_clips.make_stitched(p)
    d, detail = T.osd_start_time(p, V.probe(p), OCR, samples=3)
    assert d == dt.datetime(2026, 10, 31, 10, 0, 0) and detail == "discontinuous", (d, detail)


def test_config_rejects_removed_time_source():
    assert any("vlm_osd" in x for x in Config(time_sources=["vlm_osd"]).validate())
    assert Config().validate() == []

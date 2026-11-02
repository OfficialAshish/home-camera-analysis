"""Number plates: format rules, voting, frame-exact seeking, and the real pipeline on a video of a
car with an Indian plate (sharp, and motion-blurred with/without enhancement)."""
import os
import sqlite3
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from cctv_analysis import plates as P  # noqa: E402
from cctv_analysis import video_io as V  # noqa: E402
from cctv_analysis.config import Config  # noqa: E402
from cctv_analysis.pipeline import Analyzer  # noqa: E402
import make_plate_video  # noqa: E402

MODELS = os.environ.get("CCTV_TEST_MODEL_DIR", os.path.join(ROOT, ".test_models"))


def test_indian_format_fit():
    assert P.plate_fit("MH12AB1234") == ("MH12AB1234", 1.0)
    assert P.plate_fit("MHI2AB1234") == ("MH12AB1234", 0.75)          # one look-alike fixed
    assert P.plate_fit("HH12AB1234") == ("MH12AB1234", 0.75)          # state-code look-alike
    assert P.plate_fit("22BH1234AB")[1] == 1.0 and P.plate_fit("DL1C1234")[1] == 1.0
    for junk in ("MH12AB12", "MHI2A81234", "MH12441", "1"):          # never "corrected" into a plate
        assert P.plate_fit(junk)[1] < 0.5, junk


def test_vote_and_watchlist():
    text, conf, ok = P.vote([("MH12AB1234", 0.9), ("MH12A81234", 0.6), ("MH12AB1Z34", 0.5), ("12393", 0.4)])
    assert (text, ok) == ("MH12AB1234", True) and conf > 0.5
    assert P.vote([]) == ("", 0.0, False)
    assert P.watchlist_match("MH12AB1234", ["mh 12 ab 1284"], 1) == "MH12AB1284"
    assert P.watchlist_match("MH12AB1234", ["MH12AB1284"], 0) == ""
    assert not P.is_reportable("MH12", 0.93, False) and P.is_reportable("MH12AB1234", 0.6, True)


def test_read_frame_at_returns_the_exact_frame(tmp_path):
    """Regression: an fps filter returned the frame ~0.4 s late, mis-cropping moving vehicles."""
    import cv2
    p = str(tmp_path / "count.mp4")
    vw = cv2.VideoWriter(p, cv2.VideoWriter_fourcc(*"mp4v"), 10, (160, 120))
    for i in range(50):
        f = np.zeros((120, 160, 3), np.uint8)
        f[:, :] = (i * 5) % 256
        vw.write(f)
    vw.release()
    cap = cv2.VideoCapture(p)
    native = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        native.append(f.astype(int))
    m = V.probe(p)
    for t, idx in ((0.0, 0), (1.67, 17), (2.0, 20), (3.3, 33)):
        f = V.read_frame_at(p, t, 0, (m["width"], m["height"])).astype(int)
        nearest = min(range(len(native)), key=lambda k: np.abs(native[k] - f).mean())
        assert nearest == idx, (t, nearest)


def _run(tmp_path, video_kw, **cfg_kw):
    inp = tmp_path / "in"
    inp.mkdir(parents=True)
    make_plate_video.make(str(inp / "ch01_20261031093000.mp4"), **video_kw)
    cfg = Config(input_dir=str(inp), output_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work"),
                 model_cache_dir=MODELS, use_vlm=False, use_semantic=False, use_faces=False, use_pose=False,
                 detector_model="yolo26n.pt", detector_fallbacks=["yolo11n.pt"], det_imgsz=640,
                 make_highlight_reel=False, save_clips=False, **cfg_kw)
    Analyzer(cfg).run(build_reports=False)
    db = sqlite3.connect(os.path.join(cfg.output_dir, "analysis.db"))
    plates = db.execute("SELECT text, confidence, valid_format, method, crop_path, enhanced_path FROM plates "
                        "ORDER BY confidence DESC").fetchall()
    ev = db.execute("SELECT plates, flags, level FROM events").fetchall()
    return plates, ev


def test_plates_off_by_default_does_no_plate_work(tmp_path):
    plates, ev = _run(tmp_path, {})
    assert Config().use_plates is False and plates == [] and all(e[0] in ("[]", None) for e in ev)


def test_sharp_plate_is_read_and_watchlist_alerts(tmp_path):
    plates, ev = _run(tmp_path, {}, use_plates=True, plate_watchlist=["MH12AB1284"])
    assert plates and plates[0][0] == "MH12AB1234" and plates[0][1] > 0.9 and plates[0][2] == 1
    assert os.path.exists(plates[0][4])
    assert any("watchlist_plate" in e[1] for e in ev) and any(e[2] in ("REVIEW", "HIGH") for e in ev)


def test_motion_blurred_plate_needs_enhancement(tmp_path):
    """9-px motion blur: plain OCR cannot read it (negative control), deblur+ensemble can."""
    plain, _ = _run(tmp_path / "a", {"blur": 9, "scale": 1.2}, use_plates=True)
    assert not plain or plain[0][0] != "MH12AB1234"
    enh, _ = _run(tmp_path / "b", {"blur": 9, "scale": 1.2}, use_plates=True, plate_enhance=True)
    assert enh and enh[0][0] == "MH12AB1234" and enh[0][2] == 1 and "deblur" in enh[0][3]
    assert enh[0][5] and os.path.exists(enh[0][5])                          # enhanced image saved for a human

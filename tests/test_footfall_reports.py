"""Footfall counting lines on real pedestrian footage (OpenCV's samples/data/vtest.avi, downloaded
once into the test model cache) and the content of the generated Excel workbook."""
import html
import os
import re
import shutil
import sqlite3
import sys
import urllib.request
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cctv_analysis.config import Config  # noqa: E402
from cctv_analysis.pipeline import Analyzer  # noqa: E402

MODELS = os.environ.get("CCTV_TEST_MODEL_DIR", os.path.join(ROOT, ".test_models"))
VTEST = "https://github.com/opencv/opencv/raw/4.x/samples/data/vtest.avi"


def _vtest():
    p = os.path.join(MODELS, "vtest.avi")
    if not os.path.exists(p):
        os.makedirs(MODELS, exist_ok=True)
        urllib.request.urlretrieve(VTEST, p + ".part")
        os.replace(p + ".part", p)
    return p


def xlsx_sheet_text(path: str, sheet_name: str) -> str:
    """All text of one sheet (shared strings + inline numbers), without extra dependencies."""
    with zipfile.ZipFile(path) as z:
        wb = z.read("xl/workbook.xml").decode()
        names = [(html.unescape(n), r) for n, r in re.findall(r'<sheet name="([^"]+)" sheetId="\d+" r:id="(rId\d+)"', wb)]
        rels = dict(re.findall(r'Id="(rId\d+)"[^>]*Target="([^"]+)"', z.read("xl/_rels/workbook.xml.rels").decode()))
        target = "xl/" + rels[dict(names)[sheet_name]].lstrip("/").replace("xl/", "")
        ss = [html.unescape(x) for x in re.findall(r"<t[^>]*>([^<]*)</t>", z.read("xl/sharedStrings.xml").decode())] \
            if "xl/sharedStrings.xml" in z.namelist() else []
        xml = z.read(target).decode()
    out = []
    for c in re.finditer(r'<c ([^>]*)>(?:<f>[^<]*</f>)?<v>([^<]*)</v>', xml):
        out.append(ss[int(c.group(2))] if 't="s"' in c.group(1) else c.group(2))
    return " | ".join(out)


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ff")
    inp = tmp / "in"
    inp.mkdir()
    shutil.copy(_vtest(), inp / "ch01_20261031100000.avi")
    cfg = Config(input_dir=str(inp), output_dir=str(tmp / "out"), work_dir=str(tmp / "work"), model_cache_dir=MODELS,
                 use_vlm=False, use_semantic=False, use_pose=False, detector_model="yolo26n.pt",
                 detector_fallbacks=["yolo11n.pt"], det_imgsz=640, detect_fps=4, save_clips=False,
                 count_lines={"ch01": {"middle": [[0.5, 0.0], [0.5, 1.0]]}})
    stats = Analyzer(cfg).run()
    return cfg, stats


def test_people_are_counted_crossing_the_line(run):
    cfg, stats = run
    db = sqlite3.connect(os.path.join(cfg.output_dir, "analysis.db"))
    persons = db.execute("SELECT COUNT(*) FROM tracks WHERE cls='person'").fetchone()[0]
    assert persons >= 5, persons                                       # real pedestrians were tracked
    import json
    ev = [json.loads(r[0] or "{}") for r in db.execute("SELECT crossings FROM events")]
    tr = [json.loads(r[0] or "{}") for r in db.execute("SELECT crossings FROM tracks WHERE cls='person'")]
    ev_in = sum(e.get("middle", {}).get("in", 0) for e in ev)
    ev_out = sum(e.get("middle", {}).get("out", 0) for e in ev)
    assert ev_in + ev_out >= 2, (ev_in, ev_out)
    assert ev_in == sum(t.get("middle", {}).get("in", 0) for t in tr)  # event totals = sum of tracks
    assert ev_out == sum(t.get("middle", {}).get("out", 0) for t in tr)
    text = xlsx_sheet_text(os.path.join(cfg.output_dir, "CCTV_Report.xlsx"), "Footfall")
    assert "middle in" in text and "middle out" in text and "2026-10-31" in text


def test_workbook_sheets_and_time_source(run):
    cfg, _ = run
    wb = os.path.join(cfg.output_dir, "CCTV_Report.xlsx")
    with zipfile.ZipFile(wb) as z:
        names = [html.unescape(n) for n in re.findall(r'<sheet name="([^"]+)"', z.read("xl/workbook.xml").decode())]
    for s in ("Events", "People & Objects", "Number Plates", "Footfall", "Highlights", "Videos"):
        assert s in names, names
    ev = xlsx_sheet_text(wb, "Events")
    assert "filename" in ev                                               # per-event time source column filled
    assert "No plates" in xlsx_sheet_text(wb, "Number Plates")

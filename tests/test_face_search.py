"""Search by photo through the app API, on a clip with two real faces (OpenCV sample photos
lena.jpg / messi5.jpg, downloaded once into the test model cache; tests/make_faces.py)."""
import io
import os
import shutil
import subprocess
import sys
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cctv_analysis.app.server import create_app  # noqa: E402
from cctv_analysis.config import Config  # noqa: E402
from cctv_analysis.pipeline import Analyzer  # noqa: E402

MODELS = os.environ.get("CCTV_TEST_MODEL_DIR", os.path.join(ROOT, ".test_models"))
SAMPLES = "https://github.com/opencv/opencv/raw/4.x/samples/data/"
H = {"X-CCTV-Token": "t", "Host": "127.0.0.1:8765"}


def _sample(name):
    p = os.path.join(MODELS, name)
    if not os.path.exists(p):
        os.makedirs(MODELS, exist_ok=True)
        urllib.request.urlretrieve(SAMPLES + name, p + ".part")
        os.replace(p + ".part", p)
    return p


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("fs")
    td = tmp / "test_data"
    td.mkdir()
    for n in ("lena.jpg", "messi5.jpg"):
        shutil.copy(_sample(n), td / n)
    inp = tmp / "in"
    inp.mkdir()
    subprocess.run([sys.executable, os.path.join(ROOT, "tests", "make_faces.py"), str(inp / "ch03_20261031101500.mp4"),
                    str(inp / "ch01_20261031120000.mp4")], cwd=tmp, check=True)
    cfg = Config(input_dir=str(inp), output_dir=str(tmp / "out"), work_dir=str(tmp / "work"), model_cache_dir=MODELS,
                 use_vlm=False, use_semantic=False, use_pose=False, detector_model="yolo26n.pt",
                 detector_fallbacks=["yolo11n.pt"], det_imgsz=640, make_highlight_reel=False, save_clips=False)
    Analyzer(cfg).run(build_reports=False)
    data = tmp / "data"
    data.mkdir()
    os.symlink(MODELS, data / "models")
    app = create_app(str(data), "t", 8765)
    return {"c": app.test_client(), "out": str(tmp / "out"), "td": td}


def _search(env, path_or_bytes, name="p.jpg"):
    data = open(path_or_bytes, "rb").read() if isinstance(path_or_bytes, str) else path_or_bytes
    return env["c"].post("/api/results/face-search", query_string={"out": env["out"]}, headers=H,
                         data={"photo": (io.BytesIO(data), name)}, content_type="multipart/form-data")


def test_search_by_photo_finds_the_person(env):
    import sqlite3
    db = sqlite3.connect(os.path.join(env["out"], "analysis.db"))
    ids = {r[0] for r in db.execute("SELECT DISTINCT identity FROM tracks WHERE identity != ''")}
    assert len(ids) >= 2, ids                                         # two different people were grouped
    r = _search(env, str(env["td"] / "lena.jpg"))
    assert r.status_code == 200, r.get_json()
    m = r.get_json()["matches"]
    assert m and m[0]["appearances"] and m[0]["similarity"] >= 0.37
    lena_id = m[0]["identity"]
    cams = {a["camera"] for a in m[0]["appearances"]}
    assert cams == {"ch01", "ch03"}                                   # she walks through both clips
    r2 = _search(env, str(env["td"] / "messi5.jpg")).get_json()["matches"]
    assert r2 and r2[0]["identity"] != lena_id and {a["camera"] for a in r2[0]["appearances"]} == {"ch03"}


def test_search_by_photo_rejects_bad_input(env):
    assert _search(env, os.path.join(ROOT, "tests", "assets", "car_rear.png"), "car.png").status_code == 422
    assert _search(env, b"not an image").status_code == 400
    r = env["c"].post("/api/results/face-search", query_string={"out": env["out"]}, headers=H)
    assert r.status_code == 400

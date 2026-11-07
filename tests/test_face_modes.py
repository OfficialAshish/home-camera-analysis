"""The face mode chosen before a case run (investigation.FACE_MODES) decides all face work of the
run, and nothing else happens: on public cameras an officer may only be allowed to look for the
reference persons. Real pipeline on a clip with two real faces (Lena, then Messi); Lena's photo
is the reference."""
import os
import shutil
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import make_faces  # noqa: E402

from cctv_analysis import journey as J  # noqa: E402
from cctv_analysis.cases import CaseStore  # noqa: E402
from cctv_analysis.faces import FaceEngine  # noqa: E402
from cctv_analysis.investigation import FACE_MODES, face_options, run_case  # noqa: E402

MODELS = os.environ.get("CCTV_TEST_MODEL_DIR", os.path.join(ROOT, ".test_models"))


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    from test_face_search import _sample
    tmp = tmp_path_factory.mktemp("facemodes")
    td = tmp / "test_data"
    td.mkdir()
    for n in ("lena.jpg", "messi5.jpg"):
        shutil.copy(_sample(n), td / n)
    lena, messi = make_faces.actors(str(td))
    src = tmp / "seized" / "Gate_DVR"
    src.mkdir(parents=True)
    make_faces.make(str(src / "ch03_20261031101500.mp4"), [(lena, 2, 11, 120), (messi, 13, 22, 100)], secs=24)
    refs = tmp / "reference_photos"
    (refs / "Lena Söderberg").mkdir(parents=True)
    shutil.copy(td / "lena.jpg", refs / "Lena Söderberg" / "lena.jpg")
    return {"tmp": tmp, "src": src, "refs": refs}


def _run(clip, mode, monkeypatch):
    calls = {"embed": 0}
    real = FaceEngine.embed

    def counting(self, frame, face):
        calls["embed"] += 1
        return real(self, frame, face)
    monkeypatch.setattr(FaceEngine, "embed", counting)
    made = []
    real_init = FaceEngine.__init__

    def track_init(self, cfg):
        real_init(self, cfg)
        made.append(self)
    monkeypatch.setattr(FaceEngine, "__init__", track_init)
    c = CaseStore(str(clip["tmp"] / f"cases_{mode}")).create(f"FIR-{mode}")
    c.add_source(str(clip["src"]), "Gate DVR")
    refs = ""
    if mode == "reference":
        c.add_reference_photos(str(clip["refs"]))
        refs = c.references_dir
    opts = dict(use_vlm=False, use_semantic=False, use_pose=False, use_plates=False, detector_model="yolo26n.pt",
                detector_fallbacks=["yolo11n.pt"], det_imgsz=640, save_clips=False, excel_thumbnails=False,
                make_highlight_reel=False, model_cache_dir=MODELS, **face_options(mode, refs))
    res = run_case(c, opts, preset="fast", work_root=str(clip["tmp"] / f"work_{mode}"), model_cache_dir=MODELS)
    assert all(r["stats"]["videos_ok"] == 1 for r in res["sources"].values()), res
    out = c.source_dir("gate-dvr")
    db = sqlite3.connect(os.path.join(out, "analysis.db"))
    faces = db.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
    tracks = db.execute("SELECT identity, watchlist FROM tracks WHERE cls='person'").fetchall()
    flags = " ".join(r[0] for r in db.execute("SELECT flags FROM events"))
    db.close()
    return {"case": c, "out": out, "faces": faces, "tracks": tracks, "flags": flags, "embed": calls["embed"],
            "engines": made}


def test_the_modes_are_the_ones_the_app_offers():
    assert list(FACE_MODES) == ["off", "detect", "unique", "reference"]
    with pytest.raises(ValueError):
        face_options("reference")                        # needs the photos
    with pytest.raises(ValueError):
        face_options("everything")


def test_off_does_no_face_work(clip, monkeypatch):
    r = _run(clip, "off", monkeypatch)
    assert r["engines"] == [] and r["faces"] == 0 and r["embed"] == 0          # no face model even loaded
    assert all(not ident and not wl for ident, wl in r["tracks"]) and r["tracks"]  # people are still found
    assert not os.path.exists(os.path.join(r["out"], "identities.json"))


def test_detect_keeps_face_pictures_but_compares_nothing(clip, monkeypatch):
    r = _run(clip, "detect", monkeypatch)
    assert r["faces"] > 0 and r["embed"] == 0
    assert len(r["engines"]) == 1 and r["engines"][0].recognizer_loaded is False    # SFace never loaded
    assert all(not ident and not wl for ident, wl in r["tracks"])
    assert not os.path.exists(os.path.join(r["out"], "identities.json"))


def test_unique_groups_faces_into_persons(clip, monkeypatch):
    r = _run(clip, "unique", monkeypatch)
    assert r["faces"] > 0 and r["embed"] > 0
    idents = {ident for ident, _ in r["tracks"] if ident}
    assert len(idents) == 2 and all(i.startswith("P") for i in idents)              # Lena and Messi
    assert all(not wl for _, wl in r["tracks"]) and os.path.exists(os.path.join(r["out"], "identities.json"))


def test_reference_finds_only_the_reference_person(clip, monkeypatch):
    r = _run(clip, "reference", monkeypatch)
    c = r["case"]
    assert [x["name"] for x in c.references()] == ["Lena Söderberg"]
    added = next(e for e in c.audit.entries(50) if e["action"] == "reference_photos_added")["details"]
    assert len(added["photos"]) == 1 and len(added["photos"][0]["sha256"]) == 64
    assert r["embed"] > 0
    assert {wl for _, wl in r["tracks"] if wl} == {"Lena Söderberg"}              # Messi is not identified
    assert all(not ident for ident, _ in r["tracks"])                              # nobody is grouped
    assert not os.path.exists(os.path.join(r["out"], "identities.json")) and "watchlist_face" in r["flags"]
    pts = J.sightings(c, "face", "Lena Söderberg")
    assert pts and all(2 - 2 <= p["t_off"] <= 11 + 2 for p in pts)                  # her sightings, in her window
    c.add_reference_photos(str(clip["refs"]))                                      # same photo again: not copied twice
    assert [len(x["photos"]) for x in c.references()] == [1]
    c.remove_reference("Lena Söderberg")
    assert c.references() == [] and any(e["action"] == "reference_person_removed" for e in c.audit.entries(50))

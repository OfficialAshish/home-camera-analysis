"""Face and number-plate sightings across the sources of a case (real pipeline, real faces).

Face identity numbers are per analysed folder. In the Shop DVR Lena walks past first, so she is
the Shop's P0001; in the Bank DVR Messi walks past first, so there he is P0001 and Lena P0002.
"Find the Shop's P0001 on every camera" must find Lena in the Bank – never the Bank's P0001.
The same car (plate MH12AB1234) passes a camera of each DVR."""
import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import make_faces  # noqa: E402
import make_plate_video  # noqa: E402

from cctv_analysis import journey as J  # noqa: E402
from cctv_analysis.cases import CaseStore  # noqa: E402
from cctv_analysis.investigation import run_case  # noqa: E402

MODELS = os.environ.get("CCTV_TEST_MODEL_DIR", os.path.join(ROOT, ".test_models"))
LENA_SHOP, MESSI_SHOP = (5, 20), (28, 44)        # seconds into each face clip the person is in view
MESSI_BANK, LENA_BANK = (4, 18), (24, 40)


def _within(p, window, slack=2.0):
    return window[0] - slack <= p["t_off"] <= window[1] + slack


@pytest.fixture(scope="module")
def case(tmp_path_factory):
    from test_face_search import _sample
    tmp = tmp_path_factory.mktemp("sight")
    td = tmp / "test_data"
    td.mkdir()
    for n in ("lena.jpg", "messi5.jpg"):
        shutil.copy(_sample(n), td / n)
    lena, messi = make_faces.actors(str(td))
    shop, bank = tmp / "seized" / "Shop_DVR", tmp / "seized" / "Bank_DVR"
    shop.mkdir(parents=True)
    bank.mkdir(parents=True)
    make_faces.make(str(shop / "ch03_20261031101500.mp4"), [(lena, *LENA_SHOP, 120), (messi, *MESSI_SHOP, 100)])
    make_faces.make(str(bank / "ch05_20261031102000.mp4"), [(messi, *MESSI_BANK, 100), (lena, *LENA_BANK, 120)],
                    secs=45)
    make_plate_video.make(str(shop / "ch02_20261031101700.mp4"))
    make_plate_video.make(str(bank / "ch06_20261031102200.mp4"))
    known = tmp / "known" / "Lena"
    known.mkdir(parents=True)
    shutil.copy(td / "lena.jpg", known / "lena.jpg")
    c = CaseStore(str(tmp / "cases")).create("FIR-77-2026")
    c.add_source(str(shop), "Shop DVR", "-00:11:00")
    c.add_source(str(bank), "Bank DVR", "+00:02:00")
    opts = dict(use_vlm=False, use_semantic=False, use_pose=False, use_faces=True, face_identities=True,
                use_plates=True, known_faces_dir=str(tmp / "known"), detector_model="yolo26n.pt",
                detector_fallbacks=["yolo11n.pt"], det_imgsz=640, save_clips=False, excel_thumbnails=False,
                make_highlight_reel=False, model_cache_dir=MODELS)
    res = run_case(c, opts, preset="fast", work_root=str(tmp / "work"), model_cache_dir=MODELS)
    assert all(r["stats"]["videos_ok"] == 2 for r in res["sources"].values()), res
    return c


def _own_identity(c, sid, window):
    """The identity the source's own clustering gave the person in view during `window`."""
    import sqlite3
    db = sqlite3.connect(os.path.join(c.source_dir(sid), "analysis.db"))
    rows = db.execute("SELECT identity, first_off FROM tracks WHERE identity != '' AND identity IS NOT NULL").fetchall()
    ids = {i for i, t in rows if window[0] - 2 <= t <= window[1] + 2}
    assert len(ids) == 1, (sid, window, rows)
    return ids.pop()


def test_identity_numbers_are_per_source(case):
    assert _own_identity(case, "shop-dvr", LENA_SHOP) == "P0001"
    assert _own_identity(case, "bank-dvr", MESSI_BANK) == "P0001"           # the same number, another person
    assert _own_identity(case, "bank-dvr", LENA_BANK) == "P0002"


def test_a_face_is_followed_into_the_other_source_by_similarity(case):
    pts = J.sightings(case, "face", "P0001", {"source_id": "shop-dvr"})
    shop = [p for p in pts if p["source_id"] == "shop-dvr"]
    bank = [p for p in pts if p["source_id"] == "bank-dvr"]
    assert shop and bank, pts
    assert all(_within(p, LENA_SHOP) for p in shop) and all(p["score"] == 1.0 for p in shop)
    assert all(_within(p, LENA_BANK) for p in bank), bank                    # Lena, not the Bank's P0001 (Messi)
    assert not any(_within(p, MESSI_BANK, 0) for p in bank)
    assert all(0.42 <= p["score"] < 1.0 for p in bank)                        # found by face similarity
    times = [p["calibrated_time"] for p in pts]
    assert times == sorted(times)
    # and Messi, picked in the Bank, is followed back into the Shop
    back = J.sightings(case, "face", "P0001", {"source_id": "bank-dvr"})
    assert {p["source_id"] for p in back} == {"shop-dvr", "bank-dvr"}
    assert all(_within(p, MESSI_SHOP) for p in back if p["source_id"] == "shop-dvr")


def test_a_face_identity_needs_its_source(case):
    with pytest.raises(ValueError, match="which source"):
        J.sightings(case, "face", "P0001")
    with pytest.raises(KeyError):
        J.sightings(case, "face", "P0099", {"source_id": "shop-dvr"})


def test_an_enrolled_name_is_the_same_person_everywhere(case):
    pts = J.sightings(case, "face", "Lena")
    assert {p["source_id"] for p in pts} == {"shop-dvr", "bank-dvr"}
    assert all(_within(p, LENA_SHOP if p["source_id"] == "shop-dvr" else LENA_BANK) for p in pts)


def test_a_plate_is_found_on_both_dvrs_exact_or_within_one_edit(case):
    exact = J.sightings(case, "plate", "MH12AB1234")
    assert {p["source_id"] for p in exact} == {"shop-dvr", "bank-dvr"}, exact
    assert all(p["plate"] == "MH12AB1234" for p in exact)
    assert J.sightings(case, "plate", "mh 12 ab-1234") == exact                 # typed the way people type it
    assert J.sightings(case, "plate", "MH12AB1284") == []                       # one wrong digit, exact search
    fuzzy = J.sightings(case, "plate", "MH12AB1284", fuzzy=True)
    assert {p["source_id"] for p in fuzzy} == {"shop-dvr", "bank-dvr"}
    assert J.sightings(case, "plate", "KA01ZZ0000", fuzzy=True) == []


def test_the_journey_of_a_face_follows_calibrated_time(case):
    j = J.journey(J.sightings(case, "face", "Lena"))
    # Shop DVR 10:15 -11 min = 10:04 true; Bank DVR 10:20 +2 min = 10:22 true: Shop first
    assert [s["source_id"] for s in j["stops"]] == ["shop-dvr", "bank-dvr"]
    assert j["stops"][0]["to_next_seconds"] > 15 * 60


def test_reel_segments_cover_every_sighting_once(tmp_path):
    """Overlapping sightings of one recording become one piece of footage (no second twice);
    times are anchored on the recording, so a merged piece keeps the correct clock; other
    recordings stay separate; pieces come in calibrated order."""
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    a.write_bytes(b"x")
    b.write_bytes(b"x")

    def pt(path, t, d, cal, dvr, sid="s1"):
        return {"video_path": str(path), "t_off": t, "duration": d, "calibrated_time": cal, "dvr_time": dvr,
                "source_id": sid, "source_label": sid, "camera": "ch1", "rel_path": path.name, "offset_seconds": -60,
                "event_id": f"e{t}", "track": 1}
    pts = [pt(a, 10, 3, "2026-01-01 10:00:10", "2026-01-01 10:01:10"),
           pt(a, 12, 4, None, None),                                     # same seconds, no time of its own
           pt(a, 30, 1, "2026-01-01 10:00:30", "2026-01-01 10:01:30"),   # far apart: its own piece
           pt(b, 5, 2, "2026-01-01 09:59:00", "2026-01-01 10:00:00", "s2"),
           {"video_path": str(tmp_path / "gone.mp4"), "t_off": 1}]         # recording not available
    segs = J.reel_segments(pts, pre=2, post=4)
    assert [(g["rel_path"], g["start"], g["end"], len(g["points"])) for g in segs] == \
        [("b.mp4", 3.0, 11.0, 1), ("a.mp4", 8.0, 20.0, 2), ("a.mp4", 28.0, 35.0, 1)]
    assert segs[1]["calibrated_start"] == "2026-01-01 10:00:08"
    assert segs[1]["dvr_at_zero"].strftime(J.FMT) == "2026-01-01 10:01:00"
    assert sum(len(g["points"]) for g in segs) == 4


def test_an_examiners_plate_reading_counts_in_the_case(case, tmp_path):
    """Through the app: an examiner types the plate of the Shop's car differently (as read on an
    enhanced still) – the case audit log records it with the machine read, the sightings of the
    typed plate find that car and the machine's text no longer does on that source; removing the
    reading restores the machine's. The case's unread list shows a vehicle nobody could read."""
    import sqlite3

    from cctv_analysis.app.server import create_app
    app = create_app(str(tmp_path / "data"), "tok", 8765)
    app.extensions["cctv"]["settings"].save({"cases_root": os.path.dirname(case.root)})
    c, H = app.test_client(), {"X-CCTV-Token": "tok", "Host": "127.0.0.1:8765"}
    cid = case.case_id
    db = sqlite3.connect(os.path.join(case.source_dir("shop-dvr"), "analysis.db"))
    eid, track = db.execute("SELECT event_id, track FROM plates WHERE text='MH12AB1234' ORDER BY confidence DESC").fetchone()
    db.close()
    url = f"/api/cases/{cid}/plates/shop-dvr/{eid}/{track}"
    try:
        r = c.put(url, json={"text": "MH14XY9999", "note": "enhanced still"}, headers=H)
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["plate"]["machine_text"] == "MH12AB1234"
        typed = J.sightings(case, "plate", "MH14XY9999")
        assert [(p["source_id"], p["event_id"], p["track"], p["plate"]) for p in typed] == \
            [("shop-dvr", eid, track, "MH14XY9999")]
        machine = J.sightings(case, "plate", "MH12AB1234")
        assert ("shop-dvr", eid, track) not in {(p["source_id"], p["event_id"], p["track"]) for p in machine}
        assert "bank-dvr" in {p["source_id"] for p in machine}
        a = [x for x in case.audit.entries(50) if x["action"] == "plate_reading"][-1]["details"]
        assert (a["examiner_text"], a["machine_text"], a["source"]) == ("MH14XY9999", "MH12AB1234", "shop-dvr")
        listed = c.get(f"/api/cases/{cid}/plates", query_string={"q": "MH14XY9999"}, headers=H).get_json()["plates"]
        assert [(p["source_id"], p["text"]) for p in listed] == [("shop-dvr", "MH14XY9999")]
        unread = c.get(f"/api/cases/{cid}/plates", query_string={"unread": "1"}, headers=H).get_json()["plates"]
        assert all(p["text"] is None and p["reading"] is None for p in unread)
        assert c.put(f"/api/cases/{cid}/plates/nope/{eid}/{track}", json={"text": "X1"}, headers=H).status_code == 404
    finally:
        assert c.put(url, json={"text": None}, headers=H).status_code == 200
    assert ("shop-dvr", eid, track) in {(p["source_id"], p["event_id"], p["track"])
                                        for p in J.sightings(case, "plate", "MH12AB1234")}

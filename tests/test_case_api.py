"""Case endpoints of the desktop app, end to end with the real pipeline.

One Quick Investigation (four seized folders: two synthetic DVRs whose clocks are wrong, and the
same real pedestrian footage on two NVRs) is run through the API once per module; every test
then calls the endpoints the screens use and checks status codes *and* bodies – a guard that
rejected the request cannot satisfy them."""
import base64
import datetime as dt
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile

import cv2
import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import make_case_sources  # noqa: E402

from cctv_analysis import video_io as V  # noqa: E402
from cctv_analysis.app.server import create_app  # noqa: E402

TOKEN = "case-api-token"
HOST = "127.0.0.1:8765"
MODELS = os.environ.get("CCTV_TEST_MODEL_DIR", os.path.join(ROOT, ".test_models"))
OPTIONS = {"use_faces": False, "use_pose": False, "use_semantic": True, "use_vlm": False, "save_clips": False,
           "excel_thumbnails": False, "make_highlight_reel": False, "model_cache_dir": MODELS,
           "detector_model": "yolo11n.pt", "detector_fallbacks": [], "det_imgsz": 640, "detect_fps": 3}
CID = "FIR-0900-2026"


def H(**extra):
    return {"X-CCTV-Token": TOKEN, "Host": HOST, **extra}


def wait(c, job_id, timeout=1500):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = c.get(f"/api/jobs/{job_id}", headers=H()).get_json()["job"]
        if j["status"] not in ("queued", "running"):
            return j
        time.sleep(0.5)
    raise TimeoutError(job_id)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("caseapi")
    seized = tmp / "seized"
    make_case_sources.make(str(seized))
    shutil.rmtree(seized / "Jeweler_NVR")
    vt = os.path.join(MODELS, "vtest.avi")
    if not os.path.exists(vt):
        pytest.skip("vtest.avi not downloaded")
    (seized / "Mall_Gate_NVR").mkdir()
    (seized / "Mall_Exit_NVR").mkdir()
    subprocess.run([V.FFMPEG, "-v", "error", "-y", "-i", vt, "-t", "14", "-c:v", "mpeg4", "-q:v", "3",
                    str(seized / "Mall_Gate_NVR" / "ch02_20261031211400.avi")], check=True)
    shutil.copy(seized / "Mall_Gate_NVR" / "ch02_20261031211400.avi", seized / "Mall_Exit_NVR" / "ch05_20261031212000.avi")
    data = tmp / "data"
    data.mkdir()
    os.symlink(MODELS, data / "models")
    app = create_app(str(data), TOKEN, 8765)
    return {"tmp": tmp, "seized": seized, "app": app, "c": app.test_client()}


@pytest.fixture(scope="module")
def case(env):
    c = env["c"]
    s = env["seized"]
    body = {"case_id": CID, "fir_number": "900/2026", "police_station": "Kotwali", "investigating_officer": "SI Rao",
            "preset": "fast", "options": OPTIONS, "request_id": "quick-1",
            "folders": [{"path": str(s / "ShopA_DVR"), "label": "Shop A DVR", "clock_offset": "-00:11:00"},
                        {"path": str(s / "TrafficCam_North"), "label": "Traffic North", "clock_offset": "+00:05:00"},
                        {"path": str(s / "Mall_Gate_NVR"), "label": "Mall gate", "clock_offset": "0"},
                        {"path": str(s / "Mall_Exit_NVR"), "label": "Mall exit", "clock_offset": "+00:00:30"}]}
    r = c.post("/api/cases/quick", json=body, headers=H())
    assert r.status_code == 202, r.get_json()
    d = r.get_json()
    assert [x["source_id"] for x in d["case"]["sources"]] == ["shop-a-dvr", "traffic-north", "mall-gate", "mall-exit"]
    job = wait(c, d["job"]["id"])
    assert job["status"] == "done", job
    return job["result"]


# ------------------------------------------------------------------ guards
def test_case_routes_need_the_token_and_the_cookie_loads_media_only(env, case):
    c = env["c"]
    assert c.get("/api/cases", headers={"Host": HOST}).status_code == 401
    ck = env["app"].test_client()
    ck.set_cookie("cctv_session", TOKEN, domain="127.0.0.1")
    assert ck.get("/api/cases", headers={"Host": HOST}).status_code == 401                 # not a media route
    assert ck.post(f"/api/cases/{CID}/sources", json={}, headers={"Host": HOST}).status_code == 401
    r = ck.get(f"/api/cases/{CID}/file", query_string={"p": "case.json"}, headers={"Host": HOST})
    assert r.status_code == 200 and json.loads(r.data)["case_id"] == CID                   # reached the handler
    assert "no-store" not in r.headers.get("Cache-Control", "")


def test_case_files_cannot_escape_the_case_folder(env, case):
    c = env["c"]
    for p, code in (("../../data/settings.json", 403), ("/etc/passwd", 403), ("sources/../../x", 403),
                    ("nope.txt", 404), ("", 400)):
        assert c.get(f"/api/cases/{CID}/file", query_string={"p": p}, headers=H()).status_code == code, p
    assert c.get("/api/cases/NO-SUCH-CASE", headers=H()).status_code == 404
    assert c.get("/api/cases/..%2F..", headers=H()).status_code == 404


def test_quick_investigation_checks_every_folder_before_creating_anything(env, case):
    c, s = env["c"], env["seized"]
    before = c.get("/api/cases", headers=H()).get_json()["cases"]
    bad = [({"folders": [str(s / "ShopA_DVR"), str(s / "ShopA_DVR")]}, "same folder"),
           ({"folders": [str(s / "ShopA_DVR"), str(s / "missing")]}, "folder not found"),
           ({"folders": [{"path": str(s / "ShopA_DVR"), "clock_offset": "eleven minutes"}]}, "clock offset"),
           ({"folders": []}, "1-50 folders"),
           ({"folders": [str(s / "ShopA_DVR")], "preset": "turbo"}, "preset")]
    for b, msg in bad:
        r = c.post("/api/cases/quick", json={**b, "case_id": "SHOULD-NOT-EXIST"}, headers=H())
        assert r.status_code == 400 and msg in r.get_json()["error"], (b, r.get_json())
    r = c.post("/api/cases/quick", json={"case_id": CID, "folders": [str(s / "ShopA_DVR")]}, headers=H())
    assert r.status_code == 409                                                            # case id taken
    assert c.get("/api/cases", headers=H()).get_json()["cases"] == before


def test_the_run_analysed_and_indexed_every_source(env, case):
    assert case["case_id"] == CID and not case["cancelled"] and case["trial"] is True
    assert {k: v["videos_ok"] for k, v in case["sources"].items()} == {"shop-a-dvr": 1, "traffic-north": 1,
                                                                        "mall-gate": 1, "mall-exit": 1}
    d = env["c"].get(f"/api/cases/{CID}", headers=H()).get_json()["case"]
    st = {s["source_id"]: s["status"] for s in d["sources"]}
    assert all(x["analysed"] and x["available"] and x["events"] >= 1 and x["files"] == 1 for x in st.values())
    assert st["traffic-north"]["playback_copies"] == 1 and st["shop-a-dvr"]["playback_copies"] == 0     # raw .264
    assert st["mall-gate"]["persons"] >= 3 and st["mall-gate"]["persons"] == st["mall-exit"]["persons"]
    assert all(x["text_search"] for x in st.values())
    assert {s["source_id"]: s["clock_offset_text"] for s in d["sources"]}["shop-a-dvr"] == "-00:11:00"
    assert d["investigating_officer"] == "SI Rao" and st["shop-a-dvr"]["cameras"] == ["ch01"]


# ------------------------------------------------------------------ timeline and clocks
def _order(c, *sids):
    ev = c.get(f"/api/cases/{CID}/timeline", headers=H()).get_json()["events"]
    return [e["source_id"] for e in ev if e["source_id"] in sids]


def test_timeline_follows_calibrated_time_and_a_new_offset_applies_at_once(env, case):
    c = env["c"]
    tl = c.get(f"/api/cases/{CID}/timeline", headers=H()).get_json()
    times = [e["calibrated_time"] for e in tl["events"]]
    assert times == sorted(times) and len(tl["events"]) >= 4
    assert _order(c, "shop-a-dvr", "traffic-north") == ["shop-a-dvr", "traffic-north"]   # true order
    snap = next(e["snapshot"] for e in tl["events"] if e["snapshot"])
    r = c.get(f"/api/cases/{CID}/file", query_string={"p": snap}, headers=H())
    assert r.status_code == 200 and r.data[:3] == b"\xff\xd8\xff"                          # a JPEG
    for sid in ("shop-a-dvr", "traffic-north"):
        assert c.put(f"/api/cases/{CID}/sources/{sid}", json={"clock_offset": "0"}, headers=H()).status_code == 200
    assert _order(c, "shop-a-dvr", "traffic-north") == ["traffic-north", "shop-a-dvr"]   # the DVR clocks' order
    for sid, off in (("shop-a-dvr", "-00:11:00"), ("traffic-north", "+00:05:00")):
        assert c.put(f"/api/cases/{CID}/sources/{sid}", json={"clock_offset": off}, headers=H()).status_code == 200
    assert _order(c, "shop-a-dvr", "traffic-north") == ["shop-a-dvr", "traffic-north"]
    r = c.put(f"/api/cases/{CID}/sources/shop-a-dvr", json={"clock_offset": "11 minutes"}, headers=H())
    assert r.status_code == 400 and "-HH:MM:SS" in r.get_json()["error"]
    r = c.put(f"/api/cases/{CID}/sources/shop-a-dvr", json={"path": "/elsewhere"}, headers=H())
    assert r.status_code == 400 and "unknown field" in r.get_json()["error"]
    audit = c.get(f"/api/cases/{CID}/audit", headers=H()).get_json()
    assert audit["intact"] and sum(e["action"] == "source_updated" for e in audit["entries"]) >= 4


# ------------------------------------------------------------------ people
def test_people_by_clothing_colour_and_appearance_search_across_cameras(env, case):
    c = env["c"]
    ppl = c.get(f"/api/cases/{CID}/people", headers=H()).get_json()
    gate = [p for p in ppl["people"] if p["source_id"] == "mall-gate"]
    assert gate and ppl["total"] == len(ppl["people"]) and "black" in ppl["colors"]
    col = gate[0]["upper"]
    only = c.get(f"/api/cases/{CID}/people", query_string={"upper": col}, headers=H()).get_json()["people"]
    assert only and all(p["upper"] == col for p in only) and len(only) <= len(ppl["people"])
    assert c.get(f"/api/cases/{CID}/people", query_string={"upper": "tartan"}, headers=H()).status_code == 400
    r = c.get(f"/api/cases/{CID}/file", query_string={"p": gate[0]["thumb"]}, headers=H())
    assert r.status_code == 200 and r.data[:3] == b"\xff\xd8\xff"
    # the same footage is on the exit NVR: every gate person is found there, at the same video time
    for q in gate[:3]:
        r = c.post(f"/api/cases/{CID}/sightings", json={"kind": "appearance", "min_score": 0.9, "query": {
            "source_id": "mall-gate", "event_id": q["event_id"], "track": q["track"]}}, headers=H())
        assert r.status_code == 200, r.get_json()
        pts = r.get_json()["points"]
        me = [p for p in pts if p["source_id"] == "mall-gate" and p["event_id"] == q["event_id"]
              and p["track"] == q["track"]]
        there = [p for p in pts if p["source_id"] == "mall-exit"]
        assert me and me[0]["score"] == 1.0 and there and max(p["score"] for p in there) >= 0.99, pts
        assert all("video_path" not in p for p in pts)                                     # no absolute paths
        assert [p["calibrated_time"] for p in pts] == sorted(p["calibrated_time"] for p in pts)
    r = c.post(f"/api/cases/{CID}/sightings", json={"kind": "appearance", "query": {
        "source_id": "mall-gate", "event_id": q["event_id"], "track": q["track"], "colors": {"upper": "tartan"}}},
        headers=H())
    assert r.status_code == 400
    r = c.post(f"/api/cases/{CID}/sightings", json={"kind": "appearance", "query": {
        "source_id": "mall-gate", "event_id": "nope", "track": 1}}, headers=H())
    assert r.status_code == 404


def test_natural_language_search_ranks_the_described_scene_first(env, case):
    c = env["c"]
    r = c.post(f"/api/cases/{CID}/text-search", json={"query": "people walking in a shopping mall"}, headers=H())
    assert r.status_code == 200, r.get_json()
    res = r.get_json()
    assert set(res["searched_sources"]) == {"shop-a-dvr", "traffic-north", "mall-gate", "mall-exit"}
    hits = res["results"]
    assert hits and hits[0]["source_id"] in ("mall-gate", "mall-exit"), hits[:3]
    assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)
    assert hits[0]["calibrated_time"] and hits[0]["dvr_time"]
    assert c.get(f"/api/cases/{CID}/file", query_string={"p": hits[0]["thumb"]}, headers=H()).status_code == 200
    r2 = c.post(f"/api/cases/{CID}/text-search", json={"query": "a red and blue shape on a plain grey wall"},
                headers=H()).get_json()["results"]
    assert r2[0]["source_id"] in ("shop-a-dvr", "traffic-north"), r2[:3]
    assert c.post(f"/api/cases/{CID}/text-search", json={"query": "x"}, headers=H()).status_code == 400
    c.post("/api/cases", json={"case_id": "EMPTY-CASE"}, headers=H())
    r = c.post("/api/cases/EMPTY-CASE/text-search", json={"query": "a man"}, headers=H())
    assert r.status_code == 409 and "Scene tags" in r.get_json()["error"]


def test_describe_it_search_can_be_turned_on_later_without_analysing_again(env, case):
    """A case analysed with scene search off: turning it on and running again indexes the saved
    keyframes of the finished events (the videos are not analysed again)."""
    c, s = env["c"], env["seized"]
    r = c.post("/api/cases/quick", json={"case_id": "LATE-SEARCH", "preset": "fast", "folders": [str(s / "Mall_Gate_NVR")],
                                         "options": {**OPTIONS, "use_semantic": False}}, headers=H())
    assert r.status_code == 202, r.get_json()
    assert wait(c, r.get_json()["job"]["id"])["status"] == "done"
    assert c.post("/api/cases/LATE-SEARCH/text-search", json={"query": "people walking"}, headers=H()).status_code == 409
    r = c.post("/api/cases/LATE-SEARCH/run", json={"preset": "fast", "options": {**OPTIONS, "use_semantic": True}},
               headers=H())
    j = wait(c, r.get_json()["job"]["id"])
    assert j["status"] == "done", j
    src = j["result"]["sources"]["mall-gate-nvr"]
    assert src["videos_ok"] == 0 and src["text_index_added"] >= 3             # nothing re-analysed, keyframes indexed
    hits = c.post("/api/cases/LATE-SEARCH/text-search", json={"query": "people walking in a shopping mall"},
                  headers=H()).get_json()["results"]
    assert hits and all(h["source_id"] == "mall-gate-nvr" and h["thumb"] for h in hits)
    again = wait(c, c.post("/api/cases/LATE-SEARCH/run", json={"preset": "fast", "options": {**OPTIONS}},
                           headers=H()).get_json()["job"]["id"])
    assert "text_index_added" not in again["result"]["sources"]["mall-gate-nvr"] or \
        again["result"]["sources"]["mall-gate-nvr"]["text_index_added"] == 0             # nothing left to add


# ------------------------------------------------------------------ recordings and frames
def test_recordings_play_from_their_lossless_copy_and_only_ingested_files_are_served(env, case):
    c = env["c"]
    ev = c.get(f"/api/cases/{CID}/timeline", headers=H()).get_json()["events"]
    raw = next(e for e in ev if e["source_id"] == "traffic-north")
    r = c.get(f"/api/cases/{CID}/video", query_string={"source": "traffic-north", "rel": raw["rel_path"]}, headers=H())
    assert r.status_code == 200 and r.mimetype == "video/mp4" and r.data[4:8] == b"ftyp"
    part = c.get(f"/api/cases/{CID}/video", query_string={"source": "traffic-north", "rel": raw["rel_path"]},
                 headers=H(Range="bytes=0-99"))
    assert part.status_code == 206 and len(part.data) == 100                              # the player can seek
    mp4 = next(e for e in ev if e["source_id"] == "shop-a-dvr")
    r = c.get(f"/api/cases/{CID}/video", query_string={"source": "shop-a-dvr", "rel": mp4["rel_path"]}, headers=H())
    assert r.status_code == 200 and r.data == open(env["seized"] / "ShopA_DVR" / mp4["rel_path"], "rb").read()
    sub = env["seized"] / "ShopA_DVR" / "day1"                    # a recording in a sub-folder, asked for the Windows way
    if not sub.exists():
        sub.mkdir()
    import cctv_analysis.ingest as ING
    shutil.copy(env["seized"] / "ShopA_DVR" / mp4["rel_path"], sub / "ch01_copy.mp4")
    ING.ingest_source(str(env["seized"] / "ShopA_DVR"), os.path.join(str(env["tmp"] / "data" / "cases"), CID, "sources",
                                                                     "shop-a-dvr"))
    r = c.get(f"/api/cases/{CID}/video", query_string={"source": "shop-a-dvr", "rel": "day1\\ch01_copy.mp4"}, headers=H())
    assert r.status_code == 200 and len(r.data) == (sub / "ch01_copy.mp4").stat().st_size
    shutil.rmtree(sub)                                              # leave the seized folder as it was
    ING.ingest_source(str(env["seized"] / "ShopA_DVR"), os.path.join(str(env["tmp"] / "data" / "cases"), CID, "sources",
                                                                     "shop-a-dvr"))
    for rel in ("../Mall_Gate_NVR/ch02_20261031211400.avi", "ingest.json", "nothing.mp4"):
        assert c.get(f"/api/cases/{CID}/video", query_string={"source": "shop-a-dvr", "rel": rel},
                     headers=H()).status_code == 404, rel
    assert c.get(f"/api/cases/{CID}/video", query_string={"source": "nope", "rel": "x"}, headers=H()).status_code == 404


def test_frame_is_the_original_picture_at_full_resolution(env, case):
    c = env["c"]
    ev = c.get(f"/api/cases/{CID}/timeline", headers=H()).get_json()["events"]
    e = next(x for x in ev if x["source_id"] == "traffic-north")
    r = c.get(f"/api/cases/{CID}/frame", query_string={"source": "traffic-north", "event": e["event_id"], "t": 7},
              headers=H())
    assert r.status_code == 200 and r.mimetype == "image/png"
    img = cv2.imdecode(np.frombuffer(r.data, np.uint8), cv2.IMREAD_COLOR)
    ref = V.read_frame_at(str(env["seized"] / "TrafficCam_North" / e["rel_path"]), 7.0, 0, (640, 360))
    assert img.shape == (360, 640, 3) and (img == ref).all()                              # lossless, same frame


def test_event_detail_carries_both_times_and_case_paths(env, case):
    c = env["c"]
    ev = c.get(f"/api/cases/{CID}/timeline", headers=H()).get_json()["events"]
    for sid, shift in (("mall-gate", 0), ("mall-exit", 30)):
        e0 = next(x for x in ev if x["source_id"] == sid and x["persons"])
        r = c.get(f"/api/cases/{CID}/events/{sid}/{e0['event_id']}", headers=H())
        assert r.status_code == 200, r.get_json()
        e = r.get_json()["event"]
        f = "%Y-%m-%d %H:%M:%S"
        assert (dt.datetime.strptime(e["calibrated_start"], f) - dt.datetime.strptime(e["start"], f)).total_seconds() \
            == shift
        person = next(t for t in e["tracks"] if t["person"])
        assert person["person"].startswith(f"sources/{sid}/") and "identity_id" in person
        img = c.get(f"/api/cases/{CID}/file", query_string={"p": person["person"]}, headers=H())
        assert img.status_code == 200 and img.data[:3] == b"\xff\xd8\xff"
        assert "review" not in e and e["source_label"]
    assert c.get(f"/api/cases/{CID}/events/mall-gate/nope", headers=H()).status_code == 404
    assert c.get(f"/api/cases/{CID}/events/nope/x", headers=H()).status_code == 404


def test_relink_moves_a_source_only_to_a_folder_with_the_same_recordings(env, case):
    c, s, tmp = env["c"], env["seized"], env["tmp"]
    copy = tmp / "F_drive" / "Mall_Exit_NVR"
    shutil.copytree(s / "Mall_Exit_NVR", copy)
    r = c.post(f"/api/cases/{CID}/sources/mall-exit/relink", json={"path": str(s / "Mall_Gate_NVR")}, headers=H())
    j = wait(c, r.get_json()["job"]["id"])
    assert j["status"] == "failed" and "missing" in j["error"]                 # other DVR's file names
    r = c.post(f"/api/cases/{CID}/sources/mall-exit/relink", json={"path": str(copy)}, headers=H())
    assert r.status_code == 202
    j = wait(c, r.get_json()["job"]["id"])
    assert j["status"] == "done" and j["result"]["verified"] == "sha256" and j["result"]["files"] == 1, j
    src = {x["source_id"]: x for x in c.get(f"/api/cases/{CID}", headers=H()).get_json()["case"]["sources"]}
    assert src["mall-exit"]["path"] == str(copy) and src["mall-exit"]["status"]["available"]
    audit = c.get(f"/api/cases/{CID}/audit", headers=H()).get_json()["entries"]
    assert any(e["action"] == "source_relinked" and e["details"]["new_path"] == str(copy) for e in audit)
    r = c.post(f"/api/cases/{CID}/sources/mall-exit/relink", json={"path": str(s / "Mall_Exit_NVR"), "verify": "size"},
               headers=H())
    assert wait(c, r.get_json()["job"]["id"])["result"]["verified"] == "size"
    assert c.post(f"/api/cases/{CID}/sources/mall-exit/relink", json={"path": str(tmp / "nowhere")},
                  headers=H()).status_code == 400


# ------------------------------------------------------------------ Forensic Studio
def _studio(c, path, **kw):
    ev = c.get(f"/api/cases/{CID}/timeline", headers=H()).get_json()["events"]
    e = next(x for x in ev if x["source_id"] == "shop-a-dvr")
    return c.post(f"/api/cases/{CID}/enhance/{path}", json={"source_id": "shop-a-dvr", "event_id": e["event_id"],
                                                            "t": 6, **kw}, headers=H())


def test_forensic_studio_previews_saves_and_reproduces(env, case):
    c = env["c"]
    r = _studio(c, "preview", chain=[{"op": "CLAHE", "clip_limit": 3}, {"op": "UnsharpMask", "amount": 1.5}])
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert d["log"] == "CLAHE(clip_limit=3, tile_grid=(8,8)) → UnsharpMask(sigma=1.5, amount=1.5, threshold=0)"
    img = cv2.imdecode(np.frombuffer(base64.b64decode(d["image"].split(",", 1)[1]), np.uint8), cv2.IMREAD_COLOR)
    assert img.shape == (360, 640, 3) and (d["width"], d["height"]) == (640, 360)
    st = _studio(c, "preview", chain=[], stack={"frames": 5, "method": "median"}).get_json()
    assert st["aligned"] == 5 and st["log"].startswith("MultiFrameStack(frames=5")
    for chain, msg in (([{"op": "Sharpen9000"}], "unknown filter"), ([{"op": "CLAHE", "clip_limit": 999}], "outside"),
                       ([{"op": "CLAHE", "colour": 1}], "unknown parameter")):
        r = _studio(c, "preview", chain=chain)
        assert r.status_code == 400 and msg in r.get_json()["error"], r.get_json()
    r = _studio(c, "save", chain=[{"op": "CLAHE", "clip_limit": 3}], name="shop door")
    assert r.status_code == 201, r.get_json()
    s = r.get_json()["enhancement"]
    assert s["reproducible"] is True and s["name"].startswith("shopdoor_") and s["source"]["source_id"] == "shop-a-dvr"
    assert s["source"]["calibrated_time"] < s["source"]["dvr_time"]                          # 11 minutes fast DVR
    png = c.get(f"/api/cases/{CID}/file", query_string={"p": s["file"]}, headers=H())
    assert png.status_code == 200 and hashlib.sha256(png.data).hexdigest() == s["output_sha256"]
    names = [x["name"] for x in c.get(f"/api/cases/{CID}/enhancements", headers=H()).get_json()["enhancements"]]
    assert s["name"] in names
    audit = c.get(f"/api/cases/{CID}/audit", headers=H()).get_json()["entries"]
    assert any(e["action"] == "enhancement_saved" and e["details"]["output_sha256"] == s["output_sha256"] for e in audit)
    filters = c.get("/api/enhance/filters", headers=H()).get_json()["filters"]
    assert set(filters) >= {"CLAHE", "WienerDeblur", "Perspective"} and filters["CLAHE"]["clip_limit"]["max"] == 40.0


def test_a_saved_still_is_reproduced_from_the_recording_or_flagged(env, case, monkeypatch):
    """'Reproducible' means: decoding the recording again and re-running stack and filters gives the
    same bytes. A decode that differs (simulated) must be reported, not claimed reproducible."""
    import cctv_analysis.video_io as VV
    c = env["c"]
    real, calls = VV.read_frame_at, []

    def drifting(*a, **k):
        calls.append(1)
        f = real(*a, **k)
        return f if len(calls) == 1 else cv2.add(f, 1)
    monkeypatch.setattr(VV, "read_frame_at", drifting)
    r = _studio(c, "save", chain=[{"op": "Levels", "gamma": 1.2}], name="drift", t=5.52)
    assert r.status_code == 201 and len(calls) == 2, r.get_json()
    assert r.get_json()["enhancement"]["reproducible"] is False
    monkeypatch.undo()
    st = _studio(c, "save", chain=[], stack={"frames": 3}, name="stack3", t=6.52)
    x = st.get_json()["enhancement"]
    assert st.status_code == 201 and x["reproducible"] is True and x["log"].startswith("MultiFrameStack(frames=3")
    rec = json.loads(c.get(f"/api/cases/{CID}/file", query_string={"p": x["file"][:-4] + ".json"}, headers=H()).data)
    assert rec["reproduced_from_recording"] is True and rec["stack"]["aligned"] == 3


# ------------------------------------------------------------------ reel and court bundle
def test_highlight_reel_of_a_curated_journey(env, case):
    c = env["c"]
    ev = c.get(f"/api/cases/{CID}/timeline", headers=H()).get_json()["events"]
    items = [{"source_id": e["source_id"], "event_id": e["event_id"]} for e in ev
             if e["source_id"] in ("shop-a-dvr", "traffic-north")]
    r = c.post(f"/api/cases/{CID}/sightings", json={"kind": "list", "query": {"items": items[::-1]}}, headers=H())
    j = r.get_json()["journey"]
    assert [s["source_id"] for s in j["stops"]] == ["shop-a-dvr", "traffic-north"] and j["summary"]
    r = c.post(f"/api/cases/{CID}/reel", json={"kind": "list", "query": {"items": items}, "pre": 0.5, "post": 0.5},
               headers=H())
    assert r.status_code == 202
    job = wait(c, r.get_json()["job"]["id"])
    assert job["status"] == "done", job
    res = job["result"]
    assert res["parts"] == 2 and [x["source_id"] for x in res["index"]] == ["shop-a-dvr", "traffic-north"]
    ex = {x["name"]: x for x in c.get(f"/api/cases/{CID}/exports", headers=H()).get_json()["exports"]}
    assert ex[res["name"]]["kind"] == "reel" and ex[res["name"]]["sha256"] == res["sha256"]
    dl = c.get(f"/api/cases/{CID}/exports/{res['name']}", headers=H())
    assert dl.status_code == 200 and hashlib.sha256(dl.data).hexdigest() == res["sha256"]


def test_a_reel_never_shows_the_same_seconds_twice(env, case):
    """Reported: a reel of several sightings repeated the same footage with the same time stamp 2-3
    times – many tracks of one event (or one person split into tracks) each became a part. The
    sightings of one recording are merged into non-overlapping segments."""
    c = env["c"]
    ppl = c.get(f"/api/cases/{CID}/people", headers=H()).get_json()["people"]
    q = next(p for p in ppl if p["source_id"] == "mall-gate")
    body = {"kind": "appearance", "min_score": 0.0, "query": {"source_id": "mall-gate", "event_id": q["event_id"],
                                                               "track": q["track"]}}
    pts = c.post(f"/api/cases/{CID}/sightings", json=body, headers=H()).get_json()["points"]
    gate = [p for p in pts if p["source_id"] == "mall-gate"]
    assert len(gate) >= 4                                            # many sightings in one 14 s recording
    j = wait(c, c.post(f"/api/cases/{CID}/reel", json={**body, "pre": 1, "post": 1}, headers=H()).get_json()["job"]["id"])
    assert j["status"] == "done", j
    res = j["result"]
    assert res["sightings"] == len(pts) and res["parts"] < res["sightings"]
    by_video = {}
    for x in res["index"]:
        by_video.setdefault((x["source_id"], x["rel_path"]), []).append((x["video_start"], x["video_end"]))
    for spans in by_video.values():                                  # no second of a recording twice
        spans.sort()
        assert all(a[1] < b[0] for a, b in zip(spans, spans[1:])), spans
    gate_secs = sum(e - s for (sid, _), v in by_video.items() if sid == "mall-gate" for s, e in v)
    assert gate_secs <= 14 + 2 + 0.5                                  # at most the recording + pre/post
    dl = c.get(f"/api/cases/{CID}/exports/{res['name']}", headers=H())
    path = env["tmp"] / "reel_check.mp4"
    path.write_bytes(dl.data)
    m = V.probe(str(path))
    import hashlib as _h
    hashes = [_h.md5(cv2.resize(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)[60:-40], (64, 36)).tobytes()).hexdigest()
              for _, f in V.iter_frames(str(path), 12, 0, 0, None, src_size=(m["width"], m["height"]))]
    assert len(hashes) >= 0.9 * res["seconds"] * 12 and len(set(hashes)) >= 0.95 * len(hashes)   # no repeated frames


def test_court_bundle_has_one_certificate_per_device_and_verifies(env, case):
    c = env["c"]
    ev = c.get(f"/api/cases/{CID}/timeline", headers=H()).get_json()["events"]
    items = [{"source_id": e["source_id"], "event_id": e["event_id"]} for e in ev
             if e["source_id"] in ("shop-a-dvr", "traffic-north")]
    enh = c.get(f"/api/cases/{CID}/enhancements", headers=H()).get_json()["enhancements"][0]["name"]
    r = c.post(f"/api/cases/{CID}/bundle", json={
        "items": items, "enhancements": [enh], "notes": "for the charge sheet",
        "part_a": {"shop-a-dvr": {"name": "Ramesh Kumar", "place": "Jaipur"}},
        "part_b": {"name": "Dr A. Verma", "designation": "Scientific Officer"}}, headers=H())
    assert r.status_code == 202, r.get_json()
    job = wait(c, r.get_json()["job"]["id"])
    assert job["status"] == "done", job
    res = job["result"]
    assert set(res["certificates"]) == {"shop-a-dvr", "traffic-north"}
    dl = c.get(f"/api/cases/{CID}/exports/{res['name']}", headers=H())
    assert dl.status_code == 200 and hashlib.sha256(dl.data).hexdigest() == res["zip_sha256"]
    with zipfile.ZipFile(io.BytesIO(dl.data)) as z:
        a = json.loads(z.read("certificates/shop-a-dvr/certificate.json"))
        assert a["part_a"]["name"] == "Ramesh Kumar" and a["part_b"]["designation"] == "Scientific Officer"
        assert z.read("certificates/traffic-north/Section63_Certificate.pdf")[:5] == b"%PDF-"
    v = c.post(f"/api/cases/{CID}/exports/{res['name']}/verify", headers=H()).get_json()
    assert v["ok"] and set(v["certificates"]) == {"shop-a-dvr", "traffic-north"}
    d = c.get(f"/api/cases/{CID}", headers=H()).get_json()["case"]          # the declarants were kept
    assert {s["source_id"]: s["declarant"] for s in d["sources"]}["shop-a-dvr"] == {"name": "Ramesh Kumar",
                                                                                    "place": "Jaipur"}
    assert d["expert"]["name"] == "Dr A. Verma"
    ck = env["app"].test_client()                                            # the printable preview, as a new tab
    ck.set_cookie("cctv_session", TOKEN, domain="127.0.0.1")
    page = ck.get(f"/api/cases/{CID}/certificate", query_string={"source": "shop-a-dvr"}, headers={"Host": HOST})
    assert page.status_code == 200 and b"PREVIEW" in page.data and b"Ramesh Kumar" in page.data
    assert "style-src 'unsafe-inline'" in page.headers["Content-Security-Policy"]
    assert "script-src" not in page.headers["Content-Security-Policy"]       # default-src 'none': no script at all
    for body, code in (({}, 400), ({"items": [{"source_id": "nope", "event_id": "x"}]}, 404),
                       ({"enhancements": ["../../etc/passwd"]}, 400), ({"items": "all"}, 400)):
        assert c.post(f"/api/cases/{CID}/bundle", json=body, headers=H()).status_code == code, body


# ------------------------------------------------------------------ folders and settings
def test_a_dropped_folder_is_located_by_its_files(env, case):
    c, s = env["c"], env["seized"]
    f = s / "ShopA_DVR" / "ch01_record.mp4"
    r = c.post("/api/fs/locate", json={"name": "ShopA_DVR", "files": [{"rel": f.name, "size": f.stat().st_size}]},
               headers=H())
    assert r.status_code == 200 and r.get_json()["candidates"] == [str(s / "ShopA_DVR")]
    r = c.post("/api/fs/locate", json={"name": "ShopA_DVR", "files": [{"rel": f.name, "size": 12}]}, headers=H())
    assert r.status_code == 200 and r.get_json()["candidates"] == []                       # same name, other files
    for bad in ({"name": "a/b", "files": [{"rel": "x", "size": 1}]}, {"name": "x", "files": []},
                {"name": "x", "files": [{"rel": "../../etc/passwd", "size": 1}]}):
        assert c.post("/api/fs/locate", json=bad, headers=H()).status_code == 400, bad


def test_a_dropped_sibling_of_a_folder_used_before_is_found(tmp_path):
    """Seized drives hold several device folders side by side: after one was added (recent), a
    dropped sibling must be found next to it – the parent of a recent folder is searched first.
    Found by content only: no files, no match."""
    from cctv_analysis.app.case_api import locate_folder, search_roots
    drive = tmp_path / "E_drive"
    (drive / "ShopA_DVR").mkdir(parents=True)
    (drive / "Mall_NVR" / "ch01").mkdir(parents=True)
    (drive / "Mall_NVR" / "ch01" / "rec.mp4").write_bytes(b"x" * 1234)
    roots = search_roots([str(drive / "ShopA_DVR")])
    assert roots[:2] == [str(drive), str(drive / "ShopA_DVR")]
    want = [{"rel": "ch01/rec.mp4", "size": 1234}]
    assert locate_folder("Mall_NVR", want, roots) == [str(drive / "Mall_NVR")]
    assert locate_folder("Mall_NVR", [], roots) == []
    assert locate_folder("Mall_NVR", [{"rel": "ch01/rec.mp4", "size": 99}], roots) == []


def test_the_cases_folder_can_move_to_another_disk(env, case):
    c, tmp = env["c"], env["tmp"]
    default = c.get("/api/cases", headers=H()).get_json()["root"]
    r = c.put("/api/settings", json={"cases_root": str(tmp / "other_disk" / "cases")}, headers=H())
    assert r.status_code == 400                                              # the parent must exist
    (tmp / "other_disk").mkdir()
    assert c.put("/api/settings", json={"cases_root": str(tmp / "other_disk" / "cases")}, headers=H()).status_code == 200
    d = c.get("/api/cases", headers=H()).get_json()
    assert d["root"] == str(tmp / "other_disk" / "cases") and d["cases"] == []
    assert c.put("/api/settings", json={"cases_root": default}, headers=H()).status_code == 200
    assert CID in [x["case_id"] for x in c.get("/api/cases", headers=H()).get_json()["cases"]]


def test_run_rejects_bad_options_before_starting(env, case):
    c = env["c"]
    for body, msg in (({"options": {"no_such_option": 1}}, "Unknown config keys"), ({"preset": "turbo"}, "preset"),
                      ({"sources": ["nope"]}, "sources must be"), ({"options": "fast"}, "options")):
        r = c.post(f"/api/cases/{CID}/run", json=body, headers=H())
        assert r.status_code == 400 and msg in r.get_json()["error"], (body, r.get_json())
    assert c.post("/api/cases/EMPTY-CASE/run", json={}, headers=H()).status_code == 400   # no sources yet


def test_the_system_folder_dialog_is_offered_only_where_it_can_open(env, monkeypatch):
    import importlib.util
    c = env["c"]
    if not sys.platform.startswith("linux"):
        pytest.skip("the display check applies to Linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert c.get("/api/fs/roots", headers=H()).get_json()["native_dialog"] is False
    import cctv_analysis.app.server as srv
    started = []
    monkeypatch.setattr(srv.subprocess, "run", lambda *a, **k: started.append(a))
    r = c.post("/api/fs/pick", json={}, headers=H())
    assert r.status_code == 501 and started == []                 # answered at once, no helper process started
    monkeypatch.setenv("DISPLAY", ":0")
    assert c.get("/api/fs/roots", headers=H()).get_json()["native_dialog"] is (importlib.util.find_spec("tkinter")
                                                                                is not None)


def test_timeline_levels_are_the_pipeline_levels(env, case):
    c = env["c"]
    assert c.get(f"/api/cases/{CID}/timeline", query_string={"levels": "INFO"}, headers=H()).status_code == 400
    ok = c.get(f"/api/cases/{CID}/timeline", query_string={"levels": "HIGH,REVIEW,LOW,NORMAL"}, headers=H())
    assert ok.status_code == 200 and len(ok.get_json()["events"]) == len(
        c.get(f"/api/cases/{CID}/timeline", headers=H()).get_json()["events"])
    assert c.get(f"/api/cases/{CID}/timeline", query_string={"day_from": "31/10/2026"}, headers=H()).status_code == 400
    one = c.get(f"/api/cases/{CID}/timeline", query_string={"limit": 1}, headers=H()).get_json()
    assert len(one["events"]) == 1 and one["total"] == len(ok.get_json()["events"]) >= 4      # the page says what is left out

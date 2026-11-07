"""Desktop-app HTTP API: authentication / host / path guards must deny, real flows must work
end-to-end against a real analysed output folder (built once per module with the real pipeline)."""
import json
import os
import sys
import time
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import make_cloud_clips  # noqa: E402
import make_plate_video  # noqa: E402

from cctv_analysis.app.server import create_app  # noqa: E402

TOKEN = "t0k3n-for-tests"
PORT = 8765
HOST = f"127.0.0.1:{PORT}"
MODELS = os.environ.get("CCTV_TEST_MODEL_DIR", os.path.join(ROOT, ".test_models"))
FAST = {"use_faces": False, "use_pose": False, "use_semantic": False, "use_vlm": False, "save_clips": True,
        "model_cache_dir": MODELS, "detector_model": "yolo26n.pt", "detector_fallbacks": ["yolo11n.pt"],
        "det_imgsz": 640, "detect_fps": 3}


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("app")
    inp = tmp / "recordings"
    make_cloud_clips.make(str(inp), 2)
    (inp / "gate").mkdir()
    make_plate_video.make(str(inp / "gate" / "ch02_20261031093000.mp4"))
    app = create_app(str(tmp / "data"), TOKEN, PORT)
    c = app.test_client()
    return {"tmp": tmp, "inp": inp, "out": tmp / "results", "app": app, "c": c}


def H(**extra):
    return {"X-CCTV-Token": TOKEN, "Host": HOST, **extra}


def wait(c, job_id, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = c.get(f"/api/jobs/{job_id}", headers=H()).get_json()["job"]
        if j["status"] not in ("queued", "running"):
            return j
        time.sleep(0.5)
    raise TimeoutError(job_id)


# ------------------------------------------------------------------ guards
def test_api_requires_the_token(env):
    c = env["c"]
    assert c.get("/api/health", headers={"Host": HOST}).status_code == 401
    assert c.get("/api/health", headers={"Host": HOST, "X-CCTV-Token": "wrong"}).status_code == 401
    r = c.get("/api/health", headers=H())
    assert r.status_code == 200 and r.get_json()["ok"] is True


def test_cookie_alone_cannot_call_the_api(env):
    c = env["app"].test_client()
    c.set_cookie("cctv_session", TOKEN, domain="127.0.0.1")
    assert c.get("/api/health", headers={"Host": HOST}).status_code == 401          # CSRF: no header
    assert c.post("/api/jobs", json={}, headers={"Host": HOST}).status_code == 401


def test_foreign_host_header_is_rejected(env):
    # DNS rebinding: evil.example resolving to 127.0.0.1 must not reach the API
    r = env["c"].get("/api/health", headers={"X-CCTV-Token": TOKEN, "Host": "evil.example:8765"})
    assert r.status_code == 403


def test_index_link_sets_cookie_and_page_carries_token(env):
    c = env["app"].test_client()
    assert c.get("/", headers={"Host": HOST}).status_code == 401
    assert c.get("/?t=bad", headers={"Host": HOST}).status_code == 401
    r = c.get(f"/?t={TOKEN}", headers={"Host": HOST})
    assert r.status_code == 302 and "HttpOnly" in r.headers["Set-Cookie"] and "SameSite=Strict" in r.headers["Set-Cookie"]
    page = c.get("/", headers={"Host": HOST})
    assert page.status_code == 200 and TOKEN.encode() in page.data
    assert "script-src 'self'" in page.headers["Content-Security-Policy"]


def test_rejects_bad_config(env):
    c = env["c"]
    r = c.post("/api/jobs", json={"config": {"input_dir": str(env["inp"]), "output_dir": str(env["out"]),
                                             "no_such_option": 1}}, headers=H())
    assert r.status_code == 400 and "Unknown config keys" in r.get_json()["error"]
    r = c.post("/api/jobs", json={"config": {"input_dir": str(env["inp"]), "output_dir": str(env["out"]),
                                             "open_hour": "9"}}, headers=H())
    assert r.status_code == 400 and any("open_hour" in p for p in r.get_json()["problems"])
    r = c.post("/api/jobs", json={"config": {"input_dir": str(env["tmp"] / "nope"), "output_dir": str(env["out"])}},
               headers=H())
    assert r.status_code == 400 and "folder not found" in r.get_json()["error"]
    r = c.post("/api/jobs", json={"config": {"input_dir": str(env["inp"]), "output_dir": str(env["inp"])}}, headers=H())
    assert r.status_code == 400
    assert c.post("/api/jobs", data="x" * (2 << 20), headers=H(**{"Content-Type": "application/json"})).status_code == 413
    assert c.post("/api/jobs", data="x" * (13 << 20), headers=H(**{"Content-Type": "application/json"})).status_code == 413


def test_folder_browser_lists_directories(env):
    r = env["c"].get("/api/fs/list?path=" + str(env["inp"]), headers=H())
    d = r.get_json()
    assert r.status_code == 200 and d["dirs"] == ["31Oct26New", "gate"] and d["videos_here"] == 0


# ------------------------------------------------------------- real flow
@pytest.fixture(scope="module")
def analysed(env):
    c = env["c"]
    cfg = {"input_dir": str(env["inp"]), "output_dir": str(env["out"]), "use_plates": True, **FAST}
    r = c.post("/api/scan", json={"config": cfg, "preset": "fast"}, headers=H())
    assert r.status_code == 202
    scan = wait(c, r.get_json()["job"]["id"])
    assert scan["status"] == "done" and scan["result"]["videos"] == 3 and scan["result"]["untimed"] == 2
    e = c.post("/api/estimate", json={"config": cfg, "preset": "fast"}, headers=H())
    assert e.status_code == 200 and e.get_json()["total_seconds"] > 0 and e.get_json()["projection"] is True
    body = {"config": cfg, "preset": "fast", "request_id": "same-click"}
    r1 = c.post("/api/jobs", json=body, headers=H())
    r2 = c.post("/api/jobs", json=body, headers=H())                          # double submit
    assert r1.status_code == 202 and r2.status_code == 200
    assert r1.get_json()["job"]["id"] == r2.get_json()["job"]["id"]
    r3 = c.post("/api/jobs", json={**body, "request_id": "another"}, headers=H())
    assert r3.status_code == 409                                               # one analysis at a time
    job = wait(c, r1.get_json()["job"]["id"])
    assert job["status"] == "done", job
    assert job["result"]["stats"]["videos_ok"] == 3
    return env


def test_trial_mode_is_reported(analysed):
    lic = analysed["c"].get("/api/license", headers=H()).get_json()
    assert lic["license"]["valid"] is False and lic["license"]["state"] in ("TRIAL", "NOT_CONFIGURED")


def test_results_endpoints(analysed):
    c, out = analysed["c"], str(analysed["out"])
    s = c.get("/api/results/summary", query_string={"out": out}, headers=H()).get_json()
    assert s["videos"] == 3 and s["events"] >= 3 and s["videos_with_approx_time"] == 0
    ev = c.get("/api/results/events", query_string={"out": out, "levels": "HIGH,REVIEW,LOW,NORMAL"}, headers=H()).get_json()
    assert ev["total"] == s["events"] and len(ev["items"]) == ev["total"]
    starts = [x["start"] for x in ev["items"]]
    assert starts == sorted(starts)
    first = ev["items"][0]
    assert set(first) == {"event_id", "level", "score", "camera", "start", "end", "duration", "kind", "people", "plates",
                          "identities", "flags", "thumb", "approx_time", "review"}     # response minimalism
    pl = c.get("/api/results/plates", query_string={"out": out, "q": "MH12AB1284", "edits": 1}, headers=H()).get_json()
    assert [p["text"] for p in pl["plates"]][:1] == ["MH12AB1234"]
    none = c.get("/api/results/plates", query_string={"out": out, "q": "KA01ZZ0000", "edits": 1}, headers=H()).get_json()
    assert none["plates"] == []
    plate_ev = c.get("/api/results/events", query_string={"out": out, "plate": "MH12AB1234", "levels": ""}, headers=H()).get_json()
    assert plate_ev["total"] == 1
    d = c.get(f"/api/results/events/{plate_ev['items'][0]['event_id']}", query_string={"out": out}, headers=H()).get_json()
    assert d["plates"][0]["text"] == "MH12AB1234" and d["plates"][0]["crop"]
    img = c.get("/api/file", query_string={"out": out, "p": d["plates"][0]["crop"]}, headers=H())
    assert img.status_code == 200 and img.data[:2] == b"\xff\xd8"
    hl = c.get("/api/results/highlights", query_string={"out": out}, headers=H()).get_json()["days"]
    assert [x["day"] for x in hl] == ["2026-10-31"] and hl[0]["reel"].startswith("highlights/2026/10-Oct/")
    assert "index" not in hl[0]                                              # list stays small
    day = c.get("/api/results/highlights/2026-10-31", query_string={"out": out}, headers=H()).get_json()
    assert len(day["index"]) == hl[0]["movements"] == 3
    assert [x["time"] for x in day["index"]] == sorted(x["time"] for x in day["index"])
    assert c.get("/api/results/highlights/2026-01-01", query_string={"out": out}, headers=H()).status_code == 404
    assert c.get("/api/results/highlights/x;drop", query_string={"out": out}, headers=H()).status_code == 400
    v = c.get("/api/file", query_string={"out": out, "p": hl[0]["reel"]}, headers=H(Range="bytes=0-99"))
    assert v.status_code == 206 and len(v.data) == 100                       # seekable video
    assert c.get("/api/results/events/nope", query_string={"out": out}, headers=H()).status_code == 404


def test_file_serving_cannot_escape_the_output_folder(analysed, tmp_path):
    c, out = analysed["c"], str(analysed["out"])
    for p in ("../recordings/31Oct26New/" + os.listdir(analysed["inp"] / "31Oct26New")[0], "/etc/passwd",
              "..\\..\\x", "highlights/../../data/settings.json"):
        r = c.get("/api/file", query_string={"out": out, "p": p}, headers=H())
        assert r.status_code in (403, 404), (p, r.status_code)
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    link = analysed["out"] / "link.txt"
    if not link.exists():
        os.symlink(secret, link)
    assert c.get("/api/file", query_string={"out": out, "p": "link.txt"}, headers=H()).status_code == 403
    assert c.get("/api/file", query_string={"out": out, "p": "analysis.db"}, headers=H()).status_code == 200  # inside: ok


def test_cookie_can_load_media_only(analysed):
    c = analysed["app"].test_client()
    c.set_cookie("cctv_session", TOKEN, domain="127.0.0.1")
    out = str(analysed["out"])
    hl = analysed["c"].get("/api/results/highlights", query_string={"out": out}, headers=H()).get_json()["days"][0]
    assert c.get("/api/file", query_string={"out": out, "p": hl["reel"]}, headers={"Host": HOST}).status_code == 200
    assert c.get("/api/results/summary", query_string={"out": out}, headers={"Host": HOST}).status_code == 401


def test_review_persists_across_reanalysis_sync(analysed):
    c, out = analysed["c"], str(analysed["out"])
    eid = c.get("/api/results/events", query_string={"out": out, "levels": ""}, headers=H()).get_json()["items"][0]["event_id"]
    r = c.put(f"/api/results/events/{eid}/review", query_string={"out": out},
              json={"status": "Suspicious", "notes": "check gate"}, headers=H())
    assert r.status_code == 200
    assert c.put(f"/api/results/events/{eid}/review", query_string={"out": out}, json={"status": "Nonsense"},
                 headers=H()).status_code == 400
    # the analyzer re-syncs analysis.db after every video; reviews live elsewhere and survive
    from cctv_analysis.storage import Store
    st = Store(str(analysed["tmp"] / "w.db"), os.path.join(out, "analysis.db"))
    st.commit()
    d = c.get(f"/api/results/events/{eid}", query_string={"out": out}, headers=H()).get_json()
    assert d["review"]["status"] == "Suspicious" and d["review"]["notes"] == "check gate"
    f = c.get("/api/results/events", query_string={"out": out, "levels": "", "review": "Suspicious"}, headers=H()).get_json()
    assert [x["event_id"] for x in f["items"]] == [eid]
    allev = c.get("/api/results/events", query_string={"out": out, "levels": ""}, headers=H()).get_json()["total"]
    nr = c.get("/api/results/events", query_string={"out": out, "levels": "", "review": "Not reviewed"}, headers=H()).get_json()
    assert nr["total"] == allev - 1 and eid not in [x["event_id"] for x in nr["items"]]
    day = f["items"][0]["start"][:10]
    one_day = c.get("/api/results/events", query_string={"out": out, "levels": "", "day_from": day, "day_to": day},
                    headers=H()).get_json()["total"]
    assert one_day == allev                                                  # every test event is on that day
    none = c.get("/api/results/events", query_string={"out": out, "levels": "", "day_from": "2027-01-01",
                                                      "day_to": "2027-01-01"}, headers=H()).get_json()["total"]
    assert none == 0


def test_evidence_export_and_verification(analysed):
    from cctv_analysis.evidence import verify_package
    c, out = analysed["c"], str(analysed["out"])
    items = c.get("/api/results/events", query_string={"out": out, "levels": ""}, headers=H()).get_json()["items"]
    ids = [x["event_id"] for x in items[:2]]
    r = c.post("/api/export", json={"out": out, "event_ids": ids, "case_id": "FIR-12/2026", "operator": "Tester"},
               headers=H())
    j = wait(c, r.get_json()["job"]["id"])
    assert j["status"] == "done", j
    zp = os.path.join(out, "exports", j["result"]["name"])
    with zipfile.ZipFile(zp) as z:
        names = z.namelist()
        man = json.loads(z.read("manifest.json"))
    assert {"manifest.json", "manifest.sha256", "incident_report.html"} <= set(names)
    assert any(n.endswith("original_segment.mp4") for n in names)
    assert man["case_id"] == "FIR-122026" and man["operator"] == "Tester" and len(man["sources"]) >= 1
    assert verify_package(zp)["ok"] is True
    # tampering with any file is detected
    tampered = zp.replace(".zip", "_t.zip")
    with zipfile.ZipFile(zp) as zi, zipfile.ZipFile(tampered, "w") as zo:
        for n in zi.namelist():
            data = zi.read(n)
            if n.endswith("event.json"):
                data = data.replace(b'"score"', b'"sc0re"')
            zo.writestr(n, data)
    v = verify_package(tampered)
    assert v["ok"] is False and v["modified"]
    dl = c.get(f"/api/exports/{j['result']['name']}", query_string={"out": out}, headers=H())
    assert dl.status_code == 200 and dl.data[:2] == b"PK"
    assert c.get("/api/exports/..%2Fanalysis.db", query_string={"out": out}, headers=H()).status_code in (400, 404)
    a = c.get("/api/audit", query_string={"out": out}, headers=H()).get_json()
    assert a["intact"] is True and {"analysis_started", "analysis_finished", "evidence_export"} <= {e["action"] for e in a["entries"]}


def test_audit_log_tampering_is_detected(analysed):
    p = os.path.join(str(analysed["out"]), "audit_log.jsonl")
    lines = open(p, encoding="utf-8").read().splitlines()
    bad = lines[:]
    bad[0] = bad[0].replace("analysis_started", "analysis_startex")
    with open(p + ".bad", "w", encoding="utf-8") as fh:
        fh.write("\n".join(bad) + "\n")
    from cctv_analysis.audit import AuditLog
    assert AuditLog(p).verify()[0] is True
    ok, n, line = AuditLog(p + ".bad").verify()
    assert ok is False and line == 1


def test_rate_limit(env):
    app = create_app(str(env["tmp"] / "data2"), TOKEN, PORT)
    c = app.test_client()
    codes = [c.put("/api/settings", json={"operator": "x"}, headers=H()).status_code for _ in range(65)]
    assert codes[:60] == [200] * 60 and codes[-1] == 429


def test_ai_providers_keep_keys_secret_and_are_tested_with_a_real_picture(env):
    """Settings › AI model providers: an ordered list with model names; keys go to secrets.json
    (0600) and never back to the browser; Test sends a picture to the provider with its key;
    analyses get the first as provider and the rest as fallbacks."""
    from test_vlm_providers import FakeServer

    from cctv_analysis.app import ai_providers
    c = env["c"]
    fake = FakeServer()
    try:
        r = c.put("/api/ai/providers", json={"providers": [
            {"id": "local", "provider": "openai", "base_url": fake.url, "model": "qwen3-vl:2b", "api_key": "sk-secret-value"},
            {"id": "nim", "provider": "nvidia", "model": "meta/llama-3.2-11b-vision-instruct", "enabled": True}]}, headers=H())
        assert r.status_code == 200 and "sk-secret-value" not in r.get_data(as_text=True)
        got = {p["id"]: p for p in r.get_json()["providers"]}
        assert got["local"]["key_set"] is True and got["nim"]["key_set"] is False
        for path in ("/api/settings", "/api/ai/providers"):
            assert "sk-secret-value" not in c.get(path, headers=H()).get_data(as_text=True)
        t = c.post("/api/ai/providers/local/test", headers=H()).get_json()
        assert t["vision_ok"] is True and t["models"] == ["qwen3-vl:2b"] and fake.requests[-1]["auth"] == "Bearer sk-secret-value"
        ov = ai_providers.config_overrides(env["app"].extensions["cctv"]["settings"])
        assert (ov["vlm_provider"], ov["vlm_model"], ov["vlm_base_url"]) == ("openai", "qwen3-vl:2b", fake.url)
        assert [f["model"] for f in ov["vlm_fallbacks"]] == ["meta/llama-3.2-11b-vision-instruct"]
        assert os.environ[ov["vlm_api_key_env"]] == "sk-secret-value"
        bad = c.put("/api/ai/providers", json={"providers": [{"id": "x", "provider": "openai", "model": ""}]}, headers=H())
        assert bad.status_code == 400 and "model name is empty" in bad.get_json()["error"]
        assert c.put("/api/ai/providers", json={"providers": []}, headers=H()).status_code == 200
        assert ai_providers.config_overrides(env["app"].extensions["cctv"]["settings"]) == {}
        sp = os.path.join(env["app"].config["CCTV_DATA"], "secrets.json")
        assert oct(os.stat(sp).st_mode & 0o777) == "0o600" and "sk-secret-value" not in open(sp).read()  # removed too
    finally:
        fake.close()


def test_retime_endpoint(analysed):
    c, out = analysed["c"], str(analysed["out"])
    r = c.post("/api/retime", json={"out": out}, headers=H())
    assert r.status_code == 202
    j = wait(c, r.get_json()["job"]["id"])
    assert j["status"] == "done" and j["result"]["videos"] == 3 and j["result"]["videos_changed"] == 0
    assert c.post("/api/retime", json={"out": str(analysed["tmp"])}, headers=H()).status_code == 404

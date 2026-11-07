"""Fast unit tests for guards that the slower end-to-end tests also cover."""
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cctv_analysis import licensing as L  # noqa: E402
from cctv_analysis.app.jobs import JobManager  # noqa: E402
from cctv_analysis.audit import AuditLog  # noqa: E402
from cctv_analysis.config import Config  # noqa: E402


def test_one_analysis_at_a_time_and_idempotent_start():
    jm = JobManager()
    gate = threading.Event()
    j1, c1 = jm.start("analysis", lambda job: gate.wait(5), request_id="a", exclusive=True)
    j2, c2 = jm.start("analysis", lambda job: None, request_id="a", exclusive=True)
    j3, c3 = jm.start("analysis", lambda job: None, request_id="b", exclusive=True)
    assert c1 and not c2 and not c3 and j1.id == j2.id == j3.id
    gate.set()
    t0 = time.time()
    while j1.status == "running" and time.time() - t0 < 5:
        time.sleep(0.05)
    j4, c4 = jm.start("analysis", lambda job: "ok", request_id="c", exclusive=True)
    assert c4 and j4.id != j1.id


def test_job_failure_is_reported_not_raised():
    jm = JobManager()
    j, _ = jm.start("scan", lambda job: 1 / 0)
    t0 = time.time()
    while j.status in ("queued", "running") and time.time() - t0 < 5:
        time.sleep(0.05)
    assert j.status == "failed" and "division" in j.error


def test_audit_chain_detects_edit_delete_and_reorder(tmp_path):
    p = str(tmp_path / "audit.jsonl")
    a = AuditLog(p)
    for i in range(4):
        a.append("step", n=i)
    assert a.verify() == (True, 4, 0)
    lines = open(p).read().splitlines()
    for bad, line in ((lines[:1] + [lines[1].replace('"n": 1', '"n": 9')] + lines[2:], 2),   # edited
                      (lines[:1] + lines[2:], 2),                                             # deleted
                      ([lines[1], lines[0]] + lines[2:], 1)):                                 # reordered
        q = str(tmp_path / "bad.jsonl")
        open(q, "w").write("\n".join(bad) + "\n")
        ok, _, first = AuditLog(q).verify()
        assert not ok and first == line


def test_trial_limit_applies_only_without_a_valid_licence():
    trial = L.LicenseStatus(False, "TRIAL", "")
    assert L.apply_limits(Config(max_videos=0), trial).max_videos == L.TRIAL_MAX_VIDEOS
    assert L.apply_limits(Config(max_videos=2), trial).max_videos == 2
    assert L.apply_limits(Config(max_videos=50), trial).max_videos == L.TRIAL_MAX_VIDEOS
    ok = L.LicenseStatus(True, "LICENSED", "")
    assert L.apply_limits(Config(max_videos=0), ok).max_videos == 0


def test_case_run_progress_is_the_overall_percent_not_one_source_s():
    """run_case reports 'source 2 of 4, video 1 of 2 of that source' with the overall percent;
    the job must not replace it by the per-source video percentage (the bar jumped backwards)."""
    from cctv_analysis.app.jobs import Job
    j = Job("analysis")
    j.started = time.time() - 10
    j.update({"stage": "video", "index": 1, "total": 2, "source_index": 2, "sources": 4, "percent": 30.0})
    assert j.progress["percent"] == 30.0 and j.progress["eta_seconds"] == int(10 / 30 * 70)
    j.update({"stage": "video", "index": 2, "total": 2, "source_index": 2, "sources": 4, "percent": 40.0})
    assert j.progress["percent"] == 40.0
    single = Job("analysis")
    single.update({"stage": "video", "index": 2, "total": 2})                 # a single-folder run is unchanged
    assert single.progress["percent"] == 47.5


def test_every_setup_field_is_a_config_field_with_a_valid_default():
    """One name, one meaning: the Set up form is rendered from features.FIELDS and its values go
    straight into Config – a key that is not a Config field (or a default outside its own range /
    choices) would be silently dropped or rejected only when the officer presses Start."""
    from dataclasses import fields

    from cctv_analysis.app import features
    names = {f.name for f in fields(Config)}
    assert [k for k, *_ in features.FIELDS if k not in names] == []
    for f in features.describe()["fields"]:
        t, v = f["type"], f["default"]
        if t.startswith("range:"):
            lo, hi, _ = (float(x) for x in t.split(":")[1:])
            assert lo <= v <= hi, f
        elif t.startswith("choice:"):
            assert v in t.split(":", 1)[1].split(","), f
    cfg = Config.from_dict({f["key"]: f["default"] for f in features.describe()["fields"]})
    assert cfg.validate() == []

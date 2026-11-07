"""Section 63 BSA 2023 certificate, hash report, evidence bundle and Merkle tree."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile

import cv2
import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cctv_analysis import enhance as EN  # noqa: E402
from cctv_analysis import merkle  # noqa: E402
from cctv_analysis import section63 as S63  # noqa: E402
from cctv_analysis.audit import AuditLog  # noqa: E402
from cctv_analysis.cases import CaseStore  # noqa: E402
from cctv_analysis.ingest import ingest_source  # noqa: E402
from cctv_analysis.storage import Store  # noqa: E402


def _mth(items):
    """RFC 6962 section 2.1, written as the RFC defines it (recursive split at the largest power of 2)."""
    if not items:
        return hashlib.sha256(b"").digest()
    if len(items) == 1:
        return hashlib.sha256(b"\x00" + items[0]).digest()
    k = 1
    while k * 2 < len(items):
        k *= 2
    return hashlib.sha256(b"\x01" + _mth(items[:k]) + _mth(items[k:])).digest()


def test_merkle_root_equals_rfc6962_for_every_size():
    for n in range(0, 41):
        items = [f"file{i}.mp4  {hashlib.sha256(str(i).encode()).hexdigest()}".encode() for i in range(n)]
        assert merkle.root(items) == _mth(items).hex(), n
        for i in range(n):
            assert merkle.verify_proof(items[i], merkle.proof(items, i), merkle.root(items))
    items = [b"a", b"b", b"c"]
    assert not merkle.verify_proof(b"x", merkle.proof(items, 0), merkle.root(items))     # a forged item fails
    assert merkle.root(items) != merkle.root(list(reversed(items)))                        # order matters


def _video(path, secs=6, fps=10):
    vw = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (320, 240))
    for i in range(secs * fps):
        f = np.full((240, 320, 3), 100, np.uint8)
        cv2.rectangle(f, (10 + i * 3, 80), (50 + i * 3, 200), (40, 40, 200), -1)
        vw.write(f)
    vw.release()


@pytest.fixture
def case_with_event(tmp_path):
    seized = tmp_path / "seized" / "ShopA_DVR"
    seized.mkdir(parents=True)
    _video(str(seized / "ch01_20261031211400.mp4"))
    store = CaseStore(str(tmp_path / "cases"))
    c = store.create("FIR-112-2026", user="SI Sharma", investigating_officer="SI R. Sharma", fir_number="112/2026",
                     police_station="Kotwali", under_sections="BNS 303(2), 331(4)", district="Jaipur",
                     state="Rajasthan", incident_time="2026-10-31 21:10")
    src = c.add_source(str(seized), "Shop A DVR", "-00:11:00", user="SI Sharma",
                       device={"kind": "DVR", "make": "Hikvision", "model": "DS-7104HGHI", "serial": "J12345678",
                               "storage_medium": "HDD", "custodian": "Ramesh Kumar (shop owner)",
                               "control": ["Owned", "Operated"]})
    out = c.source_dir(src.source_id)
    ingest_source(str(seized), out)
    st = Store(os.path.join(out, "analysis.db"))
    video = str(seized / "ch01_20261031211400.mp4")
    st.insert("events", {"event_id": "ch01-20261031-211405-0001001", "video_id": 1, "video_path": video,
                         "rel_path": "ch01_20261031211400.mp4", "camera": "ch01", "start_off": 1.0, "end_off": 4.0,
                         "start_dt": "2026-10-31 21:14:05", "end_dt": "2026-10-31 21:14:08", "duration": 3.0,
                         "kind": "person", "level": "REVIEW", "score": 45, "time_source": "filename",
                         "snapshots": "[]", "flags": "[]"})
    st.close()
    img = cv2.imread(os.path.join(ROOT, "tests", "assets", "car_rear.png"))
    out_img, rec = EN.apply_chain(img, [{"op": "CLAHE", "clip_limit": 3.0}, {"op": "UnsharpMask", "sigma": 1.5}])
    saved = EN.save_enhancement(os.path.join(c.root, "enhancements"), "plate", {
        "source_id": "shop-a-dvr", "camera": "Shop A DVR/ch01", "calibrated_time": "2026-10-31 21:03:05",
        "dvr_time": "2026-10-31 21:14:05"}, img, out_img, rec)
    return c, saved, video


CERT = "certificates/shop-a-dvr/"


def _bundle(c, saved, tmp_path, **kw):
    dest = str(tmp_path / "bundle.zip")
    res = S63.build_bundle(c, [{"source_id": "shop-a-dvr", "event_id": "ch01-20261031-211405-0001001"}], dest,
                           part_a={"shop-a-dvr": {"name": "Ramesh Kumar", "relation_name": "Suresh Kumar",
                                                  "address": "12 MI Road, Jaipur", "place": "Jaipur"}},
                           part_b={"name": "Dr A. Verma", "designation": "Scientific Officer, FSL Jaipur"},
                           enhancements=[saved["name"]], operator="SI Sharma", **kw)
    return dest, res


def test_certificate_follows_the_schedule_and_lists_real_hashes(case_with_event, tmp_path):
    c, saved, video = case_with_event
    dest, res = _bundle(c, saved, tmp_path)
    with zipfile.ZipFile(dest) as z:
        names = set(z.namelist())
        cert = json.loads(z.read(CERT + "certificate.json"))
        page = " ".join(z.read(CERT + "Section63_Certificate.html").decode().split())   # as rendered: whitespace collapses
        for e in cert["exhibits"]:                    # every listed hash is the real hash of the file in the bundle
            assert hashlib.sha256(z.read(e["file"])).hexdigest() == e["sha256"]
        original = z.read("originals/shop-a-dvr/ch01-20261031-211405-0001001.mp4")
    assert {CERT + "Section63_Certificate.pdf", CERT + "Section63_Certificate.html", "checksums.sha256",
            "hash_report.csv", "README.txt", CERT + "certificate.json", "audit/case_audit_log.jsonl",
            "audit/shop-a-dvr_ingest.json", f"enhanced/{saved['name']}"} <= names
    assert original[4:8] == b"ftyp"                   # a playable MP4 cut by stream copy
    # statutory wording, both parts, in order
    for phrase in ["THE SCHEDULE", "[See section 63(4)(c)]", "PART A", "(To be filled by the Party)",
                   "was under the lawful control for regularly", "it has not affected the electronic/digital record or its accuracy",
                   "PART B", "(To be filled by the Expert)", "(Hash report to be enclosed with the certificate)",
                   "(Name, designation and signature)", "Time (IST):", "HASH REPORT",
                   "a segment may begin a few seconds before the span shown"]:
        assert phrase in page, phrase
    assert page.index("PART A") < page.index("PART B") < page.index("HASH REPORT")
    assert "☑ DVR" in page and "☐ Mobile" in page and "☑ SHA256" in page and "☐ MD5" in page
    assert "☑ Owned" in page and "☑ Operated" in page and "☐ Managed" in page
    assert "Ramesh Kumar" in page and "Scientific Officer, FSL Jaipur" in page
    # case details and device particulars
    assert cert["case"]["fir_number"] == "112/2026" and cert["case"]["under_sections"] == "BNS 303(2), 331(4)"
    dev = cert["devices"][0]
    assert (dev["make_model"], dev["serial"], dev["storage_medium"], dev["clock_offset"]) == \
        ("Hikvision DS-7104HGHI", "J12345678", "HDD", "-00:11:00")
    # the hash report: camera, both time spans (the calibrated one moved by the clock correction)
    ex = next(e for e in cert["exhibits"] if e["kind"] == "original segment")
    assert ex["camera"] == "Shop A DVR/ch01"
    assert ex["span_dvr"] == "2026-10-31 21:14:05 to 21:14:08"
    assert ex["span_calibrated"] == "2026-10-31 21:03:05 to 21:03:08"
    assert any(e["kind"].startswith("enhanced still: CLAHE(clip_limit=3, tile_grid=(8,8)) → UnsharpMask")
               for e in cert["exhibits"])
    # the seized recording's acquisition hash
    assert cert["seized_files"][0]["sha256"] == hashlib.sha256(open(video, "rb").read()).hexdigest()
    assert cert["exhibits_merkle_root"] == merkle.root(merkle.file_lines({e["file"]: e["sha256"] for e in cert["exhibits"]}))
    assert res["certificates"] == {"shop-a-dvr": cert["exhibits_merkle_root"]}
    assert len(cert["exhibits"]) == 2 and all(e["kind"] == "original segment" or e["kind"].startswith("enhanced still")
                                              for e in cert["exhibits"])      # tool records are not device records


def test_pdf_is_valid_and_carries_both_parts_and_hashes(case_with_event, tmp_path):
    from pypdf import PdfReader
    c, saved, _ = case_with_event
    dest, _ = _bundle(c, saved, tmp_path)
    with zipfile.ZipFile(dest) as z:
        pdf = z.read(CERT + "Section63_Certificate.pdf")
        cert = json.loads(z.read(CERT + "certificate.json"))
    assert pdf.startswith(b"%PDF-") and pdf.rstrip().endswith(b"%%EOF")
    p = tmp_path / "c.pdf"
    p.write_bytes(pdf)
    r = PdfReader(str(p))
    assert len(r.pages) >= 3
    text = " ".join((pg.extract_text() or "") for pg in r.pages).replace("\n", " ")
    for phrase in ("CERTIFICATE", "PART A", "PART B", "To be filled by the Expert", "HASH REPORT", "SHA256",
                   "112/2026", "Kotwali", "J12345678"):
        assert phrase in text, phrase
    flat = text.replace(" ", "")
    ex = next(e for e in cert["exhibits"] if e["kind"] == "original segment")
    assert ex["sha256"][:32] in flat                   # the digest is printed (monospace may wrap)
    assert r.metadata.title == "Section 63 BSA certificate FIR-112-2026"


def test_checksums_verify_with_sha256sum_and_tampering_is_detected(case_with_event, tmp_path):
    c, saved, _ = case_with_event
    dest, _ = _bundle(c, saved, tmp_path)
    assert S63.verify_bundle(dest)["ok"]
    x = tmp_path / "x"
    with zipfile.ZipFile(dest) as z:
        z.extractall(x)
    if shutil.which("sha256sum"):
        r = subprocess.run(["sha256sum", "-c", "checksums.sha256"], cwd=x, capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        assert r.stdout.count(": OK") == len(open(x / "checksums.sha256").read().splitlines())
    # change one byte of the original segment
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(dest) as zin, zipfile.ZipFile(bad, "w") as zout:
        for n in zin.namelist():
            data = zin.read(n)
            if n.startswith("originals/"):
                data = data[:-1] + bytes([data[-1] ^ 1])
            zout.writestr(n, data)
    v = S63.verify_bundle(str(bad))
    assert not v["ok"] and v["modified"] == ["originals/shop-a-dvr/ch01-20261031-211405-0001001.mp4"]
    # an extra file slipped into the bundle
    extra = tmp_path / "extra.zip"
    shutil.copy(dest, extra)
    with zipfile.ZipFile(extra, "a") as z:
        z.writestr("originals/planted.mp4", b"x")
    assert S63.verify_bundle(str(extra))["unlisted"] == ["originals/planted.mp4"]
    # a forged certificate root – naive (checksum list left alone) and consistent (checksum list
    # rewritten to match): only the recomputed Merkle root exposes the second one
    for consistent in (False, True):
        forged = tmp_path / f"forged{int(consistent)}.zip"
        with zipfile.ZipFile(dest) as zin, zipfile.ZipFile(forged, "w") as zout:
            files = {n: zin.read(n) for n in zin.namelist()}
            d = json.loads(files[CERT + "certificate.json"])
            d["exhibits_merkle_root"] = "0" * 64
            files[CERT + "certificate.json"] = json.dumps(d).encode()
            if consistent:
                lines = [ln.split("  ", 1) for ln in files["checksums.sha256"].decode().splitlines()]
                files["checksums.sha256"] = "".join(
                    f"{hashlib.sha256(files[n]).hexdigest() if n == CERT + 'certificate.json' else h}  {n}\n"
                    for h, n in lines).encode()
            for n, data in files.items():
                zout.writestr(n, data)
        v = S63.verify_bundle(str(forged))
        assert not v["ok"], consistent
        if consistent:
            assert v["modified"] == [] and v["certificates"]["shop-a-dvr"]["merkle_ok"] is False


def test_device_ticks_follow_the_schedule_boxes(case_with_event):
    c, _, _ = case_with_event
    for kind, ticked, other in (("NVR", ["DVR"], ""), ("Flash Drive", ["Flash Drive"], ""), ("Cloud", ["Cloud"], ""),
                                ("Other", ["Other"], "")):
        c.update_source("shop-a-dvr", device={"kind": kind})
        d = S63.certificate_data(c, [])["devices"][0]
        assert d["ticks"] == ticked and d["other"] == other, kind
    with pytest.raises(ValueError):
        c.update_source("shop-a-dvr", device={"kind": "Toaster"})


def test_blank_declarants_print_lines_not_invented_names(case_with_event, tmp_path):
    c, saved, _ = case_with_event
    data = S63.certificate_data(c, [{"file": "a.mp4", "sha256": "0" * 64, "camera": "", "span_calibrated": "",
                                     "span_dvr": ""}])
    page = S63.certificate_html(data)
    assert "I, ____________________________ (Name)" in page
    assert data["part_a"]["name"] == "" and data["part_b"]["name"] == ""


def test_a_missing_source_recording_stops_the_bundle(case_with_event, tmp_path):
    c, saved, video = case_with_event
    os.rename(video, video + ".moved")
    with pytest.raises(FileNotFoundError, match="drive plugged in"):
        _bundle(c, saved, tmp_path)
    assert not os.path.exists(tmp_path / "bundle.zip")


def test_each_device_gets_its_own_certificate_with_its_own_declarant(case_with_event, tmp_path):
    """Part A is signed by the person in charge of one device: a bundle with two DVRs carries two
    certificates, each listing only that device and that device's records."""
    c, saved, _ = case_with_event
    other = tmp_path / "seized" / "Jeweler_NVR"
    other.mkdir(parents=True)
    _video(str(other / "cam3_20261031212300.mp4"))
    src2 = c.add_source(str(other), "Jeweler NVR", "0", device={"kind": "NVR", "make": "Dahua", "serial": "NVR-9"})
    out2 = c.source_dir(src2.source_id)
    ingest_source(str(other), out2)
    st = Store(os.path.join(out2, "analysis.db"))
    st.insert("events", {"event_id": "cam3-20261031-212302-0002001", "video_id": 1,
                         "video_path": str(other / "cam3_20261031212300.mp4"), "rel_path": "cam3_20261031212300.mp4",
                         "camera": "cam3", "start_off": 2.0, "end_off": 5.0, "start_dt": "2026-10-31 21:23:02",
                         "end_dt": "2026-10-31 21:23:05", "duration": 3.0, "kind": "person", "level": "REVIEW",
                         "score": 40, "time_source": "filename", "snapshots": "[]", "flags": "[]"})
    st.close()
    c.update_source("shop-a-dvr", declarant={"name": "Ramesh Kumar", "place": "Jaipur"})
    c.update_source(src2.source_id, declarant={"name": "Anil Soni", "relation_name": "B. Soni"})
    c.set_expert({"name": "Dr A. Verma", "designation": "Scientific Officer, FSL Jaipur"})
    dest = str(tmp_path / "two.zip")
    res = S63.build_bundle(c, [{"source_id": "shop-a-dvr", "event_id": "ch01-20261031-211405-0001001"},
                               {"source_id": src2.source_id, "event_id": "cam3-20261031-212302-0002001"}], dest,
                           enhancements=[saved["name"]])
    assert set(res["certificates"]) == {"shop-a-dvr", "jeweler-nvr"}
    with zipfile.ZipFile(dest) as z:
        a = json.loads(z.read("certificates/shop-a-dvr/certificate.json"))
        b = json.loads(z.read("certificates/jeweler-nvr/certificate.json"))
        page_b = " ".join(z.read("certificates/jeweler-nvr/Section63_Certificate.html").decode().split())
        csv_rows = z.read("hash_report.csv").decode().splitlines()
    assert [d["source_id"] for d in a["devices"]] == ["shop-a-dvr"] and [d["source_id"] for d in b["devices"]] == \
        ["jeweler-nvr"]
    assert {e["file"] for e in a["exhibits"]} == {"originals/shop-a-dvr/ch01-20261031-211405-0001001.mp4",
                                                  f"enhanced/{saved['name']}"}
    assert {e["file"] for e in b["exhibits"]} == {"originals/jeweler-nvr/cam3-20261031-212302-0002001.mp4"}
    assert a["part_a"]["name"] == "Ramesh Kumar" and b["part_a"]["name"] == "Anil Soni"
    assert a["part_b"]["name"] == b["part_b"]["name"] == "Dr A. Verma"
    assert "Anil Soni" in page_b and "Ramesh Kumar" not in page_b and "Hikvision" not in page_b
    assert [x["source_id"] for x in b["seized_files"]] == ["jeweler-nvr"]          # only its own acquisition hashes
    assert csv_rows[0].split(",")[:3] == ["file", "kind", "certificate"] and len(csv_rows) > 4
    v = S63.verify_bundle(dest)
    assert v["ok"] and set(v["certificates"]) == {"shop-a-dvr", "jeweler-nvr"}
    # the declarant edits are in the case audit log
    acts = [e["action"] for e in AuditLog(os.path.join(c.root, "audit_log.jsonl")).entries(100)]
    assert "expert_updated" in acts and acts.count("source_updated") >= 2


def test_uncertifiable_inputs_are_refused(case_with_event, tmp_path):
    c, saved, _ = case_with_event
    img = cv2.imread(os.path.join(ROOT, "tests", "assets", "car_rear.png"))
    out_img, rec = EN.apply_chain(img, [{"op": "CLAHE"}])
    orphan = EN.save_enhancement(os.path.join(c.root, "enhancements"), "orphan", {"camera": "?"}, img, out_img, rec)
    with pytest.raises(ValueError, match="does not name a source"):
        S63.build_bundle(c, [], str(tmp_path / "o.zip"), enhancements=[orphan["name"]])
    with pytest.raises(ValueError, match="at least one"):
        S63.build_bundle(c, [], str(tmp_path / "o.zip"))
    with pytest.raises(ValueError, match="unknown declarant field"):
        c.update_source("shop-a-dvr", declarant={"name": "x", "phone": "1"})
    assert not os.path.exists(tmp_path / "o.zip")


def test_a_file_removed_from_the_bundle_is_reported(case_with_event, tmp_path):
    c, saved, _ = case_with_event
    dest, _ = _bundle(c, saved, tmp_path)
    cut = tmp_path / "cut.zip"
    with zipfile.ZipFile(dest) as zin, zipfile.ZipFile(cut, "w") as zout:
        for n in zin.namelist():
            if not n.startswith("enhanced/") or not n.endswith("_input.png"):
                zout.writestr(n, zin.read(n))
    v = S63.verify_bundle(str(cut))
    assert not v["ok"] and len(v["missing"]) == 1 and v["missing"][0].endswith("_input.png")


def test_an_evidence_package_without_its_recording_says_so(case_with_event, tmp_path):
    """Single-folder evidence export: when the recording is gone the package must state that the
    original segment is missing (it used to be left out silently)."""
    from cctv_analysis.evidence import export_evidence
    c, saved, video = case_with_event
    out = c.source_dir("shop-a-dvr")
    ok = export_evidence(out, ["ch01-20261031-211405-0001001"], str(tmp_path / "with.zip"))
    with zipfile.ZipFile(ok["zip"]) as z:
        ev = json.loads(z.read("events/ch01-20261031-211405-0001001/event.json"))
        assert "events/ch01-20261031-211405-0001001/original_segment.mp4" in z.namelist()
    assert ev["notes"]["original_segment"].startswith("stream copy")
    os.rename(video, video + ".moved")
    gone = export_evidence(out, ["ch01-20261031-211405-0001001"], str(tmp_path / "without.zip"))
    with zipfile.ZipFile(gone["zip"]) as z:
        ev = json.loads(z.read("events/ch01-20261031-211405-0001001/event.json"))
        report = z.read("incident_report.html").decode()
        assert not any(n.endswith("original_segment.mp4") for n in z.namelist())
    assert ev["notes"]["original_segment"].startswith("NOT INCLUDED – the recording ch01_20261031211400.mp4 was not found")
    assert "NOT INCLUDED" in report

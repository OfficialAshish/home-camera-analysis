"""Offline licences: every deny path must deny; only a genuine, current key for this machine passes."""
import datetime as dt
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cctv_analysis import licensing as L  # noqa: E402


@pytest.fixture()
def keys(tmp_path):
    """A throw-away vendor key pair made with the real vendor tool."""
    pub = tmp_path / "pub.pem"
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "license_tool.py"), "init", "--key-dir",
                        str(tmp_path / "vendor"), "--public-out", str(pub)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert oct(os.stat(tmp_path / "vendor" / "signing_key.pem").st_mode & 0o777) == "0o600"
    return tmp_path / "vendor", pub.read_bytes()


def issue(vendor, *args):
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "license_tool.py"), "issue", "--key-dir",
                        str(vendor), *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_valid_six_month_licence(keys):
    vendor, pub = keys
    key = issue(vendor, "--customer", "Ravi Stores", "--months", "6", "--start", "2026-05-03")
    st = L.verify(key, pub, today=dt.date(2026, 6, 1))
    assert st.valid and st.state == "LICENSED" and st.customer == "Ravi Stores" and st.expires == "2026-11-03"
    assert st.days_left == (dt.date(2026, 11, 3) - dt.date(2026, 6, 1)).days


def test_expiry_is_enforced(keys):
    vendor, pub = keys
    key = issue(vendor, "--customer", "X", "--days", "30", "--start", "2026-01-01")
    assert L.verify(key, pub, today=dt.date(2026, 1, 31)).valid
    st = L.verify(key, pub, today=dt.date(2026, 2, 1))
    assert not st.valid and st.state == "EXPIRED"


def test_edited_key_is_rejected(keys):
    vendor, pub = keys
    key = issue(vendor, "--customer", "X", "--days", "30", "--start", "2026-01-01")
    head, body, sig = key.split(".")
    p = json.loads(L._b64d(body))
    p["expires"] = "2099-01-01"                                 # customer tries to extend it
    forged = ".".join([head, L._b64e(json.dumps(p, sort_keys=True, separators=(",", ":")).encode()), sig])
    st = L.verify(forged, pub, today=dt.date(2026, 1, 10))
    assert not st.valid and st.state == "INVALID" and "signature" in st.reason


def test_key_from_another_vendor_is_rejected(keys, tmp_path):
    vendor, pub = keys
    other = tmp_path / "other"
    subprocess.run([sys.executable, os.path.join(ROOT, "tools", "license_tool.py"), "init", "--key-dir", str(other),
                    "--public-out", str(tmp_path / "other.pem")], check=True, capture_output=True)
    key = issue(other, "--customer", "Pirate", "--months", "12")
    assert L.verify(key, pub).state == "INVALID"


def test_machine_binding(keys):
    vendor, pub = keys
    key = issue(vendor, "--customer", "Mall", "--days", "90", "--machine", L.machine_id())
    assert L.verify(key, pub).valid
    assert L.verify(key, pub, this_machine="0" * 20).state == "INVALID"


def test_clock_rollback_detected(keys, tmp_path):
    vendor, pub = keys
    key = issue(vendor, "--customer", "X", "--days", "60", "--start", "2026-03-01")
    store = L.LicenseStore(str(tmp_path / "app"))
    assert store.install(key, pub, today=dt.date(2026, 4, 20)).valid
    assert store.status(pub, today=dt.date(2026, 4, 25)).valid
    st = store.status(pub, today=dt.date(2026, 3, 10))           # clock set back to stretch the licence
    assert not st.valid and st.state == "CLOCK_TAMPERED"


def test_fail_closed_states(keys, tmp_path):
    vendor, pub = keys
    assert L.verify("", pub).state == "TRIAL"
    assert L.verify("garbage", pub).state == "INVALID"
    assert L.verify("CCTV1.e30.AAAA", pub).state == "INVALID"
    missing = L.verify(issue(vendor, "--customer", "X", "--months", "1"), None) \
        if not os.path.exists(L.PUBLIC_KEY_PATH) else None
    if missing is not None:
        assert missing.state == "NOT_CONFIGURED" and not missing.valid
    store = L.LicenseStore(str(tmp_path / "app"))
    bad = store.install("garbage", pub)
    assert not bad.valid and store.key() == ""                  # an invalid key is never installed

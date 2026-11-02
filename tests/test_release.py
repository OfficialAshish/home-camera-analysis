"""Release checks that need the owner's credentials (docs/CREDENTIAL-TASKS.md).
Run with:  python -m pytest -m release tests/test_release.py"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cctv_analysis import licensing as L  # noqa: E402

pytestmark = pytest.mark.release


def test_licence_public_key_is_configured():
    assert os.path.exists(L.PUBLIC_KEY_PATH), "run: python tools/license_tool.py init"
    st = L.verify("")
    assert st.state == "TRIAL", st.reason          # NOT_CONFIGURED would mean no / unreadable key


@pytest.mark.skipif(sys.platform != "win32", reason="Authenticode signatures are checked on Windows")
def test_windows_exe_is_signed():
    exe = os.path.join(ROOT, "dist", "CCTVAnalysis", "CCTVAnalysis.exe")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", f"(Get-AuthenticodeSignature '{exe}').Status"],
                       capture_output=True, text=True)
    assert r.stdout.strip() == "Valid", r.stdout + r.stderr

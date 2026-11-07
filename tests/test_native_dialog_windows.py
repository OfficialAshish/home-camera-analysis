"""The Windows folder dialog really opens, in front, and returns the chosen folder (Windows only;
run by the windows-app CI job). Windows UI Automation finds the dialog the helper opened, checks it
is on screen and clicks OK on the pre-selected folder – the same path the app's
/api/fs/pick takes."""
import os
import subprocess
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

pytestmark = pytest.mark.skipif(not sys.platform.startswith("win"), reason="the Windows folder dialog")

ACCEPT = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$A = [System.Windows.Automation.AutomationElement]
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline) {
  $cond = New-Object System.Windows.Automation.PropertyCondition($A::NameProperty, "Browse For Folder")
  $win = $A::RootElement.FindFirst([System.Windows.Automation.TreeScope]::Children, $cond)
  if ($win) {
    $r = $win.Current.BoundingRectangle
    Write-Output ("FOUND {0}x{1} offscreen={2}" -f $r.Width, $r.Height, $win.Current.IsOffscreen)
    $okc = New-Object System.Windows.Automation.PropertyCondition($A::NameProperty, "OK")
    $ok = $win.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $okc)
    $ok.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
    exit 0
  }
  Start-Sleep -Milliseconds 300
}
Write-Output "NOT FOUND"
exit 1
"""


def test_the_windows_folder_dialog_opens_on_screen_and_returns_the_chosen_folder(tmp_path):
    from cctv_analysis.app.server import create_app
    target = tmp_path / "Seized DVR (ch01) – दुकान"          # spaces, brackets, a dash and Devanagari
    target.mkdir()
    app = create_app(str(tmp_path / "data"), "t", 8765)
    c = app.test_client()
    H = {"X-CCTV-Token": "t", "Host": "127.0.0.1:8765"}
    assert c.get("/api/fs/roots", headers=H).get_json()["native_dialog"] is True
    out = {}

    def ask():
        out["r"] = c.post("/api/fs/pick", json={"title": "Drishti test", "start": str(target)}, headers=H)
    t = threading.Thread(target=ask)
    t.start()
    ui = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ACCEPT],
                        capture_output=True, text=True, timeout=120)
    t.join(120)
    print(ui.stdout, ui.stderr)
    assert ui.returncode == 0 and "FOUND" in ui.stdout, ui.stdout + ui.stderr
    assert "offscreen=False" in ui.stdout                    # visible, not hidden behind a minimised owner
    r = out["r"]
    assert r.status_code == 200, r.get_json()
    assert os.path.normcase(r.get_json()["path"]) == os.path.normcase(str(target))

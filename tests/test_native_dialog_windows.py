"""The Windows folder dialog really opens, in front, and returns the chosen folder (Windows only;
run by the windows-app CI job). Windows UI Automation finds the dialog the helper opened, checks it
is on screen and clicks OK on the pre-selected folder – the same path the app's
/api/fs/pick takes. A dialog that does not appear fails the test (with the windows that were on
the screen instead) and the helper is ended, so the job never waits for it."""
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
$T = [System.Windows.Automation.TreeScope]
$C = [System.Windows.Automation.PropertyCondition]
# the dialog belongs to the app's PowerShell helper (its command line holds the helper script,
# which creates the folder-browser dialog - the pattern is split so this script does not match itself);
# a dialog window has the class #32770 and may be listed under its (invisible) owner window
$pat = '*FolderBrowser' + 'Dialog*'
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
  $p = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
       Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like $pat } | Select-Object -First 1
  if ($p) {
    $tops = $A::RootElement.FindAll($T::Children, (New-Object $C($A::ProcessIdProperty, [int]$p.ProcessId)))
    foreach ($w in $tops) {
      $dlg = if ($w.Current.ClassName -eq '#32770') { $w } else { $w.FindFirst($T::Children, (New-Object $C($A::ClassNameProperty, '#32770'))) }
      if ($dlg) {
        $r = $dlg.Current.BoundingRectangle
        Write-Output ("FOUND '{0}' {1}x{2} offscreen={3}" -f $dlg.Current.Name, $r.Width, $r.Height, $dlg.Current.IsOffscreen)
        $ok = $dlg.FindFirst($T::Descendants, (New-Object $C($A::AutomationIdProperty, '1')))     # IDOK
        $ok.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
        exit 0
      }
    }
  }
  Start-Sleep -Milliseconds 500
}
Write-Output "NOT FOUND"
# what was on the screen instead (for the CI log)
$A::RootElement.FindAll($T::Children, [System.Windows.Automation.Condition]::TrueCondition) |
  ForEach-Object { Write-Output ("  window: '{0}' class={1} pid={2}" -f $_.Current.Name, $_.Current.ClassName, $_.Current.ProcessId) }
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
  ForEach-Object { Write-Output ("  powershell pid={0} helper={1}" -f $_.ProcessId, ($_.ProcessId -ne $PID -and $_.CommandLine -like $pat)) }
exit 1
"""

# ends a dialog helper that is still open (a failed run must not keep the CI job waiting an hour)
CLEANUP = r"""
$pat = '*FolderBrowser' + 'Dialog*'
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
  Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like $pat } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
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
    t = threading.Thread(target=ask, daemon=True)
    t.start()
    try:
        ui = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ACCEPT],
                            capture_output=True, text=True, timeout=150)
        t.join(60)
    finally:
        subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", CLEANUP],
                       capture_output=True, timeout=60)
    print(ui.stdout, ui.stderr)
    assert ui.returncode == 0 and "FOUND" in ui.stdout, ui.stdout + ui.stderr
    assert "offscreen=False" in ui.stdout and "Browse For Folder" in ui.stdout   # on screen, not minimised
    r = out["r"]
    assert r.status_code == 200, r.get_json()
    assert os.path.normcase(r.get_json()["path"]) == os.path.normcase(str(target))

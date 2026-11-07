"""Start the desktop app:  python -m cctv_analysis.app  [--port 8765] [--no-browser] [--data-dir DIR]

Opens the browser at the private link (it carries a per-launch access token). Keep this
window open while using the app; close it to stop the app."""
import argparse
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser

WIN_PICKER = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
# an invisible, top-most owner window: a dialog started by a background process would otherwise
# open behind the browser (Windows does not let background processes take the foreground)
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true; $owner.ShowInTaskbar = $false; $owner.Opacity = 0
$owner.FormBorderStyle = 'None'; $owner.StartPosition = 'CenterScreen'
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.Show(); $owner.Activate(); $owner.BringToFront()
$dlg = New-Object System.Windows.Forms.FolderBrowserDialog
$dlg.Description = $env:DRISHTI_PICK_TITLE
$dlg.ShowNewFolderButton = $true
if ($env:DRISHTI_PICK_START -and (Test-Path -LiteralPath $env:DRISHTI_PICK_START)) { $dlg.SelectedPath = $env:DRISHTI_PICK_START }
$res = $dlg.ShowDialog($owner)
$owner.Close()
if ($res -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($dlg.SelectedPath) }
"""


def pick_folder(title: str = "Choose a folder", start: str = "") -> int:
    """Helper mode for the system folder dialog: prints the chosen folder ('' = cancelled).
    Exit 3 = no dialog can be shown here. Windows: the Windows folder dialog through PowerShell
    (no tkinter needed, works in the packaged app); elsewhere tkinter."""
    if sys.platform.startswith("win"):
        import subprocess
        env = {**os.environ, "DRISHTI_PICK_TITLE": title, "DRISHTI_PICK_START": start}
        try:                 # title and start folder travel as environment variables: no quoting problems
            r = subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command", WIN_PICKER],
                               capture_output=True, env=env, timeout=3600)
            if r.returncode == 0:
                sys.stdout.buffer.write(r.stdout.strip() + b"\n")
                sys.stdout.flush()
                return 0
            sys.stderr.write(r.stderr.decode("utf-8", "replace")[-500:])
        except (OSError, subprocess.TimeoutExpired) as e:
            sys.stderr.write(f"powershell folder dialog failed: {e}\n")
    try:
        import tkinter
        from tkinter import filedialog
    except Exception:
        return 3
    try:
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        path = filedialog.askdirectory(parent=root, title=title, initialdir=start or None, mustexist=True)
        root.destroy()
    except Exception:
        return 3
    sys.stdout.buffer.write((path or "").encode("utf-8") + b"\n")
    return 0


def bundled_tools() -> None:
    """In the packaged app, put the bundled ffmpeg/ffprobe first on PATH (before video_io loads)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        tools = os.path.join(base, "ffmpeg")
        if os.path.isdir(tools):
            os.environ["PATH"] = tools + os.pathsep + os.environ.get("PATH", "")


def self_test(data_dir: str) -> int:
    """Packaging check: every module imports, static files are bundled, the server answers."""
    import importlib
    import pkgutil
    import tempfile

    import cctv_analysis
    failed = []
    for m in pkgutil.walk_packages(cctv_analysis.__path__, "cctv_analysis."):
        try:
            importlib.import_module(m.name)
        except Exception as e:                       # report every broken module, not just the first
            failed.append(f"{m.name}: {e}")
    from .. import video_io as V
    from ..ocr import available_engines
    from . import server
    app = server.create_app(data_dir or tempfile.mkdtemp(), "selftest", 8765)
    c = app.test_client()
    h = {"Host": "127.0.0.1:8765", "X-CCTV-Token": "selftest"}
    ok_health = c.get("/api/health", headers=h).status_code == 200
    page = c.get("/?t=selftest", headers={"Host": "127.0.0.1:8765"})
    ok_page = page.status_code == 302 and c.get("/", headers={"Host": "127.0.0.1:8765"}).status_code == 200
    ok_static = c.get("/static/app.js", headers={"Host": "127.0.0.1:8765"}).status_code == 200
    native = {}
    try:                                    # YOLO post-processing needs torchvision's compiled NMS op
        import torch
        import torchvision
        keep = torchvision.ops.nms(torch.tensor([[0., 0., 10., 10.], [1., 1., 10., 10.]]), torch.tensor([0.9, 0.8]), 0.5)
        native["torchvision_nms"] = keep.tolist() == [0]
    except Exception as e:
        native["torchvision_nms"] = f"FAILED: {e}"
    try:                                    # OCR models are bundled with rapidocr; reading must work offline
        import cv2
        import numpy as np

        from ..ocr import RapidOCREngine
        img = np.full((60, 360, 3), 255, np.uint8)
        cv2.putText(img, "2026-10-31 05:38:27", (8, 42), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 2)
        native["ocr_read"] = any("05:38" in t for t, _ in RapidOCREngine().read(img))
    except Exception as e:
        native["ocr_read"] = f"FAILED: {e}"
    try:                                    # investigation features: case API, PDF certificate, filters, re-ID
        import cv2
        import numpy as np

        from .. import enhance, reid
        from ..cases import CaseStore
        from ..section63 import certificate_data, certificate_pdf
        native["case_api"] = c.get("/api/cases", headers=h).status_code == 200 and \
            c.get("/static/case.js", headers={"Host": "127.0.0.1:8765"}).status_code == 200
        tmp = tempfile.mkdtemp()
        case = CaseStore(os.path.join(tmp, "cases")).create("SELF-TEST")
        certificate_pdf(certificate_data(case, []), os.path.join(tmp, "c.pdf"))
        with open(os.path.join(tmp, "c.pdf"), "rb") as fh:
            native["certificate_pdf"] = fh.read(5) == b"%PDF-"
        img = np.full((64, 64, 3), 90, np.uint8)
        cv2.rectangle(img, (16, 16), (48, 48), (200, 200, 200), -1)
        out, rec = enhance.apply_chain(img, [{"op": "CLAHE"}, {"op": "WienerDeblur", "length": 5}])
        native["forensic_filters"] = out.shape == img.shape and rec["log"].startswith("CLAHE(")
        crop = np.zeros((128, 64, 3), np.uint8)
        crop[20:70] = (40, 40, 200)
        crop[70:] = (150, 70, 30)
        a = reid.appearance(crop)
        native["appearance_vector"] = a is not None and abs(float(np.linalg.norm(a["vector"])) - 1) < 1e-4
    except Exception as e:
        native["case_api"] = native.get("case_api", False)
        native["investigation"] = f"FAILED: {e}"
    report = {"modules_failed": failed, "health": ok_health, "index": ok_page, "static": ok_static,
              "ffmpeg": bool(V.FFMPEG), "ffprobe": bool(V.FFPROBE), "encoder": V.h264_args()[1] if V.FFMPEG else "",
              "ocr": available_engines(), **native}
    print(report)
    ok = not failed and ok_health and ok_page and ok_static and V.FFMPEG and V.FFPROBE and \
        native["torchvision_nms"] is True and native["ocr_read"] is True and "investigation" not in native and \
        all(native.get(k) is True for k in ("case_api", "certificate_pdf", "forensic_filters", "appearance_vector"))
    return 0 if ok else 1


def free_port(preferred: int) -> int:
    for p in [preferred] + list(range(preferred + 1, preferred + 50)):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="CCTVAnalysis", description=__doc__)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--allow-lan", action="store_true",
                    help="also accept connections from other computers (they still need the access link)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--pick-folder", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--pick-title", default="Choose a folder", help=argparse.SUPPRESS)
    ap.add_argument("--pick-start", default="", help=argparse.SUPPRESS)
    ap.add_argument("--self-test", action="store_true", help="check the installation and exit")
    a = ap.parse_args(argv)
    if a.pick_folder:
        sys.exit(pick_folder(a.pick_title, a.pick_start))
    bundled_tools()
    if a.self_test:
        sys.exit(self_test(a.data_dir))
    if a.host not in ("127.0.0.1", "localhost") and not a.allow_lan:
        sys.exit("refusing to listen on a network address without --allow-lan")
    from .server import create_app, default_data_dir
    port = free_port(a.port)
    token = secrets.token_urlsafe(32)
    data_dir = a.data_dir or default_data_dir()
    app = create_app(data_dir, token, port, allow_lan=a.allow_lan)
    url = f"http://127.0.0.1:{port}/?t={token}"
    print("=" * 70)
    print(" Drishti-AI (CCTV Analysis) is running. Open this private link in your browser:")
    print(" ", url)
    print(" Data folder:", data_dir)
    print(" Keep this window open; close it to stop the app.")
    print("=" * 70, flush=True)
    if not a.no_browser:
        threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open(url)), daemon=True).start()
    try:
        from waitress import serve
        serve(app, host=a.host if a.allow_lan else "127.0.0.1", port=port, threads=8, ident="CCTVAnalysis")
    except ImportError:
        app.run(host="127.0.0.1", port=port, threaded=True)


if __name__ == "__main__":
    main()

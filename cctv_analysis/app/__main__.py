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


def pick_folder() -> int:
    """Helper mode for the native folder dialog (exit 3 = no GUI toolkit available)."""
    try:
        import tkinter
        from tkinter import filedialog
    except Exception:
        return 3
    try:
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="Choose a folder")
        root.destroy()
    except Exception:
        return 3
    print(path or "")
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
    report = {"modules_failed": failed, "health": ok_health, "index": ok_page, "static": ok_static,
              "ffmpeg": bool(V.FFMPEG), "ffprobe": bool(V.FFPROBE), "encoder": V.h264_args()[1] if V.FFMPEG else "",
              "ocr": available_engines(), **native}
    print(report)
    ok = not failed and ok_health and ok_page and ok_static and V.FFMPEG and V.FFPROBE and \
        native["torchvision_nms"] is True and native["ocr_read"] is True
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
    ap.add_argument("--self-test", action="store_true", help="check the installation and exit")
    a = ap.parse_args(argv)
    if a.pick_folder:
        sys.exit(pick_folder())
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
    print(" CCTV Analysis is running. Open this private link in your browser:")
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

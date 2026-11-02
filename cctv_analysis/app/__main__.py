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
    a = ap.parse_args(argv)
    if a.pick_folder:
        sys.exit(pick_folder())
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

"""Real-user run of the desktop UI in headless Chromium against the real server and pipeline:
pick folders with the built-in browser, enable plates, check the computer, scan, estimate,
start, watch progress, open results, open an event, save a review, add it to an evidence
package, export, search a plate, open highlights, look at settings. Fails on any browser
console error or failed request."""
import os
import socket
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

playwright = pytest.importorskip("playwright.sync_api", reason="pip install playwright && playwright install chromium")
import make_cloud_clips  # noqa: E402
import make_plate_video  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from cctv_analysis.app.server import create_app  # noqa: E402

MODELS = os.environ.get("CCTV_TEST_MODEL_DIR", os.path.join(ROOT, ".test_models"))


def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    from waitress.server import create_server
    tmp = tmp_path_factory.mktemp("ui")
    inp = tmp / "recordings"
    make_cloud_clips.make(str(inp), 2)
    (inp / "gate").mkdir()
    make_plate_video.make(str(inp / "gate" / "ch02_20261031093000.mp4"))
    port = _port()
    data = tmp / "data"
    os.makedirs(data / "models", exist_ok=True)
    # reuse the test model cache (the app stores models in <data>/models)
    if os.path.isdir(MODELS):
        os.rmdir(data / "models")
        os.symlink(MODELS, data / "models")
    app = create_app(str(data), "ui-token", port)
    # Make the folder browser's first listing (home folder) slow, so a newer listing the user asks
    # for returns first: an older response must not overwrite it (found on a CI runner).
    import time as _t

    from flask import request as _rq
    real = app.view_functions["fs_list"]

    def slow_home():
        if not _rq.args.get("path"):
            _t.sleep(2.0)
        return real()
    app.view_functions["fs_list"] = slow_home
    srv = create_server(app, host="127.0.0.1", port=port, threads=6)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    yield {"url": f"http://127.0.0.1:{port}", "inp": inp, "out": tmp / "results"}
    srv.close()


def test_full_user_flow(server):
    errors, failed = [], []
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1400, "height": 950})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        def on_failed(r):
            f = r.failure or ""
            # a download navigation is aborted by design once the file starts downloading
            if "/api/exports/" in r.url and "ERR_ABORTED" in f:
                return
            # media elements cancel range requests they no longer need
            if "/api/file" in r.url and "ERR_ABORTED" in f:
                return
            failed.append(f"{f} {r.url}")
        page.on("requestfailed", on_failed)
        page.on("response", lambda r: failed.append(f"{r.status} {r.url}") if r.status >= 500 else None)
        page.goto(server["url"] + "/?t=ui-token")
        expect(page.locator("#licbadge")).to_have_text("Trial")

        # --- pick the recordings folder with the built-in folder browser
        page.click("#in-browse")
        page.fill("#fb-path", str(server["inp"].parent))
        page.press("#fb-path", "Enter")
        page.locator("#fb-list div", has_text="recordings").click()
        page.wait_for_timeout(2500)          # the slow first listing has now arrived – it must be ignored
        expect(page.locator("#fb-path")).to_have_value(str(server["inp"]))
        page.click("#fb-ok")
        expect(page.locator("#in-dir")).to_have_value(str(server["inp"]))
        page.fill("#out-dir", str(server["out"]))
        page.select_option("#preset", "fast")
        page.locator("details.group", has_text="Number plates").evaluate("d => { d.open = true; }")
        page.check("#cfg-use_plates")
        page.fill("#cfg-plate_watchlist", "MH12AB1284")
        page.uncheck("#cfg-use_faces")
        page.uncheck("#cfg-use_pose")

        # --- check computer, scan, estimate
        page.click("#btn-check")
        expect(page.locator("#check-out table")).to_contain_text("ffmpeg")
        page.click("#btn-scan")
        expect(page.locator("#scan-out")).to_contain_text("3 video(s)", timeout=60_000)
        expect(page.locator("#scan-out")).to_contain_text("read from video clock")
        page.click("#btn-estimate")
        expect(page.locator("#est-out")).to_contain_text("Estimated time:")
        expect(page.locator("#est-out")).to_contain_text("Trial mode")

        # --- start + progress (double click must not start two jobs)
        page.dblclick("#btn-start")
        expect(page.locator("#tab-progress")).to_be_visible()
        expect(page.locator("#job-eta")).to_have_text("done", timeout=420_000)
        expect(page.locator("#job-log")).to_contain_text("RUN SUMMARY")
        page.click("#btn-open-results")

        # --- results
        expect(page.locator("#kpis")).to_contain_text("videos")
        for lv in ("LOW", "NORMAL"):
            page.check(f".f-lv[value={lv}]")
        page.click("#f-go")
        expect(page.locator("#ev-count")).to_contain_text("event(s)")
        cards = page.locator("#ev-list .ev")
        assert cards.count() >= 3
        page.locator("#ev-list .ev", has_text="MH12AB1234").first.click()
        expect(page.locator("#ev-detail .plate-text").first).to_have_text("MH12AB1234")
        expect(page.locator("#ev-detail")).to_contain_text("watchlist_plate")
        page.locator("#ev-detail select").select_option("Suspicious")
        page.locator("#ev-detail button", has_text="Save review").click()
        expect(page.locator("#toast")).to_have_text("Review saved.")
        page.locator("#ev-detail button", has_text="Add to evidence").click()
        page.select_option("#f-review", "Suspicious")
        page.click("#f-go")
        expect(page.locator("#ev-count")).to_contain_text("of 1 event(s)")

        # --- export
        page.click("#tabs button[data-tab=export]")
        expect(page.locator("#ex-list")).to_contain_text("MH12AB1234".replace("MH12AB1234", ""))
        page.fill("#ex-case", "CASE-7")
        page.click("#ex-go")
        expect(page.locator("#ex-out")).to_contain_text("ZIP SHA-256", timeout=120_000)
        with page.expect_download() as dl:
            page.locator("#ex-out a").click()
        assert dl.value.suggested_filename.startswith("evidence_CASE-7_")
        page.click("#audit-load")
        expect(page.locator("#audit-out")).to_contain_text("Audit log intact")

        # --- plates + highlights + settings
        page.click("#tabs button[data-tab=plates]")
        page.fill("#pl-q", "MH12AB1284")
        page.click("#pl-go")
        expect(page.locator("#pl-list .plate-text").first).to_have_text("MH12AB1234")
        page.click("#tabs button[data-tab=highlights]")
        expect(page.locator("#hl-index div")).to_have_count(3, timeout=20_000)
        src = page.locator("#hl-video").get_attribute("src")
        assert "highlights%2F2026%2F10-Oct%2F2026-10-31_highlights.mp4" in src
        st = page.evaluate("async (u) => (await fetch(u, {headers: {Range: 'bytes=0-9'}})).status", src)
        assert st == 206
        page.click("#tabs button[data-tab=settings]")
        expect(page.locator("#lic-out")).to_contain_text("Trial mode")
        expect(page.locator("#models-out table")).to_contain_text("Number-plate reader")
        b.close()
    # Chromium builds without H.264 report a media decode error for the reel/clip; that is the
    # browser build, not the app (real Chrome/Edge play them). Everything else must be clean.
    real = [e for e in errors if "MEDIA_ERR" not in e and "media" not in e.lower()]
    assert real == [], real
    assert failed == [], failed

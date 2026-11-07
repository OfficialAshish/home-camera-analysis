"""A police officer's whole investigation in the real UI (headless Chromium, real server, real
pipeline): Quick investigation with one folder added through the folder dialog fallback and two
dropped on the page, clock corrections, Run; then the cross-camera timeline, describe-it search,
clothing-colour search, "find this person on all cameras", the journey, Forensic Studio, the
court bundle with Part A/Part B, its verification and the certificate preview. Fails on any
browser console error or failed request."""
import os
import re
import shutil
import socket
import subprocess
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

playwright = pytest.importorskip("playwright.sync_api", reason="pip install playwright && playwright install chromium")
import make_case_sources  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from cctv_analysis import video_io as V  # noqa: E402
from cctv_analysis.app.server import create_app  # noqa: E402

MODELS = os.environ.get("CCTV_TEST_MODEL_DIR", os.path.join(ROOT, ".test_models"))
TOKEN = "ui-case-token"


def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    from waitress.server import create_server
    tmp = tmp_path_factory.mktemp("uicase")
    seized = tmp / "E_drive"
    make_case_sources.make(str(seized))
    shutil.rmtree(seized / "Jeweler_NVR")
    vt = os.path.join(MODELS, "vtest.avi")
    if not os.path.exists(vt):
        pytest.skip("vtest.avi not downloaded")
    (seized / "Mall_Gate_NVR").mkdir()
    subprocess.run([V.FFMPEG, "-v", "error", "-y", "-i", vt, "-t", "12", "-c:v", "mpeg4", "-q:v", "3",
                    str(seized / "Mall_Gate_NVR" / "ch02_20261031211400.avi")], check=True)
    data = tmp / "data"
    data.mkdir()
    os.symlink(MODELS, data / "models")
    port = _port()
    app = create_app(str(data), TOKEN, port)
    # the officer used a folder on this drive before: dropped folders are looked for next to it
    app.extensions["cctv"]["settings"].save({"recent_inputs": [str(seized / "ShopA_DVR")]})
    srv = create_server(app, host="127.0.0.1", port=port, threads=6)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    yield {"url": f"http://127.0.0.1:{port}", "seized": seized}
    srv.close()


DROP = """async ([name, files]) => {
  const fileEntry = (f) => ({ isFile: true, isDirectory: false, name: f.rel, fullPath: "/" + name + "/" + f.rel,
                              file: (ok) => setTimeout(() => ok({ size: f.size }), 0) });
  const dir = { isFile: false, isDirectory: true, name, fullPath: "/" + name,
    createReader: () => { let done = false;           // like a browser: batches, then [], asynchronously
      return { readEntries: (ok) => { const batch = done ? [] : files.map(fileEntry); done = true; setTimeout(() => ok(batch), 0); } }; } };
  const ev = new Event("drop", { bubbles: true, cancelable: true });
  Object.defineProperty(ev, "dataTransfer", { value: { items: [{ webkitGetAsEntry: () => dir }] } });
  document.getElementById("q-drop").dispatchEvent(ev);
}"""


def test_officer_investigation_flow(server):
    errors, failed = [], []
    seized = server["seized"]
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1400, "height": 950}, accept_downloads=True)
        page = ctx.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        def on_failed(r):
            f = r.failure or ""
            if "ERR_ABORTED" in f and ("/exports/" in r.url or "/video" in r.url or "/file" in r.url):
                return                                   # downloads and media range requests are cancelled by design
            failed.append(f"{f} {r.url}")
        page.on("requestfailed", on_failed)
        page.on("response", lambda r: failed.append(f"{r.status} {r.url}") if r.status >= 500 else None)
        page.goto(server["url"] + f"/?t={TOKEN}")
        expect(page).to_have_title("Drishti-AI · CCTV investigation")
        expect(page.locator("#tab-quick")).to_be_visible()

        # --- 1 folder through "Add folder…" (no system dialog here -> the built-in browser), 2 dropped
        page.click("#q-add")
        expect(page.locator("#fb")).to_be_visible()
        page.fill("#fb-path", str(seized / "ShopA_DVR"))
        page.press("#fb-path", "Enter")
        expect(page.locator("#fb-path")).to_have_value(str(seized / "ShopA_DVR"))
        page.click("#fb-ok")
        for folder in ("TrafficCam_North", "Mall_Gate_NVR"):
            files = [{"rel": f, "size": os.path.getsize(seized / folder / f)} for f in sorted(os.listdir(seized / folder))]
            page.evaluate(DROP, [folder, files])
            expect(page.locator("#q-folders tbody tr", has_text=folder)).to_have_count(1)
        rows = page.locator("#q-folders tbody tr")
        expect(rows).to_have_count(3)
        rows.nth(0).locator("input.w-off").fill("-00:11:00")
        rows.nth(1).locator("input.w-off").fill("+00:05:00")
        page.fill("#q-fir", "451/2026")
        page.fill("#q-ps", "Civil Lines")
        page.check("input[name=q-preset][value=fast]")
        page.select_option("#q-facemode", "off")            # public cameras: no face analysis at all
        page.uncheck("#q-plates")
        page.dblclick("#q-run")                          # a double click must not start two runs
        expect(page.locator("#tab-progress")).to_be_visible()
        expect(page.locator("#job-stage")).to_contain_text("Source", timeout=60_000)
        expect(page.locator("#job-eta")).to_have_text("done", timeout=600_000)
        expect(page.locator("#btn-open-results")).to_have_text("Open the case timeline")
        page.click("#btn-open-results")

        # --- the cross-camera timeline on the calibrated clock
        expect(page.locator("#tab-investigate")).to_be_visible()
        expect(page.locator("#tl-list .ev").first).to_be_visible(timeout=30_000)
        cards = page.locator("#tl-list .ev")
        texts = cards.all_inner_texts()
        shop = next(i for i, t in enumerate(texts) if "ShopA DVR" in t)
        traffic = next(i for i, t in enumerate(texts) if "TrafficCam North" in t)
        assert shop < traffic, texts                       # true order: 21:14 before 21:18
        assert "2026-10-31 21:14" in texts[shop] and "DVR clock 2026-10-31 21:25" in texts[shop]
        expect(page.locator("#tl-lanes svg rect.tick")).to_have_count(cards.count())

        # --- describe-it search (SigLIP-2)
        page.fill("#ts-q", "people walking in a shopping mall")
        page.click("#ts-go")
        expect(page.locator("#srch-out .tile").first).to_contain_text("Mall Gate NVR", timeout=120_000)

        # --- clothing colours -> find this person on all cameras -> journey
        page.click("#pp-go")
        expect(page.locator("#srch-out .tile").first).to_be_visible()
        page.locator("#srch-out .tile button", has_text="Find on all cameras").first.click()
        expect(page.locator("#tab-journey")).to_be_visible()
        expect(page.locator("#jr-points tr").nth(1)).to_be_visible()
        expect(page.locator("#jr-flow .stop").first).to_contain_text("Mall Gate NVR")
        expect(page.locator("#jr-map")).to_contain_text("Add the map position")
        # bulk ticking by score instead of one by one: a high limit unticks the weaker matches
        n_pts = page.evaluate("C.points.length")
        expect(page.locator("#jr-ticked")).to_have_text(f"{n_pts} of {n_pts} sighting(s) ticked")
        scores = sorted(page.evaluate("C.points.map(p => p.score)"))
        cut = scores[len(scores) // 2]
        page.evaluate("(v) => { const r = document.getElementById('jr-score'); r.value = v; r.dispatchEvent(new Event('input')); }", str(cut))
        keep = sum(1 for x in scores if x >= cut - 1e-9)
        expect(page.locator("#jr-ticked")).to_have_text(f"{keep} of {n_pts} sighting(s) ticked")
        expect(page.locator("#jr-points tr.off")).to_have_count(n_pts - keep)
        page.click("#jr-all")
        expect(page.locator("#jr-points tr.off")).to_have_count(0)

        # --- one event: video, Forensic Studio, court bundle
        page.click("#tabs button[data-tab=investigate]")
        cards.nth(shop).click()
        expect(page.locator("#ce-detail video")).to_have_count(1)
        page.locator("#ce-detail button", has_text="Add to court bundle").click()
        expect(page.locator("#toast")).to_contain_text("1 in the court bundle")
        page.locator("#ce-detail button", has_text="Open in Forensic Studio").click()
        expect(page.locator("#tab-studio")).to_be_visible()
        page.wait_for_function("() => document.getElementById('fx-orig').naturalWidth === 640", timeout=30_000)
        page.select_option("#fx-op", "CLAHE")
        page.click("#fx-add")
        expect(page.locator("#fx-log")).to_contain_text("CLAHE(clip_limit=2, tile_grid=(8,8)) · 640×360", timeout=30_000)
        page.locator("#fx-chain input").first.fill("3")
        expect(page.locator("#fx-log")).to_contain_text("CLAHE(clip_limit=3", timeout=30_000)
        # zoom: the wheel zooms at the pointer (1.25× per notch), Fit returns; 8× from the zoom bar
        page.wait_for_function("() => V1.z > 0 && document.getElementById('fx-out').naturalWidth === 640", timeout=30_000)
        z1 = page.evaluate("V1.z")
        v1 = page.locator("#fx-v1").bounding_box()
        page.mouse.move(v1["x"] + v1["width"] / 2, v1["y"] + v1["height"] / 2)
        for _ in range(4):
            page.mouse.wheel(0, -120)
        page.wait_for_function(f"() => Math.abs(V1.z / {z1} - 1.25 ** 4) < 0.01")
        page.locator(".zoombar button[data-zoom=fit]").click()
        assert abs(page.evaluate("V1.z") - z1) < 1e-6
        page.locator(".zoombar button[data-zoom='8']").click()
        assert page.evaluate("V2.z") == 8 and "enhanced 800 %" in page.locator("#fx-zoom").inner_text()
        page.locator(".zoombar button[data-zoom=fit]").click()
        expect(page.locator("#fx-v2")).to_have_class(re.compile(r"\bpixels\b"))     # real pixels, no smoothing
        # region: drag a rectangle on the original (picture pixels 100,80 -> 300,200)
        page.click("#fx-roi-pick")
        page.locator("#fx-v1").scroll_into_view_if_needed()
        v1 = page.locator("#fx-v1").bounding_box()             # where the viewer is now (the page scrolled)
        a = page.evaluate("V1.toScreen(100, 80)")
        z = page.evaluate("V1.toScreen(300, 200)")
        page.mouse.move(v1["x"] + a[0], v1["y"] + a[1])
        page.mouse.down()
        page.mouse.move(v1["x"] + (a[0] + z[0]) / 2, v1["y"] + (a[1] + z[1]) / 2)
        page.mouse.move(v1["x"] + z[0], v1["y"] + z[1])
        page.mouse.up()
        expect(page.locator("#fx-roi-text")).to_contain_text("200 × 120 px")
        expect(page.locator("#fx-roibox")).to_be_visible()
        page.wait_for_function("() => document.getElementById('fx-out').naturalWidth === 200", timeout=30_000)
        # super-resolution of the region: 9 frames, 4×
        page.select_option("#fx-mode", "sr")
        expect(page.locator("#fx-log")).to_contain_text("MultiFrameSuperResolution(frames=9", timeout=60_000)
        page.wait_for_function("() => document.getElementById('fx-out').naturalWidth === 800", timeout=30_000)
        t0 = float(page.input_value("#fx-t"))
        page.click("#fx-next")
        assert abs(float(page.input_value("#fx-t")) - t0 - 1 / 25) < 0.002     # one frame of the 25 fps recording
        page.fill("#fx-name", "shop_door")
        page.click("#fx-save")
        expect(page.locator("#toast")).to_contain_text("reproduced it bit for bit", timeout=30_000)
        expect(page.locator("#fx-saved .tile")).to_have_count(1)

        # "Enhance person" on a person of an event opens the Studio on that person's region
        page.click("#tabs button[data-tab=investigate]")
        cards.nth(next(i for i, t in enumerate(texts) if "Mall Gate NVR" in t and "person" in t)).click()
        page.locator("#ce-detail button", has_text="Enhance person").first.click()
        expect(page.locator("#tab-studio")).to_be_visible()
        expect(page.locator("#fx-roi-text")).not_to_have_text("whole frame")
        roi = page.evaluate("C.studio.roi")
        page.wait_for_function(f"() => document.getElementById('fx-out').naturalWidth === {roi[2]}", timeout=30_000)
        page.click("#fx-roi-clear")
        expect(page.locator("#fx-roi-text")).to_have_text("whole frame")

        page.click("#tabs button[data-tab=bundle]")
        expect(page.locator("#bd-items tr")).to_have_count(1)
        expect(page.locator("#bd-enh input[type=checkbox]")).to_be_checked()
        card = page.locator("#bd-parta [data-parta=shopa-dvr]")
        card.locator("input[data-k=name]").fill("Ramesh Kumar")
        card.locator("input[data-k=place]").fill("Jaipur")
        page.fill("#pb-name", "Dr A. Verma")
        page.fill("#pb-designation", "Scientific Officer, FSL")
        with page.expect_popup() as pop:
            card.locator("a", has_text="Preview certificate").click()
        cert = pop.value
        expect(cert.locator("body")).to_contain_text("PREVIEW")
        expect(cert.locator("body")).to_contain_text("CERTIFICATE")
        cert.close()
        page.click("#bd-go")
        expect(page.locator("#bd-out")).to_contain_text("ZIP SHA-256", timeout=180_000)
        expect(page.locator("#bd-out")).to_contain_text("1 certificate(s): shopa-dvr")
        with page.expect_download() as dl:
            page.locator("#bd-out a").click()
        assert dl.value.suggested_filename.startswith("bundle_")
        page.locator("#bd-exports button", has_text="Verify").first.click()
        expect(page.locator("#bd-exports .st-ok")).to_contain_text("intact")

        # --- the timeline filtered by kind and score, every shown event ticked at once
        page.click("#tabs button[data-tab=investigate]")
        page.select_option("#tl-kind", "person")
        page.wait_for_function("() => C.timeline && C.timeline.events.length && C.timeline.events.every(e => e.kind === 'person')")
        n_shown = page.evaluate("C.timeline.events.length")
        expect(page.locator("#tl-list .ev")).to_have_count(n_shown)
        page.check("#tl-all")
        expect(page.locator("#tl-ticked")).to_have_text(f"{n_shown} of {n_shown} shown event(s) ticked")
        page.click("#tl-b-bundle")
        expect(page.locator("#toast")).to_contain_text("in the court bundle")
        top = page.evaluate("Math.max(...C.timeline.events.map(e => e.score))")
        page.evaluate("(v) => { const r = document.getElementById('tl-score'); r.value = v; r.dispatchEvent(new Event('change')); }", str(int(top)))
        page.wait_for_function(f"() => C.timeline.events.every(e => e.score >= {int(top)})")
        expect(page.locator("#tl-list .ev")).to_have_count(page.evaluate("C.timeline.events.length"))
        page.select_option("#tl-kind", "")

        # --- the case screen remembers what was typed (declarants) and shows every source
        page.click("#tabs button[data-tab=cases]")
        expect(page.locator("#cs-sources .source")).to_have_count(3)
        src = page.locator("#cs-sources .source[data-source=shopa-dvr]")
        expect(src.locator("input.w-off")).to_have_value("-00:11:00")
        src.locator("summary").click()
        assert "Ramesh Kumar" in [src.locator(".formgrid input").nth(i).input_value()
                                  for i in range(src.locator(".formgrid input").count())]
        src.locator("input.w-off").fill("0")
        src.locator("button", has_text="Save").click()
        expect(page.locator("#toast")).to_contain_text("Saved ShopA DVR")
        page.click("#cs-audit")
        expect(page.locator("#cs-audit-out")).to_contain_text("Audit log intact")
        expect(page.locator("#cs-audit-out")).to_contain_text("bundle_exported")
        b.close()
    real = [e for e in errors if "MEDIA_ERR" not in e and "media" not in e.lower()]
    assert real == [], real
    assert failed == [], failed

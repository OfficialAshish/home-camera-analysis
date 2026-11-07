"""Forensic Studio filters: each one does what it claims, measured, and none crashes on bad input."""
import os
import sys

import cv2
import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cctv_analysis import enhance as EN  # noqa: E402


def _psnr(a, b):
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    return 99.0 if mse == 0 else 10 * np.log10(255 ** 2 / mse)


def _text_image():
    img = np.full((120, 360, 3), 235, np.uint8)
    cv2.putText(img, "MH12AB1234", (14, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (20, 20, 20), 4, cv2.LINE_AA)
    return img


def _blur(img, k):
    return cv2.filter2D(img.astype(np.float32), -1, k, borderType=cv2.BORDER_REFLECT).round().clip(0, 255).astype(np.uint8)


def test_log_is_the_reproducible_court_format():
    chain = [{"op": "CLAHE", "clip_limit": 3.0, "tile_grid": 8}, {"op": "UnsharpMask", "sigma": 1.5}]
    assert EN.describe(chain) == ("CLAHE(clip_limit=3, tile_grid=(8,8)) → "
                                  "UnsharpMask(sigma=1.5, amount=1, threshold=0)")
    _, rec = EN.apply_chain(_text_image(), chain)
    assert rec["log"] == EN.describe(chain) and len(rec["ms"]) == 2
    assert rec["chain"][1] == {"op": "UnsharpMask", "sigma": 1.5, "amount": 1.0, "threshold": 0}


def test_clahe_recovers_shadow_detail():
    rng = np.random.default_rng(0)
    scene = _text_image().astype(np.float32)
    ink = scene[:, :, 0] < 128
    dark = 10 + scene * (35 / 235)                                       # night footage: everything in 10..45
    dark += np.linspace(0, 6, dark.shape[1])[None, :, None] + rng.normal(0, 1.0, dark.shape)
    dark = dark.clip(0, 255).astype(np.uint8)
    def separation(x):
        g = cv2.cvtColor(x, cv2.COLOR_BGR2GRAY).astype(np.float64)
        return g[~ink].mean() - g[ink].mean()

    def clahe(clip, grid):
        return EN.apply_step(dark, {"op": "CLAHE", "clip_limit": clip, "tile_grid": grid})
    base = separation(dark)
    out = clahe(3.0, 8)
    assert separation(out) > 1.15 * base and out.mean() > dark.mean()     # default: gentle, brighter shadows
    assert separation(clahe(12.0, 2)) > 2.0 * base                       # strong setting: text contrast doubled
    seps = [separation(clahe(c, 4)) for c in (1.0, 3.0, 6.0, 12.0, 40.0)]
    assert seps == sorted(seps), seps                                    # the clip limit is what limits the gain
    assert out.shape == dark.shape and out.dtype == np.uint8


@pytest.mark.parametrize("length,angle", [(9, 0), (13, 30), (7, 90)])
def test_wiener_and_richardson_lucy_undo_a_known_motion_blur(length, angle):
    sharp = _text_image()
    k = EN.psf("motion", length, angle)
    blurred = _blur(sharp, k)
    base = _psnr(blurred, sharp)
    w = EN.apply_step(blurred, {"op": "WienerDeblur", "psf": "motion", "length": length, "angle": angle, "nsr": 0.002})
    rl = EN.apply_step(blurred, {"op": "RichardsonLucy", "psf": "motion", "length": length, "angle": angle,
                                 "iterations": 40})
    assert _psnr(w, sharp) > base + 4, (base, _psnr(w, sharp))
    assert _psnr(rl, sharp) > base + 2, (base, _psnr(rl, sharp))
    # control: deconvolving with the wrong blur direction does not restore the picture as well
    wrong = EN.apply_step(blurred, {"op": "WienerDeblur", "psf": "motion", "length": length, "angle": angle + 90,
                                    "nsr": 0.002})
    assert _psnr(w, sharp) > _psnr(wrong, sharp) + 2


def test_defocus_deblur_improves_a_defocused_image():
    sharp = _text_image()
    k = EN.psf("defocus", radius=3)
    blurred = _blur(sharp, k)
    out = EN.apply_step(blurred, {"op": "WienerDeblur", "psf": "defocus", "radius": 3, "nsr": 0.005})
    assert _psnr(out, sharp) > _psnr(blurred, sharp) + 3


def test_unsharp_and_high_pass_increase_edge_energy():
    soft = cv2.GaussianBlur(_text_image(), (0, 0), 1.5)

    def edges(x):
        return float(np.abs(cv2.Laplacian(cv2.cvtColor(x, cv2.COLOR_BGR2GRAY), cv2.CV_32F)).mean())
    um = EN.apply_step(soft, {"op": "UnsharpMask", "sigma": 1.5, "amount": 1.5})
    hp = EN.apply_step(soft, {"op": "HighPass", "radius": 3, "amount": 1.0})
    assert edges(um) > 1.5 * edges(soft) and edges(hp) > 1.3 * edges(soft)
    none = EN.apply_step(soft, {"op": "UnsharpMask", "sigma": 1.5, "amount": 0.0})
    assert np.array_equal(none, soft)                                    # amount 0 = unchanged


def test_perspective_flattens_a_skewed_plate():
    plate = _text_image()
    h, w = plate.shape[:2]
    canvas = np.full((400, 600, 3), 90, np.uint8)
    dst = np.float32([[120, 90], [470, 140], [460, 300], [110, 250]])    # seen from an angle
    M = cv2.getPerspectiveTransform(np.float32([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]]), dst)
    skewed = cv2.warpPerspective(plate, M, (600, 400), dst=canvas.copy(), borderMode=cv2.BORDER_TRANSPARENT)
    pts = [[460, 300], [120, 90], [110, 250], [470, 140]]                 # any order: sorted internally
    flat = EN.apply_step(skewed, {"op": "Perspective", "points": pts, "width": w, "height": h})
    assert flat.shape == plate.shape
    corr = cv2.matchTemplate(cv2.cvtColor(flat, cv2.COLOR_BGR2GRAY), cv2.cvtColor(plate, cv2.COLOR_BGR2GRAY),
                             cv2.TM_CCOEFF_NORMED)[0, 0]
    assert corr > 0.9, corr
    with pytest.raises(ValueError):
        EN.apply_step(skewed, {"op": "Perspective", "points": [[0, 0], [1, 1], [2, 2], [3, 3]]})   # no area
    with pytest.raises(ValueError):
        EN.apply_step(skewed, {"op": "Perspective", "points": [[0, 0], [900, 0], [900, 900], [0, 900]]})


def test_multi_frame_stacking_cancels_sensor_noise_on_shifted_frames():
    rng = np.random.default_rng(3)
    clean = cv2.resize(_text_image(), (360, 120))
    frames = []
    for i in range(5):
        M = np.float32([[1, 0, (i - 2) * 1.5], [0, 1, (2 - i) * 1.0]])
        f = cv2.warpAffine(clean, M, (360, 120), borderMode=cv2.BORDER_REPLICATE).astype(np.float32)
        frames.append((f + rng.normal(0, 18, f.shape)).clip(0, 255).astype(np.uint8))
    out, rec = EN.stack_frames(frames, method="mean", motion="translation")
    assert rec["aligned"] == 5 and min(rec["ecc"]) > 0.8
    inner = (slice(10, -10), slice(10, -10))
    noise_one = np.std(frames[2].astype(np.float64)[inner] - clean.astype(np.float64)[inner])
    noise_stack = np.std(out.astype(np.float64)[inner] - clean.astype(np.float64)[inner])
    assert noise_stack < 0.65 * noise_one, (noise_one, noise_stack)       # ~1/sqrt(5) = 0.45 ideally
    med, _ = EN.stack_frames(frames, method="median", motion="euclidean")
    assert _psnr(med, clean) > _psnr(frames[2], clean) + 3
    assert rec["log"] == "MultiFrameStack(frames=5, aligned=5, motion=translation, method=mean)"


@pytest.mark.parametrize("img", [
    np.zeros((240, 320, 3), np.uint8), np.full((240, 320, 3), 255, np.uint8),
    np.random.default_rng(1).integers(0, 256, (240, 320, 3), dtype=np.uint8),       # pure sensor noise
    np.random.default_rng(2).integers(0, 256, (240, 320), dtype=np.uint8),          # grey frame
    np.full((240, 320, 3), np.nan, np.float32),                                     # broken float frame
    np.zeros((6, 6, 3), np.uint8)])
def test_no_filter_crashes_on_edge_case_frames(img):
    for op in EN.OPS:
        if op == "Perspective":
            h, w = img.shape[:2]
            step = {"op": op, "points": [[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]]}
        elif op in ("WienerDeblur", "RichardsonLucy"):
            step = {"op": op, "length": 3, "iterations": 3} if op == "RichardsonLucy" else {"op": op, "length": 3}
        else:
            step = {"op": op}
        out = EN.apply_step(img, step)
        assert out.dtype == np.uint8 and out.ndim == 3 and out.size > 0, op
        assert np.isfinite(out.astype(np.float64)).all(), op


def test_bad_parameters_are_refused_with_a_reason():
    img = _text_image()
    for step, msg in [({"op": "Sharpen"}, "unknown filter"), ({"op": "CLAHE", "clip_limit": 100}, "outside"),
                      ({"op": "CLAHE", "clip_limit": "x"}, "number"), ({"op": "CLAHE", "colour": 1}, "unknown parameter"),
                      ({"op": "Levels", "black": 200, "white": 100}, "white must be above black"),
                      ({"op": "WienerDeblur", "psf": "gauss"}, "psf"),
                      ({"op": "WienerDeblur", "length": float("nan")}, "finite"),
                      ({"op": "Perspective", "points": [[1, 2]]}, "four")]:
        with pytest.raises(ValueError, match=msg):
            EN.apply_step(img, step)
    with pytest.raises(ValueError, match="not smaller than the image"):
        EN.apply_step(np.zeros((8, 8, 3), np.uint8), {"op": "WienerDeblur", "length": 21})
    with pytest.raises(ValueError):
        EN.apply_step(np.zeros((0, 0, 3), np.uint8), {"op": "CLAHE"})
    with pytest.raises(ValueError, match="at most 20"):
        EN.apply_chain(img, [{"op": "CLAHE"}] * 21)


def test_saved_enhancement_reproduces_bit_for_bit(tmp_path):
    img = cv2.GaussianBlur(_text_image(), (0, 0), 1.2)
    chain = [{"op": "CLAHE", "clip_limit": 2.5}, {"op": "WienerDeblur", "length": 5, "nsr": 0.01},
             {"op": "UnsharpMask", "sigma": 1.0, "amount": 0.8}]
    out, rec = EN.apply_chain(img, chain)
    saved = EN.save_enhancement(str(tmp_path), "ev1_snap01", {"event_id": "ev1"}, img, out, rec, "SI Sharma")
    assert saved["log"].startswith("CLAHE(clip_limit=2.5, tile_grid=(8,8)) → WienerDeblur(")
    assert EN.reproduce(str(tmp_path), saved)
    assert [r["name"] for r in EN.list_enhancements(str(tmp_path))] == [saved["name"]]
    # a different chain does not reproduce the saved output (the check can fail)
    other = dict(saved, chain=[{"op": "CLAHE", "clip_limit": 2.6}])
    assert not EN.reproduce(str(tmp_path), other)


def _moving_plate_frames(n=15, seed=5):
    """A 44 × 11 px plate (MH12AB1234) moving by sub-pixel steps across 15 frames of a 160 × 60
    camera: rendered at 8× and area-sampled to the camera's pixels, sensor noise σ 10, JPEG q45
    (DVR compression). Returns the frames, the region around the plate in the middle frame and
    the ground truth of that region at 4×."""
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    import make_plate_video as MP
    rng = np.random.default_rng(seed)
    HI, F = 8, 4
    plate = MP.plate_image("MH12AB1234", 44 * HI, 11 * HI)
    scene = np.full((60 * HI, 160 * HI, 3), 90, np.uint8)

    def render(dx, dy):
        img = scene.copy()
        x, y = int(round(dx * HI)), int(round(dy * HI))
        img[y:y + plate.shape[0], x:x + plate.shape[1]] = plate
        return img
    frames, pos = [], []
    for i in range(n):
        dx, dy = 30 + i * 0.37, 24 + i * 0.21
        nat = cv2.resize(render(dx, dy), (160, 60), interpolation=cv2.INTER_AREA)
        nat = np.clip(nat.astype(np.float32) + rng.normal(0, 10, nat.shape), 0, 255).astype(np.uint8)
        ok, b = cv2.imencode(".jpg", nat, [cv2.IMWRITE_JPEG_QUALITY, 45])
        frames.append(cv2.imdecode(b, 1))
        pos.append((dx, dy))
    rx, ry = pos[n // 2]
    roi = [int(rx) - 3, int(ry) - 3, 50, 17]
    gt = cv2.resize(render(rx, ry), (160 * F, 60 * F), interpolation=cv2.INTER_AREA)[
        roi[1] * F:(roi[1] + roi[3]) * F, roi[0] * F:(roi[0] + roi[2]) * F]
    return frames, roi, gt


def test_multi_frame_super_resolution_recovers_a_small_moving_plate():
    """Measured: the region is followed through the moving frames, registered with sub-pixel ECC at
    4× and fused – closer to the true picture than enlarging the best single frame, much closer
    than a median of the frames without alignment (the plate moves), and the text reader reads
    the plate from the super-resolved picture but not from the single frame. Deterministic."""
    frames, roi, gt = _moving_plate_frames()
    sr, rec = EN.super_resolve(frames, roi, 4)
    assert sr.shape[:2] == gt.shape[:2] and rec["aligned"] >= 13 and rec["roi"] == roi
    single = cv2.resize(EN.crop_roi(frames[7], roi), (roi[2] * 4, roi[3] * 4), interpolation=cv2.INTER_CUBIC)
    unaligned = np.median(np.stack([cv2.resize(EN.crop_roi(f, roi), (roi[2] * 4, roi[3] * 4),
                                               interpolation=cv2.INTER_CUBIC) for f in frames]), 0).astype(np.uint8)
    p_sr, p_single, p_unaligned = _psnr(sr, gt), _psnr(single, gt), _psnr(unaligned, gt)
    assert p_sr > p_single + 1.0 and p_sr > p_unaligned + 3.0, (p_sr, p_single, p_unaligned)
    # same frames, same picture – also from buffers at other memory alignments (OpenCV's float SIMD
    # paths differ in the last bit with the alignment; that made a saved still not reproduce)
    def moved(f, k):
        buf = np.empty(f.size + 64, np.uint8)
        a = buf[k:k + f.size].reshape(f.shape)
        a[...] = f
        return a
    for k in (1, 3, 7):
        assert np.array_equal(EN.super_resolve([moved(f, k) for f in frames], roi, 4)[0], sr), k
        assert np.array_equal(EN.stack_frames([moved(f, k) for f in frames[5:10]])[0], EN.stack_frames(frames[5:10])[0]), k
    from cctv_analysis import modelcache
    from cctv_analysis.ocr import RapidOCREngine
    modelcache.init(os.environ.get("CCTV_TEST_MODEL_DIR", os.path.join(ROOT, ".test_models")))
    ocr = RapidOCREngine()

    def read(img):
        return "".join(t for t, _ in ocr.read(cv2.copyMakeBorder(img, 20, 20, 20, 20, cv2.BORDER_REPLICATE))).replace(" ", "")
    assert read(sr) == "MH12AB1234" and read(single) != "MH12AB1234"
    # factor 1 = the region followed and stacked without enlarging (Studio "several frames" with a region)
    st, rec1 = EN.super_resolve(frames, roi, 1, sharpen=False)
    assert st.shape[:2] == (roi[3], roi[2]) and rec1["op"] == "MultiFrameRegionStack"
    with pytest.raises(ValueError):
        EN.super_resolve(frames[:1], roi, 4)
    with pytest.raises(ValueError):
        EN.super_resolve(frames, roi, 4, motion="bogus")

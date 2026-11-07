# Forensic Studio (image enhancement with an audit log)

Status: **current** · Code: `enhance.py`, app screen *Forensic Studio* (`app/case_api.py` `…/enhance/*`,
`static/case.js`) · Tests: `tests/test_forensic_enhancement.py`, `tests/test_case_api.py`, `tests/test_ui_cases_playwright.py`

## In the app
*Timeline & search › event › Open in Forensic Studio at this moment* decodes the original frame
at that time from the recording (`…/frame`, lossless PNG at full resolution; raw `.264/.265`
through their lossless remux). Filters are added and tuned with a live preview (requests are
debounced and the newest result wins); Perspective takes its four corners from clicks on the
original; *Frames to stack* takes 3–9 consecutive decoded frames around the time. *Save enhanced
still* stores `<case>/enhancements/<name>_<time>.png`, its `_input.png` and a `.json` record (source,
event, camera, video file and offset, DVR and calibrated time, chain, log, hashes, versions,
operator), checks that re-running the chain reproduces the output hash, and writes
`enhancement_saved` with both hashes into the case audit log. Saved stills can be put into the
court bundle, under the certificate of the device their frame came from.

## Principle
Enhancement may only make visible what the recording contains. Every filter is deterministic
NumPy/OpenCV code with validated parameters; a chain of filters produces a text log in the form
used in court reports, and the saved result records the input and output SHA-256, the chain, the
log, the tool and library versions and the operator:

    CLAHE(clip_limit=3, tile_grid=(8,8)) → UnsharpMask(sigma=1.5, amount=1, threshold=0)

`enhance.reproduce()` re-runs the saved chain on the saved input and compares the output hash;
the same chain on the same input gives the same bytes on the same build.

## Filters
| Filter | Use | Parameters (range, default) |
|---|---|---|
| CLAHE | shadows in night footage, backlit faces, blown highlights; works on Lab lightness only | clip_limit 0.5–40 (2), tile_grid 1–32 (8) |
| Levels | black/white point and gamma | black 0–254 (0), white 1–255 (255), gamma 0.1–5 (1) |
| Denoise | non-local means | strength 1–30 (7) |
| UnsharpMask | edges of numbers and text | sigma 0.3–10 (1.5), amount 0–5 (1), threshold 0–64 (0) |
| HighPass | high-pass sharpening | radius 1–15 (3), amount 0–3 (0.6) |
| WienerDeblur | motion blur (line PSF: length px, angle °) or defocus (disc PSF: radius px) | psf motion/defocus, length 1–101 (9), angle −180–180 (0), radius 0.5–30 (3), nsr 1e-4–1 (0.01) |
| RichardsonLucy | iterative deconvolution, same PSFs | iterations 1–200 (25) |
| Perspective | flatten a plate or signboard seen at an angle: four clicked points → rectangle | points (any order; sorted to TL, TR, BR, BL), width/height (0 = from the points) |
| Crop | region of interest | x, y, w, h (0 = to the edge) |
| Upscale | enlarge for viewing (Lanczos; adds no detail) | factor 1–8 (2) |
| MultiFrameStack | 2–15 consecutive frames aligned to the middle one with OpenCV ECC (`findTransformECC`; translation / euclidean / affine / homography), then per-pixel median or mean | method, motion |

Results above 40 megapixels are refused. Deconvolution amplifies noise and compression artefacts:
raise `nsr` for noisy footage. The FFT works on a reflect-padded image so the frame edges do not ring.

## Measured (tests)
* Motion blur 9 px at 0°, 13 px at 30°, 7 px at 90° on a plate-like text image: Wiener improves
  PSNR by more than 4 dB, Richardson–Lucy by more than 2 dB; deconvolving at the wrong angle is
  at least 2 dB worse than the right one (the PSF matters).
* Defocus radius 3: Wiener improves PSNR by more than 3 dB.
* CLAHE on a plate in the 10–45 brightness range: text/background separation +21 % at the
  default (3, 8×8), more than doubled at (12, 2×2), and rising monotonically with the clip limit.
* Perspective: a plate warped into a trapezoid is rectified to correlation > 0.9 with the original.
* Five frames shifted by up to ±3 px with noise σ = 18: aligned mean stack residual noise < 0.65×
  a single frame's (ideal √5 ≈ 0.45×); ECC correlation > 0.8 for every frame.
* No filter crashes or returns non-finite values on black, white, pure-noise, grey, NaN-float or
  6×6 frames; bad parameters are refused with the reason.

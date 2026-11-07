# Forensic Studio (image enhancement with an audit log)

Status: **current** · Code: `enhance.py`, app screen *Forensic Studio* (`app/case_api.py` `…/enhance/*`,
`static/case.js`) · Tests: `tests/test_forensic_enhancement.py`, `tests/test_case_api.py`, `tests/test_ui_cases_playwright.py`

## In the app
**Opening it.** In an event (*Timeline & search*) every person has *Enhance face* and *Enhance
person*, every vehicle *Enhance car/motorcycle/…*, every plate *Enhance plate in Forensic Studio*
(also on *Plates not read*), and *Open in Forensic Studio at this moment* opens the whole frame.
The focused buttons ask `GET …/roi?source&event&track&kind=plate|vehicle|person|face` where to
look: a plate → the plate box the reader found (stored as `plates.box`, full-resolution pixels)
at the time of that read, with a margin; a vehicle / person → the detector's box nearest to the
track's first sighting; a face → the head part (top third) of the person's box. Plates and faces
open in *super-resolution*, the rest on the frame with the region marked. Sources analysed with
an older version have no plate box: the Studio opens at the event and the officer drags the region.

**The viewers.** The original frame (decoded from the recording, lossless PNG at full resolution;
raw `.264/.265` through their lossless remux) and the enhanced picture are shown side by side in
zoomable viewers: the mouse wheel zooms at the pointer, dragging moves, the zoom bar sets *Fit*,
*1:1*, 2× … 16×; *show real pixels* draws each pixel as a square (no browser smoothing), so a
16× view shows what the recording holds. The enhanced preview is lossless PNG up to 2 megapixels.
*◀ frame / frame ▶* step one frame (the source's frame rate).

**Region.** *Select region…* then drag a rectangle on the original; *Whole frame* clears it.
Coordinates are pixels of the recording's frame and are part of the saved record.

**Picture from.** *one frame* (cut to the region); *several frames, aligned and combined* –
without a region the whole frames are aligned with ECC and median-stacked (`MultiFrameStack`);
with a region the region is followed through the frames and stacked (`MultiFrameRegionStack`,
the background does not decide the alignment of a moving vehicle); *super-resolution of the
region* (`MultiFrameSuperResolution`, 5–25 frames, 2–8×, see below).

**Filters** are added and tuned with a live preview (requests are debounced and the newest
result wins); *Plate preset* (CLAHE 2.5/4 + UnsharpMask 1.2/1.5) and *Night preset* (Levels gamma
1.6 + CLAHE + Denoise) are starting points whose values stay editable. *Perspective* takes its four
corners from clicks on the enhanced picture, which meanwhile shows the steps before Perspective.

**Read and ask.** *Read plate / text (OCR)* runs the plate reader and the general text reader on
the enhanced picture and shows both with their confidence and the Indian-format fit
(`POST …/enhance/read`); opened from a plate, the officer types the plate as they read it and
*Save as the plate reading* stores it as the examiner's reading of that vehicle (`docs/PLATES.md`).
*Ask the AI* sends the enhanced picture and a question (presets: read the plate, describe the
person, describe the vehicle, read all text) to the providers of *Settings › AI model providers*
(`POST …/enhance/ask`, fallback order, `docs/VLM.md`). The answer is advice: it never changes the
picture, and question, answer, provider and the picture's SHA-256 go into the case audit log
(`ai_question`).

**Save.** *Save enhanced still* stores `<case>/enhancements/<name>_<time>.png`, its `_input.png`
(the picture the filters started from: the frame, the stack or the super-resolved region) and a
`.json` record (source, event, camera, video file and offset, DVR and calibrated time, region,
`picture_from` – how the input was made, with the per-frame ECC scores –, chain, log, hashes,
versions, operator), then **decodes the frame(s) from the recording again, re-runs stacking /
super-resolution and filters and compares the output hash** (and re-runs the chain on the saved
input). The result is stored as `reproduced_from_recording` in the record and in the
`enhancement_saved` audit entry with both hashes; a still that does not reproduce is reported to
the officer, not claimed reproducible (`test_a_saved_still_is_reproduced_from_the_recording_or_flagged`
simulates a drifting decode). Saved stills can be put into the court bundle, under the certificate
of the device their frame came from.

## Multi-frame super-resolution
A plate or face in CCTV footage covers few pixels but is seen in many consecutive frames, each
sampled at a slightly different sub-pixel position. `enhance.super_resolve` follows the region of
the middle frame through the others (normalised cross-correlation; frames where it is not found
with correlation ≥ 0.5 or not fully in view are left out), enlarges each frame's region with a
common margin, registers it to the middle frame with sub-pixel ECC on gradient images at the
enlarged size, fuses the stack with the per-pixel median and applies a mild unsharp mask. No
learned model is used: it invents nothing; the record lists the frames used and their ECC scores.

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
| MultiFrameSuperResolution / MultiFrameRegionStack | 2–31 frames, a region followed through them, enlarged 2–8× (1× = region stack), sub-pixel ECC, median fusion | frames, factor, motion, region |

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
* Super-resolution (`test_multi_frame_super_resolution_recovers_a_small_moving_plate`): a 44 × 11 px
  plate moving by sub-pixel steps across 15 frames of a 160 × 60 camera, sensor noise σ 10, JPEG
  quality 45. At 4×, PSNR against the true picture: super-resolved 18.0 dB, the best single frame
  enlarged (bicubic) 16.6 dB, a median of the frames without alignment 13.4 dB; 15 of 15 frames
  aligned. RapidOCR reads `MH12AB1234` from the super-resolved picture and not from the single
  frame (it read `HH12481234`); fast-plate-ocr reads neither at this size. Synthetic motion is
  uniform; real footage with rolling shutter, blur and IR glare gains less.
* No filter crashes or returns non-finite values on black, white, pure-noise, grey, NaN-float or
  6×6 frames; bad parameters are refused with the reason.

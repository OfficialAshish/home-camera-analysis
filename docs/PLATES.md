# Number plates (ANPR)

Status: **current** · Code: `cctv_analysis/plates.py` · Tests: `tests/test_plates.py`

## Switches
| Config | App label | Default | Effect |
|---|---|---|---|
| `use_plates` | Read vehicle number plates | off | read plates of cars, motorcycles, buses, trucks |
| `plate_enhance` | Enhance hard plates (fast vehicles) | off | motion deblur + multi-frame fusion, saves the enhanced image |
| `plate_vlm_read` | Ask the AI model when a plate is unreadable | off | VLM reads the (enhanced) crop when OCR confidence < 0.6 |
| `plate_watchlist` | Plate watchlist | – | alert `watchlist_plate` (+50) on a match within `plate_watchlist_max_edits` (1) |
| `plate_region` | – | `IN` | Indian formats (state + RTO + series + 4 digits, BH series); `any` disables format rules |
| `plate_reads_per_track` | – | 6 | best observations re-read per vehicle track |

## How a plate is read
1. During detection every vehicle track keeps its best observations (confidence × size × sharpness).
2. Those frames are re-read at **full resolution** at the exact same instant (`read_frame_at`
   returns the first frame with pts ≥ t; an earlier version returned the frame ~0.4 s later and
   cropped moving plates — fixed and regression-tested).
3. The plate is localised in the vehicle box (open-image-models YOLOv9 ONNX; OpenCV fallback),
   read by fast-plate-ocr (per-character confidences).
4. With `plate_enhance`, frames whose read is not already convincing are motion-deblurred: the
   blur direction is the track's direction of motion; the blur length (3–26 px at 2× upscale) is
   searched coarse-to-fine with Wiener deconvolution, scored by the plate OCR × format fit; the
   three best reconstructions are re-read by RapidOCR, which tolerates deconvolution ringing.
   A median of the ECC-aligned crops of all frames is read the same way.
5. Format fit: at most **one** letter↔digit substitution is accepted (e.g. `MHI2AB1234` →
   `MH12AB1234`, fit 0.75); anything needing more is treated as "not a plate" (fit 0.3) so a
   truncated read is never turned into a plausible wrong plate.
6. All reads vote character by character, weighted by confidence × fit. Partial reads (e.g. a car
   cut by the frame edge, `MH12`) stay in the `plates` table as evidence but are not reported in
   event summaries or matched against the watchlist.

## Measured accuracy (synthetic but realistic test, this repository's machine)
Video: a real car photo (`tests/assets/car_rear.png`, MIT) with an Indian plate `MH12AB1234`
painted on, driving across a 1600×900 scene for 7 s, encoded with OpenCV's lossy `mp4v` codec;
blur = horizontal motion blur applied to every frame before encoding. Plate width in the frame:
scale 1.2 ≈ 96 px, 0.9 ≈ 72 px, 0.7 ≈ 56 px. Result = fused text (confidence). Measured
2026‑11‑02 (`/tmp/plate_matrix3.log`, `/tmp/plate_matrix4.log` runs; commands in the test file).

| plate width | blur | plain OCR | with `plate_enhance` |
|---|---|---|---|
| ~96 px | 0 | **MH12AB1234** (0.998) | **MH12AB1234** (0.998) |
| ~96 px | 5 px | M110H11 (0.19) ✗ | **MH12AB1234** (0.71) |
| ~96 px | 9 px | 1 (0.42) ✗ | **MH12AB1234** (0.57) |
| ~96 px | 15 px | MM11 (0.23) ✗ | WWL5225 (0.18) ✗ |
| ~72 px | 0 | **MH12AB1234** (0.993) | **MH12AB1234** (0.989) |
| ~72 px | 5 px | MMS303 (0.25) ✗ | MH22AB1234 (0.34) ✗ one character wrong, low confidence |
| ~72 px | 9 px | M130A53 (0.25) ✗ | **MH12AB1234** (0.33) |
| ~72 px | 15 px | 3 (0.24) ✗ | M1Z1115 (0.14) ✗ |
| ~56 px | 0 | MH12AB1134 (0.92) ✗ one character wrong | MH12AB1134 (0.60) ✗ |
| ~56 px | 5–15 px | unreadable | unreadable / wrong (≤ 0.24) |

Reading the table honestly:
* Sharp plates ≥ ~70 px wide are read reliably without enhancement.
* Enhancement recovers moderate motion blur (5–9 px at ~96 px width; 9 px at ~72 px) that plain
  OCR cannot read at all. It does **not** recover 15 px blur or plates of ~56 px.
* A wrong result can still carry a plausible format. Confidence below ~0.6 means "check by eye":
  the app and the *Number Plates* sheet show the plate crop and the enhanced image next to the text.
* At ~56 px the OCR misreads one character consistently with high confidence (0.92); consistent
  identical reads stop the deblur search early, so enhancement does not help there. Use a camera
  with more pixels on the plate (zoom / closer mounting) for gates and entrances.
* Cost (CPU, one car track): ~12 s plain vs ~45–110 s with enhancement.
* Synthetic blur is uniform; real night-time blur also has noise and IR glare — expect lower
  numbers on real footage. No real-world plate dataset was available in this environment.

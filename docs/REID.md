# Person appearance search (re-identification)

Status: **current** · Code: `reid.py`, `journey.sightings(kind="appearance")`
Tests: `tests/test_person_reid.py`

## What it does
Faces are often hidden (helmets, masks, cameras high on a pole); clothing is not. For every person
track the pipeline already keeps its best full-body crop. `reid.build_index(<source output>)`
turns each crop into an appearance vector (stored in `appearance_index.npz`, incremental: only
new crops are computed). "Search this appearance across all cameras" ranks every person track of
every source of the case by cosine similarity to the chosen one and lists the matches above the
score threshold in **calibrated time order**, with both times, camera and crop.

## Built-in descriptor (default; pure OpenCV/NumPy, no model, no licence question)
1. Crop → 128×64, light blur (sensor noise must not look like texture).
2. **Background suppression**: the outer columns of a detector box are background; pixels close in
   Lab to their median colour get weight down to 0.15 (not 0, so clothing that matches the
   background still counts). Columns are also centre-weighted.
3. **Mild exposure correction** (±25 % at most): the same clothes on a darker or brighter camera.
4. Head / upper body / lower body: soft-binned hue×saturation histogram of coloured pixels and a
   brightness histogram of grey pixels (black, grey and white clothing are represented);
   torso gradient-orientation texture (stripes, prints); soft-binned aspect ratio. Square root
   (Hellinger) and L2 normalisation: scores are in [0, 1].
5. Named dominant colours of the upper and lower body (`red`, `blue`, `black`, `white`, `grey`,
   `brown` …) for display and filtering.

## Optional learned model
Any re-ID network exported to ONNX with a `1×3×256×128` RGB input (ImageNet normalisation) and one
embedding output – OSNet or FastReID exports – placed at `<models>/reid/reid.onnx` is used
together with the built-in vector (`0.6 × learned + 0.4 × built-in`). Vectors of different models
are never compared: an index built with another model falls back to the built-in score. **Not
bundled:** published re-ID weights are trained on MSMT17 / Market-1501, licensed for
non-commercial research; the owner must license or train a model for commercial sale
(`docs/COMMERCIAL-READINESS.md`). The ONNX path is tested with a generated model (input contract,
batching, normalisation, broken-model fallback).

## Measured
| Data | Result |
|---|---|
| Synthetic people, 6 outfits × 3 variants (scale 0.8–1.3×, shift, ±20 % light, three backgrounds, noise) | rank-1 18/18; same outfit ≥ 0.90, different outfits ≤ 0.55 (before background suppression and soft bins: same outfit as low as 0.34, different as high as 0.65) |
| Real pedestrians, `vtest.avi`, same person 0.2 s apart (boxes paired by overlap) vs everyone in the frame | rank-1 17/17 |
| Real pedestrians, `vtest.avi`, one ByteTrack track 1 / 2 / 4 s apart vs everyone nearby | rank-1 21/25 (84 %) / 17/23 (74 %) / 11/17 (65 %) |

**Limits, stated plainly.** All real measurements are one camera; cross-camera accuracy (other
angle, lens, white balance) was not measured – no labelled multi-camera dataset was available. The
built-in descriptor is colour-and-texture based: two people dressed alike match, and a person who
changes a jacket does not. Scores rank candidates for an investigator; they are not an
identification. Always compare the crops (and faces or plates where visible).

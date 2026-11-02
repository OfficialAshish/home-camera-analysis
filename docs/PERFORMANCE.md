# Performance

Status: **current** · All numbers measured on the development machine (Intel Xeon 2.8 GHz,
4 vCPU, 15 GB RAM, **no GPU**) on 2026‑11‑02. GPU numbers are not measured here.

## Analysis speed (`sysinfo.benchmark`, OpenCV `vtest.avi`, 768×576, busy pedestrian scene)
| preset | motion pass | detector (+pose) | reel encode |
|---|---|---|---|
| fast (yolo26n @640, no pose) | 13.3× real time | 14.1 frames/s | 9.8× real time |
| balanced (yolo26s @960 + yolo26n-pose) | 9.9× real time | 2.9 frames/s | 10.3× real time |

DVR clock OCR (RapidOCR, CPU): ~0.7 s per read on a frame with a clock; up to ~5.5 s on a busy
frame without any clock (both bands scanned). Reading stops after two empty frames, and per-event
re-reads only happen for clock-timed files (decision D2).

Number plates, one car crossing a 1600×900 frame for 7 s: ~12 s plain, ~45–110 s with
`plate_enhance` (blind deblur search) – see `docs/PLATES.md`.

End-to-end (fast preset, CPU): 5 synthetic 14-s cloud clips → 5 events, reel, reports in ≈1.5 min
including model load.

## App read paths – `tools/soak.py` (100,000 events, 30,000 plates, 20,000 reviews, 8 clients, 25 s)
Mix: 35 % event list with date+level filter, 15 % paging sorted by score, 10 % "Not reviewed",
15 % event detail, 13 % plate search (1 edit tolerance), 7 % summary, 5 % highlights. 0 errors in
all runs; latencies in ms.

| endpoint | before p50 / p95 / p99 | after p50 / p95 / p99 |
|---|---|---|
| events (date + level filter) | 16 858 / 17 270 / 17 313 | **55 / 114 / 146** |
| events (page, by score) | 13 512 / 18 535 / 18 981 | **114 / 186 / 231** |
| events ("Not reviewed") | – (not in baseline mix) | 201 / 270 / 298 |
| event detail | 15 744 / 18 220 / 18 440 | **34 / 66 / 84** |
| plate search | 33 068 / 35 242 / 35 435 | **32 / 58 / 740** (p99 = first request filling the cache) |
| summary | 152 / 152 / 152 | **10 / 24 / 174** |
| highlights (payload) | 3 686 (366 KB) | **52 (3.4 KB)** |
| **throughput** | **16 requests / 25 s** | **2 731 requests / 25 s** |

What changed (commit `c278bad`): plate search scanned every plate row with a pure-Python edit
distance under the GIL, which blocked all other requests. It now uses cached distinct texts and a
banded edit distance with early exit, and fetches only matching rows (LIMIT). Reviews are joined
in SQL (attached `reviews.db`) instead of loading all reviews per request. Highlights return day
summaries, with one day's index on demand. Date filters became plain range predicates (the
`substr()` form could not use the index). A score index was added, and the summary is cached per
database version.

Index contribution alone (same new code, app indexes dropped with `--no-indexes`): throughput
1 501 vs 2 731 requests; event filter p50 137 vs 55 ms; paging p50 287 vs 114 ms.

Reproduce: `python tools/soak.py --events 100000 --plates 30000 --reviews 20000 --clients 8 --seconds 25 [--no-indexes]`.

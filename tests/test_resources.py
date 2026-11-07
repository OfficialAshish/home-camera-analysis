"""Memory governor: smaller batches above 85 % RAM, waiting above 93 %, and the batch loops that
use it (SigLIP keyframes, appearance vectors) still finish – one item at a time."""
import os
import sys

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cctv_analysis import resources as R  # noqa: E402


def test_batches_shrink_above_85_percent_and_never_below_one():
    for used, want in ((50, 32), (84.9, 32), (85, 16), (89.9, 16), (90, 8), (92.9, 8), (99, 4), (100, 2)):
        g = R.MemoryGovernor(reader=lambda u=used: u, sleep=lambda s: None, max_wait=0.0)
        assert g.batch_size(32) == want, used
    g = R.MemoryGovernor(reader=lambda: 99.0, sleep=lambda s: None, max_wait=0.0)
    assert g.batch_size(1) == 1 and g.batch_size(0) == 1
    assert g.throttled == 2


def test_above_93_percent_the_caller_waits_until_memory_is_released():
    readings = iter([95.0, 96.0, 95.0, 80.0, 80.0])
    slept = []
    g = R.MemoryGovernor(reader=lambda: next(readings), sleep=slept.append, max_wait=30.0)
    assert g.batch_size(16) == 2                 # read 95 % -> 1 + (10 // 5) = 3 halvings, then wait
    assert slept == [1.0, 1.0] and g.waited == 2.0
    stuck = R.MemoryGovernor(reader=lambda: 99.0, sleep=lambda s: None, max_wait=0.05)
    assert stuck.wait_for_memory() is False      # gives up after max_wait and continues


def test_appearance_indexing_finishes_one_crop_at_a_time_under_memory_pressure(tmp_path, monkeypatch):
    """reid.build_index asks the governor for its batch size; at 99 % RAM it must still index every
    track (in batches of one), and give the same vectors as without pressure."""
    from cctv_analysis import reid
    from cctv_analysis.storage import Store
    out = tmp_path / "out"
    (out / "events").mkdir(parents=True)
    st = Store(str(out / "analysis.db"))
    rng = np.random.default_rng(3)
    for i in range(5):
        crop = np.zeros((128, 64, 3), np.uint8)
        crop[20:70] = rng.integers(0, 255, 3)
        crop[70:] = rng.integers(0, 255, 3)
        p = str(out / "events" / f"p{i}.jpg")
        cv2.imwrite(p, crop)
        eid = f"ev-{i}"
        st.insert("events", {"event_id": eid, "video_id": 1, "video_path": "x.mp4", "rel_path": "x.mp4", "camera": "ch01",
                             "start_off": 0.0, "end_off": 2.0, "start_dt": f"2026-10-31 21:0{i}:00", "snapshots": "[]",
                             "flags": "[]"})
        st.insert("tracks", {"event_id": eid, "track": 1, "cls": "person", "first_off": 0.0, "first_dt": "2026-10-31 21:00:00",
                             "duration": 1.0, "person_crop": p, "face_crop": ""})
    st.close()
    sizes = []
    pressured = R.MemoryGovernor(reader=lambda: 99.0, sleep=lambda s: None, max_wait=0.0)
    real = pressured.batch_size
    monkeypatch.setattr(pressured, "batch_size", lambda n: sizes.append(real(n)) or sizes[-1])
    old = R.governor()
    R.set_governor(pressured)
    try:
        res = reid.build_index(str(out), batch=8)
    finally:
        R.set_governor(old)
    assert res["tracks"] == 5 and sizes and max(sizes) == 1        # throttled to one crop per batch
    a = reid.load_index(str(out))
    (out / reid.INDEX).unlink()
    b_res = reid.build_index(str(out), batch=8)                     # no pressure
    b = reid.load_index(str(out))
    assert b_res["tracks"] == 5 and np.allclose(a["vectors"], b["vectors"])

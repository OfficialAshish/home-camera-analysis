"""Person appearance vectors and "search this appearance across all cameras"."""
import os
import shutil
import sys

import cv2
import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cctv_analysis import reid  # noqa: E402

MODELS = os.environ.get("CCTV_TEST_MODEL_DIR", os.path.join(ROOT, ".test_models"))
OUTFITS = {                       # BGR torso, BGR legs, expected names
    "red-blue": ((30, 30, 200), (140, 60, 20), ("red", "blue")),
    "yellow-black": ((40, 210, 230), (25, 25, 25), ("yellow", "black")),
    "white-grey": ((235, 235, 235), (120, 120, 120), ("white", "grey")),
    "green-black": ((40, 160, 40), (20, 20, 20), ("green", "black")),
    "black-blue": ((20, 20, 20), (150, 70, 30), ("black", "blue")),
    "blue-white": ((180, 80, 30), (230, 230, 230), ("blue", "white")),
}


def person(top, legs, w=60, h=150, bg=(110, 110, 110), shift=0, light=1.0, seed=0, stripes=False):
    rng = np.random.default_rng(seed)
    img = np.full((h, w, 3), bg, np.uint8)
    cx = w // 2 + shift
    cv2.circle(img, (cx, int(0.09 * h)), int(0.08 * h), (120, 150, 190), -1)          # head (skin)
    cv2.rectangle(img, (cx - int(0.28 * w), int(0.18 * h)), (cx + int(0.28 * w), int(0.55 * h)), top, -1)
    if stripes:
        for y in range(int(0.2 * h), int(0.55 * h), 8):
            cv2.line(img, (cx - int(0.28 * w), y), (cx + int(0.28 * w), y), (255, 255, 255), 2)
    cv2.rectangle(img, (cx - int(0.24 * w), int(0.55 * h)), (cx - 2, h - 2), legs, -1)
    cv2.rectangle(img, (cx + 2, int(0.55 * h)), (cx + int(0.24 * w), h - 2), legs, -1)
    img = (img.astype(np.float32) * light + rng.normal(0, 6, img.shape)).clip(0, 255).astype(np.uint8)
    return img


def test_colours_of_clothing_are_named():
    for name, (top, legs, (u, lo)) in OUTFITS.items():
        a = reid.appearance(person(top, legs))
        assert (a["upper"], a["lower"]) == (u, lo), name
        assert 2.0 < a["aspect"] < 3.0
        assert abs(np.linalg.norm(a["vector"]) - 1) < 1e-5


def test_same_clothes_rank_first_under_pose_light_scale_and_background_changes():
    gallery, labels = [], []
    for i, (name, (top, legs, _)) in enumerate(OUTFITS.items()):
        for j, kw in enumerate([dict(), dict(w=48, h=130, shift=3, light=0.8, bg=(90, 100, 95)),
                                dict(w=80, h=190, shift=-4, light=1.2, bg=(140, 130, 120))]):
            gallery.append(reid.appearance(person(top, legs, seed=10 * i + j, **kw))["vector"])
            labels.append(name)
    G = np.stack(gallery)
    correct = 0
    for q in range(len(G)):
        s = reid.similarity(G[q], G)
        s[q] = -1
        correct += labels[int(np.argmax(s))] == labels[q]
    assert correct == len(G)                                         # rank-1 = 100 % on controlled data
    same = [reid.similarity(G[a], G[b:b + 1])[0] for a in range(len(G)) for b in range(len(G))
            if a != b and labels[a] == labels[b]]
    diff = [reid.similarity(G[a], G[b:b + 1])[0] for a in range(len(G)) for b in range(len(G)) if labels[a] != labels[b]]
    assert min(same) > max(diff), (min(same), max(diff))             # a threshold separates them


def test_texture_tells_a_striped_shirt_from_a_plain_one():
    plain = reid.appearance(person((150, 60, 20), (20, 20, 20)))["vector"]
    plain2 = reid.appearance(person((150, 60, 20), (20, 20, 20), seed=5, shift=2))["vector"]
    striped = reid.appearance(person((150, 60, 20), (20, 20, 20), stripes=True))["vector"]
    assert reid.similarity(plain, plain2[None])[0] > reid.similarity(plain, striped[None])[0] + 0.03


def test_unusable_crops_give_no_vector():
    assert reid.appearance(None) is None
    assert reid.appearance(np.zeros((10, 5, 3), np.uint8)) is None
    a = reid.appearance(np.zeros((150, 60, 3), np.uint8))           # black crop: still a valid (black) vector
    assert a is not None and a["upper"] == "black"


def _vtest_pairs(max_pairs=60):
    """Real pedestrians: YOLO boxes in frames 0.2 s apart, paired by overlap (the same person)."""
    from ultralytics import YOLO

    from cctv_analysis import modelcache
    modelcache.init(MODELS)
    src = os.path.join(MODELS, "vtest.avi")
    if not os.path.exists(src):
        pytest.skip("vtest.avi not downloaded (tests/test_footfall_reports.py downloads it)")
    model = YOLO(modelcache.yolo_weights("yolo11n.pt"))
    cap = cv2.VideoCapture(src)
    frames = []
    for i in range(0, 400):
        ok, f = cap.read()
        if not ok:
            break
        if i % 40 in (0, 2):
            frames.append(f)
    cap.release()

    def boxes(f):
        r = model(f, classes=[0], conf=0.5, verbose=False)[0]
        return [b for b in r.boxes.xyxy.cpu().numpy().astype(int) if (b[3] - b[1]) > 70]

    def iou(a, b):
        x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
        inter = max(0, x2 - x1) * max(0, y2 - y1)
        return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)

    queries, galleries = [], []
    for k in range(0, len(frames) - 1, 2):
        b0, b1 = boxes(frames[k]), boxes(frames[k + 1])
        crops1 = [frames[k + 1][y1:y2, x1:x2] for x1, y1, x2, y2 in b1]
        for a in b0:
            m = max(range(len(b1)), key=lambda j: iou(a, b1[j]), default=None)
            if m is not None and iou(a, b1[m]) > 0.6 and len(b1) >= 3:
                queries.append(frames[k][a[1]:a[3], a[0]:a[2]])
                galleries.append((crops1, m))
    return queries[:max_pairs], galleries[:max_pairs]


def test_real_pedestrians_are_found_among_the_others():
    queries, galleries = _vtest_pairs()
    assert len(queries) >= 15, len(queries)
    hits = 0
    for q, (crops, truth) in zip(queries, galleries):
        qa = reid.appearance(q)
        G = np.stack([reid.appearance(c)["vector"] for c in crops])
        hits += int(np.argmax(reid.similarity(qa["vector"], G))) == truth
    rate = hits / len(queries)
    print(f"real pedestrians (vtest.avi, 0.2 s apart): rank-1 {hits}/{len(queries)} = {rate:.0%}")
    assert rate >= 0.8, rate


def test_real_pedestrians_two_seconds_apart_rank_above_the_others():
    """Harder, still same camera: crops of one ByteTrack track 2 s apart vs everyone else nearby.
    Measured 2026-11-07 on vtest.avi: 1 s 84 %, 2 s 74 %, 4 s 65 % (docs/REID.md)."""
    from ultralytics import YOLO

    from cctv_analysis import modelcache
    modelcache.init(MODELS)
    src = os.path.join(MODELS, "vtest.avi")
    if not os.path.exists(src):
        pytest.skip("vtest.avi not downloaded")
    model = YOLO(modelcache.yolo_weights("yolo11n.pt"))
    cap = cv2.VideoCapture(src)
    tracks = {}
    for i in range(600):
        ok, f = cap.read()
        if not ok:
            break
        r = model.track(f, persist=True, classes=[0], conf=0.4, verbose=False, tracker="bytetrack.yaml")[0]
        if r.boxes.id is None or i % 5:
            continue
        for b, tid in zip(r.boxes.xyxy.cpu().numpy().astype(int), r.boxes.id.cpu().numpy().astype(int)):
            if b[3] - b[1] >= 70:
                tracks.setdefault(int(tid), []).append((i, f[b[1]:b[3], b[0]:b[2]].copy()))
    cap.release()
    hits = n = 0
    for tid, obs in tracks.items():
        fi, crop = obs[0]
        later = [o for o in obs if o[0] - fi >= 20]
        if not later:
            continue
        target = later[0]
        gallery = [target[1]] + [min(o2, key=lambda o: abs(o[0] - target[0]))[1] for t2, o2 in tracks.items()
                                 if t2 != tid and abs(min(o2, key=lambda o: abs(o[0] - target[0]))[0] - target[0]) <= 10]
        if len(gallery) < 3:
            continue
        G = np.stack([reid.appearance(g)["vector"] for g in gallery])
        hits += int(np.argmax(G @ reid.appearance(crop)["vector"]) == 0)
        n += 1
    print(f"real pedestrians, same track 2 s apart: rank-1 {hits}/{n} = {hits / max(1, n):.0%}")
    assert n >= 15 and hits / n >= 0.65, (hits, n)


def _tiny_onnx(path):
    """A small conv net exported to ONNX with the re-ID input contract (N×3×256×128 -> N×64)."""
    import torch
    torch.manual_seed(0)
    net = torch.nn.Sequential(torch.nn.Conv2d(3, 8, 4, stride=4), torch.nn.ReLU(), torch.nn.AvgPool2d((16, 16)),
                              torch.nn.Flatten(), torch.nn.Linear(64, 64))      # 256x128 -> 64x32 -> 4x2 x 8
    net.eval()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    torch.onnx.export(net, torch.zeros(1, 3, 256, 128), path, input_names=["images"], output_names=["emb"],
                      dynamic_axes={"images": {0: "n"}, "emb": {0: "n"}}, dynamo=False)


def test_learned_model_path_loads_an_onnx_and_combines_scores(tmp_path):
    models = tmp_path / "models"
    assert reid.learned_model(str(models)) is None                   # NOT_CONFIGURED: built-in only
    _tiny_onnx(str(models / "reid" / "reid.onnx"))
    m = reid.learned_model(str(models))
    assert m is not None and m.id.startswith("onnx-") and (m.h, m.w) == (256, 128)
    crops = [person(*OUTFITS["red-blue"][:2]), person(*OUTFITS["red-blue"][:2], seed=4, shift=2),
             person(*OUTFITS["yellow-black"][:2])]
    e = m.embed(crops)
    assert e.shape == (3, 64) and np.allclose(np.linalg.norm(e, axis=1), 1, atol=1e-5)
    assert np.allclose(m.embed(crops[:1]), e[:1], atol=1e-5)          # batch of 1 = same vector
    (models / "reid" / "reid.onnx").write_bytes(b"not a model")
    assert reid.learned_model(str(models)) is None                   # a broken model is ignored, with a warning


@pytest.fixture(scope="module")
def vtest_case(tmp_path_factory):
    """The real pipeline on 25 s of real pedestrian footage, as one source of a case."""
    from cctv_analysis.cases import CaseStore
    from cctv_analysis.investigation import run_case
    src_video = os.path.join(MODELS, "vtest.avi")
    if not os.path.exists(src_video):
        pytest.skip("vtest.avi not downloaded")
    tmp = tmp_path_factory.mktemp("reid")
    seized = tmp / "seized" / "Mall_Gate_NVR"
    seized.mkdir(parents=True)
    import subprocess

    from cctv_analysis import video_io as V
    subprocess.run([V.FFMPEG, "-v", "error", "-y", "-i", src_video, "-t", "25", "-c:v", "mpeg4", "-q:v", "3",
                    str(seized / "ch02_20261031211400.avi")], check=True)
    c = CaseStore(str(tmp / "cases")).create("CASE-REID")
    c.add_source(str(seized), "Mall gate NVR", "+00:00:30")
    opts = dict(use_vlm=False, use_semantic=False, use_faces=False, use_pose=False, detector_model="yolo11n.pt",
                detector_fallbacks=[], det_imgsz=640, detect_fps=4, save_clips=False, excel_thumbnails=False,
                make_highlight_reel=False, model_cache_dir=MODELS)
    res = run_case(c, opts, preset="fast", work_root=str(tmp / "work"), model_cache_dir=MODELS)
    return c, res


def test_case_run_indexes_every_person_track_and_search_ranks_them(vtest_case):
    from cctv_analysis import journey as J
    c, res = vtest_case
    out = c.source_dir("mall-gate-nvr")
    assert res["sources"]["mall-gate-nvr"]["appearances"] >= 5
    idx = reid.load_index(out)
    assert len(idx["keys"]) == res["sources"]["mall-gate-nvr"]["appearances"]
    again = reid.build_index(out)
    assert again["computed"] == 0 and again["tracks"] == len(idx["keys"])          # incremental
    q = idx["meta"][0]
    pts = J.sightings(c, "appearance", query={"source_id": "mall-gate-nvr", "event_id": q["event_id"],
                                              "track": q["track"]}, min_score=0.0)
    assert len(pts) == len(idx["keys"])                              # every track ranked, query included
    first = [p for p in pts if p["event_id"] == q["event_id"] and p["track"] == q["track"]]
    assert first and first[0]["score"] == 1.0
    times = [p["calibrated_time"] for p in pts]
    assert times == sorted(times)                                    # chronological
    assert all(p["offset_seconds"] == 30.0 for p in pts)             # the source's clock correction applies
    strict = J.sightings(c, "appearance", query={"source_id": "mall-gate-nvr", "event_id": q["event_id"],
                                                 "track": q["track"]}, min_score=0.99)
    assert 1 <= len(strict) < len(pts)                               # the threshold filters


def test_learned_and_built_in_vectors_are_kept_apart(vtest_case, tmp_path):
    c, _ = vtest_case
    out = c.source_dir("mall-gate-nvr")
    work = tmp_path / "copy"
    shutil.copytree(out, work, ignore=shutil.ignore_patterns("playback"))
    _tiny_onnx(str(tmp_path / "m" / "reid" / "reid.onnx"))
    m = reid.learned_model(str(tmp_path / "m"))
    r = reid.build_index(str(work), model=m)
    assert r["model"] == m.id and r["computed"] == r["tracks"]         # a new model recomputes everything
    idx = reid.load_index(str(work))
    assert idx["deep"] is not None and idx["deep"].shape == (len(idx["keys"]), 64)
    qv = reid.query_vector(str(work), idx["meta"][0]["event_id"], idx["meta"][0]["track"])
    srcs = [{"source_id": "s", "out_dir": str(work), "offset_for": lambda cam: 0.0}]
    both = reid.search(srcs, qv["hand"], qv["deep"], qv["model"], 0.0)
    assert both and all(x["method"] == "learned+built-in" for x in both)
    other = reid.search(srcs, qv["hand"], qv["deep"], "onnx-different", 0.0)     # vectors of another model
    assert other and all(x["method"] == "built-in" for x in other)

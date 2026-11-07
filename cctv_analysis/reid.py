"""Person appearance (re-identification) vectors and "find this appearance on every camera".

Faces are often hidden (helmets, masks, high-mounted cameras), but clothing is not. For every
person track the pipeline saves its best full-body crop; this module turns crops into appearance
vectors and ranks every track of every source of a case by similarity to a chosen one.

**Built-in descriptor** (always available, pure OpenCV/NumPy, no model, no licence question):
the crop is resized to 128×64 and split into head, upper body (torso) and lower body; for each
part a centre-weighted hue×saturation histogram of coloured pixels plus a brightness histogram
of grey pixels (so black, white and grey clothing are represented), a torso gradient-orientation
texture histogram and the crop's aspect ratio. Histograms are square-rooted (Hellinger) and the
vector is L2-normalised, so the cosine similarity of two vectors is in [0, 1]. Dominant
upper/lower colours are named (``"red"``, ``"black"`` …) for display and filtering.

**Learned descriptor (optional):** any person re-ID network exported to ONNX with a
``1×3×256×128`` RGB input and one embedding output (OSNet / FastReID exports work) placed at
``<models>/reid/reid.onnx``. When present it is used together with the built-in descriptor
(``score = 0.6·learned + 0.4·built-in``). It is not bundled: public re-ID weights are trained on
datasets licensed for research only (MSMT17, Market-1501) – see docs/COMMERCIAL-READINESS.md.

Scores are a ranking aid for an investigator, not an identification: always compare the crops.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Dict, List, Optional, Sequence

import cv2
import numpy as np

log = logging.getLogger("cctv")
H, W = 128, 64
HUE_BINS, SAT_BINS, GREY_BINS = 12, 3, 6
PARTS = (("head", 0.0, 0.18, 0.3), ("upper", 0.18, 0.55, 1.0), ("lower", 0.55, 1.0, 0.8))
INDEX = "appearance_index.npz"
# every name color_name() can return (the app's clothing-colour filter offers exactly these)
COLOR_NAMES = ("black", "grey", "white", "red", "orange", "brown", "yellow", "green", "cyan", "blue", "purple",
               "pink")
HUE_EDGES = (0, 8, 20, 33, 78, 95, 128, 150, 170, 180)        # OpenCV hue (0..180) → name (red wraps)
HUE_NAME = ("red", "orange", "yellow", "green", "cyan", "blue", "purple", "pink", "red")
VERSION = "hand-v2"


def _weights() -> np.ndarray:
    xs = (np.arange(W) - (W - 1) / 2) / (0.32 * W)
    return np.tile(np.exp(-0.5 * xs ** 2)[None, :], (H, 1)).astype(np.float32)


_WX = _weights()


def _soft(values: np.ndarray, n: int, lo: float, hi: float, circular: bool = False):
    """Soft (linear) binning: each value splits its weight between the two nearest bins, so a
    small shift of colour or brightness (another camera, other lighting) moves weight gradually."""
    x = (values.astype(np.float32) - lo) / (hi - lo) * n - 0.5
    i0 = np.floor(x).astype(np.int32)
    f = x - i0
    i1 = i0 + 1
    if circular:
        i0, i1 = i0 % n, i1 % n
    else:
        f = np.where(i0 < 0, 1.0, np.where(i1 > n - 1, 0.0, f))
        i0, i1 = i0.clip(0, n - 1), i1.clip(0, n - 1)
    return i0, i1, f


def _part_hist(hsv: np.ndarray, w: np.ndarray) -> np.ndarray:
    h, s, v = hsv[..., 0].astype(np.float32), hsv[..., 1].astype(np.float32), hsv[..., 2].astype(np.float32)
    chroma = (s >= 50) & (v >= 45)
    h0, h1, hf = _soft(h, HUE_BINS, 0, 180, circular=True)
    s0, s1, sf = _soft(s.clip(50, 255), SAT_BINS, 50, 256)
    col = np.zeros(HUE_BINS * SAT_BINS, np.float32)
    wc = w * chroma
    for hi_, hw in ((h0, 1 - hf), (h1, hf)):
        for si_, sw in ((s0, 1 - sf), (s1, sf)):
            np.add.at(col, (hi_ * SAT_BINS + si_).ravel(), (wc * hw * sw).ravel())
    grey = np.zeros(GREY_BINS, np.float32)
    g0, g1, gf = _soft(v, GREY_BINS, 0, 256)
    wg = w * ~chroma
    np.add.at(grey, g0.ravel(), (wg * (1 - gf)).ravel())
    np.add.at(grey, g1.ravel(), (wg * gf).ravel())
    hist = np.concatenate([col, grey])
    total = hist.sum()
    return np.sqrt(hist / total) if total > 0 else hist


def _texture(gray: np.ndarray, w: np.ndarray) -> np.ndarray:
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag, ang = cv2.cartToPolar(gx, gy, angleInDegrees=True)
    bins = (np.mod(ang, 180) // 22.5).astype(np.int32).clip(0, 7)
    hist = np.zeros(9, np.float32)
    np.add.at(hist, bins.ravel(), (mag * w).ravel())
    hist[8] = float((mag * w).sum() / (w.sum() + 1e-6)) / 64.0         # overall edge strength (stripes, prints)
    s = hist[:8].sum()
    if s > 0:
        hist[:8] /= s
    return np.sqrt(hist.clip(0))


def color_name(hsv_part: np.ndarray, w: np.ndarray) -> str:
    h, s, v = hsv_part[..., 0], hsv_part[..., 1].astype(np.int32), hsv_part[..., 2].astype(np.int32)
    chroma = (s >= 60) & (v >= 50)
    votes: Dict[str, float] = {}
    wc = w[chroma]
    if wc.size:
        idx = np.searchsorted(HUE_EDGES, h[chroma], side="right") - 1
        for i, name in enumerate(HUE_NAME):
            votes[name] = votes.get(name, 0.0) + float(wc[idx == i].sum())
    vg, wg = v[~chroma], w[~chroma]
    votes["black"] = float(wg[vg < 70].sum())
    votes["grey"] = float(wg[(vg >= 70) & (vg < 180)].sum())
    votes["white"] = float(wg[vg >= 180].sum())
    if "orange" in votes and "brown" not in votes:               # dark orange reads as brown
        dark = chroma & (v < 140) & (h >= 8) & (h < 20)
        votes["brown"] = float(w[dark].sum())
        votes["orange"] -= votes["brown"]
    return max(votes, key=lambda k: votes[k]) if votes else ""


def _foreground(img: np.ndarray) -> np.ndarray:
    """Per-pixel weight that suppresses the background: a detector's person box always has
    background at its left and right edges, so pixels whose colour is close (in Lab) to the
    median colour of the outer columns get little weight. The floor (0.15) keeps clothing that
    happens to match the background from vanishing entirely."""
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)
    edge = max(2, W // 10)
    border = np.concatenate([lab[:, :edge].reshape(-1, 3), lab[:, -edge:].reshape(-1, 3)])
    bg = np.median(border, axis=0)
    d = np.linalg.norm(lab - bg, axis=2)
    return (0.15 + 0.85 / (1 + np.exp(-(d - 14) / 4))).astype(np.float32)


def _normalise_light(img: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Mild exposure correction (at most ±25 %): the same clothes on a darker or brighter camera."""
    v = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[..., 2].astype(np.float32)
    ww = w.ravel()
    order = np.argsort(v.ravel())
    cum = np.cumsum(ww[order])
    if cum[-1] <= 0:
        return img
    ref = v.ravel()[order][min(len(order) - 1, int(np.searchsorted(cum, 0.9 * cum[-1])))]
    if ref < 20:
        return img                                                   # night/black clothes: leave alone
    gain = float(np.clip(200.0 / ref, 0.8, 1.25))
    return (img.astype(np.float32) * gain).clip(0, 255).astype(np.uint8)


def appearance(crop: np.ndarray) -> Optional[dict]:
    """{"vector", "upper", "lower", "aspect"} for a person crop (BGR), or None if unusable."""
    if crop is None or crop.ndim != 3 or crop.shape[0] < 16 or crop.shape[1] < 8:
        return None
    aspect = crop.shape[0] / crop.shape[1]
    img = cv2.resize(crop, (W, H), interpolation=cv2.INTER_AREA)
    img = cv2.GaussianBlur(img, (3, 3), 0)                          # sensor noise must not look like texture
    wts = _WX * _foreground(img)
    img = _normalise_light(img, wts)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    feats, names = [], {}
    for name, a, b, weight in PARTS:
        r0, r1 = int(a * H), int(b * H)
        feats.append(weight * _part_hist(hsv[r0:r1], wts[r0:r1]))
        if name != "head":
            names[name] = color_name(hsv[r0:r1], wts[r0:r1])
    u0, u1 = int(0.18 * H), int(0.55 * H)
    feats.append(0.45 * _texture(gray[u0:u1], wts[u0:u1]))
    centers = np.array([1.6, 2.2, 2.8, 3.6])                         # aspect ratio, soft-binned
    asp = np.exp(-0.5 * ((aspect - centers) / 0.35) ** 2)
    feats.append(0.25 * asp / (np.linalg.norm(asp) + 1e-9))
    v = np.concatenate(feats).astype(np.float32)
    n = float(np.linalg.norm(v))
    if n == 0:
        return None
    return {"vector": v / n, "upper": names.get("upper", ""), "lower": names.get("lower", ""), "aspect": round(aspect, 2)}


class OnnxReID:
    """Learned re-ID embeddings from an ONNX model (1×3×256×128 RGB, ImageNet normalisation)."""
    MEAN = np.array([0.485, 0.456, 0.406], np.float32)
    STD = np.array([0.229, 0.224, 0.225], np.float32)

    def __init__(self, path: str):
        import onnxruntime as ort
        self.path = path
        self.sess = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
        inp = self.sess.get_inputs()[0]
        self.input = inp.name
        shape = inp.shape
        self.h = shape[2] if isinstance(shape[2], int) else 256
        self.w = shape[3] if isinstance(shape[3], int) else 128
        self.batched = not isinstance(shape[0], int) or shape[0] != 1
        import hashlib
        with open(path, "rb") as fh:
            self.id = "onnx-" + hashlib.sha256(fh.read()).hexdigest()[:12]

    def embed(self, crops: Sequence[np.ndarray]) -> np.ndarray:
        x = np.stack([((cv2.resize(c, (self.w, self.h))[:, :, ::-1].astype(np.float32) / 255.0 - self.MEAN) /
                       self.STD).transpose(2, 0, 1) for c in crops]).astype(np.float32)
        if self.batched:
            out = self.sess.run(None, {self.input: x})[0]
        else:
            out = np.concatenate([self.sess.run(None, {self.input: x[i:i + 1]})[0] for i in range(len(x))])
        out = out.reshape(len(crops), -1).astype(np.float32)
        return out / (np.linalg.norm(out, axis=1, keepdims=True) + 1e-9)


def learned_model(models_dir: str = "") -> Optional[OnnxReID]:
    from . import modelcache
    p = os.path.join(models_dir or modelcache.root(), "reid", "reid.onnx")
    if not os.path.isfile(p):
        return None
    try:
        return OnnxReID(p)
    except Exception as e:                       # a broken model must not stop the built-in search
        log.warning("re-ID model %s unusable: %s", p, e)
        return None


# ------------------------------------------------------------------ per-source index
def _tracks(out_dir: str) -> List[dict]:
    db = sqlite3.connect(f"file:{os.path.join(out_dir, 'analysis.db')}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute("SELECT t.event_id, t.track, t.first_dt, t.first_off, t.duration, t.identity, t.known_name, "
                          "t.person_crop, t.face_crop, e.camera, e.start_dt, e.rel_path, e.start_off "
                          "FROM tracks t JOIN events e USING(event_id) WHERE t.cls='person' AND t.person_crop != '' "
                          "ORDER BY e.start_dt, t.event_id, t.track").fetchall()
    finally:
        db.close()
    return [dict(r) for r in rows]


def build_index(out_dir: str, model: Optional[OnnxReID] = None, batch: int = 32) -> dict:
    """Appearance vectors of every person track of one analysed folder (incremental)."""
    from .resources import governor
    from .storage import rel_to, resolve
    path = os.path.join(out_dir, INDEX)
    old: Dict[str, int] = {}
    keys: List[str] = []
    vecs: List[np.ndarray] = []
    deep: List[np.ndarray] = []
    meta: List[dict] = []
    model_id = model.id if model else ""
    if os.path.exists(path):
        try:
            z = np.load(path, allow_pickle=False)
            if str(z["version"]) == VERSION and str(z["model"]) == model_id:
                okeys = [str(k) for k in z["keys"]]
                ometa = json.loads(str(z["meta"]))
                for i, k in enumerate(okeys):
                    old[k] = i
                ovecs, odeep = z["vectors"], z["deep"] if model_id else None
            else:
                okeys, ometa, ovecs, odeep = [], [], None, None
        except Exception:
            okeys, ometa, ovecs, odeep = [], [], None, None
    else:
        okeys, ometa, ovecs, odeep = [], [], None, None
    todo = []
    for r in _tracks(out_dir):
        crop = resolve(out_dir, r["person_crop"])          # follows a moved output folder
        if not crop:
            continue
        mt = int(os.path.getmtime(crop))
        key = f"{r['event_id']}|{r['track']}|{mt}"
        m = {k: r[k] for k in ("event_id", "track", "first_dt", "first_off", "duration", "identity", "known_name",
                               "camera", "start_dt", "rel_path", "start_off")}
        m["person_crop"] = os.path.relpath(crop, out_dir).replace(os.sep, "/")
        m["face_crop"] = rel_to(out_dir, r["face_crop"])
        if key in old and ovecs is not None:
            i = old[key]
            keys.append(key)
            vecs.append(ovecs[i].astype(np.float32))
            if model_id and odeep is not None:
                deep.append(odeep[i].astype(np.float32))
            meta.append({**ometa[i], **m})
        else:
            todo.append((key, crop, m))
    pending: List[tuple] = []

    def flush():
        if not pending:
            return
        imgs = [p[3] for p in pending]
        dv = model.embed(imgs) if model else None
        for j, (key, m, a, _) in enumerate(pending):
            keys.append(key)
            vecs.append(a["vector"])
            meta.append({**m, "upper": a["upper"], "lower": a["lower"], "aspect": a["aspect"]})
            if dv is not None:
                deep.append(dv[j])
        pending.clear()

    for key, crop, m in todo:
        img = cv2.imread(crop)
        a = appearance(img) if img is not None else None
        if a is None:
            continue
        pending.append((key, m, a, img))
        if len(pending) >= governor().batch_size(batch):
            flush()
    flush()
    np.savez_compressed(path + ".tmp.npz", keys=np.array(keys, dtype=str),
                        vectors=np.stack(vecs).astype(np.float16) if vecs else np.zeros((0, 1), np.float16),
                        deep=np.stack(deep).astype(np.float16) if deep else np.zeros((0, 1), np.float16),
                        meta=json.dumps(meta), version=VERSION, model=model_id)
    os.replace(path + ".tmp.npz", path)
    return {"tracks": len(keys), "computed": len(todo), "model": model_id or VERSION}


def load_index(out_dir: str) -> Optional[dict]:
    p = os.path.join(out_dir, INDEX)
    if not os.path.exists(p):
        return None
    z = np.load(p, allow_pickle=False)
    deep = z["deep"].astype(np.float32)
    return {"keys": [str(k) for k in z["keys"]], "vectors": z["vectors"].astype(np.float32),
            "deep": deep if deep.shape[0] == len(z["keys"]) and deep.shape[1] > 1 else None,
            "meta": json.loads(str(z["meta"])), "model": str(z["model"])}


def similarity(q_hand: np.ndarray, hand: np.ndarray, q_deep: Optional[np.ndarray] = None,
               deep: Optional[np.ndarray] = None) -> np.ndarray:
    s = hand @ q_hand
    if q_deep is not None and deep is not None:
        s = 0.6 * (deep @ q_deep) + 0.4 * s
    return np.clip(s, 0.0, 1.0)


def query_vector(out_dir: str, event_id: str, track: int) -> Optional[dict]:
    idx = load_index(out_dir)
    if idx is None:
        return None
    for i, m in enumerate(idx["meta"]):
        if m["event_id"] == event_id and int(m["track"]) == int(track):
            return {"hand": idx["vectors"][i], "deep": idx["deep"][i] if idx["deep"] is not None else None,
                    "model": idx["model"], "meta": m}
    return None


def search(sources: Sequence[dict], q_hand: np.ndarray, q_deep: Optional[np.ndarray] = None, q_model: str = "",
           min_score: float = 0.6, limit: int = 100, exclude: Optional[tuple] = None,
           colors: Optional[dict] = None) -> List[dict]:
    """Rank person tracks of several analysed folders.

    `sources`: [{"source_id", "out_dir", "offset_for": callable(camera) -> seconds}] (the case
    layer supplies calibrated time). Returns matches with score, both times and crop paths."""
    from .cases import calibrate
    out = []
    for s in sources:
        idx = load_index(s["out_dir"])
        if idx is None or not idx["keys"]:
            continue
        use_deep = q_deep is not None and idx["deep"] is not None and idx["model"] == q_model
        sc = similarity(q_hand, idx["vectors"], q_deep if use_deep else None, idx["deep"] if use_deep else None)
        for i in np.nonzero(sc >= min_score)[0]:
            m = idx["meta"][i]
            if exclude and (s["source_id"], m["event_id"], int(m["track"])) == exclude:
                continue
            if colors and any(colors.get(k) and m.get(k) != colors[k] for k in ("upper", "lower")):
                continue
            off = s["offset_for"](m.get("camera"))
            out.append({**m, "source_id": s["source_id"], "score": round(float(sc[i]), 3),
                        "dvr_time": m.get("first_dt") or m.get("start_dt"),
                        "calibrated_time": calibrate(m.get("first_dt") or m.get("start_dt"), off),
                        "offset_seconds": off, "method": "learned+built-in" if use_deep else "built-in"})
    out.sort(key=lambda x: -x["score"])
    out = out[:limit]
    out.sort(key=lambda x: (x["calibrated_time"] or "9999", -x["score"]))
    return out


__all__ = ["COLOR_NAMES", "OnnxReID", "appearance", "build_index", "color_name", "learned_model", "load_index", "query_vector",
           "search", "similarity"]

"""Face detection (OpenCV YuNet), quality scoring, recognition embeddings
(OpenCV SFace, Apache-2.0) and a persistent cross-video identity database
with optional known-person enrolment (staff / owner)."""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import List, Optional

import cv2
import numpy as np

log = logging.getLogger("cctv")

CACHE = os.path.expanduser("~/.cache/cctv_analysis")
MODELS = {
    "yunet": ["https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
              "https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx"],
    "sface": ["https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
              "https://huggingface.co/opencv/face_recognition_sface/resolve/main/face_recognition_sface_2021dec.onnx"],
}


def fetch(key: str) -> str:
    os.makedirs(CACHE, exist_ok=True)
    dst = os.path.join(CACHE, os.path.basename(MODELS[key][0]))
    if os.path.exists(dst) and os.path.getsize(dst) > 100_000:
        return dst
    for url in MODELS[key]:
        try:
            urllib.request.urlretrieve(url, dst + ".part")
            if os.path.getsize(dst + ".part") > 100_000:
                os.replace(dst + ".part", dst)
                return dst
        except Exception as e:
            log.warning("download failed %s: %s", url, e)
    raise RuntimeError(f"could not download {key} model")


def sharpness(img: np.ndarray) -> float:
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    return float(cv2.Laplacian(g, cv2.CV_32F).var())


class FaceEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.det = cv2.FaceDetectorYN.create(fetch("yunet"), "", (320, 320), cfg.face_score_threshold, 0.3, 50)
        self.rec = cv2.FaceRecognizerSF.create(fetch("sface"), "")

    def detect_in_people(self, frame: np.ndarray, persons: List[dict]) -> List[dict]:
        """Search faces in the upper part of each person box (much better than the
        full frame for small CCTV faces). Returns faces tagged with track id."""
        H, W = frame.shape[:2]
        out = []
        for p in persons:
            x1, y1, x2, y2 = p["xyxy"]
            bw, bh = x2 - x1, y2 - y1
            if bw < 12 or bh < 24:
                continue
            cx1 = int(max(0, x1 - 0.1 * bw)); cx2 = int(min(W, x2 + 0.1 * bw))
            # full body (tall box): face is in the top half; close-up / partial body: search almost all of it
            frac = 0.5 if bh / max(bw, 1) > 2.0 else 0.9
            cy1 = int(max(0, y1 - 0.08 * bh)); cy2 = int(min(H, y1 + frac * bh))
            crop = frame[cy1:cy2, cx1:cx2]
            if crop.size == 0:
                continue
            scale = 1.0
            if crop.shape[1] < 200:
                scale = 200.0 / crop.shape[1]
                crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            self.det.setInputSize((crop.shape[1], crop.shape[0]))
            _, faces = self.det.detect(crop)
            if faces is None:
                continue
            for f in faces:
                f = f.copy()
                f[:14] /= scale
                # layout: x, y, w, h, 5 landmarks (x, y), score -> offset positions, not sizes
                f[[0, 4, 6, 8, 10, 12]] += cx1
                f[[1, 5, 7, 9, 11, 13]] += cy1
                fw, fh = f[2], f[3]
                if min(fw, fh) < self.cfg.face_min_size:
                    continue
                out.append({"track": p.get("track"), "row": f, "score": float(f[14]),
                            "box": [float(f[0]), float(f[1]), float(f[0] + fw), float(f[1] + fh)]})
                break  # one face per person
        return out

    def detect_full(self, frame: np.ndarray) -> List[dict]:
        self.det.setInputSize((frame.shape[1], frame.shape[0]))
        _, faces = self.det.detect(frame)
        res = []
        for f in faces if faces is not None else []:
            res.append({"track": None, "row": f, "score": float(f[14]),
                        "box": [float(f[0]), float(f[1]), float(f[0] + f[2]), float(f[1] + f[3])]})
        return res

    def quality(self, frame: np.ndarray, face: dict) -> float:
        f = face["row"]
        x, y, w, h = [int(v) for v in f[:4]]
        crop = frame[max(0, y):y + h, max(0, x):x + w]
        if crop.size == 0:
            return 0.0
        # frontal-ness from landmarks: nose centred between the eyes
        le, re_, nose = f[4:6], f[6:8], f[8:10]
        eye_d = abs(re_[0] - le[0]) + 1e-6
        frontal = 1.0 - min(1.0, abs((nose[0] - (le[0] + re_[0]) / 2) / eye_d) * 1.5)
        size = min(1.0, min(w, h) / 80.0)
        sharp = min(1.0, sharpness(crop) / 150.0)
        return float(face["score"] * (0.4 * size + 0.3 * sharp + 0.3 * max(0, frontal)))

    def embed(self, frame: np.ndarray, face: dict) -> Optional[np.ndarray]:
        try:
            aligned = self.rec.alignCrop(frame, face["row"])
            e = self.rec.feature(aligned).flatten().astype(np.float32)
            return e / (np.linalg.norm(e) + 1e-9)
        except Exception:
            return None

    def crop(self, frame: np.ndarray, box, pad: float = 0.35) -> np.ndarray:
        H, W = frame.shape[:2]
        x1, y1, x2, y2 = box
        w, h = x2 - x1, y2 - y1
        return frame[int(max(0, y1 - pad * h)):int(min(H, y2 + pad * h)),
                     int(max(0, x1 - pad * w)):int(min(W, x2 + pad * w))].copy()


class IdentityDB:
    """Greedy online clustering of face embeddings, persisted between runs so that
    'P0007' means the same person across days. Known people come from
    known_faces_dir/<Name>/*.jpg and are matched first."""

    def __init__(self, path: str, match_thr: float, known_thr: float):
        self.path = path
        self.thr = match_thr
        self.known_thr = known_thr
        self.ids: List[dict] = []
        self.emb: List[np.ndarray] = []
        self.known: List[dict] = []
        if os.path.exists(path + ".json") and os.path.exists(path + ".npy"):
            with open(path + ".json") as fh:
                self.ids = json.load(fh)
            arr = np.load(path + ".npy")
            self.emb = [a for a in arr]
            if len(self.emb) != len(self.ids):
                self.ids, self.emb = [], []

    def enroll_known(self, engine: FaceEngine, folder: str) -> int:
        if not folder or not os.path.isdir(folder):
            return 0
        n = 0
        for name in sorted(os.listdir(folder)):
            d = os.path.join(folder, name)
            files = [os.path.join(d, f) for f in sorted(os.listdir(d))] if os.path.isdir(d) else [d]
            label = name if os.path.isdir(d) else os.path.splitext(name)[0]
            embs = []
            for f in files:
                img = cv2.imread(f)
                if img is None:
                    continue
                faces = sorted(engine.detect_full(img), key=lambda x: -x["score"] * (x["box"][2] - x["box"][0]))
                if faces:
                    e = engine.embed(img, faces[0])
                    if e is not None:
                        embs.append(e)
            if embs:
                c = np.mean(embs, 0)
                self.known.append({"name": label, "emb": c / np.linalg.norm(c), "n": len(embs)})
                n += 1
        log.info("Enrolled %d known people", n)
        return n

    def assign(self, emb: np.ndarray, when: str, sample: str = "", event_id: str = "") -> tuple:
        """Return (identity_id, similarity, known_name)."""
        known_name = ""
        for k in self.known:
            s = float(np.dot(emb, k["emb"]))
            if s >= self.known_thr and (not known_name or s > known_sim):
                known_name, known_sim = k["name"], s
        best, bi = -1.0, -1
        if self.emb:
            sims = np.stack(self.emb) @ emb
            bi = int(np.argmax(sims))
            best = float(sims[bi])
        if bi >= 0 and best >= self.thr:
            rec = self.ids[bi]
            n = rec["count"]
            c = self.emb[bi] * n + emb
            self.emb[bi] = c / np.linalg.norm(c)
            rec["count"] = n + 1
            ev = rec.setdefault("events", [])
            if event_id and event_id not in ev:
                ev.append(event_id)
            rec["last_seen"] = max(rec["last_seen"], when)
            rec["first_seen"] = min(rec["first_seen"], when)
            if known_name and not rec.get("name"):
                rec["name"] = known_name
            return rec["id"], best, rec.get("name", "") or known_name
        new_id = f"P{len(self.ids) + 1:04d}"
        self.ids.append({"id": new_id, "count": 1, "first_seen": when, "last_seen": when,
                         "sample": sample, "name": known_name, "events": [event_id] if event_id else []})
        self.emb.append(emb)
        return new_id, 1.0, known_name

    def set_sample(self, pid: str, path: str):
        for r in self.ids:
            if r["id"] == pid and not r.get("sample"):
                r["sample"] = path

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path + ".json", "w") as fh:
            json.dump(self.ids, fh, indent=1)
        np.save(self.path + ".npy", np.stack(self.emb) if self.emb else np.zeros((0, 128), np.float32))

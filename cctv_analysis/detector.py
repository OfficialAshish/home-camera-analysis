"""Pass 2 models: Ultralytics YOLO (detection + multi-object tracking) and an
optional pose model used for posture cues (crouching, hands raised, face
visibility)."""
from __future__ import annotations

import logging
from typing import List, Optional

import numpy as np

log = logging.getLogger("cctv")

COCO_KPT = {"nose": 0, "l_eye": 1, "r_eye": 2, "l_ear": 3, "r_ear": 4, "l_sh": 5, "r_sh": 6,
            "l_el": 7, "r_el": 8, "l_wr": 9, "r_wr": 10, "l_hip": 11, "r_hip": 12,
            "l_kn": 13, "r_kn": 14, "l_an": 15, "r_an": 16}


def resolve_device(pref: str = "auto") -> str:
    if pref and pref != "auto":
        return pref
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda:0"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


class _DropNoise(logging.Filter):
    NOISE = ("'half' is deprecated", "Use 'quantize' instead")

    def filter(self, record):
        msg = record.getMessage()
        return not any(n in msg for n in self.NOISE)


def _quiet_ultralytics():
    lg = logging.getLogger("ultralytics")
    if not any(isinstance(f, _DropNoise) for f in lg.filters):
        lg.addFilter(_DropNoise())
        for h in lg.handlers:
            h.addFilter(_DropNoise())


def load_yolo(candidates: List[str]):
    from ultralytics import YOLO
    from . import modelcache
    _quiet_ultralytics()
    last = None
    for name in candidates:
        if not name:
            continue
        try:
            m = YOLO(modelcache.yolo_weights(name))
            log.info("Loaded %s", name)
            return m, name
        except Exception as e:  # weights missing / unsupported by installed version
            last = e
            log.warning("Could not load %s (%s); trying fallback", name, e)
    raise RuntimeError(f"No YOLO model could be loaded: {last}")


def iou(a, b) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


class Detector:
    def __init__(self, cfg):
        self.cfg = cfg
        self.device = resolve_device(cfg.device)
        self.half = self.device.startswith("cuda")
        self.model, self.model_name = load_yolo([cfg.detector_model, *cfg.detector_fallbacks])
        names = self.model.names
        self.names = names if isinstance(names, dict) else dict(enumerate(names))
        if cfg.keep_classes:
            wanted = set(cfg.keep_classes)
            self.class_ids = [i for i, n in self.names.items() if n in wanted]
        else:
            self.class_ids = None
        self.pose = None
        self.pose_name = ""
        if cfg.use_pose and cfg.pose_model:
            try:
                self.pose, self.pose_name = load_yolo([cfg.pose_model, "yolo11n-pose.pt", "yolov8n-pose.pt"])
            except Exception as e:
                log.warning("Pose model disabled: %s", e)
        self._fresh = True
        self._extra = {"half": True} if self.half else {}

    def reset(self):
        """Start a new, independent tracking session (one per event)."""
        self._fresh = True

    def __call__(self, frame: np.ndarray) -> List[dict]:
        cfg = self.cfg
        r = self.model.track(frame, persist=not self._fresh, tracker=cfg.tracker, conf=cfg.det_conf,
                             imgsz=cfg.det_imgsz, classes=self.class_ids, device=self.device,
                             verbose=False, **self._extra)[0]
        self._fresh = False
        dets = []
        b = r.boxes
        if b is not None and len(b):
            xyxy = b.xyxy.cpu().numpy()
            conf = b.conf.cpu().numpy()
            cls = b.cls.cpu().numpy().astype(int)
            ids = b.id.cpu().numpy().astype(int) if b.id is not None else [None] * len(cls)
            for k in range(len(cls)):
                dets.append({"cls": self.names.get(int(cls[k]), str(cls[k])), "conf": float(conf[k]),
                             "xyxy": [float(v) for v in xyxy[k]],
                             "track": int(ids[k]) if ids[k] is not None else None, "kpts": None})
        if self.pose is not None and any(d["cls"] == "person" for d in dets):
            try:
                p = self.pose.predict(frame, conf=max(0.25, cfg.det_conf - 0.05), imgsz=cfg.det_imgsz,
                                      device=self.device, verbose=False, **self._extra)[0]
                if p.keypoints is not None and p.boxes is not None and len(p.boxes):
                    pb = p.boxes.xyxy.cpu().numpy()
                    kp = p.keypoints.data.cpu().numpy()          # (n,17,3)
                    for d in dets:
                        if d["cls"] != "person":
                            continue
                        best, bi = 0.0, -1
                        for j in range(len(pb)):
                            v = iou(d["xyxy"], pb[j])
                            if v > best:
                                best, bi = v, j
                        if best > 0.5:
                            d["kpts"] = kp[bi]
            except Exception as e:
                log.debug("pose failed: %s", e)
        return dets


# ------------------------------------------------------------ pose heuristics

def posture(kpts: Optional[np.ndarray], conf_thr: float = 0.4) -> dict:
    """Simple, explainable posture cues from COCO keypoints."""
    out = {"face_visible_pose": False, "crouching": False, "hands_up": False}
    if kpts is None:
        return out
    k = kpts

    def ok(n):
        return k[COCO_KPT[n], 2] >= conf_thr

    def p(n):
        return k[COCO_KPT[n], :2]

    out["face_visible_pose"] = ok("nose") and ok("l_eye") and ok("r_eye")
    if ok("l_sh") and ok("r_sh") and ok("l_hip") and ok("r_hip"):
        sh = (p("l_sh") + p("r_sh")) / 2
        hip = (p("l_hip") + p("r_hip")) / 2
        torso = np.linalg.norm(sh - hip) + 1e-6
        if ok("l_kn") and ok("r_kn"):
            kn = (p("l_kn") + p("r_kn")) / 2
            # knees close to hip height -> crouching / squatting / bending low
            if (kn[1] - hip[1]) < 0.35 * torso:
                out["crouching"] = True
        for w in ("l_wr", "r_wr"):
            if ok(w):
                wr = p(w)
                if wr[1] < sh[1] - 0.25 * torso:
                    out["hands_up"] = True
        if out["hands_up"]:
            both = ok("l_wr") and ok("r_wr") and p("l_wr")[1] < sh[1] and p("r_wr")[1] < sh[1]
            out["hands_up"] = bool(both)
    return out

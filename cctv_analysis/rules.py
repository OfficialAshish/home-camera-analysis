"""Explainable suspicion scoring. Every flag carries a weight and a human
reason, so a reviewer can see *why* an event was ranked high."""
from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional

import numpy as np

WEIGHTS = {
    "after_hours": 30,
    "camera_tamper": 45,
    "weapon_like_object": 40,
    "restricted_zone": 25,
    "loitering": 15,
    "running": 12,
    "crowd": 10,
    "face_hidden": 18,
    "hands_up": 20,
    "crouching": 8,
    "hand_at_waist": 6,
    "semantic_match": 15,
    "vlm_suspicious": 30,
    "unknown_person_after_hours": 10,
    "repeat_visitor": 5,
    "long_event": 5,
}


def point_in_poly(x: float, y: float, poly) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def zones_for(cfg, camera: str) -> Dict[str, list]:
    z = dict(cfg.zones.get("*", {}))
    z.update(cfg.zones.get(camera, {}))
    return z


def _hm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def is_after_hours(when: Optional[dt.datetime], cfg) -> bool:
    if when is None:
        return False
    if when.weekday() in (cfg.closed_weekdays or []):
        return True
    o, c = _hm(cfg.open_hour), _hm(cfg.close_hour)
    t = when.time()
    if o <= c:
        return not (o <= t < c)
    return c <= t < o           # shop open across midnight


def score_event(ev: dict, tracks: List[dict], cfg, vlm: Optional[dict] = None,
                semantic: Optional[List[dict]] = None, tamper: Optional[List[dict]] = None) -> tuple:
    flags = []

    def add(key, reason, scale=1.0):
        flags.append({"flag": key, "points": round(WEIGHTS[key] * scale, 1), "reason": reason})

    persons = [t for t in tracks if t["cls"] == "person" and t["duration"] >= 0.5]
    start_dt = ev.get("start_dt")
    if is_after_hours(start_dt, cfg) and (persons or ev.get("motion_peak", 0) > 0.01):
        add("after_hours", f"activity at {start_dt:%H:%M} outside shop hours {cfg.open_hour}-{cfg.close_hour}"
            + (" with people" if persons else " (no person detected)"), 1.0 if persons else 0.5)
    for tp in tamper or []:
        if tp["start"] <= ev["end"] and tp["end"] >= ev["start"]:
            add("camera_tamper", f"{tp['kind']} for {tp['end'] - tp['start']:.0f}s")
            break
    weapons = sorted({t["cls"] for t in tracks if t["cls"] in cfg.weapon_classes and t["max_conf"] >= 0.4})
    if weapons:
        add("weapon_like_object", "detected: " + ", ".join(weapons))
    zone_hits = {}
    for t in persons:
        for z, secs in t.get("zones", {}).items():
            zone_hits[z] = zone_hits.get(z, 0) + secs
    zone_hits = {z: s for z, s in zone_hits.items() if s >= 1.0}
    if zone_hits:
        add("restricted_zone", "; ".join(f"{z}: {s:.0f}s" for z, s in zone_hits.items()))
    loiter = [t for t in persons if t["duration"] >= cfg.loiter_seconds]
    if loiter:
        add("loitering", f"{len(loiter)} person(s) stayed ≥{cfg.loiter_seconds:.0f}s "
                         f"(max {max(t['duration'] for t in loiter):.0f}s)")
    runners = [t for t in persons if t.get("max_speed", 0) >= cfg.running_speed]
    if runners:
        add("running", f"{len(runners)} fast-moving person(s), {max(t['max_speed'] for t in runners):.1f} body-heights/s")
    if ev.get("max_persons", 0) >= cfg.crowd_size:
        add("crowd", f"{ev['max_persons']} people at once")
    hidden = [t for t in persons if t.get("hidden_face_s", 0) >= cfg.hidden_face_seconds]
    if hidden:
        add("face_hidden", f"{len(hidden)} person(s) facing camera but no face detected "
                           f"(possible mask/helmet/cover) ≥{cfg.hidden_face_seconds:.0f}s")
    if any(t.get("hands_up_s", 0) >= 1.5 for t in persons):
        add("hands_up", "both hands raised above shoulders (possible threat / hold-up)")
    if any(t.get("crouch_s", 0) >= 3 for t in persons):
        add("crouching", "person crouching/bending low for a while")
    if any(t.get("waist_s", 0) >= 4 for t in persons):
        add("hand_at_waist", "hand kept near waist/pocket")
    if semantic:
        best = max(semantic, key=lambda s: s["suspicious_score"])
        if best["suspicious_score"] >= cfg.semantic_alert_score and best["lift"] >= 0.6:
            add("semantic_match", f"scene resembles: '{best['top_suspicious']}' ({best['suspicious_score']:.2f})")
    if vlm and vlm.get("suspicious"):
        c = vlm.get("confidence")
        c = 0.7 if c is None else max(0.3, min(1.0, float(c)))
        add("vlm_suspicious", f"AI description: {vlm.get('category') or ''} – {vlm.get('reason') or vlm.get('description', '')[:120]}", c)
    if is_after_hours(start_dt, cfg) and any(t.get("identity") and not t.get("known_name") for t in persons):
        add("unknown_person_after_hours", "unrecognised face after hours")
    if any(t.get("identity_visits", 0) >= 5 and not t.get("known_name") for t in persons):
        add("repeat_visitor", "same unknown face seen in ≥5 events")
    if ev["end"] - ev["start"] >= 300 and persons:
        add("long_event", f"event lasted {(ev['end'] - ev['start']) / 60:.0f} min")
    staff_only = persons and all(t.get("known_name") for t in persons if t.get("identity"))
    score = sum(f["points"] for f in flags)
    if staff_only and any(t.get("identity") for t in persons) and not weapons:
        score *= 0.5
        flags.append({"flag": "known_people_only", "points": 0, "reason": "only known/staff faces – score halved"})
    return float(min(100.0, score)), flags


def summarize_track(t: dict, fps: float, frame_h: int) -> dict:
    """Turn raw per-frame observations into track statistics."""
    obs = t["obs"]
    times = np.array([o[0] for o in obs])
    out = {k: t[k] for k in ("track", "cls")}
    out["first"] = float(times.min())
    out["last"] = float(times.max())
    out["duration"] = float(out["last"] - out["first"])
    out["frames"] = len(obs)
    out["max_conf"] = float(max(o[1] for o in obs))
    out["mean_conf"] = float(np.mean([o[1] for o in obs]))
    cx = np.array([(o[2][0] + o[2][2]) / 2 for o in obs])
    cy = np.array([(o[2][1] + o[2][3]) / 2 for o in obs])
    hh = np.array([max(1.0, o[2][3] - o[2][1]) for o in obs])
    sp = 0.0
    if len(obs) >= 3:
        dt_ = np.diff(times)
        d = np.hypot(np.diff(cx), np.diff(cy)) / ((hh[1:] + hh[:-1]) / 2)
        v = d / np.maximum(dt_, 1e-3)
        v = np.convolve(v, np.ones(3) / 3, mode="valid") if len(v) >= 3 else v
        sp = float(np.max(v)) if len(v) else 0.0
    out["max_speed"] = sp
    out["path"] = [[round(float(a), 1), round(float(b), 1)] for a, b in zip(cx[::max(1, len(cx) // 40)], cy[::max(1, len(cy) // 40)])]
    step = 1.0 / fps
    out["zones"] = {z: n * step for z, n in t.get("zone_frames", {}).items()}
    out["hidden_face_s"] = t.get("hidden_frames", 0) * step
    out["hands_up_s"] = t.get("hands_up", 0) * step
    out["crouch_s"] = t.get("crouch", 0) * step
    out["waist_s"] = t.get("waist", 0) * step
    out["faces_found"] = t.get("faces_found", 0)
    return out

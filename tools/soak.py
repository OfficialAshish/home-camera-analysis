"""Bounded load test of the app's hot read paths against a large synthetic results database.

  python tools/soak.py --events 100000 --plates 30000 --reviews 20000 --clients 8 --seconds 30 [--no-indexes]

Builds <tmp>/out/analysis.db with the real schema (Store), runs the real app (waitress) and N
client threads that cycle through the event list (filters, pages), event detail, plate search,
summary and highlights. Prints per-endpoint count, error rate and p50/p95/p99 latency (ms).
--no-indexes drops the indexes added for the app (the schema before this round) for a before/after.
Every request's status and body size is checked; a non-200 counts as an error.
"""
import argparse
import json
import os
import random
import socket
import statistics
import sys
import tempfile
import threading
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cctv_analysis.app.server import create_app  # noqa: E402
from cctv_analysis.reviews import Reviews  # noqa: E402
from cctv_analysis.storage import Store  # noqa: E402

NEW_INDEXES = ["ix_ev_start", "ix_ev_cam", "ix_tr_ident", "ix_faces_ev", "ix_plates_text", "ix_plates_ev",
               "ix_alerts_kind", "ix_ev_run", "ix_ev_score"]


def build(out: str, n_events: int, n_plates: int, n_reviews: int, drop: bool):
    os.makedirs(out, exist_ok=True)
    st = Store(os.path.join(out, "w.db"), os.path.join(out, "analysis.db"))
    rnd = random.Random(1)
    cams = [f"ch{i:02d}" for i in range(1, 9)]
    letters = "ABCDEFGHJKLMNPRSTUVWXYZ"
    st.db.execute("INSERT INTO videos (id, path, rel_path, status, duration, motion_seconds, time_source) "
                  "VALUES (1, '/v/a.mp4', 'a.mp4', 'done', 3600, 600, 'filename')")
    rows, prow, trow, segs = [], [], [], []
    t0 = 1_793_000_000
    for i in range(n_events):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t0 + i * 37))
        eid = f"{cams[i % 8]}-{ts.replace('-', '').replace(':', '').replace(' ', '-')}-{i:07d}"
        lv = rnd.choices(["HIGH", "REVIEW", "LOW", "NORMAL"], [1, 4, 15, 80])[0]
        pl = []
        if i < n_plates:
            pl = ["MH%02d%s%s%04d" % (rnd.randint(1, 50), rnd.choice(letters), rnd.choice(letters), rnd.randint(0, 9999))]
            prow.append((eid, 1, "car", cams[i % 8], 3.0, ts, pl[0], 0.9, 1, 6, "[]", "ocr", "", "", ""))
        rows.append((eid, 1, "/v/a.mp4", "a.mp4", cams[i % 8], 10.0, 40.0, ts, ts, 30.0, "person", lv,
                     float(rnd.randint(0, 100)), json.dumps([{"flag": "loitering", "points": 15, "reason": "x"}]),
                     json.dumps([{"t_off": 11, "annotated": f"{out}/e/{eid}.jpg", "persons": 1}]), json.dumps(pl),
                     json.dumps(["P%04d" % rnd.randint(1, 500)]), json.dumps({"person": 1}), 1, 1, "filename"))
        trow.append((eid, 1, "person", "P%04d" % rnd.randint(1, 500), ts, 10.0))
        if i % 50 == 0:
            segs.append((ts[:10], i // 50 % 100000, eid, float(i), float(i + 30), "/s.mp4", f"{out}/h/{ts[:10]}.mp4"))
    st.db.executemany("INSERT INTO events (event_id, video_id, video_path, rel_path, camera, start_off, end_off, start_dt, "
                      "end_dt, duration, kind, level, score, flags, snapshots, plates, identities, objects, "
                      "n_person_tracks, max_persons, time_source) VALUES (" + ",".join("?" * 21) + ")", rows)
    st.db.executemany("INSERT INTO plates (event_id, track, vehicle, camera, t_off, dt, text, confidence, valid_format, "
                      "n_reads, reads, method, crop_path, enhanced_path, watchlist) VALUES (" + ",".join("?" * 15) + ")", prow)
    st.db.executemany("INSERT INTO tracks (event_id, track, cls, identity, first_dt, duration) VALUES (?,?,?,?,?,?)", trow)
    st.db.executemany("INSERT OR IGNORE INTO reel_segments VALUES (?,?,?,?,?,?,?)", segs)
    if drop:
        for ix in NEW_INDEXES:
            st.db.execute(f"DROP INDEX IF EXISTS {ix}")
    st.commit()
    rv = Reviews(out)
    for r in rows[:n_reviews]:
        rv.set(r[0], random.choice(["Suspicious", "False alarm", "OK / normal"]))
    rv.close()
    return [r[0] for r in rows], [p[6] for p in prow]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=100_000)
    ap.add_argument("--plates", type=int, default=30_000)
    ap.add_argument("--reviews", type=int, default=20_000)
    ap.add_argument("--clients", type=int, default=8)
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--no-indexes", action="store_true")
    a = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="soak_")
    out = os.path.join(tmp, "out")
    t = time.time()
    ids, plates = build(out, a.events, a.plates, a.reviews, a.no_indexes)
    print(f"built {a.events} events / {a.plates} plates / {a.reviews} reviews in {time.time() - t:.0f}s"
          f"{' (app indexes dropped)' if a.no_indexes else ''}")
    from waitress.server import create_server
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(os.path.join(tmp, "data"), "soak", port)
    app.extensions["cctv"]  # noqa: B018
    import cctv_analysis.app.server as srv_mod
    srv_mod.RateLimiter.allow = lambda self, key, n, per: True        # measure the handlers, not the limiter
    srv = create_server(app, host="127.0.0.1", port=port, threads=8)
    threading.Thread(target=srv.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    H = {"X-CCTV-Token": "soak"}
    days = sorted({i.split("-")[1][:8] for i in ids[:: max(1, len(ids) // 50)]})
    stats = {}
    lock = threading.Lock()
    stop = time.time() + a.seconds

    def req(rnd):
        k = rnd.random()
        if k < 0.35:
            d = rnd.choice(days)
            iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            return "events(filter)", f"/api/results/events?out={out}&levels=HIGH,REVIEW&day_from={iso}&day_to={iso}&offset=0"
        if k < 0.5:
            return "events(page)", f"/api/results/events?out={out}&levels=&offset={rnd.randint(0, 2000)}&sort=score"
        if k < 0.6:
            return "events(review)", f"/api/results/events?out={out}&levels=&review=Not reviewed"
        if k < 0.75:
            return "event detail", f"/api/results/events/{rnd.choice(ids)}?out={out}"
        if k < 0.88:
            return "plates search", f"/api/results/plates?out={out}&q={rnd.choice(plates)[:8]}&edits=1"
        if k < 0.95:
            return "summary", f"/api/results/summary?out={out}"
        return "highlights", f"/api/results/highlights?out={out}"

    def worker(seed):
        rnd = random.Random(seed)
        sess = requests.Session()
        while time.time() < stop:
            name, url = req(rnd)
            t0 = time.perf_counter()
            try:
                r = sess.get(base + url, headers=H, timeout=60)
                ok = r.status_code == 200 and len(r.content) > 2
                size = len(r.content)
            except Exception:
                ok, size = False, 0
            ms = (time.perf_counter() - t0) * 1000
            with lock:
                s = stats.setdefault(name, {"ms": [], "err": 0, "bytes": 0})
                s["ms"].append(ms)
                s["err"] += not ok
                s["bytes"] = max(s["bytes"], size)

    th = [threading.Thread(target=worker, args=(i,)) for i in range(a.clients)]
    [x.start() for x in th]
    [x.join() for x in th]
    srv.close()
    print(f"{'endpoint':16s} {'n':>6s} {'err%':>6s} {'p50':>8s} {'p95':>8s} {'p99':>8s} {'max KB':>8s}")
    tot = err = 0
    for name, s in sorted(stats.items()):
        q = statistics.quantiles(s["ms"], n=100) if len(s["ms"]) > 1 else [s["ms"][0]] * 99
        tot += len(s["ms"])
        err += s["err"]
        print(f"{name:16s} {len(s['ms']):6d} {100 * s['err'] / len(s['ms']):6.2f} {q[49]:8.1f} {q[94]:8.1f} "
              f"{q[98]:8.1f} {s['bytes'] / 1024:8.1f}")
    print(f"total {tot} requests in {a.seconds:.0f}s with {a.clients} clients, {err} errors")


if __name__ == "__main__":
    main()

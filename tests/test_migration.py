"""A results folder written by the original version (schema of commit bbc2e0b) must open,
migrate and work with the current code: app queries, reports, highlight index."""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cctv_analysis.config import Config  # noqa: E402
from cctv_analysis.storage import Store  # noqa: E402

OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE, rel_path TEXT, fingerprint TEXT,
  camera TEXT, start_dt TEXT, end_dt TEXT, time_source TEXT, duration REAL, fps REAL, width INT,
  height INT, codec TEXT, size_mb REAL, status TEXT, error TEXT, n_events INT, motion_seconds REAL,
  processed_at TEXT, proc_seconds REAL, heatmap_path TEXT, drive_link TEXT, models TEXT,
  lighting_changes INT, osd_masked INT);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY, video_id INT, video_path TEXT, rel_path TEXT, camera TEXT,
  start_off REAL, end_off REAL, start_dt TEXT, end_dt TEXT, duration REAL, kind TEXT,
  motion_peak REAL, motion_mean REAL, max_persons INT, n_tracks INT, n_person_tracks INT,
  objects TEXT, faces_detected INT, identities TEXT, best_conf REAL, score REAL, level TEXT,
  flags TEXT, semantic TEXT, vlm TEXT, snapshots TEXT, contact_sheet TEXT, clip_path TEXT,
  folder TEXT, after_hours INT, frames_analyzed INT);
CREATE TABLE IF NOT EXISTS tracks (
  event_id TEXT, track INT, cls TEXT, first_off REAL, last_off REAL, first_dt TEXT, duration REAL,
  frames INT, max_conf REAL, mean_conf REAL, max_speed REAL, zones TEXT, identity TEXT,
  identity_sim REAL, known_name TEXT, face_crop TEXT, person_crop TEXT, hidden_face_s REAL,
  posture TEXT, faces_found INT, path TEXT, PRIMARY KEY (event_id, track));
CREATE TABLE IF NOT EXISTS faces (
  id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, track INT, t_off REAL, dt TEXT, score REAL,
  quality REAL, box TEXT, crop_path TEXT, identity TEXT, similarity REAL, known_name TEXT);
CREATE TABLE IF NOT EXISTS detections (
  event_id TEXT, t_off REAL, dt TEXT, cls TEXT, conf REAL, track INT,
  x1 REAL, y1 REAL, x2 REAL, y2 REAL);
CREATE TABLE IF NOT EXISTS tamper (
  video_id INT, start_off REAL, end_off REAL, start_dt TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS file_hashes (
  path TEXT PRIMARY KEY, size INT, mtime INT, qhash TEXT);
CREATE TABLE IF NOT EXISTS reel_segments (
  day TEXT, idx INT, event_id TEXT, reel_start REAL, reel_end REAL, seg_path TEXT, reel_path TEXT,
  PRIMARY KEY (day, idx));
CREATE TABLE IF NOT EXISTS face_alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, kind TEXT, identity TEXT, dt TEXT, camera TEXT,
  event_id TEXT, video TEXT, t_off REAL, face_crop TEXT, person_crop TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, started TEXT, finished TEXT, videos INT, events INT, new_faces INT,
  duplicates INT, notes TEXT);
CREATE INDEX IF NOT EXISTS ix_ev_video ON events(video_id);
CREATE INDEX IF NOT EXISTS ix_det_ev ON detections(event_id);
"""


def test_old_database_migrates_and_works(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    db = sqlite3.connect(out / "analysis.db")
    db.executescript(OLD_SCHEMA)
    db.execute("INSERT INTO videos (id, path, rel_path, fingerprint, camera, start_dt, time_source, duration, status, "
               "n_events, motion_seconds) VALUES (1, '/v/a.mp4', 'a.mp4', 'x', 'ch01', '2026-11-01 11:21:59', "
               "'metadata', 14, 'done', 1, 12)")
    db.execute("INSERT INTO events (event_id, video_id, video_path, rel_path, camera, start_off, end_off, start_dt, "
               "end_dt, duration, kind, level, score, flags, snapshots, objects, identities, n_person_tracks, "
               "max_persons) VALUES ('ch01-1', 1, '/v/a.mp4', 'a.mp4', 'ch01', 1, 13, '2026-11-01 11:22:00', "
               "'2026-11-01 11:22:12', 12, 'person', 'LOW', 10, '[]', '[]', '{}', '[]', 1, 1)")
    db.commit()
    db.close()
    st = Store(str(tmp_path / "work.db"), str(out / "analysis.db"))
    cols = {r[1] for r in st.db.execute("PRAGMA table_info(events)")}
    assert {"run_id", "time_source", "luma_p99", "plates", "crossings"} <= cols
    tcols = {r[1] for r in st.db.execute("PRAGMA table_info(tracks)")}
    assert {"watchlist", "crossings"} <= tcols
    idx = {r[1] for r in st.db.execute("PRAGMA index_list(events)")}
    assert {"ix_ev_start", "ix_ev_cam", "ix_ev_score"} <= idx
    assert st.query("SELECT name FROM sqlite_master WHERE name='plates'")
    st.commit()
    from cctv_analysis.app import results as RS
    s = RS.summary(str(out))
    assert s["events"] == 1 and s["videos_with_approx_time"] == 1          # old metadata time is flagged
    ev = RS.events(str(out), levels=["LOW"])
    assert ev["total"] == 1 and ev["items"][0]["approx_time"] is True and ev["items"][0]["plates"] == []
    from cctv_analysis.report import build_reports
    cfg = Config(output_dir=str(out), work_dir=str(tmp_path / "w"), make_highlight_reel=False, make_html=True)
    res = build_reports(st, cfg)
    assert os.path.exists(res["excel"]) and os.path.exists(res["html"])


def test_reel_selection_order_and_blank_filter(tmp_path):
    from cctv_analysis.reels import events_for_day, reel_dir
    st = Store(str(tmp_path / "w.db"))
    vids = [(1, "cam/clip10.mp4", "osd(rapidocr,consensus)"), (2, "cam/clip2.mp4", "file-modified(approx)"),
            (3, "cam/clip10b.mp4", "file-modified(approx)"), (4, "cam/clip1.mp4", "filename")]
    for vid, rel, src in vids:
        st.db.execute("INSERT INTO videos (id, path, rel_path, time_source, status) VALUES (?,?,?,?, 'done')",
                      (vid, "/" + rel, rel, src))
    rows = [("e_exact_late", 1, "2026-10-31 20:00:00", "person", "LOW", 120.0),
            ("e_exact_early", 4, "2026-10-31 05:00:00", "person", "LOW", 120.0),
            ("e_approx_10b", 3, "2026-10-31 01:00:00", "person", "LOW", 120.0),     # approx: time ignored
            ("e_approx_2", 2, "2026-10-31 23:00:00", "person", "LOW", 120.0),
            ("e_blank", 4, "2026-10-31 06:00:00", "motion-only", "NORMAL", 5.0),
            ("e_tamper_dark", 4, "2026-10-31 07:00:00", "camera-tamper", "HIGH", 3.0)]
    for eid, vid, start, kind, level, luma in rows:
        rel = dict((v, r) for v, r, _ in vids)[vid]
        st.db.execute("INSERT INTO events (event_id, video_id, video_path, rel_path, camera, start_off, end_off, "
                      "start_dt, kind, level, score, n_person_tracks, luma_p99) VALUES (?,?,?,?, 'cam', 0, 5, ?,?,?, 1, 1, ?)",
                      (eid, vid, "/" + rel, rel, start, kind, level, luma))
    got = [e["event_id"] for e in events_for_day(st, "2026-10-31", "all", skip_blank=True)]
    assert got == ["e_exact_early", "e_tamper_dark", "e_exact_late", "e_approx_2", "e_approx_10b"]
    assert "e_blank" in [e["event_id"] for e in events_for_day(st, "2026-10-31", "all", skip_blank=False)]
    assert reel_dir("/o", "2026-02-04").replace(os.sep, "/") == "/o/highlights/2026/02-Feb"
    assert reel_dir("/o", "unknown-date").replace(os.sep, "/") == "/o/highlights/unknown-date"

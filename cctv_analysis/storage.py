"""SQLite store: the single source of truth. Makes runs resumable (finished
videos are skipped) and cumulative (reports always cover everything)."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from typing import Any, Dict, Iterable, List

SCHEMA = """
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


class Store:
    def __init__(self, local_path: str, remote_path: str = ""):
        self.local = local_path
        self.remote = remote_path
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        if remote_path and os.path.exists(remote_path) and not os.path.exists(local_path):
            shutil.copy2(remote_path, local_path)
        self.db = sqlite3.connect(local_path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    # columns added in later versions – keeps databases from older runs working
    _ADDED = {"videos": {"qhash": "TEXT", "duplicate_of": "TEXT", "run_id": "TEXT"},
              "events": {"run_id": "TEXT"}}

    def _migrate(self):
        for table, cols in self._ADDED.items():
            have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for c, typ in cols.items():
                if c not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {c} {typ}")

    # ------------------------------------------------------------------ util
    @staticmethod
    def _enc(v):
        if isinstance(v, (dict, list, tuple)):
            return json.dumps(v, default=str)
        return v

    def insert(self, table: str, row: Dict[str, Any], replace=True):
        keys = list(row)
        q = f"INSERT OR {'REPLACE' if replace else 'IGNORE'} INTO {table} ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})"
        cur = self.db.execute(q, [self._enc(row[k]) for k in keys])
        return cur.lastrowid

    def insert_many(self, table: str, rows: Iterable[Dict[str, Any]]):
        rows = list(rows)
        if not rows:
            return
        keys = list(rows[0])
        q = f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})"
        self.db.executemany(q, [[self._enc(r.get(k)) for k in keys] for r in rows])

    def query(self, sql: str, args=()) -> List[dict]:
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    # --------------------------------------------------------------- videos
    def cached_hash(self, path: str, size: int, mtime: int):
        r = self.db.execute("SELECT qhash FROM file_hashes WHERE path=? AND size=? AND mtime=?",
                            (path, size, mtime)).fetchone()
        return r["qhash"] if r else None

    def put_hash(self, path: str, size: int, mtime: int, qhash: str):
        self.db.execute("INSERT OR REPLACE INTO file_hashes VALUES (?,?,?,?)", (path, size, mtime, qhash))

    def done_with_hash(self, qhash: str, exclude_path: str = ""):
        r = self.db.execute("SELECT path FROM videos WHERE qhash=? AND status='done' AND path!=?",
                            (qhash, exclude_path)).fetchone()
        return r["path"] if r else None

    def video_done(self, path: str, fingerprint: str) -> bool:
        r = self.db.execute("SELECT status, fingerprint FROM videos WHERE path=?", (path,)).fetchone()
        return bool(r and r["status"] == "done" and r["fingerprint"] == fingerprint)

    def start_video(self, row: dict) -> int:
        old = self.db.execute("SELECT id FROM videos WHERE path=?", (row["path"],)).fetchone()
        if old:
            vid = old["id"]
            self.purge_video(vid)
            sets = ",".join(f"{k}=?" for k in row)
            self.db.execute(f"UPDATE videos SET {sets} WHERE id=?", [self._enc(v) for v in row.values()] + [vid])
        else:
            vid = self.insert("videos", row)
        self.db.commit()
        return vid

    def purge_video(self, vid: int):
        evs = [r["event_id"] for r in self.query("SELECT event_id FROM events WHERE video_id=?", (vid,))]
        for t in ("tracks", "faces", "detections"):
            self.db.executemany(f"DELETE FROM {t} WHERE event_id=?", [(e,) for e in evs])
        self.db.execute("DELETE FROM events WHERE video_id=?", (vid,))
        self.db.execute("DELETE FROM tamper WHERE video_id=?", (vid,))

    def update_video(self, vid: int, **kw):
        sets = ",".join(f"{k}=?" for k in kw)
        self.db.execute(f"UPDATE videos SET {sets} WHERE id=?", [self._enc(v) for v in kw.values()] + [vid])
        self.db.commit()

    def commit(self, sync: bool = True):
        self.db.commit()
        if sync and self.remote:
            try:
                os.makedirs(os.path.dirname(self.remote), exist_ok=True)
                bk = sqlite3.connect(self.remote + ".tmp")
                self.db.backup(bk)
                bk.close()
                os.replace(self.remote + ".tmp", self.remote)
            except Exception:
                pass

    def close(self):
        self.commit()
        self.db.close()

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
        self.db.commit()

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

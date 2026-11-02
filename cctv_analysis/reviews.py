"""Reviewer decisions per event (status + notes), kept in <output>/reviews.db.

Separate from analysis.db on purpose: the analyzer replaces analysis.db with its working copy
after every video, so anything written there by another process would be lost. Reports and
the app read reviews from here."""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
from typing import Dict, Optional

STATUSES = ["Not reviewed", "OK / normal", "Suspicious", "Theft confirmed", "Reported", "False alarm"]


class Reviews:
    def __init__(self, output_dir: str):
        self.path = os.path.join(output_dir, "reviews.db")
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.execute("CREATE TABLE IF NOT EXISTS reviews (event_id TEXT PRIMARY KEY, status TEXT, notes TEXT, "
                        "reviewer TEXT, updated TEXT)")
        self.db.commit()

    def set(self, event_id: str, status: str, notes: str = "", reviewer: str = "") -> dict:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        row = (event_id, status, (notes or "")[:4000], (reviewer or "")[:100],
               dt.datetime.now().astimezone().isoformat(timespec="seconds"))
        with self.db:
            self.db.execute("INSERT INTO reviews VALUES (?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET "
                            "status=excluded.status, notes=excluded.notes, reviewer=excluded.reviewer, "
                            "updated=excluded.updated", row)
        return dict(zip(("event_id", "status", "notes", "reviewer", "updated"), row))

    def get(self, event_id: str) -> Optional[dict]:
        r = self.db.execute("SELECT event_id, status, notes, reviewer, updated FROM reviews WHERE event_id=?",
                            (event_id,)).fetchone()
        return dict(zip(("event_id", "status", "notes", "reviewer", "updated"), r)) if r else None

    def all(self) -> Dict[str, dict]:
        return {r[0]: {"status": r[1], "notes": r[2], "reviewer": r[3], "updated": r[4]}
                for r in self.db.execute("SELECT event_id, status, notes, reviewer, updated FROM reviews")}

    def close(self):
        self.db.close()


def load(output_dir: str) -> Dict[str, dict]:
    """All reviews of an output folder ({} when none)."""
    if not os.path.exists(os.path.join(output_dir, "reviews.db")):
        return {}
    r = Reviews(output_dir)
    try:
        return r.all()
    finally:
        r.close()

"""Reviewer decisions per event (status + notes) and examiners' plate readings, kept in
<output>/reviews.db.

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
        # the plate as an examiner read it (text '' = examined, not readable); the machine read stays in analysis.db
        self.db.execute("CREATE TABLE IF NOT EXISTS plate_readings (event_id TEXT, track INT, text TEXT, machine_text TEXT, "
                        "reviewer TEXT, note TEXT, updated TEXT, PRIMARY KEY (event_id, track))")
        self.db.commit()

    def set_plate(self, event_id: str, track: int, text: str, machine_text: str = "", reviewer: str = "",
                  note: str = "") -> dict:
        from .plates import normalize
        t = normalize(text)
        if text and not t:
            raise ValueError("a plate reading needs letters or digits")
        if len(t) > 15:
            raise ValueError("a plate has at most 15 characters")
        row = (event_id, int(track), t, machine_text or "", (reviewer or "")[:100], (note or "")[:1000],
               dt.datetime.now().astimezone().isoformat(timespec="seconds"))
        with self.db:
            self.db.execute("INSERT INTO plate_readings VALUES (?,?,?,?,?,?,?) ON CONFLICT(event_id, track) DO UPDATE SET "
                            "text=excluded.text, machine_text=excluded.machine_text, reviewer=excluded.reviewer, "
                            "note=excluded.note, updated=excluded.updated", row)
        return dict(zip(("event_id", "track", "text", "machine_text", "reviewer", "note", "updated"), row))

    def clear_plate(self, event_id: str, track: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM plate_readings WHERE event_id=? AND track=?", (event_id, int(track)))

    def plates(self) -> Dict[tuple, dict]:
        return {(r[0], int(r[1])): {"text": r[2], "machine_text": r[3], "reviewer": r[4], "note": r[5], "updated": r[6]}
                for r in self.db.execute("SELECT event_id, track, text, machine_text, reviewer, note, updated "
                                         "FROM plate_readings")}

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


def plate_readings(output_dir: str) -> Dict[tuple, dict]:
    """{(event_id, track): examiner's reading} of an output folder ({} when none)."""
    if not os.path.exists(os.path.join(output_dir, "reviews.db")):
        return {}
    r = Reviews(output_dir)
    try:
        return r.plates()
    finally:
        r.close()


def effective_plate(row: dict, readings: Dict[tuple, dict]) -> dict:
    """A plates row with the examiner's reading applied: ``text`` is what counts (the examiner's when
    there is one, else the confident machine read, else ''), ``machine_text`` the reported machine
    read, ``ocr_text`` the machine's best guess even when not confident, ``reading`` the examiner's."""
    out = dict(row)
    out["machine_text"] = row.get("text") or ""
    rd = readings.get((row.get("event_id"), int(row.get("track") or 0)))
    out["reading"] = rd
    if rd is not None:
        out["text"] = rd["text"]
    return out

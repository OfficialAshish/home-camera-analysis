"""Append-only, hash-chained audit log (JSON lines). Each entry stores the SHA-256 of the
previous entry, so editing, deleting or reordering any line breaks ``verify()`` from that
line on – the log is tamper-evident (not tamper-proof: keep copies of exported manifests)."""
from __future__ import annotations

import datetime as dt
import getpass
import hashlib
import json
import os
import threading
from typing import Tuple

GENESIS = "0" * 64
_LOCK = threading.Lock()


def _digest(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def _last_hash(self) -> str:
        if not os.path.exists(self.path):
            return GENESIS
        last = GENESIS
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last = json.loads(line).get("hash", last)
        return last

    def append(self, action: str, **details) -> dict:
        try:
            user = getpass.getuser()
        except Exception:
            user = ""
        with _LOCK:
            entry = {"ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "user": user,
                     "action": action, "details": details, "prev": self._last_hash()}
            entry["hash"] = _digest(entry)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
        return entry

    def entries(self, limit: int = 500) -> list:
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as fh:
            rows = [json.loads(x) for x in fh if x.strip()]
        return rows[-limit:]

    def verify(self) -> Tuple[bool, int, int]:
        """(intact, entries checked, first broken line number or 0)."""
        if not os.path.exists(self.path):
            return True, 0, 0
        prev, n = GENESIS, 0
        with open(self.path, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    return False, n, i
                if e.get("prev") != prev or _digest(e) != e.get("hash"):
                    return False, n, i
                prev = e["hash"]
                n += 1
        return True, n, 0

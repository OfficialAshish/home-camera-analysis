"""CPU and RAM protection for 8 GB laptops without a GPU.

``governor()`` returns the process-wide :class:`MemoryGovernor`. Code that processes items in
batches (SigLIP keyframe encoding, appearance vectors, multi-frame stacking) asks it for the
batch size: below ``high`` (85 % of RAM in use) the nominal size is used; above it the size is
halved for every 5 percentage points over the limit (down to 1), garbage is collected, and above
``critical`` (93 %) the caller is made to wait (up to ``max_wait`` seconds) for memory to be
released before continuing. Every throttling decision is counted and logged once per minute so
a run's log shows when and how often it happened.

Memory readings come from ``psutil.virtual_memory().percent`` (system-wide, the figure Task
Manager shows). Tests inject a reader.
"""
from __future__ import annotations

import gc
import logging
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("cctv")


def _psutil_percent() -> float:
    try:
        import psutil
        return float(psutil.virtual_memory().percent)
    except Exception:                       # no psutil or no permission: never throttle on a guess
        return 0.0


class MemoryGovernor:
    def __init__(self, high: float = 85.0, critical: float = 93.0, max_wait: float = 30.0,
                 reader: Optional[Callable[[], float]] = None, sleep: Callable[[float], None] = time.sleep):
        self.high, self.critical, self.max_wait = high, critical, max_wait
        self.reader = reader or _psutil_percent
        self.sleep = sleep
        self.throttled = 0
        self.waited = 0.0
        self._last_log = 0.0
        self._lock = threading.Lock()

    def percent(self) -> float:
        return self.reader()

    def batch_size(self, nominal: int) -> int:
        """Batch size to use now (nominal when memory is fine)."""
        nominal = max(1, int(nominal))
        used = self.percent()
        if used < self.high:
            return nominal
        steps = 1 + int((used - self.high) // 5)
        size = max(1, nominal >> steps)
        gc.collect()
        with self._lock:
            self.throttled += 1
            now = time.monotonic()
            if now - self._last_log > 60:
                self._last_log = now
                log.warning("memory %.0f%% in use (limit %.0f%%): batch %d -> %d (throttled %d time(s))",
                            used, self.high, nominal, size, self.throttled)
        if used >= self.critical:
            self.wait_for_memory()
        return size

    def wait_for_memory(self) -> bool:
        """Block while memory is critical; True if it dropped below the limit in time."""
        t0 = time.monotonic()
        while self.percent() >= self.critical:
            if time.monotonic() - t0 >= self.max_wait:
                log.warning("memory still above %.0f%% after %.0f s – continuing with batch size 1",
                            self.critical, self.max_wait)
                return False
            gc.collect()
            self.sleep(1.0)
            with self._lock:
                self.waited += 1.0
        return True

    def status(self) -> dict:
        return {"percent": round(self.percent(), 1), "high": self.high, "critical": self.critical,
                "throttled": self.throttled, "waited_seconds": self.waited}


_GOV = {"g": MemoryGovernor()}


def governor() -> MemoryGovernor:
    return _GOV["g"]


def set_governor(g: MemoryGovernor) -> MemoryGovernor:
    _GOV["g"] = g
    return g

"""Background jobs for the app: analysis runs, folder scans, benchmarks, exports, model downloads.

* at most one *analysis* job at a time (a second start returns the running one's id → HTTP 409);
* a client-supplied ``request_id`` makes job creation idempotent (double-clicks, retries);
* each job captures only its own thread's log lines;
* cancellation is cooperative (the analyzer stops between videos/events and resumes next run).
"""
from __future__ import annotations

import collections
import datetime as dt
import logging
import threading
import time
import traceback
import uuid
from typing import Callable, Dict, Optional

log = logging.getLogger("cctv")


class _ThreadLog(logging.Handler):
    def __init__(self, thread_id: int, buf: collections.deque):
        super().__init__(logging.INFO)
        self.tid, self.buf = thread_id, buf
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        if record.thread == self.tid:
            try:
                self.buf.append(self.format(record))
            except Exception:
                pass


class Job:
    def __init__(self, kind: str, request_id: str = ""):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.request_id = request_id
        self.status = "queued"
        self.progress: Dict = {}
        self.result = None
        self.error = ""
        self.created = time.time()
        self.started: Optional[float] = None
        self.finished: Optional[float] = None
        self.cancel = threading.Event()
        self.log: collections.deque = collections.deque(maxlen=400)
        self.thread: Optional[threading.Thread] = None

    def update(self, ev: dict):
        p = dict(self.progress)
        p.update(ev)
        stage = ev.get("stage")
        if stage == "video":
            p.pop("event_index", None)
            p.pop("events", None)
        p["percent"] = self._percent(p)
        if self.started and p["percent"] and 0 < p["percent"] < 100 and p.get("stage") in ("video", "event", "motion"):
            el = time.time() - self.started
            p["eta_seconds"] = int(el / p["percent"] * (100 - p["percent"]))
        self.progress = p

    @staticmethod
    def _percent(p) -> float:
        st = p.get("stage")
        if st == "planning" and p.get("total"):
            return round(5.0 * p.get("index", 0) / p["total"], 1)
        if st in ("video", "motion", "event") and p.get("total"):
            frac = 0.0
            if p.get("events"):
                frac = (p.get("event_index", 0) - 1) / p["events"]
            return round(5 + 85 * (p.get("index", 1) - 1 + max(0.0, frac)) / p["total"], 1)
        if st == "reports":
            return 92.0
        if st == "finished":
            return 100.0
        return float(p.get("percent", 0) or 0)

    def public(self, log_lines: int = 0) -> dict:
        d = {"id": self.id, "kind": self.kind, "status": self.status, "progress": self.progress,
             "error": self.error, "result": self.result,
             "created": dt.datetime.fromtimestamp(self.created).isoformat(timespec="seconds"),
             "elapsed_seconds": int((self.finished or time.time()) - self.started) if self.started else 0}
        if log_lines:
            d["log"] = list(self.log)[-log_lines:]
        return d


class JobManager:
    def __init__(self):
        self._jobs: Dict[str, Job] = {}
        self._by_request: Dict[str, str] = {}
        self._lock = threading.Lock()

    def start(self, kind: str, fn: Callable[[Job], object], request_id: str = "", exclusive: bool = False):
        """Returns (job, created). With exclusive=True and a job of the same kind running, returns
        (running job, False)."""
        with self._lock:
            if request_id and request_id in self._by_request:
                return self._jobs[self._by_request[request_id]], False
            if exclusive:
                for j in self._jobs.values():
                    if j.kind == kind and j.status in ("queued", "running"):
                        return j, False
            job = Job(kind, request_id)
            self._jobs[job.id] = job
            if request_id:
                self._by_request[request_id] = job.id
        t = threading.Thread(target=self._run, args=(job, fn), name=f"job-{kind}-{job.id}", daemon=True)
        job.thread = t
        t.start()
        return job, True

    def _run(self, job: Job, fn):
        h = _ThreadLog(threading.get_ident(), job.log)
        log.addHandler(h)
        job.status, job.started = "running", time.time()
        try:
            job.result = fn(job)
            job.status = "cancelled" if job.cancel.is_set() else "done"
        except Exception as e:
            job.status, job.error = "failed", str(e)[:500]
            log.error("job %s (%s) failed: %s\n%s", job.id, job.kind, e, traceback.format_exc())
        finally:
            job.finished = time.time()
            log.removeHandler(h)

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def running(self, kind: str) -> Optional[Job]:
        return next((j for j in self._jobs.values() if j.kind == kind and j.status in ("queued", "running")), None)

    def list(self, limit: int = 30) -> list:
        return [j.public() for j in sorted(self._jobs.values(), key=lambda j: -j.created)[:limit]]

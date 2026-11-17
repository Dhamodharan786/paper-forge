"""Background jobs for the UI: one at a time, each with a live log."""
from __future__ import annotations

import threading
import time
import uuid
from typing import Callable

from .cost import CapExceeded


class Busy(RuntimeError):
    pass


class Jobs:
    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._active: str | None = None

    def start(self, kind: str, fn: Callable[[Callable], object]) -> str:
        with self._lock:
            if self._active and self.jobs[self._active]["status"] == "running":
                raise Busy(f"A {self.jobs[self._active]['kind']} job is still running. Wait for it to finish.")
            jid = uuid.uuid4().hex[:8]
            job = {"id": jid, "kind": kind, "status": "running", "lines": [], "result": None, "error": None,
                   "startedAt": time.time()}
            self.jobs[jid] = job
            self._active = jid

        def log(*parts) -> None:
            job["lines"].append(" ".join(str(p) for p in parts))

        def run() -> None:
            try:
                job["result"] = fn(log)
                job["status"] = "done"
            except CapExceeded as e:
                job["error"], job["status"] = str(e), "error"
            except Exception as e:  # noqa: BLE001  show any failure in the UI instead of dying silently
                job["error"], job["status"] = f"{type(e).__name__}: {e}", "error"
            job["finishedAt"] = time.time()

        threading.Thread(target=run, daemon=True).start()
        return jid

    def get(self, jid: str, since: int = 0) -> dict:
        job = self.jobs.get(jid)
        if not job:
            raise KeyError(f"no job {jid}")
        return {"id": jid, "kind": job["kind"], "status": job["status"], "total": len(job["lines"]),
                "lines": job["lines"][since:], "result": job["result"], "error": job["error"]}

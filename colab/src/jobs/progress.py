from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

class JobCancelled(RuntimeError):
    pass

@dataclass
class JobProgress:
    download: float = 0
    extract: float = 0
    upload: float = 0

@dataclass
class JobState:
    job_id: str
    payload: dict[str, Any]
    status: str = "pending"
    step: str = "pending"
    progress: JobProgress = field(default_factory=JobProgress)
    current_file: str = ""
    bytes_done: int = 0
    bytes_total: int = 0
    speed: float = 0
    logs: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancel: bool = False
    _tick_at: float = field(default_factory=time.time)
    _tick_bytes: int = 0
    _phase: str = ""
    _phase_done: int = 0
    _phase_total: int = 0
    _phase_total_keys: set[tuple[str, str]] = field(default_factory=set)

    def log(self, message: str) -> None:
        self.logs.append(message)
        self.logs = self.logs[-200:]
        self.updated_at = time.time()

    def set(self, *, status: str | None = None, step: str | None = None, current_file: str | None = None) -> None:
        if status:
            self.status = status
        if step:
            self.step = step
        if current_file is not None:
            self.current_file = current_file
        self.updated_at = time.time()

    def add_bytes(self, n: int, total: int = 0, phase: str = "download", total_key: str | None = None) -> None:
        if phase != self._phase:
            self._phase = phase
            self._phase_done = 0
            self._phase_total = 0
            self._phase_total_keys.clear()
        if total_key and total:
            key = (phase, total_key)
            if key not in self._phase_total_keys:
                self._phase_total += total
                self._phase_total_keys.add(key)
        elif total and total != self._phase_total:
            self._phase_done = 0
            self._phase_total = total
        self.bytes_done += n
        self._phase_done += n
        if self._phase_total:
            self.bytes_total = self._phase_total
            setattr(self.progress, phase, min(100, self._phase_done / self._phase_total * 100))
        now = time.time()
        elapsed = now - self._tick_at
        if elapsed >= 1:
            self.speed = (self.bytes_done - self._tick_bytes) / elapsed
            self._tick_at = now
            self._tick_bytes = self.bytes_done
        self.updated_at = now

    def check_cancelled(self) -> None:
        if self.cancel:
            raise JobCancelled("JOB_CANCELLED")

    def view(self) -> dict[str, Any]:
        phases = ["download", "extract", "upload"] if (self.payload.get("options") or {}).get("extract") else ["download", "upload"]
        return {
            "jobId": self.job_id,
            "status": self.status,
            "step": self.step,
            "progress": self.progress.__dict__,
            "phases": phases,
            "currentFile": self.current_file,
            "bytesDone": self._phase_done,
            "bytesTotal": self._phase_total or self.bytes_total,
            "bytesCumulative": self.bytes_done,
            "speed": self.speed,
            "logs": self.logs[-50:],
            "error": self.error,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

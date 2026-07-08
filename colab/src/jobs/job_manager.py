from __future__ import annotations

import asyncio
import uuid
from typing import Any

from ..config import MAX_JOBS
from .progress import JobState
from .transfer_job import run_transfer

class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, JobState] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.sem = asyncio.Semaphore(MAX_JOBS)

    async def _run(self, job: JobState) -> None:
        async with self.sem:
            await run_transfer(job)

    def start(self, payload: dict[str, Any]) -> JobState:
        job = JobState(job_id=str(payload.get("jobId") or uuid.uuid4()), payload=payload)
        self.jobs[job.job_id] = job
        self.tasks[job.job_id] = asyncio.create_task(self._run(job))
        return job

    def get(self, job_id: str) -> JobState | None:
        return self.jobs.get(job_id)

    def list(self) -> list[dict[str, Any]]:
        return [j.view() for j in sorted(self.jobs.values(), key=lambda x: x.created_at, reverse=True)]

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False
        job.cancel = True
        return True

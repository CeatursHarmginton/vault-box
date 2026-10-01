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
        self._load_existing_jobs()

    def _load_existing_jobs(self) -> None:
        try:
            from ..config import JOBS_DIR
            if not JOBS_DIR.exists():
                return
            for job_dir in sorted(JOBS_DIR.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
                if job_dir.is_dir():
                    state_file = job_dir / "job_state.json"
                    if state_file.exists():
                        job = JobState.load_from_state_file(state_file)
                        if job:
                            self.jobs[job.job_id] = job
                            job.save_state()
            if self.jobs:
                print(f"[*] Loaded {len(self.jobs)} existing transfer job(s) from local disk.", flush=True)
        except Exception as exc:
            print(f"[!] Error loading existing jobs: {exc}", flush=True)

    async def _run(self, job: JobState) -> None:
        async with self.sem:
            def _thread_worker():
                import sys
                if sys.platform == "win32" and hasattr(asyncio, "ProactorEventLoop"):
                    loop = asyncio.ProactorEventLoop()
                else:
                    loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(run_transfer(job))
                finally:
                    try:
                        loop.run_until_complete(loop.shutdown_asyncgens())
                    except Exception:
                        pass
                    loop.close()

            await asyncio.to_thread(_thread_worker)

    def start(self, payload: dict[str, Any]) -> JobState:
        job_id = str(payload.get("jobId") or uuid.uuid4())
        existing = self.jobs.get(job_id)
        if existing and existing.status in {"pending", "running", "downloading", "uploading", "extracting"}:
            if job_id in self.tasks and not self.tasks[job_id].done():
                return existing
        job = JobState(job_id=job_id, payload=payload)
        job.save_state()
        self.jobs[job.job_id] = job
        self.tasks[job.job_id] = asyncio.create_task(self._run(job))
        return job

    def resume(self, job_id: str, new_payload: dict[str, Any] | None = None) -> JobState:
        import time
        job = self.jobs.get(job_id)
        if not job:
            from ..config import JOBS_DIR
            state_file = JOBS_DIR / job_id / "job_state.json"
            if state_file.exists():
                job = JobState.load_from_state_file(state_file)
                if job:
                    self.jobs[job.job_id] = job
        if not job:
            if new_payload:
                return self.start(new_payload)
            raise KeyError(f"Job {job_id} not found to resume")

        if job.status in {"pending", "running", "downloading", "uploading", "extracting"}:
            if job_id in self.tasks and not self.tasks[job_id].done():
                return job

        if new_payload:
            job.payload.update(new_payload)
            if new_payload.get("source"):
                job.payload["source"] = new_payload["source"]
            if new_payload.get("target"):
                job.payload["target"] = new_payload["target"]
            if new_payload.get("options"):
                job.payload.setdefault("options", {}).update(new_payload["options"])

        job.cancel = False
        job.error = None
        job.status = "pending"
        job.step = "pending"
        job.confirm_action = None
        job.confirm_event.clear()
        job.failed_items = []
        job.files_failed = 0
        job.updated_at = time.time()
        job.log(f"[Resume] Transfer resumed by user. Continuing transfer...")
        job.save_state()

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
        job.status = "cancelled"
        job.step = "cancelled"
        job.save_state()
        # Wake up any thread waiting on confirmation so it can check the cancel flag
        job.confirm_event.set()
        return True

    def delete(self, job_id: str) -> bool:
        job = self.jobs.pop(job_id, None)
        task = self.tasks.pop(job_id, None)
        if task and not task.done():
            task.cancel()
        from ..utils.temp_storage import cleanup_job
        cleanup_job(job_id)
        return True

    def active_job_count(self) -> int:
        """Returns the number of transfer jobs currently active or pending."""
        active_statuses = {"pending", "running", "downloading", "uploading", "extracting", "optimizing", "waiting_confirmation"}
        return sum(1 for job in self.jobs.values() if job.status in active_statuses)

    def has_active_jobs(self) -> bool:
        """Returns True if any jobs are currently active or running."""
        return self.active_job_count() > 0

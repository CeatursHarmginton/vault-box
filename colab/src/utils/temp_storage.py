from __future__ import annotations

import shutil
from pathlib import Path

from ..config import JOBS_DIR

def job_dirs(job_id: str) -> dict[str, Path]:
    base = JOBS_DIR / job_id
    dirs = {k: base / k for k in ("input", "output", "work")}
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs

def cleanup_job(job_id: str) -> None:
    shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)

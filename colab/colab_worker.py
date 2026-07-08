from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel


API_TOKEN = os.environ.get("PIKPAK_COLAB_TOKEN", "change-me")
DEFAULT_DRIVE_DIR = os.environ.get("PIKPAK_DRIVE_DIR", "/content/drive/MyDrive/PikPak_Downloads")
CHUNK_SIZE = 1024 * 1024

app = FastAPI(title="PikPak Manager Colab Worker")


class JobRequest(BaseModel):
    url: str
    name: str
    size: int = 0
    relative_dir: str = ""
    drive_output_dir: str = DEFAULT_DRIVE_DIR


@dataclass
class Job:
    id: str
    url: str
    name: str
    relative_dir: str
    drive_output_dir: str
    status: str = "queued"
    progress: float = 0
    downloaded: int = 0
    total: int = 0
    expected_size: int = 0
    speed: float = 0
    message: str = "Queued"
    output_path: str | None = None
    created_at: float = 0
    updated_at: float = 0

    def view(self):
        return asdict(self)


jobs: dict[str, Job] = {}


def check_token(x_api_token: str | None) -> None:
    if API_TOKEN and API_TOKEN != "change-me" and x_api_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid API token")


def safe_name(name: str) -> str:
    cleaned = "".join("_" if ch in '<>:"/\\|?*' else ch for ch in name).strip()
    return cleaned or "pikpak-download"


@app.get("/health")
async def health():
    return {"ok": True, "jobs": len(jobs)}


@app.post("/jobs")
async def create_job(payload: JobRequest, x_api_token: str | None = Header(default=None)):
    check_token(x_api_token)
    now = time.time()
    job = Job(
        id=str(uuid.uuid4()),
        url=payload.url,
        name=safe_name(payload.name),
        relative_dir=payload.relative_dir.strip("/\\"),
        drive_output_dir=payload.drive_output_dir or DEFAULT_DRIVE_DIR,
        expected_size=max(0, int(payload.size or 0)),
        created_at=now,
        updated_at=now,
    )
    jobs[job.id] = job
    asyncio.create_task(run_job(job))
    return job.view()


@app.get("/jobs")
async def list_jobs(x_api_token: str | None = Header(default=None)):
    check_token(x_api_token)
    return {"jobs": [job.view() for job in jobs.values()]}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str, x_api_token: str | None = Header(default=None)):
    check_token(x_api_token)
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return jobs[job_id].view()


async def run_job(job: Job) -> None:
    job.status = "running"
    job.message = "Downloading to Google Drive"
    job.updated_at = time.time()
    out_dir = Path(job.drive_output_dir) / job.relative_dir if job.relative_dir else Path(job.drive_output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = out_dir / job.name
    part_path = Path(str(final_path) + ".part")
    try:
        if shutil.which("aria2c"):
            ok = await asyncio.to_thread(download_with_aria2, job, part_path)
            if not ok:
                await download_with_httpx(job, part_path)
        else:
            await download_with_httpx(job, part_path)
        final_size = part_path.stat().st_size if part_path.exists() else 0
        expected = int(job.expected_size or job.total or 0)
        if expected and final_size != expected:
            raise RuntimeError(f"Downloaded size mismatch: got {final_size}, expected {expected}")
        part_path.replace(final_path)
        job.status = "completed"
        job.progress = 1
        job.output_path = str(final_path)
        job.message = "Completed"
        job.updated_at = time.time()
    except Exception as exc:
        job.status = "failed"
        job.message = str(exc)
        job.updated_at = time.time()


def download_with_aria2(job: Job, part_path: Path) -> bool:
    cmd = [
        "aria2c",
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--continue=true",
        "--max-connection-per-server=16",
        "--split=16",
        "--min-split-size=1M",
        "--summary-interval=1",
        "--max-tries=8",
        "--retry-wait=2",
        "--dir",
        str(part_path.parent),
        "--out",
        part_path.name,
        job.url,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.stdout is not None
    for line in proc.stdout:
        job.message = line.strip()[-160:] or "Downloading with aria2c"
        if job.expected_size:
            job.total = job.expected_size
        job.updated_at = time.time()
    if proc.wait() != 0:
        return False
    size = part_path.stat().st_size if part_path.exists() else 0
    if job.expected_size and size != job.expected_size:
        raise RuntimeError(f"aria2c size mismatch: got {size}, expected {job.expected_size}")
    job.downloaded = size
    job.total = job.expected_size or job.total or size
    job.progress = min(size / job.total, 1.0) if job.total else 0
    return True


def content_range(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    try:
        unit, rest = value.strip().split(" ", 1)
        bounds, total = rest.split("/", 1)
        start, end = bounds.split("-", 1)
        if unit.lower() != "bytes" or total == "*":
            return None
        return int(start), int(end), int(total)
    except (ValueError, AttributeError):
        return None


async def download_with_httpx(job: Job, part_path: Path) -> None:
    downloaded = part_path.stat().st_size if part_path.exists() else 0
    if job.expected_size and downloaded > job.expected_size:
        part_path.unlink(missing_ok=True)
        downloaded = 0
    headers = {"Range": f"bytes={downloaded}-"} if downloaded else {}
    last_tick = time.time()
    last_bytes = downloaded
    async with httpx.AsyncClient(follow_redirects=True, timeout=None) as client:
        async with client.stream("GET", job.url, headers=headers) as response:
            response.raise_for_status()
            content_length = int(response.headers.get("content-length") or 0)
            total = downloaded + content_length if response.status_code == 206 else content_length
            parsed_range = content_range(response.headers.get("content-range")) if response.status_code == 206 else None
            if downloaded and response.status_code != 206:
                part_path.unlink(missing_ok=True)
                downloaded = 0
                last_bytes = 0
                total = content_length
            elif downloaded and parsed_range:
                start, _end, ranged_total = parsed_range
                if start != downloaded:
                    raise RuntimeError(f"Resume range mismatch: got start {start}, expected {downloaded}")
                total = ranged_total
            elif downloaded:
                raise RuntimeError("Resume response missing Content-Range")
            if job.expected_size and total and total != job.expected_size:
                raise RuntimeError(f"Remote size mismatch: got {total}, expected {job.expected_size}")
            mode = "ab" if response.status_code == 206 and part_path.exists() else "wb"
            if mode == "wb":
                downloaded = 0
            job.total = job.expected_size or total
            with part_path.open(mode) as fh:
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    fh.write(chunk)
                    downloaded += len(chunk)
                    now = time.time()
                    job.downloaded = downloaded
                    job.progress = min(downloaded / job.total, 1.0) if job.total else 0
                    if now - last_tick >= 0.75:
                        job.speed = (downloaded - last_bytes) / max(now - last_tick, 0.001)
                        job.updated_at = now
                        last_tick = now
                        last_bytes = downloaded

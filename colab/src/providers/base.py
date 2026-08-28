from __future__ import annotations

import asyncio
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import httpx

from ..config import CHUNK_SIZE, FOLDER_DOWNLOAD_CONCURRENCY, FOLDER_UPLOAD_CONCURRENCY
from ..jobs.progress import JobState
from ..utils.image_optimizer import VIDEO_EXTENSIONS

class ProviderFailure(RuntimeError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

class BaseProvider(ABC):
    name = "base"

    @abstractmethod
    async def validate_credentials(self, credentials: dict[str, Any]) -> dict[str, Any]: ...

    async def list_files(self, credentials: dict[str, Any], path_or_id: str) -> dict[str, Any]:
        raise ProviderFailure("NOT_SUPPORTED", f"{self.name} list not supported")

    @abstractmethod
    async def download_file(self, credentials: dict[str, Any], file_ref: dict[str, Any], local_path: Path, progress: JobState) -> Path: ...

    async def download_folder(self, credentials: dict[str, Any], folder_ref: dict[str, Any], local_dir: Path, progress: JobState, _sem: asyncio.Semaphore | None = None) -> list[Path]:
        listing = await self.list_files(credentials, str(folder_ref.get("id") or folder_ref.get("path") or "/"))
        items = listing.get("items") or listing.get("files") or []
        progress.log(f"{self.name} list folder {folder_ref.get('name') or folder_ref.get('path') or folder_ref.get('id')}: {len(items)} item(s)")
        for item in items:
            if not (item.get("type") == "folder" or item.get("is_folder") or item.get("isdir")) and not _skip_optimize_video(item, progress):
                progress.files_to_download += 1
        sem = _sem or asyncio.Semaphore(max(1, FOLDER_DOWNLOAD_CONCURRENCY))

        async def save(item: dict[str, Any]) -> list[Path]:
            progress.check_cancelled()
            if item.get("type") == "folder" or item.get("is_folder") or item.get("isdir"):
                sub = local_dir / _safe_name(item.get("name") or item.get("server_filename") or "folder")
                sub.mkdir(parents=True, exist_ok=True)
                return await self.download_folder(credentials, item, sub, progress, sem)
            if _skip_optimize_video(item, progress):
                progress.files_skipped += 1
                progress.log(f"[SKIP] Video ignored by image optimizer: {item.get('name') or item.get('server_filename') or item.get('id') or 'file'}")
                return []
            async with sem:
                return [await self.download_file(credentials, item, local_dir / _safe_name(item.get("name") or item.get("server_filename") or item.get("id") or "file"), progress)]

        return [path for batch in await asyncio.gather(*(save(item) for item in items)) for path in batch]

    @abstractmethod
    async def upload_file(self, credentials: dict[str, Any], local_path: Path, target_ref: dict[str, Any], progress: JobState) -> dict[str, Any]: ...

    async def upload_folder(self, credentials: dict[str, Any], local_dir: Path, target_ref: dict[str, Any], progress: JobState) -> dict[str, Any]:
        paths = [path for path in sorted(local_dir.rglob("*")) if path.is_file()]
        sem = asyncio.Semaphore(max(1, FOLDER_UPLOAD_CONCURRENCY))
        root_target = target_ref

        progress.files_to_upload = len(paths)
        progress.files_uploaded = 0
        progress.files_skipped = 0
        if len(paths) > 1:
            progress.log(f"[UPLOAD] Xử lý song song: {min(len(paths), max(1, FOLDER_UPLOAD_CONCURRENCY))} worker(s)")

        async def upload(path: Path) -> dict[str, Any] | None:
            progress.check_cancelled()
            async with sem:
                rel = "/".join(part for part in (str(root_target.get("relative_path") or "").strip("/"), path.relative_to(local_dir).as_posix()) if part)
                try:
                    res = await self.upload_file(credentials, path, {**root_target, "relative_path": rel}, progress)
                    progress.files_uploaded += 1
                    progress.log(f"[{progress.files_uploaded + progress.files_skipped}/{progress.files_to_upload}] Uploaded: {path.name}")
                    return res
                except ProviderFailure as exc:
                    if "duplicated" in exc.message.lower() or "repeated" in exc.message.lower():
                        progress.files_skipped += 1
                        progress.log(f"[{progress.files_uploaded + progress.files_skipped}/{progress.files_to_upload}] Skipped: {path.name}")
                        return None
                    raise

        results = await asyncio.gather(*(upload(path) for path in paths))
        uploaded = [item for item in results if item is not None]
        skipped = len(results) - len(uploaded)
        return {"ok": True, "uploaded": len(uploaded), "skipped": skipped, "items": uploaded}

async def stream_download(url: str, dest: Path, progress: JobState, *, headers: dict[str, str] | None = None, phase: str = "download") -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    expected = 0
    last_exc: Exception | None = None
    for attempt in range(5):
        done = part.stat().st_size if part.exists() else 0
        attempt_start = done
        req_headers = dict(headers or {})
        if done:
            req_headers["Range"] = f"bytes={done}-"
        try:
            async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
                async with client.stream("GET", url, headers=req_headers) as resp:
                    if resp.status_code in (401, 403):
                        raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", "Provider rejected download credentials")
                    resp.raise_for_status()
                    if done and resp.status_code != 206:
                        part.unlink(missing_ok=True)
                        done = 0
                    total = done + int(resp.headers.get("content-length") or 0)
                    expected = total or expected
                    with part.open("ab" if done else "wb") as fh:
                        async for chunk in resp.aiter_bytes(CHUNK_SIZE):
                            progress.check_cancelled()
                            fh.write(chunk)
                            done += len(chunk)
                            progress.add_bytes(len(chunk), total, phase, str(dest))
            if not expected or part.stat().st_size >= expected:
                break
            raise RuntimeError(f"Download incomplete: got {part.stat().st_size} bytes, expected {expected}")
        except ProviderFailure:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt == 4:
                raise
            progress.log(f"[RETRY] Download interrupted, resuming: {dest.name}")
            if not part.exists() or part.stat().st_size <= attempt_start:
                await asyncio.sleep(min(2 ** attempt, 8))
    if expected and part.stat().st_size < expected and last_exc:
        raise last_exc
    part.replace(dest)
    progress.files_downloaded += 1
    progress.log(f"[{progress.files_downloaded}/{progress.files_to_download}] Downloaded: {dest.name}")
    return dest

def safe_name(name: str) -> str:
    clean = "".join(c for c in str(name or "file") if c not in '<>:"/\\|?*').strip().strip(".")
    return clean or "file"

_safe_name = safe_name

def _skip_optimize_video(item: dict[str, Any], progress: JobState) -> bool:
    if not (progress.payload.get("options") or {}).get("optimize_image"):
        return False
    name = str(item.get("name") or item.get("server_filename") or item.get("path") or item.get("id") or "")
    return Path(name).suffix.lower() in VIDEO_EXTENSIONS


def copy_tree_files(src: Path, dst: Path) -> list[Path]:
    dst.mkdir(parents=True, exist_ok=True)
    out = []
    for path in src.rglob("*"):
        if path.is_file():
            target = dst / path.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            out.append(target)
    return out

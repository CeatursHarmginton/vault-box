from __future__ import annotations

import asyncio
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import httpx

from ..config import CHUNK_SIZE, FOLDER_DOWNLOAD_CONCURRENCY, FOLDER_UPLOAD_CONCURRENCY
from ..jobs.progress import JobState

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
            if not (item.get("type") == "folder" or item.get("is_folder") or item.get("isdir")):
                progress.files_to_download += 1
        sem = _sem or asyncio.Semaphore(max(1, FOLDER_DOWNLOAD_CONCURRENCY))

        async def save(item: dict[str, Any]) -> list[Path]:
            progress.check_cancelled()
            if item.get("type") == "folder" or item.get("is_folder") or item.get("isdir"):
                sub = local_dir / _safe_name(item.get("name") or item.get("server_filename") or "folder")
                sub.mkdir(parents=True, exist_ok=True)
                return await self.download_folder(credentials, item, sub, progress, sem)
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
    done = part.stat().st_size if part.exists() else 0
    req_headers = dict(headers or {})
    if done:
        req_headers["Range"] = f"bytes={done}-"
    async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
        async with client.stream("GET", url, headers=req_headers) as resp:
            if resp.status_code in (401, 403):
                raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", "Provider rejected download credentials")
            resp.raise_for_status()
            total = done + int(resp.headers.get("content-length") or 0)
            mode = "ab" if done and resp.status_code == 206 else "wb"
            if mode == "wb":
                done = 0
            with part.open(mode + "") as fh:
                async for chunk in resp.aiter_bytes(CHUNK_SIZE):
                    progress.check_cancelled()
                    fh.write(chunk)
                    done += len(chunk)
                    progress.add_bytes(len(chunk), total, phase, str(dest))
    part.replace(dest)
    progress.files_downloaded += 1
    progress.log(f"[{progress.files_downloaded}/{progress.files_to_download}] Downloaded: {dest.name}")
    return dest

def safe_name(name: str) -> str:
    clean = "".join(c for c in str(name or "file") if c not in '<>:"/\\|?*').strip().strip(".")
    return clean or "file"

_safe_name = safe_name


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

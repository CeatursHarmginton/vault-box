from __future__ import annotations

import json
import mimetypes
import os
import shutil
import time
import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from .base import BaseProvider, ProviderFailure, safe_name, stream_download
from ..jobs.progress import JobState

DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
FIELDS = "id,name,mimeType,size,parents,webContentLink,webViewLink"
CHUNK = 8 * 1024 * 1024
DRIVE_MOUNT = Path(os.environ.get("COLAB_DRIVE_MOUNT", "/content/drive/MyDrive"))

class DriveProvider(BaseProvider):
    name = "drive"

    def _mounted(self) -> bool:
        return DRIVE_MOUNT.exists() and DRIVE_MOUNT.is_dir()

    def _mount_path(self, ref: dict[str, Any], default: str = "/") -> Path:
        raw = str(ref.get("path") or ref.get("id") or default).replace("\\", "/")
        if raw in {"", "root"}:
            raw = "/"
        clean = raw.lstrip("/")
        path = (DRIVE_MOUNT / clean).resolve()
        root = DRIVE_MOUNT.resolve()
        if path != root and root not in path.parents:
            raise ProviderFailure("TARGET_FOLDER_NOT_FOUND", "Drive mount path escapes MyDrive")
        return path

    def _use_mount(self, credentials: dict[str, Any]) -> bool:
        return bool(credentials.get("mount"))

    def _require_mount(self) -> None:
        if not self._mounted():
            raise ProviderFailure("DRIVE_NOT_MOUNTED", "Mount Google Drive in Colab first")

    def _token(self, c: dict[str, Any]) -> str:
        token = c.get("access_token") or c.get("token") or c.get("web_access_token")
        if not token:
            raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", "Drive access token missing")
        return str(token)

    def _headers(self, c: dict[str, Any], extra: dict[str, str] | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token(c)}", **(extra or {})}

    async def _request(self, c: dict[str, Any], method: str, url: str, **kw: Any) -> httpx.Response:
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
            resp = await client.request(method, url, headers=self._headers(c, kw.pop("headers", None)), **kw)
        if resp.status_code == 401:
            raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", "Drive token expired or revoked")
        if resp.status_code >= 400:
            raise ProviderFailure("UPLOAD_FAILED" if method != "GET" else "DOWNLOAD_FAILED", resp.text[:500], {"status": resp.status_code})
        return resp

    async def validate_credentials(self, credentials: dict[str, Any]) -> dict[str, Any]:
        if self._use_mount(credentials):
            return {"ok": self._mounted(), "mounted": self._mounted(), "mountPath": str(DRIVE_MOUNT)}
        resp = await self._request(credentials, "GET", f"{DRIVE_API}/about", params={"fields": "user"})
        return {"ok": True, "account": resp.json().get("user") or {}}

    async def list_files(self, credentials: dict[str, Any], path_or_id: str) -> dict[str, Any]:
        if self._use_mount(credentials):
            self._require_mount()
            folder = self._mount_path({"path": path_or_id})
            if not folder.is_dir():
                raise ProviderFailure("TARGET_FOLDER_NOT_FOUND", "Drive folder not found")
            return {"items": [{"id": p.relative_to(DRIVE_MOUNT).as_posix(), "path": p.relative_to(DRIVE_MOUNT).as_posix(), "name": p.name, "type": "folder" if p.is_dir() else "file", "size": p.stat().st_size if p.is_file() else 0} for p in sorted(folder.iterdir())]}
        parent = path_or_id or "root"
        resp = await self._request(credentials, "GET", f"{DRIVE_API}/files", params={
            "q": f"'{parent}' in parents and trashed=false",
            "fields": f"files({FIELDS})",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "pageSize": "1000",
        })
        files = resp.json().get("files") or []
        return {"items": [{"id": f["id"], "name": f["name"], "type": "folder" if f.get("mimeType") == FOLDER_MIME else "file", "mimeType": f.get("mimeType"), "size": f.get("size")} for f in files]}

    async def download_file(self, credentials: dict[str, Any], file_ref: dict[str, Any], local_path: Path, progress: JobState) -> Path:
        if self._use_mount(credentials):
            self._require_mount()
            src = self._mount_path(file_ref)
            if not src.is_file():
                raise ProviderFailure("SOURCE_FILE_NOT_FOUND", "Drive mounted file path not found")
            dest = local_path if local_path.suffix else local_path / safe_name(file_ref.get("name") or src.name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            progress.set(step="downloading", current_file=dest.name)
            await asyncio.to_thread(shutil.copy2, src, dest)
            progress.files_downloaded += 1
            progress.add_bytes(src.stat().st_size, src.stat().st_size, "download", str(dest))
            progress.log(f"[{progress.files_downloaded}/{progress.files_to_download}] Downloaded: {dest.name}")
            return dest
        fid = str(file_ref.get("id") or "")
        if not fid:
            raise ProviderFailure("SOURCE_FILE_NOT_FOUND", "Drive file id missing")
        meta = (await self._request(credentials, "GET", f"{DRIVE_API}/files/{fid}", params={"fields": FIELDS, "supportsAllDrives": "true"})).json()
        name = file_ref.get("name") or meta.get("name") or fid
        local_path = local_path if local_path.suffix else local_path / safe_name(name)
        if str(meta.get("mimeType") or "").startswith("application/vnd.google-apps."):
            export_mime, ext = ("application/pdf", ".pdf")
            url = f"{DRIVE_API}/files/{fid}/export?{urlencode({'mimeType': export_mime})}"
            if not local_path.name.endswith(ext):
                local_path = local_path.with_name(local_path.name + ext)
        else:
            url = f"{DRIVE_API}/files/{fid}?alt=media&supportsAllDrives=true"
        progress.set(step="downloading", current_file=local_path.name)
        return await stream_download(url, local_path, progress, headers=self._headers(credentials))

    async def upload_file(self, credentials: dict[str, Any], local_path: Path, target_ref: dict[str, Any], progress: JobState) -> dict[str, Any]:
        if self._use_mount(credentials):
            self._require_mount()
            rel = str(target_ref.get("relative_path") or local_path.name).strip("/")
            dest = (self._mount_path(target_ref) / rel).resolve()
            root = DRIVE_MOUNT.resolve()
            if dest != root and root not in dest.parents:
                raise ProviderFailure("UPLOAD_FAILED", "Drive target path escapes MyDrive")
            dest.parent.mkdir(parents=True, exist_ok=True)
            progress.set(step="uploading", current_file=dest.name)
            await asyncio.to_thread(shutil.copy2, local_path, dest)
            size = local_path.stat().st_size
            progress.add_bytes(size, size, "upload", str(local_path))
            return {"id": dest.relative_to(DRIVE_MOUNT).as_posix(), "name": dest.name, "path": dest.relative_to(DRIVE_MOUNT).as_posix()}
        parent = str(target_ref.get("id") or target_ref.get("path") or "root")
        name = Path(target_ref.get("relative_path") or local_path.name).name
        size = local_path.stat().st_size
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        progress.set(step="uploading", current_file=name)
        init = await self._request(credentials, "POST", f"{DRIVE_UPLOAD_API}/files", params={"uploadType": "resumable", "fields": FIELDS, "supportsAllDrives": "true"}, headers={
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": mime,
            "X-Upload-Content-Length": str(size),
        }, content=json.dumps({"name": name, "parents": [parent]}))
        session = init.headers.get("Location")
        if not session:
            raise ProviderFailure("UPLOAD_FAILED", "Drive resumable session missing")
        offset = 0
        with local_path.open("rb") as fh:
            while offset < size:
                progress.check_cancelled()
                fh.seek(offset)
                data = fh.read(min(CHUNK, size - offset))
                end = offset + len(data) - 1
                async with httpx.AsyncClient(timeout=None) as client:
                    resp = await client.put(session, headers=self._headers(credentials, {"Content-Length": str(len(data)), "Content-Range": f"bytes {offset}-{end}/{size}"}), content=data)
                if resp.status_code in (200, 201):
                    progress.add_bytes(len(data), size, "upload", str(local_path))
                    return resp.json()
                if resp.status_code != 308:
                    raise ProviderFailure("UPLOAD_FAILED", resp.text[:500], {"status": resp.status_code})
                rng = resp.headers.get("Range", "")
                next_offset = int(rng.rsplit("-", 1)[1]) + 1 if "-" in rng else end + 1
                progress.add_bytes(max(0, next_offset - offset), size, "upload", str(local_path))
                offset = next_offset
        raise ProviderFailure("UPLOAD_FAILED", "Drive upload ended early")

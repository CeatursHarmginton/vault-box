from __future__ import annotations

import json
import base64
import hashlib
import mimetypes
import os
import shutil
import time
import asyncio
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlencode

import httpx

from .base import BaseProvider, ProviderFailure, safe_name, stream_download
from ..jobs.progress import JobState

DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
DRIVE_WEB_FILES_API = "https://clients6.google.com/drive/v2internal"
DRIVE_WEB_UPLOAD_API = "https://clients6.google.com/upload/drive/v2internal"
DRIVE_USERCONTENT = "https://drive.usercontent.google.com"
DRIVE_WEB_ORIGIN = "https://drive.google.com"
FOLDER_MIME = "application/vnd.google-apps.folder"
FIELDS = "id,name,mimeType,size,parents,webContentLink,webViewLink"
CHUNK = 8 * 1024 * 1024
WEB_MULTIPART_MAX = 5 * 1024 * 1024
DRIVE_MOUNT = Path(os.environ.get("COLAB_DRIVE_MOUNT", "/content/drive/MyDrive"))

def _q_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "\\'")

def _relative_folder_parts(relative_path: str) -> list[str]:
    rel = str(relative_path or "").replace("\\", "/").strip("/")
    parent = PurePosixPath(rel).parent
    return [safe_name(part) for part in parent.parts if part and part not in (".", "..")]

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

    def _mount_ref(self, ref: dict[str, Any]) -> bool:
        if "path" in ref:
            raw = str(ref.get("path") or "")
            return bool(raw and not raw.startswith("id:"))
        raw = str(ref.get("id") or "")
        return raw.startswith("/") or "/" in raw

    def _require_mount(self) -> None:
        if not self._mounted():
            raise ProviderFailure("DRIVE_NOT_MOUNTED", "Mount Google Drive in Colab first")

    def _token(self, c: dict[str, Any]) -> str:
        token = c.get("access_token") or c.get("token") or c.get("web_access_token")
        if not token:
            raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", "Drive access token missing")
        return str(token)

    def _web_session(self, c: dict[str, Any]) -> bool:
        return self._token(c).lower().startswith("sapisidhash ")

    def _cookie_header(self, c: dict[str, Any]) -> str:
        return "; ".join(f"{k}={v}" for k, v in (c.get("cookies") or {}).items() if k and v)

    def _web_auth(self, c: dict[str, Any]) -> str:
        token = self._token(c)
        cookies = c.get("cookies") or {}
        sapisid = cookies.get("SAPISID") or cookies.get("__Secure-3PAPISID") or cookies.get("__Secure-1PAPISID")
        if token.lower().startswith("sapisidhash ") and sapisid:
            ts = str(int(time.time()))
            return f"SAPISIDHASH {ts}_{hashlib.sha1(f'{ts} {sapisid} {DRIVE_WEB_ORIGIN}'.encode('utf-8')).hexdigest()}"
        return token

    def _web_headers(self, c: dict[str, Any], extra: dict[str, str] | None = None, *, auth: bool = True) -> dict[str, str]:
        headers = {
            "origin": DRIVE_WEB_ORIGIN,
            "referer": DRIVE_WEB_ORIGIN + "/",
            "user-agent": "Mozilla/5.0",
            "x-goog-authuser": str(c.get("authuser") or "0"),
        }
        cookie = self._cookie_header(c)
        if cookie:
            headers["cookie"] = cookie
        headers.update({k: v for k, v in (c.get("auth_headers") or {}).items() if str(k).lower() not in {"authorization", "x-goog-api-key"}})
        if auth:
            headers["Authorization"] = self._web_auth(c)
        if extra:
            headers.update(extra)
        return headers

    def _web_key(self, c: dict[str, Any]) -> str:
        keys = c.get("api_keys") or {}
        return str(keys.get("drivefrontend-pa.clients6.google.com") or keys.get("clients6.google.com") or "")

    def _api_parent(self, ref: Any) -> str:
        raw = ref if isinstance(ref, str) else (ref.get("id") or ref.get("path") or "root")
        return "root" if str(raw or "").strip() in {"", "/"} else str(raw)

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
        if self._web_session(credentials):
            if not self._cookie_header(credentials):
                raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", "Drive web-session cookies missing")
            return {"ok": True, "authMode": "web_session"}
        resp = await self._request(credentials, "GET", f"{DRIVE_API}/about", params={"fields": "user"})
        return {"ok": True, "account": resp.json().get("user") or {}}

    async def list_files(self, credentials: dict[str, Any], path_or_id: str) -> dict[str, Any]:
        if self._use_mount(credentials):
            self._require_mount()
            folder = self._mount_path({"path": path_or_id})
            if not folder.is_dir():
                raise ProviderFailure("TARGET_FOLDER_NOT_FOUND", "Drive folder not found")
            return {"items": [{"id": p.relative_to(DRIVE_MOUNT).as_posix(), "path": p.relative_to(DRIVE_MOUNT).as_posix(), "name": p.name, "type": "folder" if p.is_dir() else "file", "size": p.stat().st_size if p.is_file() else 0} for p in sorted(folder.iterdir())]}
        parent = self._api_parent(path_or_id)
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
        if self._use_mount(credentials) and self._mount_ref(file_ref):
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
        if self._use_mount(credentials) and not (credentials.get("access_token") or credentials.get("token") or credentials.get("web_access_token")):
            raise ProviderFailure("SOURCE_FILE_NOT_FOUND", "Drive source must be a MyDrive path after mounting")
        fid = str(file_ref.get("id") or "")
        if not fid:
            raise ProviderFailure("SOURCE_FILE_NOT_FOUND", "Drive file id missing")
        if self._web_session(credentials):
            info = await self._web_download_info(credentials, fid)
            name = file_ref.get("name") or info.get("name") or fid
            local_path = local_path if local_path.suffix else local_path / safe_name(name)
            progress.set(step="downloading", current_file=local_path.name)
            return await stream_download(info["url"], local_path, progress, headers=self._web_headers(credentials, auth=False))
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
        if self._web_session(credentials):
            return await self._web_upload_file(credentials, local_path, target_ref, progress)
        rel = str(target_ref.get("relative_path") or local_path.name)
        parent = await self._api_ensure_relative_parent(credentials, self._api_parent(target_ref), rel)
        name = PurePosixPath(rel.replace("\\", "/")).name
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

    async def _api_ensure_relative_parent(self, credentials: dict[str, Any], parent: str, relative_path: str) -> str:
        current = parent or "root"
        for part in _relative_folder_parts(relative_path):
            query = f"'{_q_escape(current)}' in parents and name='{_q_escape(part)}' and mimeType='{FOLDER_MIME}' and trashed=false"
            resp = await self._request(credentials, "GET", f"{DRIVE_API}/files", params={"q": query, "fields": "files(id,name)", "supportsAllDrives": "true", "includeItemsFromAllDrives": "true", "pageSize": "1"})
            match = next(iter(resp.json().get("files") or []), None)
            if match:
                current = str(match["id"])
                continue
            created = await self._request(credentials, "POST", f"{DRIVE_API}/files", params={"fields": FIELDS, "supportsAllDrives": "true"}, json={"name": part, "mimeType": FOLDER_MIME, "parents": [current]})
            current = str(created.json().get("id") or "")
        return current

    async def _web_download_info(self, credentials: dict[str, Any], file_id: str) -> dict[str, Any]:
        params = {"id": file_id, "authuser": str(credentials.get("authuser") or "0"), "export": "download"}
        headers = self._web_headers(credentials, {
            "x-json-requested": "true",
            "x-drive-first-party": "DriveWebUi",
            "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
        }, auth=False)
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            try:
                await client.get(f"{DRIVE_USERCONTENT}/auth_warmup", headers=self._web_headers(credentials, auth=False))
            except Exception:
                pass
            resp = await client.post(f"{DRIVE_USERCONTENT}/uc", params=params, headers=headers, content=b"")
        if resp.status_code in (401, 403):
            raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", "Drive web session expired or revoked")
        if resp.status_code >= 400:
            raise ProviderFailure("DOWNLOAD_FAILED", resp.text[:500], {"status": resp.status_code})
        text = resp.text.lstrip(")]}'\n")
        try:
            payload = json.loads(text)
        except Exception:
            payload = {}
        return {
            "url": payload.get("downloadUrl") or f"{DRIVE_USERCONTENT}/download?id={file_id}&export=download&authuser={credentials.get('authuser') or '0'}&confirm=t",
            "name": payload.get("fileName") or "",
        }

    async def _web_upload_file(self, credentials: dict[str, Any], local_path: Path, target_ref: dict[str, Any], progress: JobState) -> dict[str, Any]:
        size = local_path.stat().st_size
        rel = str(target_ref.get("relative_path") or local_path.name)
        parent = await self._web_ensure_relative_parent(credentials, self._api_parent(target_ref), rel)
        name = PurePosixPath(rel.replace("\\", "/")).name
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if size > WEB_MULTIPART_MAX:
            return await self._web_upload_resumable(credentials, local_path, parent, name, mime, progress)
        boundary = f"vaultbox-drive-web-{int(time.time() * 1000)}"
        metadata = {"title": name, "mimeType": mime, "parents": [{"id": parent}]}
        body = (
            f"--{boundary}\r\ncontent-type: application/json; charset=UTF-8\r\n\r\n"
            + json.dumps(metadata, ensure_ascii=False)
            + f"\r\n--{boundary}\r\ncontent-transfer-encoding: base64\r\ncontent-type: {mime}\r\n\r\n"
            + base64.b64encode(local_path.read_bytes()).decode("ascii")
            + f"\r\n--{boundary}--\r\n"
        ).encode("utf-8")
        params = {"uploadType": "multipart", "supportsTeamDrives": "true"}
        key = self._web_key(credentials)
        if key:
            params["key"] = key
        progress.set(step="uploading", current_file=name)
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
            resp = await client.post(
                f"{DRIVE_WEB_UPLOAD_API}/files",
                params=params,
                headers=self._web_headers(credentials, {"content-type": f"multipart/related; boundary={boundary}"}),
                content=body,
            )
        if resp.status_code in (401, 403):
            raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", "Drive web session expired or revoked")
        if resp.status_code >= 400:
            raise ProviderFailure("UPLOAD_FAILED", resp.text[:500], {"status": resp.status_code})
        progress.add_bytes(size, size, "upload", str(local_path))
        data = resp.json()
        return {"id": data.get("id"), "name": data.get("title") or data.get("name") or name}

    async def _web_ensure_relative_parent(self, credentials: dict[str, Any], parent: str, relative_path: str) -> str:
        current = parent or "root"
        key = self._web_key(credentials)
        params_base = {"supportsTeamDrives": "true", **({"key": key} if key else {})}
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
            for part in _relative_folder_parts(relative_path):
                query = f"'{_q_escape(current)}' in parents and title='{_q_escape(part)}' and mimeType='{FOLDER_MIME}' and trashed = false"
                resp = await client.get(f"{DRIVE_WEB_FILES_API}/files", params={**params_base, "q": query, "fields": "items(id,title,mimeType)"}, headers=self._web_headers(credentials))
                if resp.status_code in (401, 403):
                    raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", "Drive web session expired or revoked")
                if resp.status_code >= 400:
                    raise ProviderFailure("UPLOAD_FAILED", resp.text[:500], {"status": resp.status_code})
                match = next(iter(resp.json().get("items") or []), None)
                if match:
                    current = str(match.get("id") or "")
                    continue
                resp = await client.post(f"{DRIVE_WEB_FILES_API}/files", params={**params_base, "fields": "id,title,mimeType,parents"}, headers=self._web_headers(credentials), json={"title": part, "mimeType": FOLDER_MIME, "parents": [{"id": current}]})
                if resp.status_code in (401, 403):
                    raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", "Drive web session expired or revoked")
                if resp.status_code >= 400:
                    raise ProviderFailure("UPLOAD_FAILED", resp.text[:500], {"status": resp.status_code})
                current = str(resp.json().get("id") or "")
        return current

    async def _web_upload_resumable(self, credentials: dict[str, Any], local_path: Path, parent: str, name: str, mime: str, progress: JobState) -> dict[str, Any]:
        size = local_path.stat().st_size
        params = {"uploadType": "resumable", "supportsTeamDrives": "true"}
        key = self._web_key(credentials)
        if key:
            params["key"] = key
        init_headers = {
            "content-type": "application/json",
            "x-upload-content-type": mime,
            "x-upload-content-length": str(size),
        }
        progress.set(step="uploading", current_file=name)
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
            init = await client.post(
                f"{DRIVE_WEB_UPLOAD_API}/files",
                params=params,
                headers=self._web_headers(credentials, init_headers),
                content=json.dumps({"title": name, "mimeType": mime, "parents": [{"id": parent}]}),
            )
            if init.status_code in (401, 403):
                raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", "Drive web session expired or revoked")
            if init.status_code >= 400:
                raise ProviderFailure("UPLOAD_FAILED", init.text[:500], {"status": init.status_code})
            session = init.headers.get("Location") or init.headers.get("location")
            if not session:
                raise ProviderFailure("UPLOAD_FAILED", "Drive web resumable session missing")
            offset = 0
            with local_path.open("rb") as fh:
                while offset < size:
                    progress.check_cancelled()
                    fh.seek(offset)
                    data = fh.read(min(CHUNK, size - offset))
                    end = offset + len(data) - 1
                    resp = await client.put(
                        session,
                        headers=self._web_headers(credentials, {
                            "content-length": str(len(data)),
                            "content-range": f"bytes {offset}-{end}/{size}",
                            "content-type": mime,
                        }),
                        content=data,
                    )
                    if resp.status_code in (200, 201):
                        progress.add_bytes(len(data), size, "upload", str(local_path))
                        result = resp.json()
                        return {"id": result.get("id"), "name": result.get("title") or result.get("name") or name}
                    if resp.status_code != 308:
                        raise ProviderFailure("UPLOAD_FAILED", resp.text[:500], {"status": resp.status_code})
                    rng = resp.headers.get("Range") or resp.headers.get("range") or ""
                    next_offset = int(rng.rsplit("-", 1)[1]) + 1 if "-" in rng else end + 1
                    progress.add_bytes(max(0, next_offset - offset), size, "upload", str(local_path))
                    offset = next_offset
        raise ProviderFailure("UPLOAD_FAILED", "Drive web resumable upload ended early")

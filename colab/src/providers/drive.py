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

from .base import BaseProvider, ProviderFailure, dict_lock, owner_store, safe_name, shared_client, stream_download
from ..jobs.progress import JobState

DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
DRIVE_WEB_FILES_API = "https://clients6.google.com/drive/v2internal"
DRIVE_WEB_UPLOAD_API = "https://clients6.google.com/upload/drive/v2internal"
DRIVE_USERCONTENT = "https://drive.usercontent.google.com"
DRIVE_WEB_ORIGIN = "https://drive.google.com"
DRIVE_WEB_API_KEY = "AIzaSyD_InbmSFufIEps5UAt2NmB_3LvBH3Sz_8"
DRIVE_CLIENT_VERSION = "drive.web-frontend_20260824.12_p0"
DRIVE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
DRIVE_KEY_HOST_ORDER = (
    "drivefrontend-pa.clients6.google.com",
    "workspacevideo-pa.clients6.google.com",
    "blobcomments-pa.clients6.google.com",
    "clients6.google.com",
)
FOLDER_MIME = "application/vnd.google-apps.folder"
FIELDS = "id,name,mimeType,size,parents,webContentLink,webViewLink"
CHUNK = 8 * 1024 * 1024
WEB_MULTIPART_MAX = 5 * 1024 * 1024
API_MULTIPART_MAX = 5 * 1024 * 1024
DRIVE_MOUNT = Path(os.environ.get("COLAB_DRIVE_MOUNT", "/content/drive/MyDrive"))

def _read_at(fh: Any, offset: int, length: int) -> bytes:
    fh.seek(offset)
    return fh.read(length)

def _q_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "\\'")

def _multipart_body(boundary: str, metadata: str, mime: str, raw: bytes, *, as_base64: bool = False) -> bytes:
    encoding = "content-transfer-encoding: base64\r\n" if as_base64 else ""
    payload = base64.b64encode(raw) if as_base64 else raw
    return b"".join([
        f"--{boundary}\r\ncontent-type: application/json; charset=UTF-8\r\n\r\n".encode("utf-8"),
        metadata.encode("utf-8"),
        f"\r\n--{boundary}\r\n{encoding}content-type: {mime}\r\n\r\n".encode("utf-8"),
        payload,
        f"\r\n--{boundary}--\r\n".encode("utf-8"),
    ])

def _clean_id(raw: Any) -> str:
    s = str(raw or "").strip()
    if s.startswith("id:"):
        s = s[3:].strip()
    return s

def _relative_folder_parts(relative_path: str) -> list[str]:
    rel = str(relative_path or "").replace("\\", "/").strip("/")
    parent = PurePosixPath(rel).parent
    return [safe_name(part) for part in parent.parts if part and part not in (".", "..")]

_API_KEY_ERROR_MARKERS = (
    "api_key_http_referrer_blocked",
    "api_key_invalid",
    "api_key_service_blocked",
    "requests from referer",
    "api key not valid",
    "drive frontend private api",
    "has not been used in project",
    "is disabled. enable it by visiting",
    "drivefrontend-pa.googleapis.com",
    "api keys are not supported",
)

def _is_api_key_error(body: str) -> bool:
    lowered = (body or "").lower()
    return any(marker in lowered for marker in _API_KEY_ERROR_MARKERS)

class DriveProvider(BaseProvider):
    name = "drive"

    def _client(self) -> httpx.AsyncClient:
        """One keep-alive client per job loop; Drive auth travels in headers, not in the client."""
        return shared_client("drive", timeout=None, follow_redirects=True)

    def _folder_locks(self, owner: Any) -> dict[Any, asyncio.Lock]:
        return owner_store(f"{self.name}:folders", owner).setdefault("locks", {})

    def _folder_cache(self, owner: Any) -> dict[Any, str]:
        return owner_store(f"{self.name}:folders", owner).setdefault("cache", {})

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
            cookies = c.get("cookies") or {}
            if any(cookies.get(k) for k in ("SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID")):
                return "SAPISIDHASH"
            raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", "Drive access token missing")
        return str(token)

    def _web_session(self, c: dict[str, Any]) -> bool:
        if c.get("auth_mode") == "web_session":
            return True
        cookies = c.get("cookies") or {}
        if any(cookies.get(k) for k in ("SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID", "SID", "HSID", "SSID")):
            return True
        token = str(c.get("access_token") or c.get("token") or c.get("web_access_token") or "").lower()
        return token.startswith("sapisidhash ")

    def _cookie_header(self, c: dict[str, Any]) -> str:
        cookies = c.get("cookies") or {}
        if isinstance(cookies, str):
            return cookies
        if isinstance(cookies, dict):
            return "; ".join(f"{k}={v}" for k, v in cookies.items() if k and v)
        return ""

    def _cookie_jar(self, c: dict[str, Any]) -> httpx.Cookies:
        jar = httpx.Cookies()
        cookies = c.get("cookies") or {}
        if isinstance(cookies, str):
            from http.cookies import SimpleCookie
            sc = SimpleCookie()
            sc.load(cookies)
            cookies = {k: v.value for k, v in sc.items()}
        if isinstance(cookies, dict):
            for name, value in cookies.items():
                if name and value:
                    jar.set(str(name), str(value), domain=".google.com", path="/")
                    jar.set(str(name), str(value), domain=".googleusercontent.com", path="/")
                    jar.set(str(name), str(value), domain="drive.google.com", path="/")
                    jar.set(str(name), str(value), domain="drive.usercontent.google.com", path="/")
        return jar

    def _web_auth(self, c: dict[str, Any]) -> str:
        cookies = c.get("cookies") or {}
        if isinstance(cookies, str):
            from http.cookies import SimpleCookie
            sc = SimpleCookie()
            sc.load(cookies)
            cookies = {k: v.value for k, v in sc.items()}
        sapisid = cookies.get("SAPISID")
        sec1 = cookies.get("__Secure-1PAPISID")
        sec3 = cookies.get("__Secure-3PAPISID")
        if sapisid or sec1 or sec3:
            origin = (c.get("auth_headers") or {}).get("X-Origin") or (c.get("auth_headers") or {}).get("x-origin") or DRIVE_WEB_ORIGIN
            ts = str(int(time.time()))
            parts: list[str] = []
            if sapisid:
                digest = hashlib.sha1(f"{ts} {sapisid} {origin}".encode("utf-8")).hexdigest()
                parts.append(f"SAPISIDHASH {ts}_{digest}")
            if sec1:
                digest1 = hashlib.sha1(f"{ts} {sec1} {origin}".encode("utf-8")).hexdigest()
                parts.append(f"SAPISID1PHASH {ts}_{digest1}")
            if sec3:
                digest3 = hashlib.sha1(f"{ts} {sec3} {origin}".encode("utf-8")).hexdigest()
                parts.append(f"SAPISID3PHASH {ts}_{digest3}")
            return " ".join(parts)
        token = self._token(c)
        if token and not token.lower().startswith("sapisidhash "):
            return f"Bearer {token}"
        return token

    def _web_headers(self, c: dict[str, Any], extra: dict[str, str] | None = None, *, auth: bool = True) -> dict[str, str]:
        headers = {
            "origin": DRIVE_WEB_ORIGIN,
            "referer": DRIVE_WEB_ORIGIN + "/",
            "user-agent": DRIVE_USER_AGENT,
            "x-goog-authuser": str(c.get("authuser") or "0"),
            "x-goog-drive-client-version": DRIVE_CLIENT_VERSION,
        }
        cookie = self._cookie_header(c)
        if cookie:
            headers["cookie"] = cookie
        headers.update({k: v for k, v in (c.get("auth_headers") or {}).items() if str(k).lower() not in {"authorization", "x-goog-api-key", "cookie"}})
        if auth:
            headers["Authorization"] = self._web_auth(c)
        if extra:
            headers.update(extra)
        return headers

    def _web_key(self, c: dict[str, Any]) -> str:
        keys = c.get("api_keys") or {}
        for host in DRIVE_KEY_HOST_ORDER:
            key = keys.get(host)
            if key and key != "AIzaSyBc1bLOZpOtg3-qgMjSQ6pmn6HbE2zjzJg":
                return str(key)
        return DRIVE_WEB_API_KEY

    def _api_parent(self, ref: Any) -> str:
        raw = ref if isinstance(ref, str) else (ref.get("id") or ref.get("path") or "root")
        cleaned = _clean_id(raw)
        return "root" if cleaned in {"", "/"} else cleaned

    def _headers(self, c: dict[str, Any], extra: dict[str, str] | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token(c)}", **(extra or {})}

    async def _send_request(self, client: Any, method: str, url: str, **kwargs: Any) -> httpx.Response:
        fn = getattr(client, method.lower(), None)
        if callable(fn):
            return await fn(url, **kwargs)
        if hasattr(client, "request"):
            return await client.request(method, url, **kwargs)
        raise AttributeError(f"{client} has no {method.lower()} or request method")

    async def _request(self, c: dict[str, Any], method: str, url: str, **kw: Any) -> httpx.Response:
        headers = self._headers(c, kw.pop("headers", None))
        resp = await self._send_request(self._client(), method, url, headers=headers, **kw)
        if resp.status_code == 401:
            raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", "Drive token expired or revoked")
        if resp.status_code >= 400:
            raise ProviderFailure("UPLOAD_FAILED" if method != "GET" else "DOWNLOAD_FAILED", resp.text[:500], {"status": resp.status_code})
        return resp

    def _update_cookies_from_response(self, credentials: dict[str, Any], resp: httpx.Response) -> None:
        if not resp:
            return
        headers = getattr(resp, "headers", {}) or {}
        set_cookies = headers.get_list("set-cookie") if hasattr(headers, "get_list") else []
        if not set_cookies and "set-cookie" in headers:
            set_cookies = [headers["set-cookie"]]
        if not set_cookies:
            return
        cookies = credentials.setdefault("cookies", {})
        if isinstance(cookies, str):
            from http.cookies import SimpleCookie
            sc = SimpleCookie()
            sc.load(cookies)
            cookies = {k: v.value for k, v in sc.items()}
            credentials["cookies"] = cookies
        if isinstance(cookies, dict):
            for sc_hdr in set_cookies:
                for chunk in sc_hdr.split(";"):
                    chunk = chunk.strip()
                    if "=" in chunk:
                        k, v = chunk.split("=", 1)
                        k = k.strip()
                        if k and not k.lower().startswith(("domain", "path", "expires", "max-age", "samesite", "httponly", "secure")):
                            cookies[k] = v.strip()

    async def _refresh_web_session(self, credentials: dict[str, Any]) -> bool:
        """Refresh browser-session cookies inside the Colab job only."""
        try:
            client = self._client()
            for url, auth in (
                (f"{DRIVE_WEB_ORIGIN}/drive/my-drive", True),
                (f"{DRIVE_USERCONTENT}/auth_warmup", False),
            ):
                try:
                    resp = await self._send_request(
                        client,
                        "GET",
                        url,
                        headers=self._web_headers(credentials, auth=auth),
                        follow_redirects=True,
                        timeout=15.0,
                    )
                    self._update_cookies_from_response(credentials, resp)
                except Exception:
                    pass

            key = self._web_key(credentials)
            resp = await self._send_request(
                client,
                "GET",
                f"{DRIVE_WEB_FILES_API}/about",
                headers=self._web_headers(credentials),
                params={"key": key, "$unique": "gc"} if key else {},
                timeout=15.0,
            )
            self._update_cookies_from_response(credentials, resp)
            return resp.status_code < 400
        except Exception:
            return False

    async def _web_request(self, credentials: dict[str, Any], method: str, url: str, **kwargs: Any) -> httpx.Response:
        client = self._client()
        extra_headers = kwargs.pop("headers", None)
        headers = self._web_headers(credentials, extra_headers)
        headers.pop("X-Goog-Api-Key", None)
        headers.pop("x-goog-api-key", None)

        is_upload = "/upload/" in url
        params = dict(kwargs.pop("params", None) or {})

        # Colab's v2internal/upload calls work cookie-bound; do not leak a
        # captured frontend key there because older captures may be referrer-bound.
        key = self._web_key(credentials)
        host = str(httpx.URL(url).host or "")
        if key and host != "clients6.google.com" and "key" not in params:
            params["key"] = key
            if not is_upload:
                params.setdefault("$unique", "gc")

        resp = await self._send_request(client, method, url, headers=headers, params=params, **kwargs)
        self._update_cookies_from_response(credentials, resp)

        if resp.status_code in (401, 403):
            body_lower = resp.text[:300].lower()
            is_key_error = any(m in body_lower for m in _API_KEY_ERROR_MARKERS)

            # 1. If it looks like the API key is the problem, retry without it
            if is_key_error and "key" in params:
                params.pop("key", None)
                params.pop("$unique", None)
                resp = await self._send_request(client, method, url, headers=headers, params=params, **kwargs)
                self._update_cookies_from_response(credentials, resp)
                if resp.status_code < 400:
                    return resp

            # Restore API key if needed
            if key and "key" not in params:
                params["key"] = key
                if not is_upload:
                    params.setdefault("$unique", "gc")

            # 2. Try Bearer token if available
            bearer_token = str(credentials.get("web_access_token") or credentials.get("access_token") or credentials.get("token") or "")
            if bearer_token and not bearer_token.lower().startswith("sapisidhash "):
                bearer_headers = self._web_headers(credentials, extra_headers)
                bearer_headers["Authorization"] = f"Bearer {bearer_token}"
                resp = await self._send_request(client, method, url, headers=bearer_headers, params=params, **kwargs)
                self._update_cookies_from_response(credentials, resp)
                if resp.status_code < 400:
                    return resp

            # 3. Autonomous self-refresh of web session cookies (renews SIDCC and session state)
            for attempt in range(3):
                if attempt:
                    await asyncio.sleep(1.0 * attempt)
                await self._refresh_web_session(credentials)
                fresh_headers = self._web_headers(credentials, extra_headers)
                fresh_headers.pop("X-Goog-Api-Key", None)
                fresh_headers.pop("x-goog-api-key", None)
                resp = await self._send_request(client, method, url, headers=fresh_headers, params=params, **kwargs)
                self._update_cookies_from_response(credentials, resp)
                if resp.status_code < 400:
                    return resp

            if resp.status_code in (401, 403):
                raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", "Drive web session expired or revoked")
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
        if self._web_session(credentials):
            # 1. Primary: POST drivefrontend-pa.clients6.google.com/v1/items:list
            page_size = 1000
            body = [[None] * 44, [page_size, "", [2, 5]]]
            body[0][4] = 0
            body[0][21] = 0
            body[0][24] = [4, 1, 1]
            body[0][35] = [[1]]
            body[0][43] = [[parent, 0]]
            try:
                resp = await self._web_request(
                    credentials,
                    "POST",
                    "https://drivefrontend-pa.clients6.google.com/v1/items:list",
                    json=body,
                    headers={
                        "content-type": "application/json+protobuf",
                        "x-goog-fieldmask": "items(id,title,mime_type,file_size,modified_date_millis,create_date_millis,thumbnail_version,alternate_link,explicitly_trashed,trashed,parent),continuation_token",
                    },
                )
                data = resp.json()
                raw_items = data[0] if isinstance(data, list) and data and isinstance(data[0], list) else []
                items = []
                for it in raw_items:
                    if not isinstance(it, list) or not it:
                        continue
                    fid = str(it[0] if len(it) > 0 and it[0] else "")
                    if not fid:
                        continue
                    name = str(it[2] if len(it) > 2 and it[2] else "")
                    mime = str(it[3] if len(it) > 3 and it[3] else "")
                    created_ms = it[9] if len(it) > 9 else None
                    modified_ms = it[10] if len(it) > 10 else None
                    size = int(it[13] if len(it) > 13 and it[13] else 0)
                    trashed = bool((len(it) > 25 and it[25]) or (len(it) > 87 and it[87]))
                    if trashed:
                        continue
                    items.append({
                        "id": fid,
                        "name": name,
                        "type": "folder" if mime == FOLDER_MIME else "file",
                        "mimeType": mime,
                        "size": size,
                        "modifiedTime": modified_ms,
                        "createdTime": created_ms,
                    })
                return {"items": items}
            except Exception:
                # 2. Fallback: GET clients6.google.com/drive/v2internal/files
                params = {
                    "q": f"'{_q_escape(parent)}' in parents and trashed = false",
                    "maxResults": "1000",
                    "supportsTeamDrives": "true",
                }
                resp = await self._web_request(credentials, "GET", f"{DRIVE_WEB_FILES_API}/files", params=params)
                v2_items = resp.json().get("items") or []
                return {"items": [{
                    "id": it.get("id"),
                    "name": it.get("title") or it.get("name"),
                    "type": "folder" if it.get("mimeType") == FOLDER_MIME else "file",
                    "mimeType": it.get("mimeType"),
                    "size": int(it.get("fileSize") or 0) if it.get("fileSize") else 0,
                    "modifiedTime": it.get("modifiedDate") or it.get("modified_date_millis"),
                    "createdTime": it.get("createdDate") or it.get("create_date_millis"),
                } for it in v2_items]}
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
            progress.add_bytes(src.stat().st_size, src.stat().st_size, "download", str(dest))
            return dest
        if self._use_mount(credentials) and not (credentials.get("access_token") or credentials.get("token") or credentials.get("web_access_token")):
            raise ProviderFailure("SOURCE_FILE_NOT_FOUND", "Drive source must be a MyDrive path after mounting")
        fid = _clean_id(file_ref.get("id") or file_ref.get("path") or "")
        if not fid:
            raise ProviderFailure("SOURCE_FILE_NOT_FOUND", "Drive file id missing")
        if self._web_session(credentials):
            info = await self._web_download_info(credentials, fid)
            name = file_ref.get("name") or info.get("name") or fid
            local_path = local_path if local_path.suffix else local_path / safe_name(name)
            progress.set(step="downloading", current_file=local_path.name)
            try:
                return await stream_download(
                    info["url"],
                    local_path,
                    progress,
                    headers=self._web_headers(credentials, auth=False),
                    cookies=self._cookie_jar(credentials),
                )
            except ProviderFailure as exc:
                if exc.code != "INVALID_PROVIDER_CREDENTIALS" and int((exc.details or {}).get("status") or 0) not in (401, 403):
                    raise
                await self._refresh_web_session(credentials)
                info = await self._web_download_info(credentials, fid)
                return await stream_download(
                    info["url"],
                    local_path,
                    progress,
                    headers=self._web_headers(credentials, auth=False),
                    cookies=self._cookie_jar(credentials),
                )
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
        meta_dict: dict[str, Any] = {"name": name, "parents": [parent]}
        try:
            from ..utils.video_thumbnail import get_cached_thumbnail
            thumb_bytes = get_cached_thumbnail(local_path)
            if thumb_bytes and len(thumb_bytes) <= 2 * 1024 * 1024:
                meta_dict["contentHints"] = {
                    "thumbnail": {
                        "image": base64.urlsafe_b64encode(thumb_bytes).decode("ascii"),
                        "mimeType": "image/jpeg",
                    }
                }
        except Exception:
            pass
        if size <= API_MULTIPART_MAX:
            boundary = f"vaultbox-drive-{int(time.time() * 1000)}"
            body = _multipart_body(boundary, json.dumps(meta_dict, ensure_ascii=False), mime, await asyncio.to_thread(local_path.read_bytes))
            resp = await self._request(credentials, "POST", f"{DRIVE_UPLOAD_API}/files", params={"uploadType": "multipart", "fields": FIELDS, "supportsAllDrives": "true"}, headers={"Content-Type": f"multipart/related; boundary={boundary}"}, content=body)
            progress.add_bytes(size, size, "upload", str(local_path))
            return resp.json()
        init = await self._request(credentials, "POST", f"{DRIVE_UPLOAD_API}/files", params={"uploadType": "resumable", "fields": FIELDS, "supportsAllDrives": "true"}, headers={
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": mime,
            "X-Upload-Content-Length": str(size),
        }, content=json.dumps(meta_dict))
        session = init.headers.get("Location")
        if not session:
            raise ProviderFailure("UPLOAD_FAILED", "Drive resumable session missing")
        offset = 0
        with local_path.open("rb") as fh:
            while offset < size:
                progress.check_cancelled()
                data = await asyncio.to_thread(_read_at, fh, offset, min(CHUNK, size - offset))
                end = offset + len(data) - 1
                resp = await self._client().put(session, headers=self._headers(credentials, {"Content-Length": str(len(data)), "Content-Range": f"bytes {offset}-{end}/{size}"}), content=data)
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

    async def replace_file(self, credentials: dict[str, Any], local_path: Path, source_ref: dict[str, Any], progress: JobState) -> dict[str, Any]:
        fid = str(source_ref.get("id") or source_ref.get("path") or "")
        if self._use_mount(credentials) and self._mount_ref(source_ref):
            self._require_mount()
            dest = self._mount_path(source_ref)
            if not dest.is_file():
                raise ProviderFailure("SOURCE_FILE_NOT_FOUND", "Drive mounted file path not found")
            progress.set(step="uploading", current_file=dest.name)
            await asyncio.to_thread(shutil.copy2, local_path, dest)
            size = local_path.stat().st_size
            progress.add_bytes(size, size, "upload", str(local_path))
            return {"id": dest.relative_to(DRIVE_MOUNT).as_posix(), "name": dest.name, "path": dest.relative_to(DRIVE_MOUNT).as_posix()}
        if self._web_session(credentials):
            if not fid:
                raise ProviderFailure("SOURCE_FILE_NOT_FOUND", "Drive file id missing")
            name = str(source_ref.get("name") or local_path.name)
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            return await self._web_replace_file(credentials, local_path, fid, name, mime, progress, source_ref=source_ref)
        if not fid:
            raise ProviderFailure("SOURCE_FILE_NOT_FOUND", "Drive file id missing")
        meta = (await self._request(credentials, "GET", f"{DRIVE_API}/files/{fid}", params={"fields": FIELDS, "supportsAllDrives": "true"})).json()
        if str(meta.get("mimeType") or "").startswith("application/vnd.google-apps."):
            raise ProviderFailure("UPLOAD_FAILED", "Drive Workspace files cannot be overwritten with binary upload")
        name = str(source_ref.get("name") or meta.get("name") or local_path.name)
        size = local_path.stat().st_size
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        progress.set(step="uploading", current_file=name)
        meta_dict: dict[str, Any] = {"name": name}
        try:
            from ..utils.video_thumbnail import get_cached_thumbnail
            thumb_bytes = get_cached_thumbnail(local_path)
            if thumb_bytes and len(thumb_bytes) <= 2 * 1024 * 1024:
                meta_dict["contentHints"] = {
                    "thumbnail": {
                        "image": base64.urlsafe_b64encode(thumb_bytes).decode("ascii"),
                        "mimeType": "image/jpeg",
                    }
                }
        except Exception:
            pass
        if size <= API_MULTIPART_MAX:
            boundary = f"vaultbox-drive-{int(time.time() * 1000)}"
            body = _multipart_body(boundary, json.dumps(meta_dict, ensure_ascii=False), mime, await asyncio.to_thread(local_path.read_bytes))
            resp = await self._request(credentials, "PATCH", f"{DRIVE_UPLOAD_API}/files/{fid}", params={"uploadType": "multipart", "fields": FIELDS, "supportsAllDrives": "true"}, headers={"Content-Type": f"multipart/related; boundary={boundary}"}, content=body)
            progress.add_bytes(size, size, "upload", str(local_path))
            out = resp.json()
            if out.get("id") != fid or int(out.get("size") or 0) != size:
                raise ProviderFailure("UPLOAD_FAILED", "Drive overwrite verification failed")
            return out
        init = await self._request(credentials, "PATCH", f"{DRIVE_UPLOAD_API}/files/{fid}", params={"uploadType": "resumable", "fields": FIELDS, "supportsAllDrives": "true"}, headers={
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": mime,
            "X-Upload-Content-Length": str(size),
        }, content=json.dumps(meta_dict))
        session = init.headers.get("Location")
        if not session:
            raise ProviderFailure("UPLOAD_FAILED", "Drive resumable session missing")
        offset = 0
        with local_path.open("rb") as fh:
            while offset < size:
                progress.check_cancelled()
                data = await asyncio.to_thread(_read_at, fh, offset, min(CHUNK, size - offset))
                end = offset + len(data) - 1
                resp = await self._client().put(session, headers=self._headers(credentials, {"Content-Length": str(len(data)), "Content-Range": f"bytes {offset}-{end}/{size}"}), content=data)
                if resp.status_code in (200, 201):
                    progress.add_bytes(len(data), size, "upload", str(local_path))
                    out = resp.json()
                    if out.get("id") != fid or int(out.get("size") or 0) != size:
                        raise ProviderFailure("UPLOAD_FAILED", "Drive overwrite verification failed")
                    return out
                if resp.status_code != 308:
                    raise ProviderFailure("UPLOAD_FAILED", resp.text[:500], {"status": resp.status_code})
                rng = resp.headers.get("Range", "")
                next_offset = int(rng.rsplit("-", 1)[1]) + 1 if "-" in rng else end + 1
                progress.add_bytes(max(0, next_offset - offset), size, "upload", str(local_path))
                offset = next_offset
        raise ProviderFailure("UPLOAD_FAILED", "Drive overwrite ended early")

    async def _api_ensure_relative_parent(self, credentials: dict[str, Any], parent: str, relative_path: str) -> str:
        cache = self._folder_cache(credentials)
        locks = self._folder_locks(credentials)
        current = parent or "root"
        for part in _relative_folder_parts(relative_path):
            key = ("api", current, part)
            known = cache.get(key)
            if known:
                current = known
                continue
            # Single-flight per (parent, name): concurrent uploads must not create the same folder twice.
            async with dict_lock(locks, key):
                known = cache.get(key)
                if not known:
                    query = f"'{_q_escape(current)}' in parents and name='{_q_escape(part)}' and mimeType='{FOLDER_MIME}' and trashed=false"
                    resp = await self._request(credentials, "GET", f"{DRIVE_API}/files", params={"q": query, "fields": "files(id,name)", "supportsAllDrives": "true", "includeItemsFromAllDrives": "true", "pageSize": "1"})
                    match = next(iter(resp.json().get("files") or []), None)
                    if match:
                        known = str(match["id"])
                    else:
                        created = await self._request(credentials, "POST", f"{DRIVE_API}/files", params={"fields": FIELDS, "supportsAllDrives": "true"}, json={"name": part, "mimeType": FOLDER_MIME, "parents": [current]})
                        known = str(created.json().get("id") or "")
                    cache[key] = known
            current = known
        return current

    async def _web_download_info(self, credentials: dict[str, Any], file_id: str) -> dict[str, Any]:
        file_id = _clean_id(file_id)
        params = {"id": file_id, "authuser": str(credentials.get("authuser") or "0"), "export": "download"}
        resp = None
        for attempt in range(2):
            headers = self._web_headers(credentials, {
                "x-json-requested": "true",
                "x-drive-first-party": "DriveWebUi",
                "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
            }, auth=False)
            async with httpx.AsyncClient(timeout=60, follow_redirects=True, cookies=self._cookie_jar(credentials)) as client:
                try:
                    warmup = await client.get(f"{DRIVE_USERCONTENT}/auth_warmup", headers=self._web_headers(credentials, auth=False))
                    self._update_cookies_from_response(credentials, warmup)
                except Exception:
                    pass
                resp = await client.post(f"{DRIVE_USERCONTENT}/uc", params=params, headers=headers, content=b"")
                self._update_cookies_from_response(credentials, resp)
            if resp.status_code not in (401, 403):
                break
            if attempt == 0:
                await self._refresh_web_session(credentials)
        if resp is None or resp.status_code in (401, 403):
            raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", "Drive web session expired or revoked", {"status": getattr(resp, "status_code", 0)})
        if resp.status_code in (400, 404, 500, 502, 503):
            url = f"{DRIVE_USERCONTENT}/download?id={file_id}&export=download&authuser={credentials.get('authuser') or '0'}&confirm=t"
            return {"url": url, "name": file_id}
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
        metadata = json.dumps({"title": name, "mimeType": mime, "parents": [{"id": parent}]}, ensure_ascii=False)
        encoded = base64.b64encode(await asyncio.to_thread(local_path.read_bytes)).decode("ascii")
        progress.set(step="uploading", current_file=name)
        boundary = f"vaultbox-drive-web-{int(time.time() * 1000)}"
        body = (
            f"--{boundary}\r\ncontent-type: application/json; charset=UTF-8\r\n\r\n"
            + metadata
            + f"\r\n--{boundary}\r\ncontent-transfer-encoding: base64\r\ncontent-type: {mime}\r\n\r\n"
            + encoded
            + f"\r\n--{boundary}--\r\n"
        ).encode("utf-8")
        resp = await self._web_request(
            credentials,
            "POST",
            f"{DRIVE_WEB_UPLOAD_API}/files",
            params={"uploadType": "multipart", "supportsTeamDrives": "true"},
            headers={"content-type": f"multipart/related; boundary={boundary}"},
            content=body,
        )
        progress.add_bytes(size, size, "upload", str(local_path))
        data = resp.json()
        return {"id": data.get("id"), "name": data.get("title") or data.get("name") or name}

    async def _web_ensure_relative_parent(self, credentials: dict[str, Any], parent: str, relative_path: str) -> str:
        cache = self._folder_cache(credentials)
        locks = self._folder_locks(credentials)
        current = parent or "root"
        for part in _relative_folder_parts(relative_path):
            cache_key = ("web", current, part)
            known = cache.get(cache_key)
            if known:
                current = known
                continue
            async with dict_lock(locks, cache_key):
                known = cache.get(cache_key)
                if not known:
                    query = f"'{_q_escape(current)}' in parents and title='{_q_escape(part)}' and mimeType='{FOLDER_MIME}' and trashed = false"
                    try:
                        resp = await self._web_request(
                            credentials,
                            "GET",
                            f"{DRIVE_WEB_FILES_API}/files",
                            params={"supportsTeamDrives": "true", "q": query, "fields": "items(id,title,mimeType)"},
                        )
                        match = next(iter(resp.json().get("items") or []), None)
                        if match:
                            known = str(match.get("id") or "")
                    except Exception:
                        pass
                    if not known:
                        try:
                            listing = await self.list_files(credentials, current)
                            match = next((it for it in (listing.get("items") or []) if it.get("name") == part and it.get("type") == "folder"), None)
                            if match and match.get("id"):
                                known = str(match["id"])
                        except Exception:
                            pass
                    if not known:
                        resp = await self._web_request(
                            credentials,
                            "POST",
                            f"{DRIVE_WEB_FILES_API}/files",
                            params={"supportsTeamDrives": "true", "fields": "id,title,mimeType,parents"},
                            json={"title": part, "mimeType": FOLDER_MIME, "parents": [{"id": current}]},
                        )
                        known = str(resp.json().get("id") or "")
                    cache[cache_key] = known
            current = known
        return current

    async def _query_resumable_offset(self, client: Any, credentials: dict[str, Any], session_uri: str, size: int) -> int:
        try:
            headers = self._web_headers(credentials, {
                "content-length": "0",
                "content-range": f"bytes */{size}",
            })
            resp = await self._send_request(client, "PUT", session_uri, headers=headers)
            self._update_cookies_from_response(credentials, resp)
            if resp.status_code == 308:
                rng = resp.headers.get("Range") or resp.headers.get("range") or ""
                return int(rng.rsplit("-", 1)[1]) + 1 if "-" in rng else 0
            if resp.status_code in (200, 201):
                return size
        except Exception:
            pass
        return 0

    async def _web_upload_resumable(self, credentials: dict[str, Any], local_path: Path, parent: str, name: str, mime: str, progress: JobState) -> dict[str, Any]:
        size = local_path.stat().st_size
        init_headers = {
            "content-type": "application/json",
            "x-upload-content-type": mime,
            "x-upload-content-length": str(size),
        }
        progress.set(step="uploading", current_file=name)
        init_payload = json.dumps({"title": name, "mimeType": mime, "parents": [{"id": parent}]})
        async def new_session() -> str:
            init = await self._web_request(
                credentials,
                "POST",
                f"{DRIVE_WEB_UPLOAD_API}/files",
                params={"uploadType": "resumable", "supportsTeamDrives": "true"},
                headers=init_headers,
                content=init_payload,
            )
            session_uri = init.headers.get("Location") or init.headers.get("location")
            if not session_uri:
                raise ProviderFailure("UPLOAD_FAILED", "Drive web resumable session missing")
            return str(session_uri)

        session = await new_session()
        offset = 0
        client = self._client()
        with local_path.open("rb") as fh:
            while offset < size:
                progress.check_cancelled()
                data = await asyncio.to_thread(_read_at, fh, offset, min(CHUNK, size - offset))
                end = offset + len(data) - 1
                for attempt in range(5):
                    try:
                        chunk_headers = self._web_headers(credentials, {
                            "content-length": str(len(data)),
                            "content-range": f"bytes {offset}-{end}/{size}",
                            "content-type": mime,
                        })
                        resp = await self._send_request(
                            client,
                            "PUT",
                            session,
                            headers=chunk_headers,
                            content=data,
                        )
                        self._update_cookies_from_response(credentials, resp)
                        if resp.status_code in (200, 201):
                            progress.add_bytes(len(data), size, "upload", str(local_path))
                            result = resp.json()
                            return {"id": result.get("id"), "name": result.get("title") or result.get("name") or name}
                        if resp.status_code == 308:
                            rng = resp.headers.get("Range") or resp.headers.get("range") or ""
                            next_offset = int(rng.rsplit("-", 1)[1]) + 1 if "-" in rng else end + 1
                            progress.add_bytes(max(0, next_offset - offset), size, "upload", str(local_path))
                            offset = next_offset
                            break
                        if resp.status_code in (401, 403):
                            if attempt < 4:
                                await self._refresh_web_session(credentials)
                                await asyncio.sleep(min(2 ** attempt, 4))
                                offset = await self._query_resumable_offset(client, credentials, session, size)
                                if offset == 0:
                                    session = await new_session()
                                continue
                            raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", "Drive web session expired or revoked")
                        if resp.status_code >= 500 or resp.status_code in (400, 408, 429):
                            if attempt < 4:
                                await asyncio.sleep(min(2 ** attempt, 8))
                                offset = await self._query_resumable_offset(client, credentials, session, size)
                                continue
                        raise ProviderFailure("UPLOAD_FAILED", resp.text[:500], {"status": resp.status_code})
                    except (httpx.HTTPError, OSError) as net_err:
                        if attempt < 4:
                            await asyncio.sleep(min(2 ** attempt, 8))
                            offset = await self._query_resumable_offset(client, credentials, session, size)
                            continue
                        raise ProviderFailure("UPLOAD_FAILED", f"Network error during upload: {net_err}")
        raise ProviderFailure("UPLOAD_FAILED", "Drive web resumable upload ended early")

    async def _web_replace_file(self, credentials: dict[str, Any], local_path: Path, fid: str, name: str, mime: str, progress: JobState, source_ref: dict[str, Any] | None = None) -> dict[str, Any]:
        parent = ""
        if source_ref:
            parent = str(source_ref.get("parent_id") or source_ref.get("parent") or (source_ref.get("parents") or [""])[0] or "")
        if not parent:
            try:
                meta = (await self._web_request(credentials, "GET", f"{DRIVE_WEB_FILES_API}/files/{fid}", params={"fields": "parents(id)", "supportsTeamDrives": "true"})).json()
                parent = str(((meta.get("parents") or [{}])[0]).get("id") or "root")
            except Exception:
                parent = "root"

        upload_target = {"id": parent, "name": name, "relative_path": name}
        res = await self._web_upload_file(credentials, local_path, upload_target, progress)
        if fid and not res.get("id"):
            res["id"] = fid

        if fid and fid != res.get("id"):
            try:
                await self.delete_file(credentials, {"id": fid})
            except Exception:
                pass
        return res

    async def delete_file(self, credentials: dict[str, Any], file_ref: dict[str, Any]) -> dict[str, Any]:
        if self._use_mount(credentials) and self._mount_ref(file_ref):
            self._require_mount()
            dest = self._mount_path(file_ref)
            if dest.is_file():
                dest.unlink(missing_ok=True)
            elif dest.is_dir():
                shutil.rmtree(dest, ignore_errors=True)
            return {"ok": True}
        fid = _clean_id(file_ref.get("id") or file_ref.get("path") or "")
        if not fid:
            return {"ok": False, "error": "Drive file id missing"}
        if self._web_session(credentials):
            try:
                resp = await self._web_request(
                    credentials,
                    "POST",
                    "https://drivefrontend-pa.clients6.google.com/v1/items:delete",
                    headers={"content-type": "application/json+protobuf"},
                    json=[[fid]],
                )
                if resp.status_code in (200, 204):
                    return {"ok": True}
            except Exception:
                pass
            del_resp = await self._web_request(
                credentials,
                "DELETE",
                f"{DRIVE_WEB_FILES_API}/files/{fid}",
            )
            return {"ok": del_resp.status_code in (200, 204)}
        resp = await self._request(credentials, "DELETE", f"{DRIVE_API}/files/{fid}", params={"supportsAllDrives": "true"})
        return {"ok": resp.status_code in (200, 204)}

    async def delete_item(self, credentials: dict[str, Any], file_ref: dict[str, Any]) -> dict[str, Any]:
        return await self.delete_file(credentials, file_ref)


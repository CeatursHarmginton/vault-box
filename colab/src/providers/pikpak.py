from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import mimetypes
import time
import xml.etree.ElementTree as ET
from email.utils import formatdate
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from .base import BaseProvider, ProviderFailure, safe_name, stream_download
from ..config import PIKPAK_UPLOAD_CONCURRENCY
from ..jobs.progress import JobState

API = "https://api-drive.mypikpak.com"
USER = "https://user.mypikpak.com"
CLIENT_ID = "YNxT9w7GMdWvEOKa"
CLIENT_VERSION = "1.47.1"
PACKAGE = "com.pikcloud.pikpak"
WEB_CLIENT_ID = "YUMx5nI8ZU8Ap8pm"
WEB_CLIENT_VERSION = "2.0.0"
WEB_PACKAGE = "mypikpak.com"
MIN_PART = 5 * 1024 * 1024
PART = 8 * 1024 * 1024
SALTS = ["Gez0T9ijiI9WCeTsKSg3SMlx", "zQdbalsolyb1R/", "ftOjr52zt51JD68C3s", "yeOBMH0JkbQdEFNNwQ0RI9T3wU/v", "BRJrQZiTQ65WtMvwO", "je8fqxKPdQVJiy1DM6Bc9Nb1", "niV", "9hFCW2R1", "sHKHpe2i96", "p7c5E6AcXQ/IJUuAEC9W6", "", "aRv9hjc9P+Pbn+u3krN6", "BzStcgE8qVdqjEH16l4", "SqgeZvL5j9zoHP95xWHt", "zVof5yaJkPe3VFpadPof"]
WEB_SALTS = ["C9qPpZLN8ucRTaTiUMWYS9cQvWOE", "+r6CQVxjzJV6LCV", "F", "pFJRC", "9WXYIDGrwTCz2OiVlgZa90qpECPD6olt", "/750aCr4lm/Sly/c", "RB+DT/gZCrbV", "", "CyLsf7hdkIRxRm215hl", "7xHvLi2tOYP0Y92b", "ZGTXXxu8E/MIWaEDB+Sm/", "1UI3", "E7fP5Pfijd+7K+t6Tg/NhuLq0eEUVChpJSkrKxpO", "ihtqpG6FMt65+Xk+tWUH2", "NhXXU9rg4XXdzo7u5o"]

def _task_id(data: dict[str, Any]) -> str:
    return str(((data.get("file") or {}).get("params") or {}).get("task_id") or (data.get("task") or {}).get("id") or "")

def _file_id(data: dict[str, Any]) -> str:
    return str((data.get("file") or data).get("id") or "")

def _token(c: dict[str, Any]) -> tuple[str, str]:
    if c.get("encoded_token"):
        data = json.loads(base64.b64decode(str(c["encoded_token"])).decode())
        return str(data.get("access_token") or ""), str(data.get("refresh_token") or "")
    return str(c.get("access_token") or c.get("token") or ""), str(c.get("refresh_token") or "")

def _captcha_sign(device_id: str, ts: str) -> str:
    sign = CLIENT_ID + CLIENT_VERSION + PACKAGE + device_id + ts
    for salt in SALTS:
        sign = hashlib.md5((sign + salt).encode()).hexdigest()
    return f"1.{sign}"

def _web_device_id(device_id: str) -> str:
    return str(device_id or "").split(".")[-1][:32]

def _web_captcha_sign(device_id: str, ts: str) -> str:
    sign = WEB_CLIENT_ID + WEB_CLIENT_VERSION + WEB_PACKAGE + _web_device_id(device_id) + ts
    for salt in WEB_SALTS:
        sign = hashlib.md5((sign + salt).encode()).hexdigest()
    return f"1.{sign}"

def _ua(device_id: str, user_id: str = "") -> str:
    sha = hashlib.sha1(f"{device_id}{PACKAGE}1appkey".encode()).hexdigest()
    devsign = f"div101.{device_id}{hashlib.md5(sha.encode()).hexdigest()}"
    return f"ANDROID-{PACKAGE}/{CLIENT_VERSION} protocolVersion/200 clientid/{CLIENT_ID} clientversion/{CLIENT_VERSION} networktype/WIFI deviceid/{device_id} devicesign/{devsign} sdkversion/2.0.4.204000 datetime/{int(time.time()*1000)} usrno/{user_id} appname/{PACKAGE} devicename/Xiaomi_M2004j7ac osversion/13 platformversion/10 devicemodel/M2004J7AC"

def _gcid(path: Path, size: int) -> str:
    piece = 0x40000
    while size and size / piece > 0x200:
        piece <<= 1
    outer = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(piece), b""):
            outer.update(hashlib.sha1(chunk).digest())
    return outer.hexdigest()

def _oss_resource(bucket: str, key: str, query: dict[str, str] | None = None) -> str:
    out = f"/{bucket}/{key}"
    if not query:
        return out
    parts = [k if query[k] == "" else f"{k}={query[k]}" for k in ("uploads", "partNumber", "uploadId") if k in query]
    return out + ("?" + "&".join(parts) if parts else "")

def _oss_headers(params: dict[str, Any], method: str, content_type: str, query: dict[str, str] | None = None, content_md5: str = "") -> dict[str, str]:
    date = formatdate(usegmt=True)
    oss = {"x-oss-date": date, "x-oss-security-token": str(params.get("security_token") or ""), "x-oss-user-agent": "aliyun-sdk-js/6.23.0 Chrome 148.0.0.0 on Windows 10 64-bit"}
    canonical = "".join(f"{k}:{oss[k]}\n" for k in sorted(oss))
    raw = f"{method.upper()}\n{content_md5}\n{content_type}\n{date}\n{canonical}{_oss_resource(str(params.get('bucket') or ''), str(params.get('key') or ''), query)}"
    sig = base64.b64encode(hmac.new(str(params.get("access_key_secret") or "").encode(), raw.encode(), hashlib.sha1).digest()).decode()
    return {"Authorization": f"OSS {params.get('access_key_id')}:{sig}", **oss, "Content-Type": content_type, **({"Content-MD5": content_md5} if content_md5 else {})}

def _xml(raw: str, tag: str) -> str:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return ""
    node = root.find(f".//{tag}")
    return node.text if node is not None and node.text else ""

class PikPakSession:
    def __init__(self, c: dict[str, Any]) -> None:
        self.credentials = c
        self.access_token, self.refresh_token = _token(c)
        self.device_id = str(c.get("device_id") or c.get("deviceid") or hashlib.md5(self.access_token.encode()).hexdigest())
        self.user_id = str(c.get("user_id") or c.get("sub") or "")
        self.captcha = ""

    def headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        if not self.access_token:
            raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", "PikPak access token missing")
        h = {"Authorization": f"Bearer {self.access_token}", "User-Agent": _ua(self.device_id, self.user_id), "X-Device-Id": self.device_id, "Content-Type": "application/json; charset=utf-8"}
        if self.captcha:
            h["X-Captcha-Token"] = self.captcha
        h.update(extra or {})
        return h

    async def req(self, method: str, url: str, **kw: Any) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
            resp = await client.request(method, url, headers=self.headers(kw.pop("headers", None)), **kw)
        if resp.status_code == 401:
            raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", "PikPak token expired or revoked")
        try:
            data = resp.json() if resp.text else {}
        except Exception:
            data = {"body": resp.text}
        if resp.status_code >= 400 or data.get("error"):
            raise ProviderFailure("UPLOAD_FAILED" if method != "GET" else "DOWNLOAD_FAILED", data.get("error_description") or resp.text[:500], {"status": resp.status_code, "body": data})
        return data

    async def captcha_init(self, action: str) -> str:
        variants = [(CLIENT_ID, CLIENT_VERSION, PACKAGE, self.device_id, _captcha_sign)]
        if str(self.device_id).startswith("wdi"):
            variants.insert(0, (WEB_CLIENT_ID, WEB_CLIENT_VERSION, WEB_PACKAGE, _web_device_id(self.device_id), _web_captcha_sign))
        last: Exception | None = None
        for client_id, version, package, device_id, signer in variants:
            ts = str(int(time.time() * 1000))
            try:
                data = await self.req("POST", f"{USER}/v1/shield/captcha/init", json={
                    "client_id": client_id,
                    "action": action,
                    "device_id": device_id,
                    "meta": {"captcha_sign": signer(self.device_id, ts), "client_version": version, "package_name": package, "user_id": self.user_id, "timestamp": ts},
                })
                break
            except Exception as exc:
                last = exc
        else:
            raise last or ProviderFailure("INVALID_PROVIDER_CREDENTIALS", "PikPak captcha init failed")
        token = str(data.get("captcha_token") or "")
        if not token:
            raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", "PikPak captcha verification required")
        return token

class PikPakProvider(BaseProvider):
    name = "pikpak"

    async def validate_credentials(self, credentials: dict[str, Any]) -> dict[str, Any]:
        s = PikPakSession(credentials)
        about = await s.req("GET", f"{API}/drive/v1/about")
        return {"ok": True, "account": about}

    async def list_files(self, credentials: dict[str, Any], path_or_id: str) -> dict[str, Any]:
        s = PikPakSession(credentials)
        params = {"parent_id": path_or_id or None, "thumbnail_size": "SIZE_SMALL", "limit": "1000", "filters": json.dumps({"trashed": {"eq": False}, "phase": {"eq": "PHASE_TYPE_COMPLETE"}})}
        try:
            data = await s.req("GET", f"{API}/drive/v1/files", params=params)
        except ProviderFailure:
            s.captcha = await s.captcha_init("GET:/drive/v1/files")
            try:
                data = await s.req("GET", f"{API}/drive/v1/files", params=params)
            finally:
                s.captcha = ""
        return {"items": [{"id": f.get("id"), "name": f.get("name"), "type": "folder" if f.get("kind") == "drive#folder" else "file", "size": f.get("size")} for f in data.get("files") or []]}

    async def download_file(self, credentials: dict[str, Any], file_ref: dict[str, Any], local_path: Path, progress: JobState) -> Path:
        s = PikPakSession(credentials)
        fid = str(file_ref.get("id") or "")
        if not fid:
            raise ProviderFailure("SOURCE_FILE_NOT_FOUND", "PikPak file id missing")
        s.captcha = await s.captcha_init(f"GET:/drive/v1/files/{fid}")
        try:
            info = await s.req("GET", f"{API}/drive/v1/files/{fid}")
        finally:
            s.captcha = ""
        url = info.get("web_content_link") or ((info.get("medias") or [{}])[0].get("link") or {}).get("url")
        if not url:
            raise ProviderFailure("DOWNLOAD_FAILED", "PikPak did not return download url")
        dest = local_path if local_path.suffix else local_path / safe_name(file_ref.get("name") or info.get("name") or fid)
        progress.set(step="downloading", current_file=dest.name)
        return await stream_download(str(url), dest, progress)

    async def _create_folder(self, s: PikPakSession, parent_id: str, name: str) -> str:
        payload = {"kind": "drive#folder", "name": name, "parent_id": parent_id or ""}
        last: Exception | None = None
        data: dict[str, Any] = {}
        for action in ("", f"POST:{API}/drive/v1/files", "POST:/drive/v1/files"):
            try:
                if action:
                    s.captcha = await s.captcha_init(action)
                data = await s.req("POST", f"{API}/drive/v1/files", json=payload)
                break
            except Exception as exc:
                last = exc
            finally:
                s.captcha = ""
        if not data:
            raise last or ProviderFailure("UPLOAD_FAILED", "PikPak create folder failed")
        return str((data.get("file") or data).get("id") or "")

    async def _ensure_relative_parent(self, s: PikPakSession, parent_id: str, relative_path: str) -> str:
        current = parent_id or ""
        for part in [p for p in Path(relative_path).parent.as_posix().split("/") if p and p != "."]:
            try:
                listing = await self.list_files(s.credentials, current)
            except ProviderFailure:
                listing = {"items": []}
            match = next((i for i in listing.get("items") or [] if i.get("type") == "folder" and i.get("name") == part), None)
            current = str(match.get("id")) if match else await self._create_folder(s, current, part)
        return current

    async def _wait_upload_task(self, s: PikPakSession, task_id: str, progress: JobState, timeout: float = 120.0, poll: float = 2.0) -> dict[str, Any]:
        if not task_id:
            return {}
        deadline = time.time() + timeout
        last: dict[str, Any] = {}
        last_phase = None
        while time.time() < deadline:
            progress.check_cancelled()
            try:
                task = await s.req("GET", f"{API}/drive/v1/tasks/{task_id}")
            except ProviderFailure:
                s.captcha = await s.captcha_init(f"GET:/drive/v1/tasks/{task_id}")
                try:
                    task = await s.req("GET", f"{API}/drive/v1/tasks/{task_id}")
                finally:
                    s.captcha = ""
            last = task
            phase = str(task.get("phase") or task.get("status") or "")
            if phase != last_phase:
                last_phase = phase
            if phase == "PHASE_TYPE_COMPLETE":
                progress.progress.upload = 100
                progress.updated_at = time.time()
                return task
            if phase == "PHASE_TYPE_ERROR":
                raise ProviderFailure("UPLOAD_FAILED", "PikPak upload task failed", {"task": task})
            await asyncio.sleep(poll)
        raise ProviderFailure("UPLOAD_FAILED", "PikPak upload task timed out", {"task": last, "task_id": task_id})

    async def _wait_file_complete(self, s: PikPakSession, file_id: str, progress: JobState, timeout: float = 120.0, poll: float = 2.0) -> dict[str, Any]:
        if not file_id:
            raise ProviderFailure("UPLOAD_FAILED", "PikPak upload response missing task id and file id")
        deadline = time.time() + timeout
        last: dict[str, Any] = {}
        last_phase = None
        while time.time() < deadline:
            progress.check_cancelled()
            try:
                info = await s.req("GET", f"{API}/drive/v1/files/{file_id}")
            except ProviderFailure:
                s.captcha = await s.captcha_init(f"GET:/drive/v1/files/{file_id}")
                try:
                    info = await s.req("GET", f"{API}/drive/v1/files/{file_id}")
                finally:
                    s.captcha = ""
            last = info
            phase = str(info.get("phase") or "")
            if phase != last_phase:
                last_phase = phase
            if phase == "PHASE_TYPE_COMPLETE":
                progress.progress.upload = 100
                progress.updated_at = time.time()
                return info
            if phase == "PHASE_TYPE_ERROR":
                raise ProviderFailure("UPLOAD_FAILED", "PikPak upload file failed", {"file": info})
            await asyncio.sleep(poll)
        raise ProviderFailure("UPLOAD_FAILED", "PikPak upload file did not become visible", {"file": last, "file_id": file_id})

    async def upload_file(self, credentials: dict[str, Any], local_path: Path, target_ref: dict[str, Any], progress: JobState) -> dict[str, Any]:
        s = PikPakSession(credentials)
        name = Path(target_ref.get("relative_path") or local_path.name).name
        parent_id = await self._ensure_relative_parent(s, str(target_ref.get("id") or target_ref.get("path") or ""), str(target_ref.get("relative_path") or name))
        size = local_path.stat().st_size
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        progress.set(step="uploading", current_file=name)
        payload = {"hash": _gcid(local_path, size), "name": name, "size": str(size), "kind": "drive#file", "id": "", "parent_id": parent_id, "upload_type": "UPLOAD_TYPE_RESUMABLE", "folder_type": "NORMAL", "resumable": {"provider": "PROVIDER_ALIYUN"}}
        last: Exception | None = None
        create: dict[str, Any] = {}
        for action in ("", f"POST:{API}/drive/v1/files", "POST:/drive/v1/files"):
            try:
                if action:
                    s.captcha = await s.captcha_init(action)
                create = await s.req("POST", f"{API}/drive/v1/files", json=payload)
                break
            except ProviderFailure as exc:
                if "duplicated" in exc.message.lower() or "repeated" in exc.message.lower():
                    return {"file": {"name": name, "parent_id": parent_id}, "task": {"status": "skipped_duplicate"}, "upload": {"size": size, "parts": 0}}
                last = exc
            except Exception as exc:
                last = exc
            finally:
                s.captcha = ""
        if not create:
            raise last or ProviderFailure("UPLOAD_FAILED", "PikPak create upload failed")
        params = ((create.get("resumable") or {}).get("params") or {})
        task_id = _task_id(create)
        if not params:
            ready = await self._wait_upload_task(s, task_id, progress) if task_id else await self._wait_file_complete(s, _file_id(create), progress)
            return {"file": create.get("file") or ready, "task": ready if task_id else create.get("task"), "upload": {"size": size, "parts": 0}}
        base = f"https://{params.get('endpoint')}/{quote(str(params.get('key') or ''), safe='/')}"
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as http:
            q = {"uploads": ""}
            init = await http.post(f"{base}?{urlencode(q)}", headers=_oss_headers(params, "POST", mime, q))
            if init.is_error:
                raise ProviderFailure("UPLOAD_FAILED", init.text[:500])
            upload_id = _xml(init.text, "UploadId")
            if not upload_id:
                raise ProviderFailure("UPLOAD_FAILED", "OSS uploadId missing")
            ranges = [(i + 1, off, min(PART, size - off)) for i, off in enumerate(range(0, max(size, 1), PART))]
            parts: list[tuple[int, str]] = []
            sem = asyncio.Semaphore(max(1, PIKPAK_UPLOAD_CONCURRENCY))
            async def put_part(part_no: int, off: int, n: int) -> tuple[int, str]:
                async with sem:
                    progress.check_cancelled()
                    with local_path.open("rb") as fh:
                        fh.seek(off)
                        data = fh.read(n)
                    q2 = {"partNumber": str(part_no), "uploadId": upload_id}
                    resp = await http.put(f"{base}?{urlencode(q2)}", content=data, headers=_oss_headers(params, "PUT", mime, q2))
                    if resp.is_error:
                        raise ProviderFailure("UPLOAD_FAILED", resp.text[:500])
                    progress.add_bytes(len(data), size, "upload", str(local_path))
                    return part_no, resp.headers.get("etag", "").strip('"')
            parts = sorted(await asyncio.gather(*(put_part(*r) for r in ranges)))
            body = ('<?xml version="1.0" encoding="UTF-8"?>\n<CompleteMultipartUpload>\n' + "".join(f"<Part><PartNumber>{n}</PartNumber><ETag>\"{e}\"</ETag></Part>\n" for n, e in parts) + "</CompleteMultipartUpload>").encode()
            md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
            q3 = {"uploadId": upload_id}
            done = await http.post(f"{base}?{urlencode(q3)}", content=body, headers=_oss_headers(params, "POST", "application/xml", q3, md5))
            if done.is_error:
                raise ProviderFailure("UPLOAD_FAILED", done.text[:500])
        task = await self._wait_upload_task(s, task_id, progress)
        return {"file": create.get("file"), "task": task, "upload": {"size": size, "parts": len(parts)}}

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
import zlib
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import httpx

from .base import BaseProvider, ProviderFailure, safe_name, stream_download
from ..config import TERABOX_UPLOAD_CONCURRENCY
from ..jobs.progress import JobState

DEFAULT_HOST = "https://www.terabox.com"
VALIDATION_HOSTS = ("https://www.terabox.com", "https://www.1024terabox.com", "https://www.terabox.app", "https://dm.terabox.com", "https://dm.terabox.app")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
CONST = {"app_id": "250528", "web": "1", "channel": "dubox", "clienttype": "0"}
PART = 4 * 1024 * 1024
UPLOAD_CONCURRENCY = TERABOX_UPLOAD_CONCURRENCY
JS_PAT = (r"fn%28%22([0-9A-Fa-f]+)%22%29", r'"jsToken"\s*:\s*"([0-9A-Fa-f]+)"', r"jsToken['\"]?\s*[:=]\s*['\"]([0-9A-Fa-f]+)['\"]")
BD_PAT = (r'"bdstoken"\s*:\s*"([0-9a-f]{32})"', r"bdstoken['\"]?\s*[:=]\s*['\"]([0-9a-f]{32})['\"]")
TOKEN_EXPIRED = {"4000020", "4000023", "450016"}

def _cookie_dict(c: dict[str, Any]) -> dict[str, str]:
    cookies = dict(c.get("cookies") or {})
    raw = str(c.get("cookie") or "")
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    if c.get("ndus"):
        cookies["ndus"] = str(c["ndus"])
    return {k: v for k, v in cookies.items() if k and v}

def _first(patterns: tuple[str, ...], text: str) -> str:
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return ""

def _hashes(path: Path) -> dict[str, Any]:
    file_hash = hashlib.md5()
    slice_hash = hashlib.md5()
    crc = 0
    chunks = []
    left = 256 * 1024
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(PART), b""):
            file_hash.update(chunk)
            crc = zlib.crc32(chunk, crc)
            if left:
                take = min(left, len(chunk))
                slice_hash.update(chunk[:take])
                left -= take
            chunks.append(hashlib.md5(chunk).hexdigest())
    return {"file": file_hash.hexdigest(), "slice": slice_hash.hexdigest(), "crc32": crc & 0xFFFFFFFF, "chunks": chunks or [hashlib.md5(b"").hexdigest()]}

class TeraBoxSession:
    def __init__(self, credentials: dict[str, Any]) -> None:
        self.cookies = _cookie_dict(credentials)
        self.base = str(credentials.get("region_host") or DEFAULT_HOST).rstrip("/")
        self.jstoken = str(credentials.get("jstoken") or credentials.get("jsToken") or "")
        self.bdstoken = str(credentials.get("bdstoken") or "")

    def headers(self, *, referer: str = "", json_accept: bool = True) -> dict[str, str]:
        h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*" if json_accept else "text/html,*/*", "Accept-Language": "en-US,en;q=0.9", "Referer": referer or f"{self.base}/main?category=all", "X-Requested-With": "XMLHttpRequest"}
        if not json_accept:
            h.pop("X-Requested-With", None)
        return h

    def params(self, **extra: Any) -> dict[str, str]:
        out = dict(CONST)
        if self.jstoken:
            out["jsToken"] = self.jstoken
        out.update({k: str(v) for k, v in extra.items() if v is not None})
        return out

    async def request_json(self, method: str, url: str, *, context: str, **kw: Any) -> dict[str, Any]:
        async with httpx.AsyncClient(cookies=self.cookies, timeout=None, follow_redirects=True, headers={"User-Agent": UA}) as client:
            resp = await client.request(method, url, **kw)
            self.cookies.update({k: v for k, v in resp.cookies.items() if v})
        if resp.status_code in (401, 403):
            raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", f"TeraBox rejected credentials during {context}")
        try:
            data = resp.json()
        except Exception as exc:
            raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", f"TeraBox returned non-JSON during {context}") from exc
        errno = data.get("errno", data.get("errcode"))
        if errno not in (None, 0, "0"):
            code = int(errno) if str(errno).lstrip("-").isdigit() else -1
            if code in {111, -62, 6, -6, 4000023}:
                raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", f"TeraBox session invalid ({context})", {"errno": code})
            if code in {31034, -32}:
                raise ProviderFailure("PROVIDER_RATE_LIMITED", f"TeraBox rate limited ({context})", {"errno": code})
            if code == -9:
                raise ProviderFailure("SOURCE_FILE_NOT_FOUND", f"TeraBox path not found ({context})")
            failure_code = "UPLOAD_FAILED" if any(token in context for token in ("upload", "precreate", "create folder")) else "DOWNLOAD_FAILED"
            raise ProviderFailure(failure_code, f"TeraBox API error ({context})", {"errno": code, "body": data})
        return data

    async def bootstrap_tokens(self, *, force: bool = False) -> None:
        if self.jstoken and self.bdstoken and not force:
            return
        async with httpx.AsyncClient(cookies=self.cookies, timeout=30, follow_redirects=True) as client:
            resp = await client.get(f"{self.base}/main?category=all", headers=self.headers(json_accept=False))
            self.cookies.update({k: v for k, v in resp.cookies.items() if v})
            html = resp.text
        self.jstoken = ("" if force else self.jstoken) or _first(JS_PAT, html)
        self.bdstoken = ("" if force else self.bdstoken) or _first(BD_PAT, html)

    async def ready(self) -> None:
        if not self.cookies.get("ndus"):
            raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", "TeraBox ndus cookie missing")
        hosts = [self.base, *[h for h in VALIDATION_HOSTS if h != self.base]]
        for host in hosts:
            try:
                data = await self.request_json("GET", f"{host}/passport/get_info", context="get_info", params=self.params(), headers=self.headers(referer=f"{host}/main?category=all"))
                if "data" in data:
                    self.base = host
                    break
            except ProviderFailure:
                continue
        await self.bootstrap_tokens()

class TeraBoxProvider(BaseProvider):
    name = "terabox"

    def __init__(self) -> None:
        self._folder_lock = asyncio.Lock()

    async def validate_credentials(self, credentials: dict[str, Any]) -> dict[str, Any]:
        s = TeraBoxSession(credentials)
        await s.ready()
        return {"ok": True, "region_host": s.base}

    async def list_files(self, credentials: dict[str, Any], path_or_id: str) -> dict[str, Any]:
        s = TeraBoxSession(credentials)
        await s.ready()
        data = await s.request_json("GET", f"{s.base}/api/list", context=f"list {path_or_id}", params=s.params(order="time", desc=1, dir=path_or_id or "/", num=1000, page=1, showempty=0), headers=s.headers())
        return {"items": [{"id": i.get("path"), "path": i.get("path"), "name": i.get("server_filename"), "type": "folder" if i.get("isdir") else "file", "size": i.get("size", 0)} for i in data.get("list") or []]}

    async def _dlink(self, s: TeraBoxSession, path: str) -> dict[str, Any]:
        data = await s.request_json("GET", f"{s.base}/api/filemetas", context=f"filemetas {path}", params=s.params(target=json.dumps([path], ensure_ascii=False), dlink=1), headers=s.headers())
        info = (data.get("info") or [{}])[0]
        if not info.get("dlink"):
            raise ProviderFailure("SOURCE_FILE_NOT_FOUND", f"TeraBox file not found: {path}")
        return info

    async def _resolve_file_paths(self, credentials: dict[str, Any], ref: dict[str, Any]) -> list[str]:
        path = str(ref.get("path") or ref.get("id") or "")
        if not path:
            raise ProviderFailure("SOURCE_FILE_NOT_FOUND", "TeraBox path missing")
        return [path]

    async def _create_folder(self, s: TeraBoxSession, path: str) -> None:
        try:
            await s.request_json("POST", f"{s.base}/api/create", context=f"create folder {path}", params=s.params(a="commit"), data={"path": path, "isdir": "1", "block_list": "[]"}, headers={**s.headers(), "Content-Type": "application/x-www-form-urlencoded"})
        except ProviderFailure as exc:
            text = f"{exc.message} {exc.details}".lower()
            if not any(token in text for token in ("repeat", "exist", "already", "-8")):
                raise

    async def _find_child_folder(self, s: TeraBoxSession, parent: str, name: str) -> str:
        data = await s.request_json("GET", f"{s.base}/api/list", context=f"list {parent}", params=s.params(order="time", desc=1, dir=parent or "/", num=1000, page=1, showempty=0), headers=s.headers())
        for item in data.get("list") or []:
            if item.get("isdir") and item.get("server_filename") == name and item.get("path"):
                return str(item["path"])
        return ""

    async def _ensure_relative_parent(self, s: TeraBoxSession, parent: str, relative_path: str) -> str:
        # ponytail: one provider-wide lock; use per-parent locks if folder creation throughput matters.
        async with self._folder_lock:
            current = parent.rstrip("/") or "/"
            for part in [p for p in Path(relative_path).parent.as_posix().split("/") if p and p != "."]:
                next_path = await self._find_child_folder(s, current, part)
                if not next_path:
                    next_path = f"{current}/{part}" if current != "/" else f"/{part}"
                    await self._create_folder(s, next_path)
                current = next_path
            return current

    async def download_file(self, credentials: dict[str, Any], file_ref: dict[str, Any], local_path: Path, progress: JobState) -> Path:
        s = TeraBoxSession(credentials)
        await s.ready()
        path = (await self._resolve_file_paths(credentials, file_ref))[0]
        meta = await self._dlink(s, path)
        name = file_ref.get("name") or meta.get("server_filename") or PurePosixPath(path).name
        dest = local_path if local_path.suffix else local_path / safe_name(name)
        progress.set(step="downloading", current_file=dest.name)
        cookie = "; ".join(f"{k}={v}" for k, v in s.cookies.items())
        return await stream_download(str(meta["dlink"]), dest, progress, headers={"User-Agent": UA, "Cookie": cookie})

    async def _locate_upload_hosts(self, s: TeraBoxSession) -> list[str]:
        hosts = []
        try:
            prefix = (urlparse(s.base).hostname or "").split(".", 1)[0]
            if prefix:
                hosts.append(f"https://{prefix}-d.terabox.com")
        except Exception:
            pass
        hosts.extend(["https://d.terabox.com", "https://dm-d.terabox.com"])
        seen: set[str] = set()
        async with httpx.AsyncClient(cookies=s.cookies, timeout=30, follow_redirects=True, headers={"User-Agent": UA}) as client:
            for host in hosts:
                if host in seen:
                    continue
                seen.add(host)
                try:
                    resp = await client.get(
                        f"{host}/rest/2.0/pcs/file",
                        params={"method": "locateupload"},
                        headers={**s.headers(referer=f"{s.base}/vietnamese/main?category=all"), "Content-Type": "application/json;charset=UTF-8"},
                    )
                    s.cookies.update({k: v for k, v in resp.cookies.items() if v})
                    payload = resp.json()
                except Exception:
                    continue
                host_value = payload.get("host")
                if host_value:
                    return [f"https://{str(host_value).removeprefix('https://').removeprefix('http://').strip('/')}"]
                servers = [str(item) for item in (payload.get("server") or []) if item]
                if servers:
                    return [f"https://{server.removeprefix('https://').removeprefix('http://').strip('/')}" for server in servers]
        return ["https://dm1-cdata.terabox.com", "https://dm2-cdata.terabox.com", "https://kul-cdata.terabox.com"]

    async def _precreate_upload(self, s: TeraBoxSession, remote_path: str, parent: str, size: int, hashes: dict[str, Any], rtype: str = "2") -> dict[str, Any]:
        form = {
            "path": remote_path,
            "autoinit": "1",
            "target_path": parent,
            "block_list": json.dumps(hashes["chunks"]),
            "size": str(size),
            "rtype": rtype,
            "file_limit_switch_v34": "true",
            "g_identity": "",
            "local_mtime": "0",
            "content-md5": hashes["file"],
            "slice-md5": hashes["slice"],
            "content-crc32": str(hashes["crc32"]),
        }
        last: dict[str, Any] = {}
        for attempt in range(2):
            async with httpx.AsyncClient(cookies=s.cookies, timeout=None, follow_redirects=True, headers={"User-Agent": UA}) as client:
                resp = await client.post(f"{s.base}/api/precreate", params=s.params(jsToken=s.jstoken), data=form, headers={**s.headers(), "Content-Type": "application/x-www-form-urlencoded"})
                s.cookies.update({k: v for k, v in resp.cookies.items() if v})
            try:
                payload = resp.json()
            except Exception as exc:
                if attempt == 0:
                    await s.bootstrap_tokens(force=True)
                    continue
                raise ProviderFailure("UPLOAD_FAILED", f"TeraBox returned non-JSON during precreate {remote_path}") from exc
            last = payload
            errno = str(payload.get("errno", payload.get("errcode", "")))
            if errno in TOKEN_EXPIRED and attempt == 0:
                await s.bootstrap_tokens(force=True)
                continue
            if resp.status_code >= 400:
                raise ProviderFailure("UPLOAD_FAILED", f"TeraBox precreate HTTP {resp.status_code}", {"body": resp.text[:300]})
            if payload.get("errno", payload.get("errcode")) not in (None, 0, "0"):
                raise ProviderFailure("UPLOAD_FAILED", f"TeraBox precreate failed", {"body": payload})
            return payload
        raise ProviderFailure("UPLOAD_FAILED", "TeraBox precreate failed", {"body": last})

    async def _upload_part(self, client: httpx.AsyncClient, s: TeraBoxSession, host: str, local_path: Path, remote_path: str, upload_id: str, idx: int, part_size: int, mime: str) -> None:
        with local_path.open("rb") as fh:
            fh.seek(idx * PART)
            data = fh.read(part_size)
        resp = await client.post(
            f"{host}/rest/2.0/pcs/superfile2",
            params={**CONST, "method": "upload", "path": remote_path, "uploadid": upload_id, "partseq": str(idx)},
            files={"file": ("blob", data, mime)},
            headers={"Origin": s.base, "Referer": f"{s.base}/"},
        )
        s.cookies.update({k: v for k, v in resp.cookies.items() if v})
        if resp.status_code >= 400:
            raise ProviderFailure("UPLOAD_FAILED", f"TeraBox upload part {idx} failed HTTP {resp.status_code}", {"body": resp.text[:200]})
        try:
            payload = resp.json()
        except Exception as exc:
            raise ProviderFailure("UPLOAD_FAILED", f"TeraBox upload part {idx} returned non-JSON") from exc
        errno = payload.get("errno") if payload.get("errno") is not None else payload.get("error_code")
        if errno not in (None, 0, "0"):
            raise ProviderFailure("UPLOAD_FAILED", f"TeraBox upload part {idx} failed", {"errno": errno, "body": payload})

    async def _upload_parts(self, s: TeraBoxSession, host: str, local_path: Path, remote_path: str, upload_id: str, size: int, mime: str, progress: JobState, concurrency: int) -> None:
        part_sizes = [0] if size == 0 else [min(PART, size - off) for off in range(0, size, PART)]
        sem = asyncio.Semaphore(max(1, int(concurrency or 1)))
        lock = asyncio.Lock()
        async with httpx.AsyncClient(cookies=s.cookies, timeout=httpx.Timeout(None, connect=30.0), headers={"User-Agent": UA}) as client:
            async def run(idx: int, part_size: int) -> None:
                async with sem:
                    last: ProviderFailure | None = None
                    for attempt in range(3):
                        progress.check_cancelled()
                        try:
                            await self._upload_part(client, s, host, local_path, remote_path, upload_id, idx, part_size, mime)
                            async with lock:
                                progress.add_bytes(part_size, size, "upload", remote_path)
                            return
                        except ProviderFailure as exc:
                            last = exc
                            if attempt < 2:
                                await asyncio.sleep(0.5 * (attempt + 1))
                    raise last or ProviderFailure("UPLOAD_FAILED", f"TeraBox upload part {idx} failed")
            await asyncio.gather(*(run(i, part_size) for i, part_size in enumerate(part_sizes)))

    async def upload_file(self, credentials: dict[str, Any], local_path: Path, target_ref: dict[str, Any], progress: JobState) -> dict[str, Any]:
        s = TeraBoxSession(credentials)
        await s.ready()
        rel = str(target_ref.get("relative_path") or local_path.name)
        parent = await self._ensure_relative_parent(s, str(target_ref.get("id") or target_ref.get("path") or "/"), rel)
        name = Path(rel).name
        remote_path = f"{parent}/{name}" if parent != "/" else f"/{name}"
        size = local_path.stat().st_size
        hashes = _hashes(local_path)
        progress.set(step="uploading", current_file=name)
        
        options = progress.payload.get("options") or {}
        replace = bool(options.get("replace"))
        rtype = "3" if replace else "2"
        
        pre = await self._precreate_upload(s, remote_path, parent, size, hashes, rtype=rtype)
        upload_id = pre.get("uploadid") or pre.get("upload_id")
        if not upload_id:
            raise ProviderFailure("UPLOAD_FAILED", "TeraBox precreate did not return uploadid")
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        last = ""
        uploaded = False
        fallbacks = tuple(dict.fromkeys((UPLOAD_CONCURRENCY, 16, 8, 4, 2, 1)))
        for host in await self._locate_upload_hosts(s):
            for concurrency in fallbacks:
                try:
                    await self._upload_parts(s, host, local_path, remote_path, str(upload_id), size, mime, progress, concurrency)
                    uploaded = True
                    break
                except Exception as exc:
                    last = getattr(exc, "message", str(exc))
            if uploaded:
                break
        if not uploaded:
            raise ProviderFailure("UPLOAD_FAILED", f"TeraBox upload parts failed: {last}")
        return await s.request_json("POST", f"{s.base}/api/create", context=f"upload create {remote_path}", params=s.params(a="commit"), data={
            "path": remote_path, "size": str(size), "isdir": "0", "uploadid": str(upload_id), "target_path": parent, "block_list": json.dumps(hashes["chunks"]), "content-md5": hashes["file"], "slice-md5": hashes["slice"], "content-crc32": str(hashes["crc32"]), "rtype": rtype, "local_mtime": "0",
        }, headers={**s.headers(), "Content-Type": "application/x-www-form-urlencoded"})

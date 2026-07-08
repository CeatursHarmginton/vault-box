from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
import zlib
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from .base import BaseProvider, ProviderFailure, stream_download
from ..jobs.progress import JobState

DEFAULT_HOST = "https://www.terabox.com"
VALIDATION_HOSTS = ("https://www.terabox.com", "https://www.1024terabox.com", "https://www.terabox.app", "https://dm.terabox.com", "https://dm.terabox.app")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
CONST = {"app_id": "250528", "web": "1", "channel": "dubox", "clienttype": "0"}
PART = 4 * 1024 * 1024
JS_PAT = (r"fn%28%22([0-9A-Fa-f]+)%22%29", r'"jsToken"\s*:\s*"([0-9A-Fa-f]+)"', r"jsToken['\"]?\s*[:=]\s*['\"]([0-9A-Fa-f]+)['\"]")
BD_PAT = (r'"bdstoken"\s*:\s*"([0-9a-f]{32})"', r"bdstoken['\"]?\s*[:=]\s*['\"]([0-9a-f]{32})['\"]")

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
            raise ProviderFailure("DOWNLOAD_FAILED", f"TeraBox API error ({context})", {"errno": code, "body": data})
        return data

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
        if not self.jstoken or not self.bdstoken:
            async with httpx.AsyncClient(cookies=self.cookies, timeout=30, follow_redirects=True) as client:
                html = (await client.get(f"{self.base}/main?category=all", headers=self.headers(json_accept=False))).text
            self.jstoken = self.jstoken or _first(JS_PAT, html)
            self.bdstoken = self.bdstoken or _first(BD_PAT, html)

class TeraBoxProvider(BaseProvider):
    name = "terabox"

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

    async def download_file(self, credentials: dict[str, Any], file_ref: dict[str, Any], local_path: Path, progress: JobState) -> Path:
        s = TeraBoxSession(credentials)
        await s.ready()
        path = (await self._resolve_file_paths(credentials, file_ref))[0]
        meta = await self._dlink(s, path)
        name = file_ref.get("name") or meta.get("server_filename") or PurePosixPath(path).name
        dest = local_path if local_path.suffix else local_path / name
        progress.set(step="downloading", current_file=dest.name)
        cookie = "; ".join(f"{k}={v}" for k, v in s.cookies.items())
        return await stream_download(str(meta["dlink"]), dest, progress, headers={"User-Agent": UA, "Cookie": cookie})

    async def upload_file(self, credentials: dict[str, Any], local_path: Path, target_ref: dict[str, Any], progress: JobState) -> dict[str, Any]:
        s = TeraBoxSession(credentials)
        await s.ready()
        parent = str(target_ref.get("id") or target_ref.get("path") or "/").rstrip("/") or "/"
        name = Path(target_ref.get("relative_path") or local_path.name).name
        remote_path = f"{parent}/{name}" if parent != "/" else f"/{name}"
        size = local_path.stat().st_size
        hashes = _hashes(local_path)
        progress.set(step="uploading", current_file=name)
        pre = await s.request_json("POST", f"{s.base}/api/precreate", context=f"precreate {remote_path}", params=s.params(), data={
            "path": remote_path, "autoinit": "1", "target_path": parent, "block_list": json.dumps(hashes["chunks"]), "size": str(size), "rtype": "2", "content-md5": hashes["file"], "slice-md5": hashes["slice"], "content-crc32": str(hashes["crc32"]),
        }, headers={**s.headers(), "Content-Type": "application/x-www-form-urlencoded"})
        upload_id = pre.get("uploadid") or pre.get("upload_id")
        if not upload_id:
            raise ProviderFailure("UPLOAD_FAILED", "TeraBox precreate did not return uploadid")
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        hosts = ["https://d.terabox.com", "https://dm-d.terabox.com", "https://dm1-cdata.terabox.com", "https://kul-cdata.terabox.com"]
        parts = [(i, off, min(PART, size - off)) for i, off in enumerate(range(0, max(size, 1), PART))]
        last = ""
        for host in hosts:
            try:
                async with httpx.AsyncClient(cookies=s.cookies, timeout=None, headers={"User-Agent": UA}) as client:
                    for idx, off, n in parts:
                        progress.check_cancelled()
                        with local_path.open("rb") as fh:
                            fh.seek(off)
                            data = fh.read(n)
                        resp = await client.post(f"{host}/rest/2.0/pcs/superfile2", params={**CONST, "method": "upload", "path": remote_path, "uploadid": upload_id, "partseq": str(idx)}, files={"file": ("blob", data, mime)}, headers={"Origin": s.base, "Referer": f"{s.base}/"})
                        if resp.status_code >= 400:
                            raise RuntimeError(resp.text[:200])
                        progress.add_bytes(len(data), size, "upload")
                break
            except Exception as exc:
                last = str(exc)
        else:
            raise ProviderFailure("UPLOAD_FAILED", f"TeraBox upload parts failed: {last}")
        return await s.request_json("POST", f"{s.base}/api/create", context=f"upload create {remote_path}", params=s.params(a="commit"), data={
            "path": remote_path, "size": str(size), "isdir": "0", "uploadid": str(upload_id), "target_path": parent, "block_list": json.dumps(hashes["chunks"]), "content-md5": hashes["file"], "slice-md5": hashes["slice"], "content-crc32": str(hashes["crc32"]), "rtype": "2",
        }, headers={**s.headers(), "Content-Type": "application/x-www-form-urlencoded"})

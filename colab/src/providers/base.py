from __future__ import annotations

import asyncio
import contextvars
import json
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import httpx

from ..config import CHUNK_SIZE, FOLDER_DOWNLOAD_CONCURRENCY, FOLDER_UPLOAD_CONCURRENCY
from ..jobs.progress import JobState
from ..utils.image_optimizer import IMAGE_EXTENSIONS

_DOWNLOAD_LOG_CONTEXT = contextvars.ContextVar("download_log_context", default=None)

def _loop_store(name: str) -> dict[Any, Any]:
    """Scratch space attached to the running loop, so it dies with the job that owns the loop."""
    loop = asyncio.get_running_loop()
    store = getattr(loop, "_vaultbox_store", None)
    if store is None:
        store = {}
        setattr(loop, "_vaultbox_store", store)
    return store.setdefault(name, {})

def owner_store(name: str, owner: Any) -> dict[str, Any]:
    """Per-owner slot inside the loop store; keeps a strong ref so id(owner) cannot be recycled."""
    store = _loop_store(name)
    entry = store.get(id(owner))
    if entry is None or entry[0] is not owner:
        entry = (owner, {})
        store[id(owner)] = entry
    return entry[1]

def dict_lock(locks: dict[Any, asyncio.Lock], key: Any) -> asyncio.Lock:
    """One lock per key: unrelated keys never queue behind each other."""
    lock = locks.get(key)
    if lock is None:
        lock = locks[key] = asyncio.Lock()
    return lock

def shared_client(key: str, **kwargs: Any) -> httpx.AsyncClient:
    """One keep-alive client per (loop, key): every request after the first skips the TCP+TLS handshake."""
    clients = _loop_store("http_clients")
    client = clients.get(key)
    if client is None or getattr(client, "is_closed", False):
        client = httpx.AsyncClient(**kwargs)
        clients[key] = client
    return client

def track_client(client: Any) -> Any:
    """Register a client owned elsewhere so close_shared_clients() still releases its sockets."""
    _loop_store("http_clients")[id(client)] = client
    return client

async def close_shared_clients() -> None:
    clients = _loop_store("http_clients")
    for key in list(clients):
        client = clients.pop(key, None)
        aclose = getattr(client, "aclose", None)
        if aclose is None:
            continue
        try:
            await aclose()
        except Exception:
            pass

class ProviderFailure(RuntimeError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

# TeraBox answers a dlink with {"errno": 400141, "errmsg": "need verify"} when it wants the
# web session re-verified; the other codes are the token-expiry family the web client fixes
# by re-scraping jsToken. All of them are recoverable, so they get a verify round + retries
# before the item is skipped.
VERIFY_ERRNOS = {-19, 9013, 9019, 400141, 4000020, 4000023, 450016}
VERIFY_HINTS = ("need verify", "verify", "captcha", "risk control", "token expired")
VERIFY_ROUNDS = 3
DOWNLOAD_RETRIES = 3
DOWNLOAD_RETRY_DELAY = 10.0
# Failures where the bytes are probably still fetchable later: retry, then skip the item.
RETRYABLE_DOWNLOAD_CODES = {"DOWNLOAD_FAILED", "PROVIDER_NEEDS_VERIFY", "PROVIDER_RATE_LIMITED"}
SKIPPABLE_DOWNLOAD_CODES = RETRYABLE_DOWNLOAD_CODES | {"SOURCE_FILE_NOT_FOUND"}
ARCHIVE_DOWNLOAD_EXTENSIONS = (".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".iso")

def is_verify_error(err: dict[str, Any] | None) -> bool:
    if not err:
        return False
    try:
        if int(str(err.get("errno"))) in VERIFY_ERRNOS:
            return True
    except (TypeError, ValueError):
        pass
    message = str(err.get("message") or "").lower()
    return any(hint in message for hint in VERIFY_HINTS)

def is_retryable_download_failure(exc: BaseException) -> bool:
    return isinstance(exc, ProviderFailure) and exc.code in RETRYABLE_DOWNLOAD_CODES

def is_skippable_download_failure(exc: BaseException) -> bool:
    return isinstance(exc, ProviderFailure) and exc.code in SKIPPABLE_DOWNLOAD_CODES

def retry_delay_for(exc: ProviderFailure, delay: float = DOWNLOAD_RETRY_DELAY) -> float:
    return delay if exc.code in {"PROVIDER_NEEDS_VERIFY", "PROVIDER_RATE_LIMITED"} else 0.0

async def sleep_cancellable(seconds: float, progress: JobState) -> None:
    """Wait in 0.5s slices so a cancel during the retry backoff lands promptly."""
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        progress.check_cancelled()
        step = min(0.5, remaining)
        await asyncio.sleep(step)
        remaining -= step

async def download_with_retry(
    factory: Any,
    *,
    progress: JobState,
    label: str,
    retries: int = DOWNLOAD_RETRIES,
    delay: float = DOWNLOAD_RETRY_DELAY,
) -> Any:
    """Await `factory()`, retrying provider refusals `retries` times, `delay` apart.

    Provider-side verification (refresh tokens, re-issue the dlink) already happened inside
    the download itself; this is the outer fallback for when verifying did not clear it.
    The last failure propagates so the caller can skip the item.
    """
    last: ProviderFailure | None = None
    for attempt in range(max(1, retries + 1)):
        progress.check_cancelled()
        try:
            return await factory()
        except ProviderFailure as exc:
            if not is_retryable_download_failure(exc):
                raise
            last = exc
            if attempt >= retries:
                break
            wait = retry_delay_for(exc, delay)
            suffix = f"in {int(wait)}s" if wait else "now"
            progress.log(f"[RETRY {attempt + 1}/{retries}] Download failed ({exc.message}); retrying {label} {suffix}")
            await sleep_cancellable(wait, progress)
    raise last or ProviderFailure("DOWNLOAD_FAILED", f"Download failed: {label}")

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
        eligible = [item for item in items if not (item.get("type") == "folder" or item.get("is_folder") or item.get("isdir")) and not _skip_optimize_item(item, progress)]
        progress.files_to_download += len(eligible)
        sem = _sem or asyncio.Semaphore(max(1, FOLDER_DOWNLOAD_CONCURRENCY))
        log_token = _DOWNLOAD_LOG_CONTEXT.set({"done": 0, "total": len(eligible), "lock": asyncio.Lock()})

        async def save(item: dict[str, Any]) -> list[Path]:
            progress.check_cancelled()
            if item.get("type") == "folder" or item.get("is_folder") or item.get("isdir"):
                sub = local_dir / _safe_name(item.get("name") or item.get("server_filename") or "folder")
                sub.mkdir(parents=True, exist_ok=True)
                return await self.download_folder(credentials, item, sub, progress, sem)
            if _skip_optimize_item(item, progress):
                progress.files_skipped += 1
                progress.log(f"[SKIP] File ignored by image optimizer: {item.get('name') or item.get('server_filename') or item.get('id') or 'file'}")
                return []
            async with sem:
                dest = local_dir / _safe_name(item.get("name") or item.get("server_filename") or item.get("id") or "file")
                try:
                    return [await download_with_retry(
                        lambda: self.download_file(credentials, item, dest, progress),
                        progress=progress, label=dest.name,
                    )]
                except ProviderFailure as exc:
                    # One unfetchable file must not sink the whole folder: record it and let the
                    # rest through. The folder then stays in the queue (see JobState.failed_items).
                    if not is_skippable_download_failure(exc):
                        raise
                    progress.fail_file(dest.name, exc.code, exc.message)
                    return []

        try:
            return [path for batch in await asyncio.gather(*(save(item) for item in items)) for path in batch]
        finally:
            _DOWNLOAD_LOG_CONTEXT.reset(log_token)

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

async def stream_download(url: str, dest: Path, progress: JobState, *, headers: dict[str, str] | None = None, phase: str = "download", on_verify: Any = None) -> Path:
    """Stream `url` into `dest`, resuming across connection drops.

    When the provider answers with a JSON error instead of bytes and `on_verify` is given,
    the hook gets a chance to re-verify the session and hand back a fresh url/headers; that
    is what recovers TeraBox's `need verify`. Without a hook (or once the rounds run out)
    the failure surfaces as PROVIDER_NEEDS_VERIFY so the caller can retry, then skip.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    strict = dest.suffix.lower() in IMAGE_EXTENSIONS
    rounds = VERIFY_ROUNDS if on_verify else 1
    for verify_round in range(rounds):
        expected = 0
        content_type = ""
        last_exc: Exception | None = None
        for attempt in range(5):
            done = part.stat().st_size if part.exists() else 0
            attempt_start = done
            req_headers = dict(headers or {})
            if done:
                req_headers["Range"] = f"bytes={done}-"
            try:
                client = shared_client("download", timeout=None, follow_redirects=True)
                async with client.stream("GET", url, headers=req_headers) as resp:
                    if resp.status_code in (401, 403):
                        raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", "Provider rejected download credentials")
                    resp.raise_for_status()
                    if done and resp.status_code != 206:
                        part.unlink(missing_ok=True)
                        done = 0
                    total = done + int(resp.headers.get("content-length") or 0)
                    expected = total or expected
                    content_type = resp.headers.get("content-type", content_type)
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
        err = _download_error_payload(part, content_type, strict)
        if not err:
            break
        part.unlink(missing_ok=True)
        needs_verify = is_verify_error(err)
        if not (needs_verify and on_verify and verify_round < rounds - 1):
            code = "PROVIDER_NEEDS_VERIFY" if needs_verify else "DOWNLOAD_FAILED"
            raise ProviderFailure(code, f"Provider returned error instead of file: {err.get('message')}", err)
        progress.log(f"[VERIFY {verify_round + 1}/{rounds - 1}] Provider wants verification ({err.get('message')}); refreshing session for {dest.name}")
        fresh = await on_verify(verify_round)
        if not (fresh and fresh.get("url")):
            raise ProviderFailure("PROVIDER_NEEDS_VERIFY", f"Provider returned error instead of file: {err.get('message')}", err)
        url = str(fresh["url"])
        if fresh.get("headers"):
            headers = dict(fresh["headers"])
    part.replace(dest)
    progress.files_downloaded += 1
    ctx = _DOWNLOAD_LOG_CONTEXT.get()
    if ctx:
        async with ctx["lock"]:
            ctx["done"] += 1
            progress.log(f"[{ctx['done']}/{ctx['total']}] Downloaded: {dest.name}")
    else:
        progress.log(f"[{progress.files_downloaded}/{progress.files_to_download}] Downloaded: {dest.name}")
    return dest

def _download_error_payload(path: Path, content_type: str, strict: bool) -> dict[str, Any] | None:
    if path.stat().st_size > 4096:
        return None
    raw = path.read_bytes().lstrip()
    if not raw.startswith((b"{", b"[")):
        return None
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    errno = data.get("errno", data.get("errcode", data.get("error_code")))
    message = data.get("errmsg") or data.get("error_description") or data.get("error") or data.get("message")
    if not message and errno in (None, 0, "0"):
        return None
    payload = {"errno": errno, "message": str(message or "download error"), "body": data}
    # A complete errno+errmsg envelope is a provider refusal whatever the content-type claims;
    # trusting the content-type here would leave a 70-byte "video" on disk.
    if message and errno not in (None, 0, "0"):
        return payload
    if "json" not in content_type.lower() and not strict:
        return None
    return payload

def safe_name(name: str) -> str:
    clean = "".join(c for c in str(name or "file") if c not in '<>:"/\\|?*').strip().strip(".")
    return clean or "file"

_safe_name = safe_name

def _skip_optimize_item(item: dict[str, Any], progress: JobState) -> bool:
    if not (progress.payload.get("options") or {}).get("optimize_image"):
        return False
    options = progress.payload.get("options") or {}
    name = str(item.get("name") or item.get("server_filename") or item.get("path") or item.get("id") or "")
    lower_name = name.lower()
    suffix = Path(name).suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        if options.get("extract") and (lower_name.endswith(ARCHIVE_DOWNLOAD_EXTENSIONS) or (len(suffix) == 4 and suffix[1:].isdigit())):
            return False
        return True
    size = int(item.get("size") or item.get("file_size") or item.get("bytes") or 0)
    if not size:
        return False
    min_target = int(float(options.get("min_target_mb", 1.0)) * 1024 * 1024)
    max_target = int(float(options.get("max_target_mb", 3.0)) * 1024 * 1024)
    return size >= min_target if options.get("upscale") else size <= max_target


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

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Any

from ..extract.extractor import extract_archives
from ..config import FOLDER_DOWNLOAD_CONCURRENCY, UPLOAD_FILE_CONCURRENCY
from ..providers import PROVIDERS
from ..providers.base import ProviderFailure, close_shared_clients, safe_name
from ..security import redact
from ..utils.image_optimizer import VIDEO_EXTENSIONS
from ..utils.temp_storage import cleanup_job, job_dirs
from .progress import JobCancelled, JobState

CONFIRM_TIMEOUT_SECONDS = 120

async def run_transfer(job: JobState) -> None:
    dirs = job_dirs(job.job_id)
    payload = job.payload
    try:
        job.set(status="running", step="downloading")
        source = payload["source"]
        target = payload["target"]
        options = payload.get("options") or {}
        src = PROVIDERS[str(source.get("provider") or "").lower()]
        dst = PROVIDERS[str(target.get("provider") or "").lower()]
        job.files_downloaded = 0
        job.files_to_download = 0
        job.files_uploaded = 0
        job.files_skipped = 0
        job.files_to_upload = 0

        job.log(f"Job start: {source.get('provider')} -> {target.get('provider')}")
        job.log(f"Accounts: source={source.get('accountId') or source.get('account_id') or '-'} target={target.get('accountId') or target.get('account_id') or '-'}")

        if options.get("optimize_image") and len(source.get("items") or []) > 1:
            await _run_optimized_batches(job, dirs, source, target, options, src, dst)
            job.log(f"Done: Downloaded {job.files_downloaded}/{job.files_to_download} file(s), Uploaded {job.files_uploaded}/{job.files_to_upload} file(s) (skipped {job.files_skipped} file(s))")
            job.set(status="completed", step="completed")
            return

        # Count individual files in the source list beforehand to prevent incorrect [index/index] counts
        file_items = [item for item in (source.get("items") or []) if not (item.get("type") == "folder" or item.get("is_folder"))]
        if options.get("optimize_image"):
            skipped_videos = [item for item in file_items if _is_video_item(item)]
            for item in skipped_videos:
                job.files_skipped += 1
                job.log(f"[SKIP] Video ignored by image optimizer: {item.get('name') or item.get('id') or 'file'}")
            file_items = [item for item in file_items if not _is_video_item(item)]
        job.files_to_download = len(file_items)

        downloaded: list[Path] = []
        has_folder_source = False
        sem = asyncio.Semaphore(max(1, FOLDER_DOWNLOAD_CONCURRENCY))

        async def download_one(item: dict[str, Any]) -> Path:
            async with sem:
                job.check_cancelled()
                return await src.download_file(source.get("credentials") or {}, item, dirs["input"], job)

        for item in source.get("items") or []:
            job.check_cancelled()
            item_type = item.get("type") or ("folder" if item.get("is_folder") else "file")
            if item_type == "folder":
                has_folder_source = True
                raw_name = str(item.get("name") or item.get("path") or item.get("id") or "folder").replace("\\", "/")
                folder_dir = dirs["input"] / safe_name(PurePosixPath(raw_name).name or raw_name)
                folder_dir.mkdir(parents=True, exist_ok=True)
                downloaded.extend(await src.download_folder(source.get("credentials") or {}, item, folder_dir, job))
            else:
                continue
        downloaded.extend(await asyncio.gather(*(download_one(item) for item in file_items)))
        if not downloaded and options.get("optimize_image") and job.files_skipped:
            job.log("No image files found for optimization.")
            job.set(status="completed", step="completed")
            return
        if not downloaded:
            raise ProviderFailure("SOURCE_FILE_NOT_FOUND", "No source files downloaded")
        stray = [p for p in downloaded if not p.is_relative_to(dirs["input"])]
        if stray:
            raise ProviderFailure("DOWNLOAD_FAILED", "Downloaded files landed outside the job directory", {"paths": [str(p) for p in stray[:5]]})
        missing = [p for p in downloaded if not p.is_file()]
        if missing:
            raise ProviderFailure("DOWNLOAD_FAILED", "Downloaded files missing on disk", {"paths": [str(p) for p in missing[:5]]})
        job.log(f"Downloaded files: {len(downloaded)}")

        out_root = dirs["input"]
        outputs = downloaded
        if options.get("extract"):
            job.log("Extract enabled: scanning downloaded files for archives...")
            outputs = await extract_archives(dirs["input"], dirs["output"], job, _archive_passwords(options), bool(options.get("deleteArchiveAfterExtract")))
            out_root = dirs["output"]
            job.log(f"Extract stage output files: {len(outputs)}")

        if options.get("optimize_image"):
            job.set(step="optimizing")
            job.log("Starting image optimization...")
            from ..utils.image_optimizer import optimize_directory
            opt_dest = dirs["output"] / "optimized"
            opt_dest.mkdir(parents=True, exist_ok=True)
            job.optimized_files = await asyncio.to_thread(
                optimize_directory, out_root, opt_dest, options, job,
                cancel_check=job.check_cancelled,
            )
            out_root = opt_dest
            outputs = [p for p in out_root.rglob("*") if p.is_file()]
            
            # Only ask for confirmation if there are actual optimized image results
            if job.optimized_files:
                conf_mode = options.get("confirm_action") or options.get("confirmation_mode")
                if conf_mode == "replace":
                    action = "replace"
                    job.log("Confirmation strategy: replace original files directly.")
                elif conf_mode in ("upload_new", "auto"):
                    action = "upload_new"
                    options["_auto_confirm_upload_new"] = True
                    job.log("Confirmation strategy: auto upload as new (fallback to replace if failed).")
                else:
                    action = _wait_for_confirmation(job, len(job.optimized_files))

                if action == "replace":
                    options["replace"] = True
                elif action == "upload_new":
                    options["replace"] = False
                    options["upload_prefix"] = "results"
                    if has_folder_source:
                        options["upload_prefix"] = f"results/{_selected_folder_name(source)} (optimized)"
            else:
                job.log("No image files found for optimization, skipping confirmation.")
                
            job.set(status="running", step="uploading")
        else:
            job.set(step="uploading")
        preserve_tree = bool(options.get("preserveFolderStructure") or has_folder_source)
        if len(outputs) == 1 and outputs[0].is_file() and not preserve_tree:
            job.files_to_upload = 1
            result = await _upload_one_with_retry(job, target, options, dst, outputs[0])
        else:
            upload_root = out_root
            if options.get("optimize_image") and has_folder_source:
                folder_root = _only_child_dir(out_root)
                if folder_root:
                    upload_root = folder_root
            if out_root == dirs["input"] and len(outputs) == 1 and not preserve_tree:
                job.files_to_upload = 1
                result = await _upload_one_with_retry(job, target, options, dst, outputs[0])
            else:
                if not any(p.is_file() for p in upload_root.rglob("*")):
                    raise ProviderFailure("UPLOAD_FAILED", "No files staged for upload", {"root": str(upload_root)})
                if options.get("optimize_image"):
                    result = await _upload_outputs_with_retry(job, target, options, dst, upload_root, {})
                else:
                    result = await dst.upload_folder(target.get("credentials") or {}, upload_root, _upload_target(target.get("folder") or {}, "", options), job)
        job.log(f"Done: Downloaded {job.files_downloaded}/{job.files_to_download} file(s), Uploaded {job.files_uploaded}/{job.files_to_upload} file(s) (skipped {job.files_skipped} file(s))")
        job.set(status="completed", step="completed")
    except JobCancelled:
        job.error = {"code": "JOB_CANCELLED", "message": "Job cancelled", "details": {}}
        job.set(status="cancelled", step="cancelled")
    except ProviderFailure as exc:
        job.error = {"code": exc.code, "message": exc.message, "details": exc.details}
        job.log(f"Failed: {exc.code} {exc.message}")
        job.set(status="failed", step="failed")
    except Exception as exc:
        job.error = {"code": "TRANSFER_FAILED", "message": str(exc), "details": {"type": exc.__class__.__name__}}
        job.log(f"Failed: {exc}")
        job.set(status="failed", step="failed")
    finally:
        # Nested finally: an await here can be interrupted by cancellation, and the credential
        # scrubbing below must run either way.
        try:
            await close_shared_clients()
        finally:
            if job.status == "completed" and (payload.get("options") or {}).get("cleanupAfterFinish", True):
                cleanup_job(job.job_id)
            # Drop provider credential refs after run.
            payload.get("source", {}).pop("credentials", None)
            payload.get("target", {}).pop("credentials", None)


def _upload_target(folder: dict[str, Any], relative_path: str, options: dict[str, Any]) -> dict[str, Any]:
    prefix = str(options.get("upload_prefix") or "").strip("/")
    if not prefix:
        return {**folder, **({"relative_path": relative_path} if relative_path else {})}
    return {**folder, "relative_path": f"{prefix}/{relative_path}".rstrip("/")}

async def _run_optimized_batches(job: JobState, dirs: dict[str, Path], source: dict[str, Any], target: dict[str, Any], options: dict[str, Any], src: Any, dst: Any) -> None:
    from ..utils.image_optimizer import optimize_directory

    action: str | None = None
    for index, item in enumerate(source.get("items") or []):
        job.check_cancelled()
        item_type = item.get("type") or ("folder" if item.get("is_folder") else "file")
        if item_type != "folder" and _is_video_item(item):
            job.files_skipped += 1
            job.log(f"[SKIP] Video ignored by image optimizer: {item.get('name') or item.get('id') or 'file'}")
            job.completed_items.append(_queue_item_ref(source, item))
            continue

        batch_input = dirs["input"] / f"batch-{index}"
        batch_output = dirs["output"] / f"batch-{index}" / "optimized"
        batch_input.mkdir(parents=True, exist_ok=True)
        batch_output.mkdir(parents=True, exist_ok=True)
        job.set(status="running", step="downloading")
        downloaded = await _download_batch_item(job, source, src, item, batch_input)
        if not downloaded:
            job.log(f"No image files found for optimization: {_item_name(item)}")
            shutil.rmtree(batch_input, ignore_errors=True)
            shutil.rmtree(batch_output.parent, ignore_errors=True)
            job.completed_items.append(_queue_item_ref(source, item))
            continue
        _validate_downloads(downloaded, batch_input)
        job.log(f"Downloaded files: {len(downloaded)}")

        optimize_input = batch_input
        if options.get("extract"):
            job.log("Extract enabled: scanning downloaded files for archives...")
            batch_extract = dirs["output"] / f"batch-{index}" / "extracted"
            outputs = await extract_archives(batch_input, batch_extract, job, _archive_passwords(options), bool(options.get("deleteArchiveAfterExtract")))
            optimize_input = batch_extract
            job.log(f"Extract stage output files: {len(outputs)}")

        job.set(step="optimizing")
        job.log(f"Starting image optimization: {_item_name(item)}")
        batch_results = await asyncio.to_thread(
            optimize_directory, optimize_input, batch_output, options, job,
            cancel_check=job.check_cancelled,
        )
        job.optimized_files.extend(batch_results)
        batch_files = [p for p in batch_output.rglob("*") if p.is_file()]
        if not batch_results and not batch_files:
            job.log(f"No image files found for optimization: {_item_name(item)}")
            shutil.rmtree(batch_input, ignore_errors=True)
            shutil.rmtree(batch_output.parent, ignore_errors=True)
            job.completed_items.append(_queue_item_ref(source, item))
            continue

        if batch_results and action is None:
            conf_mode = options.get("confirm_action") or options.get("confirmation_mode")
            if conf_mode == "replace":
                action = "replace"
                job.log("Confirmation strategy: replace original files directly.")
            elif conf_mode in ("upload_new", "auto"):
                action = "upload_new"
                options["_auto_confirm_upload_new"] = True
                job.log("Confirmation strategy: auto upload as new (fallback to replace if failed).")
            else:
                action = _wait_for_confirmation(job, len(batch_results))
        action = action or ("replace" if options.get("replace") else "upload_new")
        options["replace"] = action == "replace"
        options.pop("upload_prefix", None)
        if action == "upload_new":
            options["upload_prefix"] = "results" if item_type != "folder" else f"results/{_item_name(item)} (optimized)"

        upload_root = batch_output
        if item_type == "folder":
            folder_root = _only_child_dir(batch_output)
            if folder_root:
                upload_root = folder_root
        job.set(status="running", step="uploading")
        await _upload_outputs_with_retry(job, target, options, dst, upload_root, item)
        shutil.rmtree(batch_input, ignore_errors=True)
        shutil.rmtree(batch_output.parent, ignore_errors=True)
        job.completed_items.append(_queue_item_ref(source, item))

    if not job.optimized_files and job.files_skipped:
        job.log("No image files found for optimization.")

async def _download_batch_item(job: JobState, source: dict[str, Any], src: Any, item: dict[str, Any], batch_input: Path) -> list[Path]:
    item_type = item.get("type") or ("folder" if item.get("is_folder") else "file")
    if item_type == "folder":
        raw_name = str(item.get("name") or item.get("path") or item.get("id") or "folder").replace("\\", "/")
        folder_dir = batch_input / safe_name(PurePosixPath(raw_name).name or raw_name)
        folder_dir.mkdir(parents=True, exist_ok=True)
        return await src.download_folder(source.get("credentials") or {}, item, folder_dir, job)
    job.files_to_download += 1
    return [await src.download_file(source.get("credentials") or {}, item, batch_input, job)]

async def _upload_outputs(job: JobState, target: dict[str, Any], options: dict[str, Any], dst: Any, upload_root: Path, item: dict[str, Any]) -> None:
    files = [p for p in upload_root.rglob("*") if p.is_file()]
    if not files:
        raise ProviderFailure("UPLOAD_FAILED", "No files staged for upload", {"root": str(upload_root)})
    item_type = item.get("type") or ("folder" if item.get("is_folder") else "file")
    if len(files) == 1 and item_type != "folder":
        job.files_to_upload += 1
        job._upload_log_done = 0
        job._upload_log_total = 1
        await _upload_path_with_retry(job, target, options, dst, files[0], files[0].name, item)
        return
    job.files_to_upload += len(files)
    job._upload_log_done = 0
    job._upload_log_total = len(files)
    workers = max(1, min(UPLOAD_FILE_CONCURRENCY, len(files)))
    gate = _UploadGate()
    sem = asyncio.Semaphore(workers)
    job.log(f"Uploading {len(files)} file(s), {workers} in parallel")

    async def one(path: Path) -> None:
        async with sem:
            if gate.abort is not None:
                return
            rel = path.relative_to(upload_root).as_posix()
            await _upload_path_with_retry(job, target, options, dst, path, rel, item, gate)

    results = await asyncio.gather(*(one(p) for p in sorted(files)), return_exceptions=True)
    if gate.abort is not None:
        raise gate.abort
    for outcome in results:
        if isinstance(outcome, BaseException):
            raise outcome

class _UploadGate:
    """Shared stop gate for the parallel uploaders of one batch.

    On UPLOAD_FAILED every worker parks on `resume`; exactly one runs the recovery
    (auto-replace fallback, or the wait for another target account) and bumps
    `generation`, so workers that failed against the old account just retry
    instead of each asking the user again.
    """

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.resume = asyncio.Event()
        self.resume.set()
        self.generation = 0
        self.abort: BaseException | None = None

async def _recover_upload(job: JobState, target: dict[str, Any], options: dict[str, Any], exc: ProviderFailure, gate: _UploadGate | None, attempt_gen: int) -> bool:
    """Run one recovery round. Returns True when the caller should retry the file."""
    if gate is None:
        if _fallback_auto_upload_new_to_replace(job, options, exc):
            return True
        await _wait_for_retry_account(job, target, exc)
        return True
    gate.resume.clear()
    async with gate.lock:
        if gate.abort is not None:
            return False
        if gate.generation != attempt_gen:
            gate.resume.set()
            return True  # recovered by another worker while this upload was in flight.
        try:
            if not _fallback_auto_upload_new_to_replace(job, options, exc):
                await _wait_for_retry_account(job, target, exc)
        except BaseException as err:
            gate.abort = err
            return False
        finally:
            gate.generation += 1
            gate.resume.set()
    return True

async def _upload_path_with_retry(job: JobState, target: dict[str, Any], options: dict[str, Any], dst: Any, path: Path, rel: str, item: dict[str, Any], gate: _UploadGate | None = None) -> None:
    while True:
        attempt_gen = 0
        if gate is not None:
            await gate.resume.wait()
            if gate.abort is not None:
                return
            attempt_gen = gate.generation
        folder = _item_target_folder(target.get("folder") or {}, item)
        target_ref = _upload_target(folder, rel, options)
        try:
            await dst.upload_file(target.get("credentials") or {}, path, target_ref, job)
            job.files_uploaded += 1
            job.error = None
            job._upload_log_done = getattr(job, "_upload_log_done", 0) + 1
            job.log(f"[{job._upload_log_done}/{getattr(job, '_upload_log_total', job.files_to_upload)}] Uploaded: {path.name}")
            return
        except ProviderFailure as exc:
            auto_replace = bool(options.get("_auto_confirm_upload_new")) and not options.get("replace")
            if not auto_replace:
                if "duplicated" in exc.message.lower() or "repeated" in exc.message.lower():
                    job.files_skipped += 1
                    job._upload_log_done = getattr(job, "_upload_log_done", 0) + 1
                    job.log(f"[{job._upload_log_done}/{getattr(job, '_upload_log_total', job.files_to_upload)}] Skipped (duplicate): {path.name}")
                    return
                if exc.code != "UPLOAD_FAILED":
                    raise
            if not await _recover_upload(job, target, options, exc, gate, attempt_gen):
                if gate is not None and gate.abort is not None:
                    return
                raise

async def _upload_one_with_retry(job: JobState, target: dict[str, Any], options: dict[str, Any], dst: Any, path: Path) -> dict[str, Any]:
    while True:
        try:
            result = await dst.upload_file(target.get("credentials") or {}, path, _upload_target(target.get("folder") or {}, path.name, options), job)
            job.files_uploaded = 1
            job.error = None
            job.log(f"[1/1] Uploaded: {path.name}")
            return result
        except ProviderFailure as exc:
            if _fallback_auto_upload_new_to_replace(job, options, exc):
                continue
            if "duplicated" in exc.message.lower() or "repeated" in exc.message.lower():
                job.files_skipped = 1
                job.log(f"[1/1] Skipped (duplicate): {path.name}")
                return {"ok": True, "uploaded": 0, "skipped": 1, "items": []}
            if exc.code != "UPLOAD_FAILED":
                raise
            await _wait_for_retry_account(job, target, exc)

async def _upload_outputs_with_retry(job: JobState, target: dict[str, Any], options: dict[str, Any], dst: Any, upload_root: Path, item: dict[str, Any]) -> None:
    while True:
        try:
            await _upload_outputs(job, target, options, dst, upload_root, item)
            job.error = None
            return
        except ProviderFailure as exc:
            if exc.code != "UPLOAD_FAILED":
                raise
            if _fallback_auto_upload_new_to_replace(job, options, exc):
                continue
            await _wait_for_retry_account(job, target, exc)
            continue

def _fallback_auto_upload_new_to_replace(job: JobState, options: dict[str, Any], exc: ProviderFailure) -> bool:
    if not options.get("_auto_confirm_upload_new") or options.get("replace"):
        return False
    options["_auto_confirm_upload_new"] = False
    options["replace"] = True
    options.pop("upload_prefix", None)
    job.log(f"Auto upload_new failed ({exc.message}); retrying with replace.")
    return True

async def _wait_for_retry_account(job: JobState, target: dict[str, Any], exc: ProviderFailure) -> None:
    job.error = {"code": exc.code, "message": exc.message, "details": exc.details}
    job.log(f"Upload failed: {exc.message}. Waiting for another target account...")
    job.confirm_action = None
    job.confirm_event.clear()
    job.set(status="waiting_target_account", step="uploading")
    # Poll instead of Event.wait(): this coroutine shares its loop with the other uploaders.
    while not job.confirm_event.is_set():
        job.check_cancelled()
        await asyncio.sleep(0.05)
    if job.confirm_action == "cancel":
        raise JobCancelled()
    if job.confirm_action != "retry_upload":
        raise exc
    new_target = dict(job.payload.get("target") or {})
    target.clear()
    target.update(new_target)
    job.set(status="running", step="uploading")

def _wait_for_confirmation(job: JobState, count: int) -> str:
    job.set(status="waiting_confirmation", step="optimized")
    job.log(f"Optimization finished: processed {count} images. Waiting for confirmation...")
    deadline = time.monotonic() + CONFIRM_TIMEOUT_SECONDS
    while not job.confirm_event.wait(timeout=1.0):
        job.check_cancelled()
        if time.monotonic() >= deadline:
            job.confirm_action = "upload_new"
            (job.payload.get("options") or {})["_auto_confirm_upload_new"] = True
            job.log("Confirmation timeout after 120s; auto action=upload_new")
            break
    job.log(f"User confirmation received: action={job.confirm_action}")
    if job.confirm_action == "cancel":
        raise JobCancelled()
    return job.confirm_action or "upload_new"

def _validate_downloads(downloaded: list[Path], root: Path) -> None:
    stray = [p for p in downloaded if not p.is_relative_to(root)]
    if stray:
        raise ProviderFailure("DOWNLOAD_FAILED", "Downloaded files landed outside the job directory", {"paths": [str(p) for p in stray[:5]]})
    missing = [p for p in downloaded if not p.is_file()]
    if missing:
        raise ProviderFailure("DOWNLOAD_FAILED", "Downloaded files missing on disk", {"paths": [str(p) for p in missing[:5]]})

def _item_target_folder(folder: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    item_type = item.get("type") or ("folder" if item.get("is_folder") else "file")
    raw = str(item.get("path") or item.get("id") or "")
    if item_type == "folder" and raw:
        return {**folder, "id": raw, "path": raw}
    if raw:
        parent = str(PurePosixPath(raw).parent)
        if parent and parent != ".":
            return {**folder, "id": parent, "path": parent}
    return folder

def _item_name(item: dict[str, Any]) -> str:
    raw_name = str(item.get("name") or item.get("path") or item.get("id") or "item").replace("\\", "/")
    return safe_name(PurePosixPath(raw_name).name or raw_name)

def _queue_item_ref(source: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": item.get("provider") or (item.get("meta") or {}).get("provider") or source.get("provider"),
        "id": item.get("id") or item.get("path"),
        "path": item.get("path") or item.get("id"),
    }

def _is_video_item(item: dict[str, Any]) -> bool:
    return Path(str(item.get("name") or item.get("path") or item.get("id") or "")).suffix.lower() in VIDEO_EXTENSIONS

def _archive_passwords(options: dict[str, Any]) -> Any:
    return options.get("archive_passwords") or options.get("archivePasswords") or options.get("archivePassword") or options.get("archive_password")

def _selected_folder_name(source: dict[str, Any]) -> str:
    folder = next((item for item in source.get("items") or [] if item.get("type") == "folder" or item.get("is_folder")), {})
    raw_name = str(folder.get("name") or folder.get("path") or folder.get("id") or "folder").replace("\\", "/")
    return safe_name(PurePosixPath(raw_name).name or raw_name)

def _only_child_dir(path: Path) -> Path | None:
    children = [child for child in path.iterdir()]
    return children[0] if len(children) == 1 and children[0].is_dir() else None

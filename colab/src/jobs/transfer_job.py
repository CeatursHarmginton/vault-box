from __future__ import annotations

import asyncio
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from ..extract.extractor import extract_archives
from ..config import FOLDER_DOWNLOAD_CONCURRENCY
from ..providers import PROVIDERS
from ..providers.base import ProviderFailure, safe_name
from ..security import redact
from ..utils.image_optimizer import VIDEO_EXTENSIONS
from ..utils.temp_storage import cleanup_job, job_dirs
from .progress import JobCancelled, JobState

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
                # Wait for user confirmation (threading.Event — poll with cancel check)
                job.set(status="waiting_confirmation", step="optimized")
                job.log(f"Optimization finished: processed {len(job.optimized_files)} images. Waiting for confirmation...")
                while not job.confirm_event.wait(timeout=1.0):
                    job.check_cancelled()
                
                job.log(f"User confirmation received: action={job.confirm_action}")
                if job.confirm_action == "cancel":
                    raise JobCancelled()
                
                if job.confirm_action == "replace":
                    options["replace"] = True
                elif job.confirm_action == "upload_new":
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
        before_total, before_uploaded, before_skipped = job.files_to_upload, job.files_uploaded, job.files_skipped
        await _upload_outputs_with_retry(job, target, options, dst, upload_root, item)
        job.files_to_upload += before_total
        job.files_uploaded += before_uploaded
        job.files_skipped += before_skipped
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
    folder = _item_target_folder(target.get("folder") or {}, item)
    item_type = item.get("type") or ("folder" if item.get("is_folder") else "file")
    if len(files) == 1 and item_type != "folder":
        job.files_to_upload += 1
        await dst.upload_file(target.get("credentials") or {}, files[0], _upload_target(folder, files[0].name, options), job)
        job.files_uploaded += 1
        job.log(f"[{job.files_uploaded}/{job.files_to_upload}] Uploaded: {files[0].name}")
        return
    await dst.upload_folder(target.get("credentials") or {}, upload_root, _upload_target(folder, "", options), job)

async def _upload_one_with_retry(job: JobState, target: dict[str, Any], options: dict[str, Any], dst: Any, path: Path) -> dict[str, Any]:
    while True:
        try:
            result = await dst.upload_file(target.get("credentials") or {}, path, _upload_target(target.get("folder") or {}, path.name, options), job)
            job.files_uploaded = 1
            job.error = None
            job.log(f"[1/1] Uploaded: {path.name}")
            return result
        except ProviderFailure as exc:
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
            await _wait_for_retry_account(job, target, exc)
            continue

async def _wait_for_retry_account(job: JobState, target: dict[str, Any], exc: ProviderFailure) -> None:
    job.error = {"code": exc.code, "message": exc.message, "details": exc.details}
    job.log(f"Upload failed: {exc.message}. Waiting for another target account...")
    job.confirm_action = None
    job.confirm_event.clear()
    job.set(status="waiting_target_account", step="uploading")
    while not job.confirm_event.wait(timeout=1.0):
        job.check_cancelled()
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
    while not job.confirm_event.wait(timeout=1.0):
        job.check_cancelled()
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

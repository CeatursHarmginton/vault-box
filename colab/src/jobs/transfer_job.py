from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Any

from ..extract.extractor import extract_archives, is_archive_name
from ..config import FOLDER_DOWNLOAD_CONCURRENCY, IS_KAGGLE, UPLOAD_FILE_CONCURRENCY
from ..providers import PROVIDERS
from ..providers.base import ProviderFailure, close_shared_clients, download_with_retry, is_skippable_download_failure, safe_name
from ..security import redact
from ..utils.image_optimizer import VIDEO_EXTENSIONS
from ..utils.temp_storage import cleanup_job, job_dirs
from .progress import JobCancelled, JobState

CONFIRM_TIMEOUT_SECONDS = 120
COLAB_DEFAULT_FREE_BYTES = 80 * 1024 ** 3

def _should_process_video_thumbnails(options: dict[str, Any], payload: dict[str, Any] | None = None) -> bool:
    payload = payload or {}
    is_opt = bool(
        options.get("optimize_image")
        or options.get("is_optimize_page")
        or payload.get("is_optimize_page")
        or payload.get("job_type") == "optimize"
        or payload.get("mode") == "optimize"
    )
    if is_opt:
        return False
    return bool(options.get("set_video_thumbnail", False) or options.get("trim_intro", False))

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

        items = source.get("items") or []
        has_folder_batch = any((item.get("type") == "folder" or item.get("is_folder")) for item in items)
        has_mixed_source_scope = len({_item_scope(source, item) for item in items}) > 1
        if options.get("optimize_image") and len(items) > 1:
            await _run_optimized_batches(job, dirs, source, target, options, src, dst)
            job.log(f"Done: Downloaded {job.files_downloaded}/{job.files_to_download} file(s), Uploaded {job.files_uploaded}/{job.files_to_upload} file(s) (skipped {job.files_skipped} file(s))")
            _log_skip_summary(job)
            if job.failed_items and not job.completed_items:
                raise ProviderFailure("DOWNLOAD_FAILED", f"All {len(job.failed_items)} item(s) skipped after retries", {"failedItems": job.failed_items[:5]})
            job.set(status="completed", step="completed")
            return

        # Count individual files in the source list beforehand to prevent incorrect [index/index] counts
        file_items = [item for item in (source.get("items") or []) if not (item.get("type") == "folder" or item.get("is_folder"))]
        if options.get("optimize_image"):
            skipped_videos = [item for item in file_items if _is_video_item(item)]
            for item in skipped_videos:
                job.files_skipped += 1
                _record_optimized_skip(job, item, "Skipped")
                job.log(f"[SKIP] Video ignored by image optimizer: {item.get('name') or item.get('id') or 'file'}")
            skipped_archives = [item for item in file_items if not options.get("extract") and _is_archive_item(item)]
            for item in skipped_archives:
                job.files_skipped += 1
                _record_optimized_skip(job, item, "Skipped")
                job.log(f"[SKIP] Archive ignored by image optimizer: {item.get('name') or item.get('id') or 'file'}")
            file_items = [item for item in file_items if not _is_video_item(item) and not (not options.get("extract") and _is_archive_item(item))]
        job.files_to_download = len(file_items)

        strategy = str(options.get("transfer_strategy") or options.get("transferStrategy") or "").lower()
        has_backup_folder = any(
            str(it.get("name") or it.get("path") or it.get("id") or "").endswith("__backup_resume")
            for it in items
        )
        if options.get("resume_from_backup") or has_backup_folder:
            strategy = "backup_resume"

        if strategy:
            job.log(f"Transfer strategy: {strategy}")

        if not options.get("optimize_image"):
            if strategy in ("backup_resume", "backup", "resume"):
                await _run_backup_resume_batches(job, dirs, source, target, options, src, dst, items)
                job.log(f"Done: Downloaded {job.files_downloaded}/{job.files_to_download} file(s), Uploaded {job.files_uploaded}/{job.files_to_upload} file(s) (skipped {job.files_skipped} file(s))")
                _log_skip_summary(job)
                if job.failed_items and not job.completed_items:
                    raise ProviderFailure("DOWNLOAD_FAILED", f"All {len(job.failed_items)} item(s) skipped after retries", {"failedItems": job.failed_items[:5]})
                job.set(status="completed", step="completed")
                return

            if strategy in ("file_by_file", "file", "streaming", "pipeline") and len(items) > 1:
                await _run_file_pipeline_batches(job, dirs, source, target, options, src, dst, items)
                job.log(f"Done: Downloaded {job.files_downloaded}/{job.files_to_download} file(s), Uploaded {job.files_uploaded}/{job.files_to_upload} file(s) (skipped {job.files_skipped} file(s))")
                _log_skip_summary(job)
                if job.failed_items and not job.completed_items:
                    raise ProviderFailure("DOWNLOAD_FAILED", f"All {len(job.failed_items)} item(s) skipped after retries", {"failedItems": job.failed_items[:5]})
                job.set(status="completed", step="completed")
                return

            if strategy in ("smart_folder", "folder") and (has_folder_batch or len(items) > 1):
                await _run_smart_folder_batches(job, dirs, source, target, options, src, dst, items)
                job.log(f"Done: Downloaded {job.files_downloaded}/{job.files_to_download} file(s), Uploaded {job.files_uploaded}/{job.files_to_upload} file(s) (skipped {job.files_skipped} file(s))")
                _log_skip_summary(job)
                if job.failed_items and not job.completed_items:
                    raise ProviderFailure("DOWNLOAD_FAILED", f"All {len(job.failed_items)} item(s) skipped after retries", {"failedItems": job.failed_items[:5]})
                job.set(status="completed", step="completed")
                return

        if not options.get("optimize_image") and not has_folder_batch and len(file_items) > 1 and _needs_download_batches(file_items, dirs["input"], options):
            await _run_plain_file_batches(job, dirs, source, target, options, src, dst, file_items)
            job.log(f"Done: Downloaded {job.files_downloaded}/{job.files_to_download} file(s), Uploaded {job.files_uploaded}/{job.files_to_upload} file(s) (skipped {job.files_skipped} file(s))")
            _log_skip_summary(job)
            if job.failed_items and not job.completed_items:
                raise ProviderFailure("DOWNLOAD_FAILED", f"All {len(job.failed_items)} item(s) skipped after retries", {"failedItems": job.failed_items[:5]})
            job.set(status="completed", step="completed")
            return

        downloaded: list[Path] = []
        has_folder_source = False
        is_links_provider = (
            str(source.get("provider") or "").lower() == "links"
            or any(str(i.get("provider") or (i.get("meta") or {}).get("provider") or source.get("provider") or "").lower() == "links" for i in file_items)
        )
        default_concurrency = 1 if is_links_provider else FOLDER_DOWNLOAD_CONCURRENCY
        concurrency = int(options.get("download_concurrency") or default_concurrency)
        sem = asyncio.Semaphore(max(1, concurrency))

        async def download_one(item: dict[str, Any]) -> list[Path]:
            async with sem:
                job.check_cancelled()
                item_name = _item_name(item)
                item_size = _item_size(item)
                item_k = _queue_item_key(source, item)

                if item_name:
                    safe_cand = safe_name(item_name)
                    cand_paths = [dirs["input"] / safe_cand]
                    if not safe_cand.lower().endswith((".mp4", ".mkv", ".webm", ".ts")):
                        cand_paths.append(dirs["input"] / f"{safe_cand}.mp4")
                    for existing in cand_paths:
                        if existing.is_file() and existing.stat().st_size > 0:
                            part_files = list(dirs["input"].glob(f"{existing.name}*.part*")) + list(dirs["input"].glob(f".tmp_ytdlp_*{existing.stem[:8]}*"))
                            if not part_files:
                                job.log(f"[Cache-Hit] File already downloaded in workspace: {existing.name} ({existing.stat().st_size} bytes). Skipping re-download.")
                                job.start_file(item_name, phase="download", size=existing.stat().st_size, key=item_k)
                                job.finish_file(item_name, phase="download", size=existing.stat().st_size, key=item_k)
                                if item_k != item_name:
                                    job.file_sizes[item_k] = existing.stat().st_size
                                job.files_downloaded += 1
                                _remember_source_ref(job, existing, item)
                                return [existing]

                job.start_file(item_name, phase="download", size=item_size, key=item_k)
                job.start_item(item_k, name=item_name)
                item_prov = str(item.get("provider") or (item.get("meta") or {}).get("provider") or source.get("provider") or "").lower()
                item_src = PROVIDERS.get(item_prov, src)
                item_creds = item.get("credentials") or source.get("credentials") or {}
                try:
                    path = await download_with_retry(
                        lambda: item_src.download_file(item_creds, item, dirs["input"], job),
                        progress=job, label=item_name,
                    )
                    actual_size = path.stat().st_size if (path and path.exists()) else item_size
                    job.finish_file(item_name, phase="download", size=actual_size, key=item_k)
                    if item_k != item_name:
                        job.file_sizes[item_k] = actual_size
                    job.files_downloaded += 1
                    job.downloaded_files.add(item_name)
                    job.save_state()
                    job.log(f"[{job.files_downloaded}/{job.files_to_download or len(file_items)}] Downloaded: {item_name}")
                    _remember_source_ref(job, path, item)
                    return [path]
                except ProviderFailure as exc:
                    job.finish_file(item_name, phase="download", key=item_k)
                    if not is_skippable_download_failure(exc):
                        raise
                    _mark_item_skipped(job, source, item, exc.message)
                    return []
                except Exception:
                    job.finish_file(item_name, phase="download", key=item_k)
                    raise

        for item in source.get("items") or []:
            job.check_cancelled()
            item_type = item.get("type") or ("folder" if item.get("is_folder") else "file")
            if item_type == "folder":
                has_folder_source = True
                raw_name = str(item.get("name") or item.get("path") or item.get("id") or "folder").replace("\\", "/")
                folder_dir = dirs["input"] / safe_name(PurePosixPath(raw_name).name or raw_name)
                folder_dir.mkdir(parents=True, exist_ok=True)
                failed_before = job.files_failed
                item_prov = str(item.get("provider") or (item.get("meta") or {}).get("provider") or source.get("provider") or "").lower()
                item_src = PROVIDERS.get(item_prov, src)
                item_creds = item.get("credentials") or source.get("credentials") or {}
                try:
                    downloaded.extend(await item_src.download_folder(item_creds, item, folder_dir, job))
                except ProviderFailure as exc:
                    if not is_skippable_download_failure(exc):
                        raise
                    _mark_item_skipped(job, source, item, exc.message)
                    continue
                if job.files_failed > failed_before:
                    _mark_item_skipped(job, source, item, f"{job.files_failed - failed_before} file(s) unavailable after retries")
            else:
                continue
        if file_items:
            effective_concurrency = max(1, min(concurrency, len(file_items)))
            job.log(f"Downloading {len(file_items)} file(s), up to {effective_concurrency} in parallel")
            if str(source.get("provider") or "").lower() == "links":
                for batch in await asyncio.gather(*(download_one(item) for item in file_items)):
                    downloaded.extend(batch)
                downloaded = [
                    p for p in sorted(dirs["input"].rglob("*")) 
                    if p.is_file() 
                    and not p.name.endswith((".aria2", ".ytdl", ".part", ".tmp"))
                    and not (p.name.lower() in ("cookies.txt", "cookie.txt", "cookies.json") or p.name.lower().startswith("cookie"))
                    and ".part-Frag" not in p.name
                    and not p.name.startswith(".tmp")
                    and not p.name.startswith(".")
                    and p.stat().st_size > 0
                ]
            else:
                for batch in await asyncio.gather(*(download_one(item) for item in file_items)):
                    downloaded.extend(batch)
        if not downloaded and options.get("optimize_image") and job.files_skipped:
            job.log("No image files found for optimization.")
            _mark_remaining_items_completed(job, source)
            _log_skip_summary(job)
            job.set(status="completed", step="completed")
            return
        if not downloaded:
            if job.failed_items:
                raise ProviderFailure("DOWNLOAD_FAILED", f"All {len(job.failed_items)} item(s) skipped after retries; nothing downloaded", {"failedItems": job.failed_items[:5]})
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
            # Extract into its own subdir: the optimizer writes to a sibling ("optimized"), and
            # having its output nested inside its input would make it re-scan its own results.
            extract_root = dirs["output"] / "extracted"
            extract_root.mkdir(parents=True, exist_ok=True)
            outputs = await extract_archives(dirs["input"], extract_root, job, _archive_passwords(options), bool(options.get("deleteArchiveAfterExtract")))
            out_root = extract_root
            job.log(f"Extract stage output files: {len(outputs)}")

        if options.get("optimize_image"):
            job.set(step="optimizing")
            job.log("Starting image optimization...")
            from ..utils.image_optimizer import optimize_directory
            opt_input = out_root
            opt_dest = dirs["output"] / "optimized"
            opt_dest.mkdir(parents=True, exist_ok=True)
            job.optimized_files = await asyncio.to_thread(
                optimize_directory, opt_input, opt_dest, options, job,
                cancel_check=job.check_cancelled,
            )
            out_root = opt_dest
            outputs = [p for p in out_root.rglob("*") if p.is_file()]
            
            # Only ask for confirmation if there are actual optimized image results
            actual_compressed = [f for f in (job.optimized_files or []) if f.get("status") != "Skipped"]
            if actual_compressed:
                is_opt_page = bool(options.get("is_optimize_page") or payload.get("is_optimize_page") or payload.get("mode") == "optimize" or payload.get("job_type") == "optimize")
                conf_mode = options.get("confirm_action") or options.get("confirmation_mode")
                if conf_mode == "replace":
                    action = "replace"
                    job.log("Confirmation strategy: replace original files directly.")
                elif conf_mode in ("upload_new", "auto") or options.get("_auto_confirm_upload_new") or options.get("is_optimize_page") is False:
                    action = "upload_new"
                    options["_auto_confirm_upload_new"] = True
                    job.log("Confirmation strategy: auto upload as new (fallback to replace if failed).")
                else:
                    action = _wait_for_confirmation(job, len(actual_compressed))

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
        if _should_process_video_thumbnails(options, payload):
            try:
                from ..utils.video_thumbnail import process_video_thumbnails
                outputs = await asyncio.to_thread(
                    process_video_thumbnails,
                    outputs,
                    options,
                    job,
                )
            except Exception as exc:
                job.log(f"[Thumbnail] Warning during video thumbnailing: {exc}")
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
                    upload_item = {"_replace_refs": _optimized_replace_refs(job, job.optimized_files, out_root, upload_root, opt_input)}
                    result = await _upload_outputs_with_retry(job, target, options, dst, upload_root, upload_item)
                else:
                    while True:
                        try:
                            result = await dst.upload_folder(target.get("credentials") or {}, upload_root, _upload_target(target.get("folder") or {}, "", options), job)
                            job.error = None
                            break
                        except ProviderFailure as exc:
                            if exc.code != "UPLOAD_FAILED":
                                raise
                            if _fallback_auto_upload_new_to_replace(job, options, exc):
                                continue
                            await _wait_for_retry_account(job, target, exc)
                            continue
        _mark_remaining_items_completed(job, source)
        job.log(f"Done: Downloaded {job.files_downloaded}/{job.files_to_download} file(s), Uploaded {job.files_uploaded}/{job.files_to_upload} file(s) (skipped {job.files_skipped} file(s))")
        _log_skip_summary(job)
        job.set(status="completed", step="completed")
    except JobCancelled:
        job.error = {"code": "JOB_CANCELLED", "message": "Job cancelled", "details": {}}
        _mark_remaining_items_failed(job, source, "Cancelled")
        job.set(status="cancelled", step="cancelled")
    except ProviderFailure as exc:
        job.error = {"code": exc.code, "message": exc.message, "details": exc.details}
        job.log(f"Failed: {exc.code} {exc.message}")
        _mark_remaining_items_failed(job, source, f"{exc.code}: {exc.message}")
        job.set(status="failed", step="failed")
    except Exception as exc:
        err_msg = str(exc).strip()
        if isinstance(exc, (KeyError, IndexError, AttributeError, TypeError, ValueError)):
            err_msg = f"{exc.__class__.__name__}: {err_msg}" if err_msg else exc.__class__.__name__
        job.error = {"code": "TRANSFER_FAILED", "message": err_msg or "Unexpected transfer error", "details": {"type": exc.__class__.__name__}}
        job.log(f"Failed: {err_msg or exc.__class__.__name__}")
        _mark_remaining_items_failed(job, source, err_msg or exc.__class__.__name__)
        job.set(status="failed", step="failed")
    finally:
        # Nested finally: an await here can be interrupted by cancellation, and the credential
        # scrubbing below must run either way.
        try:
            await close_shared_clients()
        finally:
            try:
                from ..utils.video_thumbnail import clear_cached_thumbnails
                clear_cached_thumbnails()
            except Exception:
                pass
            if job.status == "completed" and (payload.get("options") or {}).get("cleanupAfterFinish", True):
                cleanup_job(job.job_id)
            # Drop provider credential refs after run.
            payload.get("source", {}).pop("credentials", None)
            payload.get("target", {}).pop("credentials", None)
            job.save_state()


def _upload_target(folder: dict[str, Any], relative_path: str, options: dict[str, Any]) -> dict[str, Any]:
    prefix = str(options.get("upload_prefix") or "").strip("/")
    if not prefix:
        return {**folder, **({"relative_path": relative_path} if relative_path else {})}
    return {**folder, "relative_path": f"{prefix}/{relative_path}".rstrip("/")}

async def _run_optimized_batches(job: JobState, dirs: dict[str, Path], source: dict[str, Any], target: dict[str, Any], options: dict[str, Any], src: Any, dst: Any) -> None:
    from ..utils.image_optimizer import optimize_directory

    action: str | None = None
    candidates: list[dict[str, Any]] = []
    for item in source.get("items") or []:
        item_ref = _queue_item_ref(source, item)
        item_k = _queue_item_key(source, item)
        item_name_str = _item_name(item)
        item_type = item.get("type") or ("folder" if item.get("is_folder") else "file")
        if item_type != "folder" and _is_video_item(item):
            job.files_skipped += 1
            _record_optimized_skip(job, item, "Skipped")
            job.log(f"[SKIP] Video ignored by image optimizer: {item.get('name') or item.get('id') or 'file'}")
            job.finish_item(item_k, status="skipped", name=item_name_str)
            timing = job.item_timings.get(item_k) or {}
            job.completed_items.append({
                **item_ref,
                "startTime": timing.get("startTime"),
                "endTime": timing.get("endTime"),
                "duration": timing.get("duration"),
            })
            continue
        if item_type != "folder" and not options.get("extract") and _is_archive_item(item):
            job.files_skipped += 1
            _record_optimized_skip(job, item, "Skipped")
            job.log(f"[SKIP] Archive ignored by image optimizer: {item.get('name') or item.get('id') or 'file'}")
            job.finish_item(item_k, status="skipped", name=item_name_str)
            timing = job.item_timings.get(item_k) or {}
            job.completed_items.append({
                **item_ref,
                "startTime": timing.get("startTime"),
                "endTime": timing.get("endTime"),
                "duration": timing.get("duration"),
            })
            continue

        candidates.append(item)

    groupable = not any((_item_type(item) == "folder") for item in candidates) and len({_item_scope(source, item) for item in candidates}) <= 1
    groups = _download_batches(candidates, dirs["input"], options, job) if groupable else [[item] for item in candidates]
    for index, batch_items in enumerate(groups):
        job.check_cancelled()
        for item in batch_items:
            job.start_item(_queue_item_key(source, item), name=_item_name(item))
        item = batch_items[0]
        item_type = _item_type(item) if len(batch_items) == 1 else "batch"
        batch_input = dirs["input"] / f"batch-{index}"
        batch_output = dirs["output"] / f"batch-{index}" / "optimized"
        batch_input.mkdir(parents=True, exist_ok=True)
        batch_output.mkdir(parents=True, exist_ok=True)
        job.set(status="running", step="downloading")
        failed_before = job.files_failed
        concurrency = int(options.get("download_concurrency") or FOLDER_DOWNLOAD_CONCURRENCY)
        sem = asyncio.Semaphore(max(1, concurrency))

        async def _download_one_opt(batch_item: dict[str, Any]) -> list[Path]:
            async with sem:
                try:
                    return await _download_batch_item(job, source, src, batch_item, batch_input)
                except ProviderFailure as exc:
                    # Retries are already exhausted inside the download; move to the next queue item
                    # and leave this one in the queue so it can be picked up again later.
                    if not is_skippable_download_failure(exc):
                        raise
                    _mark_item_skipped(job, source, batch_item, exc.message)
                    return []

        downloaded = []
        for batch in await asyncio.gather(*(_download_one_opt(batch_item) for batch_item in batch_items)):
            downloaded.extend(batch)
        item_failed = job.files_failed > failed_before
        if item_failed:
            for batch_item in batch_items:
                _mark_item_skipped(job, source, batch_item, f"{job.files_failed - failed_before} file(s) unavailable after retries")
        if not downloaded:
            job.log(f"No image files found for optimization: {_batch_name(batch_items)}")
            shutil.rmtree(batch_input, ignore_errors=True)
            shutil.rmtree(batch_output.parent, ignore_errors=True)
            if not item_failed:
                _finish_items(job, source, batch_items)
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
        job.log(f"Starting image optimization: {_batch_name(batch_items)}")
        batch_results = await asyncio.to_thread(
            optimize_directory, optimize_input, batch_output, options, job,
            cancel_check=job.check_cancelled,
        )
        job.optimized_files.extend(batch_results)
        batch_files = [p for p in batch_output.rglob("*") if p.is_file()]
        if not batch_results and not batch_files:
            job.log(f"No image files found for optimization: {_batch_name(batch_items)}")
            shutil.rmtree(batch_input, ignore_errors=True)
            shutil.rmtree(batch_output.parent, ignore_errors=True)
            if not item_failed:
                _finish_items(job, source, batch_items)
            continue

        if batch_results and action is None:
            is_opt_page = bool(options.get("is_optimize_page") or (job.payload or {}).get("is_optimize_page") or (job.payload or {}).get("mode") == "optimize" or (job.payload or {}).get("job_type") == "optimize")
            conf_mode = options.get("confirm_action") or options.get("confirmation_mode")
            if conf_mode == "replace":
                action = "replace"
                job.log("Confirmation strategy: replace original files directly.")
            elif conf_mode in ("upload_new", "auto") or options.get("_auto_confirm_upload_new") or options.get("is_optimize_page") is False:
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
        item_target, item_dst = _item_upload_target(source, target, dst, item)
        upload_item = {**item, "_replace_refs": _optimized_replace_refs(job, batch_results, batch_output, upload_root, optimize_input)}
        await _upload_outputs_with_retry(job, item_target, options, item_dst, upload_root, upload_item)
        shutil.rmtree(batch_input, ignore_errors=True)
        shutil.rmtree(batch_output.parent, ignore_errors=True)
        if not item_failed:
            _finish_items(job, source, [item for item in batch_items if not _item_is_failed(job, source, item)])

    if not job.optimized_files and job.files_skipped:
        job.log("No image files found for optimization.")

async def _run_plain_file_batches(job: JobState, dirs: dict[str, Path], source: dict[str, Any], target: dict[str, Any], options: dict[str, Any], src: Any, dst: Any, file_items: list[dict[str, Any]]) -> None:
    job.files_to_download = len(file_items)
    groups = _download_batches(file_items, dirs["input"], options, job)
    is_links_provider = (
        str(source.get("provider") or "").lower() == "links"
        or any(str(i.get("provider") or (i.get("meta") or {}).get("provider") or source.get("provider") or "").lower() == "links" for i in file_items)
    )
    default_concurrency = 1 if is_links_provider else FOLDER_DOWNLOAD_CONCURRENCY
    concurrency = int(options.get("download_concurrency") or default_concurrency)
    sem = asyncio.Semaphore(max(1, concurrency))

    async def download_one(item: dict[str, Any], batch_input: Path) -> list[Path]:
        async with sem:
            job.check_cancelled()
            item_name = _item_name(item)
            item_size = _item_size(item)
            item_k = _queue_item_key(source, item)

            if item_name:
                safe_cand = safe_name(item_name)
                cand_paths = [batch_input / safe_cand]
                if not safe_cand.lower().endswith((".mp4", ".mkv", ".webm", ".ts")):
                    cand_paths.append(batch_input / f"{safe_cand}.mp4")
                for existing in cand_paths:
                    if existing.is_file() and existing.stat().st_size > 0:
                        part_files = list(batch_input.glob(f"{existing.name}*.part*")) + list(batch_input.glob(f".tmp_ytdlp_*{existing.stem[:8]}*"))
                        if not part_files:
                            job.log(f"[Cache-Hit] File already downloaded in batch: {existing.name} ({existing.stat().st_size} bytes). Skipping re-download.")
                            job.start_file(item_name, phase="download", size=existing.stat().st_size, key=item_k)
                            job.finish_file(item_name, phase="download", size=existing.stat().st_size, key=item_k)
                            if item_k != item_name:
                                job.file_sizes[item_k] = existing.stat().st_size
                            job.files_downloaded += 1
                            _remember_source_ref(job, existing, item)
                            return [existing]

            job.start_file(item_name, phase="download", size=item_size, key=item_k)
            job.start_item(item_k, name=item_name)
            item_prov = str(item.get("provider") or (item.get("meta") or {}).get("provider") or source.get("provider") or "").lower()
            item_src = PROVIDERS.get(item_prov, src)
            item_creds = item.get("credentials") or source.get("credentials") or {}
            try:
                path = await download_with_retry(
                    lambda: item_src.download_file(item_creds, item, batch_input, job),
                    progress=job, label=item_name,
                )
                actual_size = path.stat().st_size if (path and path.exists()) else item_size
                job.finish_file(item_name, phase="download", size=actual_size, key=item_k)
                if item_k != item_name:
                    job.file_sizes[item_k] = actual_size
                job.files_downloaded += 1
                job.log(f"[{job.files_downloaded}/{job.files_to_download or len(file_items)}] Downloaded: {item_name}")
                _remember_source_ref(job, path, item)
                return [path]
            except ProviderFailure as exc:
                job.finish_file(item_name, phase="download", key=item_k)
                if not is_skippable_download_failure(exc):
                    raise
                _mark_item_skipped(job, source, item, exc.message)
                return []
            except Exception:
                job.finish_file(item_name, phase="download", key=item_k)
                raise

    for index, batch_items in enumerate(groups):
        batch_input = dirs["input"] / f"batch-{index}"
        batch_output = dirs["output"] / f"batch-{index}"
        batch_input.mkdir(parents=True, exist_ok=True)
        for item in batch_items:
            job.start_item(_queue_item_key(source, item), name=_item_name(item))
        job.set(status="running", step="downloading")
        effective_concurrency = max(1, min(concurrency, len(batch_items)))
        job.log(f"Downloading batch {index + 1}/{len(groups)} ({len(batch_items)} files), up to {effective_concurrency} in parallel")
        if str(source.get("provider") or "").lower() == "links":
            downloaded = [path for batch in await asyncio.gather(*(download_one(item, batch_input) for item in batch_items)) for path in batch]
            downloaded = [
                p for p in sorted(batch_input.rglob("*"))
                if p.is_file()
                and not p.name.endswith((".aria2", ".ytdl", ".part", ".tmp"))
                and not (p.name.lower() in ("cookies.txt", "cookie.txt", "cookies.json") or p.name.lower().startswith("cookie"))
                and ".part-Frag" not in p.name
                and not p.name.startswith(".tmp")
                and not p.name.startswith(".")
                and p.stat().st_size > 0
            ]
        else:
            downloaded = [path for batch in await asyncio.gather(*(download_one(item, batch_input) for item in batch_items)) for path in batch]
        downloaded = [p for p in downloaded if p.is_file()]
        if not downloaded:
            shutil.rmtree(batch_input, ignore_errors=True)
            shutil.rmtree(batch_output, ignore_errors=True)
            continue
        _validate_downloads(downloaded, batch_input)
        upload_root = batch_input
        if options.get("extract"):
            job.log("Extract enabled: scanning downloaded files for archives...")
            upload_root = batch_output / "extracted"
            outputs = await extract_archives(batch_input, upload_root, job, _archive_passwords(options), bool(options.get("deleteArchiveAfterExtract")))
            job.log(f"Extract stage output files: {len(outputs)}")
        job.set(status="running", step="uploading")
        await _upload_outputs_with_retry(job, target, options, dst, upload_root, {"type": "folder"})
        shutil.rmtree(batch_input, ignore_errors=True)
        shutil.rmtree(batch_output, ignore_errors=True)
        _finish_items(job, source, [item for item in batch_items if not _item_is_failed(job, source, item)])

async def _run_smart_folder_batches(
    job: JobState,
    dirs: dict[str, Path],
    source: dict[str, Any],
    target: dict[str, Any],
    options: dict[str, Any],
    src: Any,
    dst: Any,
    items: list[dict[str, Any]],
) -> None:
    folder_items = [it for it in items if (it.get("type") == "folder" or it.get("is_folder"))]
    file_items = [it for it in items if not (it.get("type") == "folder" or it.get("is_folder"))]
    total_groups = len(folder_items) + (1 if file_items else 0)

    job.log(f"[Smart Folder] Bắt đầu truyền tải theo từng thư mục ({len(folder_items)} folder, {len(file_items)} file)")

    for idx, f_item in enumerate(folder_items, 1):
        job.check_cancelled()
        raw_name = str(f_item.get("name") or f_item.get("path") or f_item.get("id") or f"folder_{idx}").replace("\\", "/")
        f_name = safe_name(PurePosixPath(raw_name).name or raw_name)
        f_key = _queue_item_key(source, f_item)
        job.start_item(f_key, name=f_name)

        job.log(f"[Smart Folder {idx}/{total_groups}] >>> Bắt đầu xử lý thư mục: {f_name}")
        batch_input = dirs["input"] / f"folder-batch-{idx}"
        batch_output = dirs["output"] / f"folder-batch-{idx}"
        folder_dir = batch_input / f_name
        folder_dir.mkdir(parents=True, exist_ok=True)
        batch_output.mkdir(parents=True, exist_ok=True)

        job.set(status="running", step="downloading")
        f_prov = str(f_item.get("provider") or (f_item.get("meta") or {}).get("provider") or source.get("provider") or "").lower()
        f_src = PROVIDERS.get(f_prov, src)
        f_creds = f_item.get("credentials") or source.get("credentials") or {}
        failed_before = job.files_failed

        try:
            downloaded = await f_src.download_folder(f_creds, f_item, folder_dir, job)
        except ProviderFailure as exc:
            if not is_skippable_download_failure(exc):
                raise
            _mark_item_skipped(job, source, f_item, exc.message)
            shutil.rmtree(batch_input, ignore_errors=True)
            shutil.rmtree(batch_output, ignore_errors=True)
            continue

        if job.files_failed > failed_before:
            _mark_item_skipped(job, source, f_item, f"{job.files_failed - failed_before} file(s) unavailable after retries")

        if not downloaded:
            job.log(f"[Smart Folder {idx}/{total_groups}] Thư mục trống hoặc không có file: {f_name}")
            shutil.rmtree(batch_input, ignore_errors=True)
            shutil.rmtree(batch_output, ignore_errors=True)
            if not (job.files_failed > failed_before):
                _finish_items(job, source, [f_item])
            continue

        upload_root = batch_input
        outputs = downloaded
        if options.get("extract"):
            job.log(f"[Smart Folder {idx}/{total_groups}] Giải nén các file nén trong thư mục {f_name}...")
            extract_root = batch_output / "extracted"
            outputs = await extract_archives(batch_input, extract_root, job, _archive_passwords(options), bool(options.get("deleteArchiveAfterExtract")))
            upload_root = extract_root
            job.log(f"Extract stage output files: {len(outputs)}")

        if _should_process_video_thumbnails(options, job.payload):
            try:
                from ..utils.video_thumbnail import process_video_thumbnails
                outputs = await asyncio.to_thread(
                    process_video_thumbnails,
                    outputs,
                    options,
                    job,
                )
            except Exception as exc:
                job.log(f"[Thumbnail] Warning during video thumbnailing: {exc}")

        job.set(status="running", step="uploading")
        job.log(f"[Smart Folder {idx}/{total_groups}] Đang upload thư mục {f_name} ({len(outputs)} files) lên đích...")
        await _upload_outputs_with_retry(job, target, options, dst, upload_root, f_item)

        if options.get("cleanupAfterFinish", True):
            shutil.rmtree(batch_input, ignore_errors=True)
            shutil.rmtree(batch_output, ignore_errors=True)
            job.log(f"[Smart Folder {idx}/{total_groups}] Đã dọn dẹp đĩa tạm Colab cho thư mục {f_name}.")

        _finish_items(job, source, [f_item])
        job.log(f"[Smart Folder {idx}/{total_groups}] ✓ Hoàn tất thư mục: {f_name}")

    if file_items:
        file_grp = len(folder_items) + 1
        job.log(f"[Smart Folder {file_grp}/{total_groups}] >>> Bắt đầu xử lý {len(file_items)} file riêng lẻ...")
        await _run_plain_file_batches(job, dirs, source, target, options, src, dst, file_items)

async def _run_file_pipeline_batches(
    job: JobState,
    dirs: dict[str, Path],
    source: dict[str, Any],
    target: dict[str, Any],
    options: dict[str, Any],
    src: Any,
    dst: Any,
    items: list[dict[str, Any]],
) -> None:
    file_items = [it for it in items if not (it.get("type") == "folder" or it.get("is_folder"))]
    folder_items = [it for it in items if (it.get("type") == "folder" or it.get("is_folder"))]
    job.files_to_download = len(file_items)
    if not options.get("extract"):
        job.files_to_upload = len(file_items)
        job._pipeline_preallocated = True
    dl_concurrency = int(options.get("download_concurrency") or options.get("downloadConcurrency") or FOLDER_DOWNLOAD_CONCURRENCY)
    ul_concurrency = int(options.get("upload_concurrency") or options.get("uploadConcurrency") or options.get("upload_parallel") or UPLOAD_FILE_CONCURRENCY)
    dl_sem = asyncio.Semaphore(max(1, dl_concurrency))
    ul_sem = asyncio.Semaphore(max(1, ul_concurrency))

    job.log(f"[Pipeline] Bắt đầu truyền tải File-by-File (download: {max(1, dl_concurrency)} items song song, upload: {max(1, ul_concurrency)} items song song)")

    if folder_items:
        job.log(f"[Pipeline] Phát hiện {len(folder_items)} thư mục, xử lý thư mục theo smart folder...")
        await _run_smart_folder_batches(job, dirs, source, target, options, src, dst, folder_items)

    if not file_items:
        return

    async def process_one_file(idx: int, item: dict[str, Any]) -> None:
        item_name = _item_name(item)
        item_size = _item_size(item)
        item_k = _queue_item_key(source, item)
        item_prov = str(item.get("provider") or (item.get("meta") or {}).get("provider") or source.get("provider") or "").lower()
        item_src = PROVIDERS.get(item_prov, src)
        item_creds = item.get("credentials") or source.get("credentials") or {}

        pipe_in = dirs["input"] / f"pipe-{idx}"
        pipe_out = dirs["output"] / f"pipe-{idx}"
        pipe_in.mkdir(parents=True, exist_ok=True)

        async with dl_sem:
            job.check_cancelled()
            job.start_file(item_name, phase="download", size=item_size, key=item_k)
            job.start_item(item_k, name=item_name)
            try:
                path = await download_with_retry(
                    lambda: item_src.download_file(item_creds, item, pipe_in, job),
                    progress=job, label=item_name,
                )
                actual_size = path.stat().st_size if (path and path.exists()) else item_size
                job.finish_file(item_name, phase="download", size=actual_size, key=item_k)
                if item_k != item_name:
                    job.file_sizes[item_k] = actual_size
                job.files_downloaded += 1
                job.log(f"[{job.files_downloaded}/{job.files_to_download}] Downloaded: {item_name}")
                _remember_source_ref(job, path, item)
            except ProviderFailure as exc:
                job.finish_file(item_name, phase="download", key=item_k)
                if not is_skippable_download_failure(exc):
                    raise
                _mark_item_skipped(job, source, item, exc.message)
                shutil.rmtree(pipe_in, ignore_errors=True)
                return
            except Exception:
                job.finish_file(item_name, phase="download", key=item_k)
                shutil.rmtree(pipe_in, ignore_errors=True)
                raise

        upload_root = pipe_in
        if options.get("extract") and is_archive_name(path.name):
            pipe_out.mkdir(parents=True, exist_ok=True)
            upload_root = pipe_out / "extracted"
            await extract_archives(pipe_in, upload_root, job, _archive_passwords(options), bool(options.get("deleteArchiveAfterExtract")))

        if _should_process_video_thumbnails(options, job.payload):
            try:
                from ..utils.video_thumbnail import process_video_thumbnails
                files_to_thumb = [p for p in upload_root.rglob("*") if p.is_file()]
                await asyncio.to_thread(process_video_thumbnails, files_to_thumb, options, job)
            except Exception as exc:
                job.log(f"[Thumbnail] Warning during video thumbnailing: {exc}")

        try:
            async with ul_sem:
                if job.files_downloaded >= job.files_to_download:
                    job.set(status="running", step="uploading")
                job.log(f"[Pipeline] Upload ngay file vừa tải: {item_name}")
                await _upload_outputs_with_retry(job, target, options, dst, upload_root, item)
            _finish_items(job, source, [item])
        except Exception as exc:
            job.files_skipped += 1
            _mark_item_skipped(job, source, item, f"Upload error: {exc}")
            job.log(f"[Pipeline] [SKIP] Lỗi upload cho {item_name}: {exc}. Bỏ qua file này và tiếp tục...")
        finally:
            if options.get("cleanupAfterFinish", True):
                shutil.rmtree(pipe_in, ignore_errors=True)
                shutil.rmtree(pipe_out, ignore_errors=True)

    await asyncio.gather(*(process_one_file(i, item) for i, item in enumerate(file_items)))


async def _download_batch_item(job: JobState, source: dict[str, Any], src: Any, item: dict[str, Any], batch_input: Path) -> list[Path]:
    item_type = item.get("type") or ("folder" if item.get("is_folder") else "file")
    item_prov = str(item.get("provider") or (item.get("meta") or {}).get("provider") or source.get("provider") or "").lower()
    item_src = PROVIDERS.get(item_prov, src)
    item_creds = item.get("credentials") or source.get("credentials") or {}
    if item_type == "folder":
        raw_name = str(item.get("name") or item.get("path") or item.get("id") or "folder").replace("\\", "/")
        folder_dir = batch_input / safe_name(PurePosixPath(raw_name).name or raw_name)
        folder_dir.mkdir(parents=True, exist_ok=True)
        return await item_src.download_folder(item_creds, item, folder_dir, job)
    job.files_to_download += 1
    item_name = _item_name(item)
    item_size = _item_size(item)
    item_k = _queue_item_key(source, item)
    job.start_file(item_name, phase="download", size=item_size, key=item_k)
    job.start_item(item_k, name=item_name)
    try:
        path = await download_with_retry(
            lambda: item_src.download_file(item_creds, item, batch_input, job),
            progress=job, label=item_name,
        )
        actual_size = path.stat().st_size if (path and path.exists()) else item_size
        job.finish_file(item_name, phase="download", size=actual_size, key=item_k)
        if item_k != item_name:
            job.file_sizes[item_k] = actual_size
        job.files_downloaded += 1
        job.log(f"[{job.files_downloaded}/{job.files_to_download}] Downloaded: {item_name}")
        _remember_source_ref(job, path, item)
        return [path]
    except Exception:
        job.finish_file(item_name, phase="download", key=item_k)
        raise

async def _run_backup_resume_batches(
    job: JobState,
    dirs: dict[str, Path],
    source: dict[str, Any],
    target: dict[str, Any],
    options: dict[str, Any],
    src: Any,
    dst: Any,
    items: list[dict[str, Any]],
) -> None:
    from .hls_backup_pipeline import run_resumable_backup_pipeline
    file_items = [it for it in items if not (it.get("type") == "folder" or it.get("is_folder"))]
    raw_folder_items = [it for it in items if (it.get("type") == "folder" or it.get("is_folder"))]

    resume_folder_items = []
    plain_folder_items = []
    for it in raw_folder_items:
        f_name = str(it.get("name") or it.get("path") or it.get("id") or "")
        if f_name.endswith("__backup_resume") or options.get("resume_from_backup"):
            resume_folder_items.append(it)
        else:
            plain_folder_items.append(it)

    pipeline_items = list(file_items) + resume_folder_items
    job.files_to_download = len(pipeline_items)
    job.files_to_upload = len(pipeline_items)
    job.log(f"[Backup-Resume] Kích hoạt chế độ Sao lưu phân đoạn Resumable ({len(pipeline_items)} mục)")

    for idx, item in enumerate(pipeline_items):
        item_name = _item_name(item)
        item_k = _queue_item_key(source, item)
        item_url = str(item.get("path") or item.get("id") or item.get("url") or "")
        is_backup_folder = (item in resume_folder_items) or item_name.endswith("__backup_resume") or options.get("resume_from_backup")
        is_stream = is_backup_folder or any(k in item_url.lower() for k in (".m3u8", ".mpd", "/hls/", "stream")) or str(source.get("provider") or "").lower() == "links"

        if is_stream:
            job.start_item(item_k, name=item_name)
            try:
                final_file = await run_resumable_backup_pipeline(
                    job, dirs, source, target, options, src, dst, item
                )
                if final_file is not None:
                    # Fully completed: all segments backed up + merged + uploaded
                    job.files_downloaded += 1
                    job.files_uploaded += 1
                    job.finish_item(item_k, status="done", name=item_name)
                    timing = job.item_timings.get(item_k) or {}
                    job.completed_items.append({
                        **_queue_item_ref(source, item),
                        "startTime": timing.get("startTime"),
                        "endTime": timing.get("endTime"),
                        "duration": timing.get("duration"),
                    })
                    job.log(f"[Backup-Resume] Hoàn tất truyền tải file: {item_name}")
                else:
                    # Partial session: segments backed up to cloud but not yet fully merged
                    job.files_downloaded += 1
                    job.finish_item(item_k, status="done", name=item_name)
                    timing = job.item_timings.get(item_k) or {}
                    job.completed_items.append({
                        **_queue_item_ref(source, item),
                        "startTime": timing.get("startTime"),
                        "endTime": timing.get("endTime"),
                        "duration": timing.get("duration"),
                    })
                    job.log(f"[Backup-Resume] Phiên backup chưa hoàn tất, dữ liệu an toàn trên Cloud. Resume lần sau để tiếp tục: {item_name}")
            except Exception as exc:
                job.finish_item(item_k, status="failed", name=item_name)
                timing = job.item_timings.get(item_k) or {}
                job.failed_items.append({
                    **_queue_item_ref(source, item),
                    "name": item_name,
                    "reason": str(exc),
                    "startTime": timing.get("startTime"),
                    "endTime": timing.get("endTime"),
                    "duration": timing.get("duration"),
                })
                job.files_skipped += 1
                job.log(f"[Backup-Resume] [SKIP] Lỗi xử lý luồng cho {item_name}: {exc}. Bỏ qua mục này và tiếp tục xử lý các mục còn lại...")
                continue
        else:
            # Fallback to standard pipeline batch for regular files
            await _run_file_pipeline_batches(job, dirs, source, target, options, src, dst, [item])

    if plain_folder_items:
        await _run_smart_folder_batches(job, dirs, source, target, options, src, dst, plain_folder_items)

def _download_batches(items: list[dict[str, Any]], root: Path, options: dict[str, Any], job: JobState) -> list[list[dict[str, Any]]]:
    budget = _download_budget(root, options)
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    used = 0
    for item in items:
        size = _item_size(item)
        if current and size and used + size > budget:
            groups.append(current)
            current, used = [], 0
        if size > budget:
            actual_free = shutil.disk_usage(root).free
            if size > actual_free:
                job.log(f"[BUDGET] Warning: {_item_name(item)} is {_gb(size)}GB, above free disk {_gb(actual_free)}GB; download may fail.")
            else:
                job.log(f"[BUDGET] Warning: {_item_name(item)} is {_gb(size)}GB, above safe batch {_gb(budget)}GB; allowing because disk has {_gb(actual_free)}GB free.")
        current.append(item)
        used += size
    if current:
        groups.append(current)
    if len(groups) > 1:
        job.log(f"[BUDGET] Split downloads into {len(groups)} batch(es), safe batch {_gb(budget)}GB.")
    return groups

def _needs_download_batches(items: list[dict[str, Any]], root: Path, options: dict[str, Any]) -> bool:
    budget = _download_budget(root, options)
    total = sum(_item_size(item) for item in items)
    return bool(total and total > budget)

def _download_budget(root: Path, options: dict[str, Any]) -> int:
    free = min(shutil.disk_usage(root).free, COLAB_DEFAULT_FREE_BYTES)
    if options.get("extract"):
        return max(1, free // 3)
    if options.get("optimize_image"):
        return max(1, free // 2)
    return max(1, free)

def _item_size(item: dict[str, Any]) -> int:
    try:
        return max(0, int(item.get("size") or item.get("file_size") or item.get("bytes") or 0))
    except (TypeError, ValueError):
        return 0

def _gb(size: int) -> str:
    return f"{size / 1024 ** 3:.1f}"

def _batch_name(items: list[dict[str, Any]]) -> str:
    return _item_name(items[0]) if len(items) == 1 else f"{len(items)} file batch"

def _item_type(item: dict[str, Any]) -> str:
    return item.get("type") or ("folder" if item.get("is_folder") else "file")

def _finish_items(job: JobState, source: dict[str, Any], items: list[dict[str, Any]]) -> None:
    for item in items:
        item_k = _queue_item_key(source, item)
        name = _item_name(item)
        job.finish_item(item_k, status="done", name=name)
        timing = job.item_timings.get(item_k) or {}
        job.completed_items.append({
            **_queue_item_ref(source, item),
            "startTime": timing.get("startTime"),
            "endTime": timing.get("endTime"),
            "duration": timing.get("duration"),
        })

def _item_scope(source: dict[str, Any], item: dict[str, Any]) -> tuple[str, str]:
    provider = str(item.get("provider") or (item.get("meta") or {}).get("provider") or source.get("provider") or "").lower()
    account = str(item.get("accountId") or item.get("account_id") or (item.get("meta") or {}).get("accountId") or (item.get("meta") or {}).get("account_id") or source.get("accountId") or source.get("account_id") or "")
    return provider, account

def _record_optimized_skip(job: JobState, item: dict[str, Any], status: str) -> None:
    name = str(item.get("name") or item.get("path") or item.get("id") or "file").replace("\\", "/")
    size = int(item.get("size") or item.get("bytes") or 0)
    job.optimized_files.append({
        "name": name,
        "source_name": name,
        "original_size": size,
        "optimized_size": size,
        "status": status,
        "quality": "-",
    })

def _item_upload_target(source: dict[str, Any], target: dict[str, Any], dst: Any, item: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    provider, account = _item_scope(source, item)
    source_provider, source_account = _item_scope({}, source)
    target_provider = str(target.get("provider") or "").lower()
    target_account = str(target.get("accountId") or target.get("account_id") or "")
    mixed_source_scope = len({_item_scope(source, it) for it in source.get("items") or []}) > 1
    if target_provider != source_provider or target_account != source_account:
        return target, dst
    if mixed_source_scope and provider == target_provider and account == target_account:
        return {**target, "folder": _item_target_folder(target.get("folder") or {}, item)}, dst
    if provider == target_provider and account == target_account:
        return target, dst
    return {
        **target,
        "provider": provider,
        "accountId": account,
        "account_id": account,
        "credentials": item.get("credentials") or target.get("credentials") or {},
        "folder": _item_target_folder(target.get("folder") or {}, item),
    }, PROVIDERS.get(provider, dst)

def _mark_item_skipped(job: JobState, source: dict[str, Any], item: dict[str, Any], reason: str) -> None:
    """Park a queue item: skipped this run, still in the queue for the next one."""
    ref = _queue_item_ref(source, item)
    key = (str(ref.get("provider") or ""), str(ref.get("accountId") or ref.get("account_id") or ""), str(ref.get("id") or ""))
    if any((str(f.get("provider") or ""), str(f.get("accountId") or f.get("account_id") or ""), str(f.get("id") or "")) == key for f in job.failed_items):
        return
    item_k = _queue_item_key(source, item)
    job.finish_item(item_k, status="skipped", name=_item_name(item))
    timing = job.item_timings.get(item_k) or {}
    job.failed_items.append({
        **ref,
        "name": _item_name(item),
        "reason": reason,
        "startTime": timing.get("startTime"),
        "endTime": timing.get("endTime"),
        "duration": timing.get("duration"),
    })
    job.log(f"[SKIP] Kept in queue, moving to the next item: {_item_name(item)} ({reason})")

def _mark_remaining_items_completed(job: JobState, source: dict[str, Any]) -> None:
    """Name every item that was not skipped, so the queue drops exactly those."""
    seen = {(str(entry.get("provider") or ""), str(entry.get("accountId") or entry.get("account_id") or ""), str(entry.get("id") or "")) for entry in (*job.failed_items, *job.completed_items)}
    unseen_items = []
    for item in source.get("items") or []:
        ref = _queue_item_ref(source, item)
        key = (str(ref.get("provider") or ""), str(ref.get("accountId") or ref.get("account_id") or ""), str(ref.get("id") or ""))
        if key not in seen:
            unseen_items.append((item, ref, key))
            seen.add(key)

    if not unseen_items:
        return

    now = time.time()
    elapsed = max(1.0, now - float(job.created_at or now))
    avg_dur = max(0.1, round(elapsed / len(unseen_items), 2))

    for item, ref, _ in unseen_items:
        item_k = _queue_item_key(source, item)
        job.finish_item(item_k, status="done", name=_item_name(item), duration=avg_dur)
        timing = job.item_timings.get(item_k) or {}
        job.completed_items.append({
            **ref,
            "startTime": timing.get("startTime"),
            "endTime": timing.get("endTime"),
            "duration": timing.get("duration"),
        })

def _mark_remaining_items_failed(job: JobState, source: dict[str, Any], reason: str) -> None:
    """Mark any uncompleted item as failed in job.failed_items when a job fails."""
    seen = {(str(entry.get("provider") or ""), str(entry.get("accountId") or entry.get("account_id") or ""), str(entry.get("id") or "")) for entry in (*job.failed_items, *job.completed_items)}
    for item in (source.get("items") or []):
        ref = _queue_item_ref(source, item)
        key = (str(ref.get("provider") or ""), str(ref.get("accountId") or ref.get("account_id") or ""), str(ref.get("id") or ""))
        if key not in seen:
            item_k = _queue_item_key(source, item)
            job.finish_item(item_k, status="failed", name=_item_name(item))
            timing = job.item_timings.get(item_k) or {}
            job.failed_items.append({
                **ref,
                "name": _item_name(item),
                "reason": reason,
                "startTime": timing.get("startTime"),
                "endTime": timing.get("endTime"),
                "duration": timing.get("duration"),
            })
            seen.add(key)

def _log_skip_summary(job: JobState) -> None:
    if not job.failed_items:
        return
    job.log(f"[SKIP] {len(job.failed_items)} item(s) skipped and left in the queue: {', '.join(str(entry.get('name') or entry.get('id')) for entry in job.failed_items[:10])}")
    if job.files_failed:
        job.log(f"[SKIP] {job.files_failed} file(s) could not be downloaded after retries.")

async def _upload_outputs(job: JobState, target: dict[str, Any], options: dict[str, Any], dst: Any, upload_root: Path, item: dict[str, Any]) -> None:
    files = [p for p in upload_root.rglob("*") if p.is_file()]
    if not files:
        raise ProviderFailure("UPLOAD_FAILED", "No files staged for upload", {"root": str(upload_root)})
    item_type = item.get("type") or ("folder" if item.get("is_folder") else "file")
    if len(files) == 1 and item_type != "folder":
        if not getattr(job, "_pipeline_preallocated", False):
            job.files_to_upload += 1
        job._upload_log_done = 0
        job._upload_log_total = getattr(job, "files_to_upload", 1) or 1
        f0_sz = files[0].stat().st_size if files[0].exists() else 0
        job.start_file(files[0].name, phase="upload", size=f0_sz)
        try:
            await _upload_path_with_retry(job, target, options, dst, files[0], files[0].name, item)
            job.finish_file(files[0].name, phase="upload", size=f0_sz)
            job.finish_item(files[0].name, status="done", name=files[0].name)
        except Exception:
            job.finish_file(files[0].name, phase="upload")
            raise
        return
    if not getattr(job, "_pipeline_preallocated", False):
        job.files_to_upload += len(files)
    job._upload_log_done = 0
    upload_concurrency = int(options.get("upload_concurrency") or options.get("uploadConcurrency") or options.get("upload_parallel") or UPLOAD_FILE_CONCURRENCY)
    workers = max(1, min(upload_concurrency, len(files)))
    gate = _UploadGate()
    sem = asyncio.Semaphore(workers)
    prefix = str(options.get("upload_prefix") or "").strip("/")

    async def one(path: Path) -> None:
        async with sem:
            if gate.abort is not None:
                return
            rel = path.relative_to(upload_root).as_posix()
            upload_key = f"{prefix}/{rel}" if prefix else rel
            file_sz = path.stat().st_size if path.exists() else 0
            if upload_key in job.uploaded_files:
                job.log(f"[Cache-Hit] File already uploaded: {upload_key}. Skipping re-upload.")
                job.files_uploaded += 1
                job._upload_log_done = getattr(job, "_upload_log_done", 0) + 1
                job.finish_item(path.name, status="done", name=path.name)
                return
            job.start_file(path.name, phase="upload", size=file_sz)
            try:
                await _upload_path_with_retry(job, target, options, dst, path, rel, item, gate)
                job.uploaded_files.add(upload_key)
                job.save_state()
                job.finish_file(path.name, phase="upload", size=file_sz)
                job.finish_item(path.name, status="done", name=path.name)
            except Exception:
                job.finish_file(path.name, phase="upload")
                raise

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

class _ItemSkippedFailure(ProviderFailure):
    def __init__(self, message: str = "Upload skipped"):
        super().__init__("ITEM_SKIPPED", message)

async def _recover_upload(job: JobState, target: dict[str, Any], options: dict[str, Any], exc: ProviderFailure, gate: _UploadGate | None, attempt_gen: int) -> bool:
    """Run one recovery round. Returns True when the caller should retry the file."""
    if gate is None:
        if _fallback_auto_upload_new_to_replace(job, options, exc):
            return True
        try:
            await _wait_for_retry_account(job, target, exc)
            return True
        except _ItemSkippedFailure:
            return False
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
        except _ItemSkippedFailure:
            return False
        except BaseException as err:
            gate.abort = err
            return False
        finally:
            gate.generation += 1
            gate.resume.set()
    return True

async def _upload_path_with_retry(job: JobState, target: dict[str, Any], options: dict[str, Any], dst: Any, path: Path, rel: str, item: dict[str, Any], gate: _UploadGate | None = None) -> None:
    retry_count = 0
    while retry_count < 3:
        retry_count += 1
        attempt_gen = 0
        if gate is not None:
            await gate.resume.wait()
            if gate.abort is not None:
                return
            attempt_gen = gate.generation
        folder = _item_target_folder(target.get("folder") or {}, item) if options.get("replace") else (target.get("folder") or {})
        target_ref = _upload_target(folder, rel, options)
        try:
            source_ref = _replacement_source_ref(item, path, rel) if options.get("replace") and hasattr(dst, "replace_file") else None
            if source_ref:
                await dst.replace_file(target.get("credentials") or {}, path, source_ref, job)
            else:
                await dst.upload_file(target.get("credentials") or {}, path, target_ref, job)
            job.files_uploaded += 1
            job.error = None
            job._upload_log_done = getattr(job, "_upload_log_done", 0) + 1
            job.log(f"[{job._upload_log_done}/{getattr(job, '_upload_log_total', job.files_to_upload)}] Uploaded: {path.name}")
            return
        except _ItemSkippedFailure as exc:
            failed_ref = item if (item.get("id") or item.get("path") or item.get("relay")) else (source_ref or item)
            _mark_item_skipped(job, job.payload.get("source") or {}, failed_ref, exc.message)
            job.files_skipped += 1
            job._upload_log_done = getattr(job, "_upload_log_done", 0) + 1
            job.log(f"[{job._upload_log_done}/{getattr(job, '_upload_log_total', job.files_to_upload)}] [SKIP] Upload failed for {path.name}: {exc.message}. Skipping to next file...")
            return
        except ProviderFailure as exc:
            if _is_rename_failure(exc):
                failed_ref = item if (item.get("id") or item.get("path") or item.get("relay")) else (source_ref or item)
                _mark_item_skipped(job, job.payload.get("source") or {}, failed_ref, exc.message)
                job.files_skipped += 1
                job._upload_log_done = getattr(job, "_upload_log_done", 0) + 1
                job.log(f"[{job._upload_log_done}/{getattr(job, '_upload_log_total', job.files_to_upload)}] Skipped (rename failed): {path.name}")
                return
            auto_replace = bool(options.get("_auto_confirm_upload_new")) and not options.get("replace")
            if not auto_replace:
                if "duplicated" in exc.message.lower() or "repeated" in exc.message.lower():
                    job.files_skipped += 1
                    job._upload_log_done = getattr(job, "_upload_log_done", 0) + 1
                    job.log(f"[{job._upload_log_done}/{getattr(job, '_upload_log_total', job.files_to_upload)}] Skipped (duplicate): {path.name}")
                    return
                if exc.code != "UPLOAD_FAILED":
                    raise
            try:
                recovered = await _recover_upload(job, target, options, exc, gate, attempt_gen)
            except _ItemSkippedFailure:
                recovered = False
            if not recovered:
                if gate is not None and gate.abort is not None:
                    return
                if not IS_KAGGLE:
                    raise exc
                failed_ref = item if (item.get("id") or item.get("path") or item.get("relay")) else (source_ref or item)
                _mark_item_skipped(job, job.payload.get("source") or {}, failed_ref, exc.message)
                job.files_skipped += 1
                job._upload_log_done = getattr(job, "_upload_log_done", 0) + 1
                job.log(f"[{job._upload_log_done}/{getattr(job, '_upload_log_total', job.files_to_upload)}] [SKIP] Upload error on {path.name}: {exc.message}. Continuing with remaining files...")
                return

async def _upload_one_with_retry(job: JobState, target: dict[str, Any], options: dict[str, Any], dst: Any, path: Path) -> dict[str, Any]:
    file_sz = path.stat().st_size if path.exists() else 0
    if path.name in job.uploaded_files:
        job.log(f"[Cache-Hit] File already uploaded: {path.name}. Skipping re-upload.")
        job.files_uploaded = 1
        job.finish_file(path.name, phase="upload", size=file_sz)
        return {"ok": True, "uploaded": 1, "skipped": 0, "items": [{"name": path.name}]}
    job.start_file(path.name, phase="upload", size=file_sz)
    try:
        while True:
            try:
                source = (job.payload.get("source") or {}).get("items") or []
                source_ref = {**source[0], "name": path.name} if source and options.get("replace") and hasattr(dst, "replace_file") else None
                result = await (dst.replace_file(target.get("credentials") or {}, path, source_ref, job) if source_ref else dst.upload_file(target.get("credentials") or {}, path, _upload_target(target.get("folder") or {}, path.name, options), job))
                job.files_uploaded = 1
                job.uploaded_files.add(path.name)
                job.save_state()
                job.error = None
                job.finish_file(path.name, phase="upload", size=file_sz)
                job.log(f"[1/1] Uploaded: {path.name}")
                return result
            except _ItemSkippedFailure as exc:
                source = (job.payload.get("source") or {}).get("items") or []
                if source:
                    _mark_item_skipped(job, job.payload.get("source") or {}, source[0], exc.message)
                job.files_skipped = 1
                job.finish_file(path.name, phase="upload")
                job.log(f"[1/1] [SKIP] Upload failed for {path.name}: {exc.message}")
                return {"ok": True, "uploaded": 0, "skipped": 1, "items": []}
            except ProviderFailure as exc:
                if _is_rename_failure(exc):
                    source = (job.payload.get("source") or {}).get("items") or []
                    if source:
                        _mark_item_skipped(job, job.payload.get("source") or {}, source[0], exc.message)
                    job.files_skipped = 1
                    job.finish_file(path.name, phase="upload")
                    job.log(f"[1/1] Skipped (rename failed): {path.name}")
                    return {"ok": True, "uploaded": 0, "skipped": 1, "items": []}
                if _fallback_auto_upload_new_to_replace(job, options, exc):
                    continue
                if "duplicated" in exc.message.lower() or "repeated" in exc.message.lower():
                    job.files_skipped = 1
                    job.finish_file(path.name, phase="upload")
                    job.log(f"[1/1] Skipped (duplicate): {path.name}")
                    return {"ok": True, "uploaded": 0, "skipped": 1, "items": []}
                try:
                    await _wait_for_retry_account(job, target, exc)
                except _ItemSkippedFailure:
                    source = (job.payload.get("source") or {}).get("items") or []
                    if source:
                        _mark_item_skipped(job, job.payload.get("source") or {}, source[0], exc.message)
                    job.files_skipped = 1
                    job.finish_file(path.name, phase="upload")
                    job.log(f"[1/1] [SKIP] Upload failed for {path.name}: {exc.message}")
                    return {"ok": True, "uploaded": 0, "skipped": 1, "items": []}
    except Exception:
        job.finish_file(path.name, phase="upload")
        raise

async def _upload_outputs_with_retry(job: JobState, target: dict[str, Any], options: dict[str, Any], dst: Any, upload_root: Path, item: dict[str, Any]) -> None:
    while True:
        try:
            await _upload_outputs(job, target, options, dst, upload_root, item)
            job.error = None
            return
        except _ItemSkippedFailure as exc:
            job.log(f"[SKIP] Upload outputs skipped: {exc.message}")
            return
        except ProviderFailure as exc:
            if exc.code != "UPLOAD_FAILED":
                raise
            if _fallback_auto_upload_new_to_replace(job, options, exc):
                continue
            try:
                await _wait_for_retry_account(job, target, exc)
            except _ItemSkippedFailure:
                job.log(f"[SKIP] Upload outputs skipped: {exc.message}")
                return
            continue

def _fallback_auto_upload_new_to_replace(job: JobState, options: dict[str, Any], exc: ProviderFailure) -> bool:
    if not options.get("_auto_confirm_upload_new") or options.get("replace"):
        return False
    if exc.code != "UPLOAD_FAILED":
        return False
    options["_auto_confirm_upload_new"] = False
    if options.get("extract"):
        job.log(f"Auto upload_new failed ({exc.message}); keeping upload_new for extracted archive outputs.")
        return False
    options["replace"] = True
    options.pop("upload_prefix", None)
    job.log(f"Auto upload_new failed ({exc.message}); retrying with replace.")
    return True

def _is_rename_failure(exc: ProviderFailure) -> bool:
    return exc.code == "UPLOAD_FAILED" and "rename" in f"{exc.message} {exc.details}".lower()

def _item_is_failed(job: JobState, source: dict[str, Any], item: dict[str, Any]) -> bool:
    ref = _queue_item_ref(source, item)
    key = (str(ref.get("provider") or ""), str(ref.get("accountId") or ref.get("account_id") or ""), str(ref.get("id") or ""))
    return any((str(f.get("provider") or ""), str(f.get("accountId") or f.get("account_id") or ""), str(f.get("id") or "")) == key for f in job.failed_items)

async def _wait_for_retry_account(job: JobState, target: dict[str, Any], exc: ProviderFailure) -> None:
    job.error = {"code": exc.code, "message": exc.message, "details": exc.details}
    job.log(f"Upload failed: {exc.message}. Waiting for another target account{' (auto-skipping in 30s)...' if IS_KAGGLE else '...'}")
    job.confirm_action = None
    job.confirm_event.clear()
    job.set(status="waiting_target_account", step="uploading")
    deadline = (time.monotonic() + 30.0) if IS_KAGGLE else None
    while not job.confirm_event.is_set():
        job.check_cancelled()
        if deadline is not None and time.monotonic() >= deadline:
            job.log(f"[Auto-Skip] No replacement account provided within 30s. Skipping failed file and continuing...")
            job.set(status="running", step="uploading")
            raise _ItemSkippedFailure(exc.message)
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

def _remember_source_ref(job: JobState, path: Path, item: dict[str, Any]) -> None:
    refs = getattr(job, "_source_refs", None)
    if refs is None:
        refs = {}
        setattr(job, "_source_refs", refs)
    refs[str(path.resolve())] = dict(item)

def _optimized_replace_refs(job: JobState, results: list[dict[str, Any]], output_root: Path, upload_root: Path, input_root: Path) -> dict[str, dict[str, Any]]:
    source_refs = getattr(job, "_source_refs", {})
    by_stem = {Path(key).stem: ref for key, ref in source_refs.items()}
    out: dict[str, dict[str, Any]] = {}
    for result in results:
        name = str(result.get("name") or "").replace("\\", "/")
        source_name = str(result.get("source_name") or "").replace("\\", "/")
        if not name:
            continue
        ref = source_refs.get(str((input_root / source_name).resolve())) if source_name else None
        ref = ref or by_stem.get(PurePosixPath(name).stem)
        if not ref:
            continue
        output_path = output_root / name
        if output_path.is_relative_to(upload_root):
            out[output_path.relative_to(upload_root).as_posix()] = ref
    return out

def _replacement_source_ref(item: dict[str, Any], path: Path, rel: str) -> dict[str, Any] | None:
    refs = item.get("_replace_refs") if isinstance(item.get("_replace_refs"), dict) else {}
    ref = refs.get(str(rel).replace("\\", "/")) or refs.get(path.name)
    if ref:
        return {**ref, "name": path.name}
    relay = item.get("relay") if isinstance(item.get("relay"), dict) else {}
    if not (item.get("path") or item.get("id") or relay.get("sourcePath") or relay.get("sourceId")):
        return None
    item_type = item.get("type") or ("folder" if item.get("is_folder") else "file")
    return {**item, "name": path.name} if item_type != "folder" else None

def _item_target_folder(folder: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    item_type = item.get("type") or ("folder" if item.get("is_folder") else "file")
    relay = item.get("relay") if isinstance(item.get("relay"), dict) else {}
    raw = str(relay.get("sourcePath") or relay.get("sourceId") or item.get("path") or item.get("id") or "")
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
    account = item.get("accountId") or item.get("account_id") or (item.get("meta") or {}).get("accountId") or (item.get("meta") or {}).get("account_id") or source.get("accountId") or source.get("account_id")
    relay = item.get("relay") if isinstance(item.get("relay"), dict) else {}
    original = relay.get("sourcePath") or relay.get("sourceId")
    ref = {
        "provider": item.get("provider") or (item.get("meta") or {}).get("provider") or source.get("provider"),
        "id": original or item.get("id") or item.get("path"),
        "path": original or item.get("path") or item.get("id"),
    }
    if account:
        ref["accountId"] = account
        ref["account_id"] = account
    return ref

def _queue_item_key(source: dict[str, Any], item: dict[str, Any]) -> str:
    provider = str(item.get("provider") or (item.get("meta") or {}).get("provider") or source.get("provider") or "").lower()
    account = str(item.get("accountId") or item.get("account_id") or (item.get("meta") or {}).get("accountId") or (item.get("meta") or {}).get("account_id") or source.get("accountId") or source.get("account_id") or "")
    relay = item.get("relay") if isinstance(item.get("relay"), dict) else {}
    original = relay.get("sourcePath") or relay.get("sourceId")
    item_id = str(original or item.get("id") or item.get("path") or item.get("name") or "")
    return f"{provider}:{account}:{item_id}"

def _is_video_item(item: dict[str, Any]) -> bool:
    return Path(str(item.get("name") or item.get("path") or item.get("id") or "")).suffix.lower() in VIDEO_EXTENSIONS

def _is_archive_item(item: dict[str, Any]) -> bool:
    return is_archive_name(str(item.get("name") or item.get("path") or item.get("id") or ""))

def _archive_passwords(options: dict[str, Any]) -> Any:
    return options.get("archive_passwords") or options.get("archivePasswords") or options.get("archivePassword") or options.get("archive_password")

def _selected_folder_name(source: dict[str, Any]) -> str:
    folder = next((item for item in source.get("items") or [] if item.get("type") == "folder" or item.get("is_folder")), {})
    raw_name = str(folder.get("name") or folder.get("path") or folder.get("id") or "folder").replace("\\", "/")
    return safe_name(PurePosixPath(raw_name).name or raw_name)

def _only_child_dir(path: Path) -> Path | None:
    children = [child for child in path.iterdir()]
    return children[0] if len(children) == 1 and children[0].is_dir() else None

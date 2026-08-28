from __future__ import annotations

import asyncio
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from ..extract.extractor import extract_archives
from ..providers import PROVIDERS
from ..providers.base import ProviderFailure, safe_name
from ..security import redact
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

        downloaded: list[Path] = []
        has_folder_source = False
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
                job.files_to_download += 1
                downloaded.append(await src.download_file(source.get("credentials") or {}, item, dirs["input"], job))
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
            outputs = await extract_archives(dirs["input"], dirs["output"], job, options.get("archivePassword") or options.get("archive_password"), bool(options.get("deleteArchiveAfterExtract")))
            out_root = dirs["output"] if any(p.is_relative_to(dirs["output"]) for p in outputs) else dirs["input"]

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
            else:
                job.log("No image files found for optimization, skipping confirmation.")
                
            job.set(status="running", step="uploading")
        else:
            job.set(step="uploading")
        preserve_tree = bool(options.get("preserveFolderStructure") or has_folder_source)
        if len(outputs) == 1 and outputs[0].is_file() and not preserve_tree:
            job.files_to_upload = 1
            try:
                result = await dst.upload_file(target.get("credentials") or {}, outputs[0], _upload_target(target.get("folder") or {}, outputs[0].name, options), job)
                job.files_uploaded = 1
                job.log(f"[1/1] Uploaded: {outputs[0].name}")
            except ProviderFailure as exc:
                if "duplicated" in exc.message.lower() or "repeated" in exc.message.lower():
                    job.files_skipped = 1
                    job.log(f"[1/1] Skipped (duplicate): {outputs[0].name}")
                    result = {"ok": True, "uploaded": 0, "skipped": 1, "items": []}
                else:
                    raise
        else:
            upload_root = out_root
            if out_root == dirs["input"] and len(outputs) == 1 and not preserve_tree:
                job.files_to_upload = 1
                try:
                    result = await dst.upload_file(target.get("credentials") or {}, outputs[0], _upload_target(target.get("folder") or {}, outputs[0].name, options), job)
                    job.files_uploaded = 1
                    job.log(f"[1/1] Uploaded: {outputs[0].name}")
                except ProviderFailure as exc:
                    if "duplicated" in exc.message.lower() or "repeated" in exc.message.lower():
                        job.files_skipped = 1
                        job.log(f"[1/1] Skipped (duplicate): {outputs[0].name}")
                        result = {"ok": True, "uploaded": 0, "skipped": 1, "items": []}
                    else:
                        raise
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
        if (payload.get("options") or {}).get("cleanupAfterFinish", True):
            cleanup_job(job.job_id)
        # Drop provider credential refs after run.
        payload.get("source", {}).pop("credentials", None)
        payload.get("target", {}).pop("credentials", None)


def _upload_target(folder: dict[str, Any], relative_path: str, options: dict[str, Any]) -> dict[str, Any]:
    prefix = str(options.get("upload_prefix") or "").strip("/")
    if not prefix:
        return {**folder, **({"relative_path": relative_path} if relative_path else {})}
    return {**folder, "relative_path": f"{prefix}/{relative_path}".rstrip("/")}

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ..extract.extractor import extract_archives
from ..providers import PROVIDERS
from ..providers.base import ProviderFailure
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
        job.log(f"Job start: {source.get('provider')} -> {target.get('provider')}")

        downloaded: list[Path] = []
        for item in source.get("items") or []:
            job.check_cancelled()
            item_type = item.get("type") or ("folder" if item.get("is_folder") else "file")
            if item_type == "folder":
                downloaded.extend(await src.download_folder(source.get("credentials") or {}, item, dirs["input"] / str(item.get("name") or item.get("id") or "folder"), job))
            else:
                downloaded.append(await src.download_file(source.get("credentials") or {}, item, dirs["input"], job))
        if not downloaded:
            raise ProviderFailure("SOURCE_FILE_NOT_FOUND", "No source files downloaded")

        out_root = dirs["input"]
        outputs = downloaded
        if options.get("extract"):
            outputs = await extract_archives(dirs["input"], dirs["output"], job, options.get("archivePassword") or options.get("archive_password"), bool(options.get("deleteArchiveAfterExtract")))
            out_root = dirs["output"] if any(p.is_relative_to(dirs["output"]) for p in outputs) else dirs["input"]

        job.set(step="uploading")
        if len(outputs) == 1 and outputs[0].is_file() and not options.get("preserveFolderStructure"):
            result = await dst.upload_file(target.get("credentials") or {}, outputs[0], target.get("folder") or {}, job)
        else:
            upload_root = out_root
            if out_root == dirs["input"] and len(outputs) == 1:
                result = await dst.upload_file(target.get("credentials") or {}, outputs[0], target.get("folder") or {}, job)
            else:
                result = await dst.upload_folder(target.get("credentials") or {}, upload_root, target.get("folder") or {}, job)
        job.log(f"Done: {redact(result)}")
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

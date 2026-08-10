from __future__ import annotations

import asyncio
from pathlib import Path
from unittest import TestCase

from src.jobs.progress import JobState
from src.jobs.transfer_job import run_transfer
from src.providers import PROVIDERS
from src.providers.terabox import TeraBoxProvider, TeraBoxSession


class OneFileFolderSource:
    async def download_folder(self, credentials, folder_ref, local_dir: Path, progress: JobState):
        path = local_dir / "only.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok")
        return [path]


class UploadRecorder:
    def __init__(self):
        self.calls = []

    async def upload_file(self, credentials, local_path, target_ref, progress):
        self.calls.append(("file", local_path))
        return {"ok": True}

    async def upload_folder(self, credentials, local_dir, target_ref, progress):
        self.calls.append(("folder", local_dir))
        return {"ok": True}


class TransferJobTests(TestCase):
    def test_folder_source_uploads_tree_even_when_one_file(self):
        dst = UploadRecorder()
        old = dict(PROVIDERS)
        PROVIDERS.update({"fake-source": OneFileFolderSource(), "fake-dst": dst})
        try:
            job = JobState("folder-one-file", {
                "source": {"provider": "fake-source", "items": [{"type": "folder", "name": "root"}]},
                "target": {"provider": "fake-dst", "folder": {}},
                "options": {"cleanupAfterFinish": True},
            })
            asyncio.run(run_transfer(job))
        finally:
            PROVIDERS.clear()
            PROVIDERS.update(old)

        self.assertEqual(job.status, "completed")
        self.assertEqual(dst.calls[0][0], "folder")

    def test_progress_phases_skip_extract_when_disabled(self):
        job = JobState("no-extract", {"options": {"extract": False}})
        self.assertEqual(job.view()["phases"], ["download", "upload"])

    def test_terabox_upload_falls_back_to_lower_concurrency(self):
        class Provider(TeraBoxProvider):
            def __init__(self):
                self.concurrency = []

            async def _ensure_relative_parent(self, s, parent, relative_path):
                return "/"

            async def _precreate_upload(self, s, remote_path, parent, size, hashes):
                return {"uploadid": "up1"}

            async def _locate_upload_hosts(self, s):
                return ["https://upload.test"]

            async def _upload_parts(self, s, host, local_path, remote_path, upload_id, size, mime, progress, concurrency):
                self.concurrency.append(concurrency)
                if concurrency == 6:
                    raise Exception("too many")
                progress.add_bytes(size, size, "upload")

        async def ready(self):
            return None

        async def request_json(self, *args, **kwargs):
            return {"ok": True}

        old_ready = TeraBoxSession.ready
        old_request_json = TeraBoxSession.request_json
        TeraBoxSession.ready = ready
        TeraBoxSession.request_json = request_json
        try:
            with __import__("tempfile").TemporaryDirectory() as tmp:
                path = Path(tmp) / "a.bin"
                path.write_bytes(b"x" * 9)
                provider = Provider()
                job = JobState("upload", {})
                asyncio.run(provider.upload_file({"cookies": {"ndus": "x"}}, path, {"id": "/"}, job))
        finally:
            TeraBoxSession.ready = old_ready
            TeraBoxSession.request_json = old_request_json

        self.assertEqual(provider.concurrency[:2], [6, 4])
        self.assertEqual(job.progress.upload, 100)

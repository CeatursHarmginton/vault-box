from __future__ import annotations

import asyncio
import json
import shutil
import threading
import time
from pathlib import Path
from unittest import TestCase

from src.jobs.progress import JobState
from src.extract import extractor as extractor_mod
from src.extract.extractor import archives, is_archive_name
from src.jobs import transfer_job as transfer_job_mod
from src.jobs.transfer_job import run_transfer
from src.providers import PROVIDERS
from src.providers import base as base_mod
from src.utils import image_optimizer
from src.providers.base import ProviderFailure
from src.providers.pikpak import PikPakProvider
from src.providers import pikpak as pikpak_mod
from src.providers.terabox import TeraBoxProvider, TeraBoxSession
from src.providers import drive as drive_mod
from src.providers.drive import DriveProvider

def test_colab_download_concurrency_default_is_cdn_friendly():
    from src.config import FOLDER_DOWNLOAD_CONCURRENCY

    assert FOLDER_DOWNLOAD_CONCURRENCY == 12

def test_archive_detection_supports_common_and_split_formats(tmp_path):
    names = [
        "a.zip", "b.7z", "c.rar", "d.tar.gz", "e.iso",
        "movie.7z.001", "movie.7z.002",
        "book.zip.001", "book.zip.002",
        "rarset.part01.rar", "rarset.part02.rar",
        "manual.part.rar", "notapart1.rar", "chapter part1.rar",
        "old.rar", "old.r00",
    ]
    for name in names:
        (tmp_path / name).write_text("x")

    picked = {p.name for p in archives(tmp_path)}

    assert {"a.zip", "b.7z", "c.rar", "d.tar.gz", "e.iso", "movie.7z.001", "book.zip.001", "rarset.part01.rar", "manual.part.rar", "notapart1.rar", "chapter part1.rar", "old.rar"} <= picked
    assert {"movie.7z.002", "book.zip.002", "rarset.part02.rar", "old.r00"}.isdisjoint(picked)
    assert is_archive_name("anything.001")
    assert not is_archive_name("anything.002")

def test_multipart_rar_with_glob_chars_does_not_fail_precheck(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    archive = input_dir / "[XR Unc3ns0red] name.part1.rar"
    archive.write_text("x")

    class Proc:
        returncode = 0

        async def communicate(self):
            return b"", None

    async def fake_exec(*args, **kwargs):
        target = Path(args[-1]) if args[0] == "unrar" else Path(next(arg for arg in args if str(arg).startswith("-o"))[2:])
        target.mkdir(parents=True, exist_ok=True)
        (target / "ok.jpg").write_text("ok")
        return Proc()

    monkeypatch.setattr(extractor_mod.shutil, "which", lambda name: name)
    monkeypatch.setattr(extractor_mod.asyncio, "create_subprocess_exec", fake_exec)

    out = asyncio.run(extractor_mod.extract_archives(input_dir, output_dir, JobState("globchars", {}), None))

    assert [p.relative_to(output_dir).as_posix() for p in out] == ["[XR Unc3ns0red] name/ok.jpg"]

def test_extract_failure_falls_back_to_original_archive_files(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    (input_dir / "broken.part01.rar").write_text("x")
    (input_dir / "broken.part02.rar").write_text("x")
    (input_dir / "note.txt").write_text("ok")

    class Proc:
        returncode = 2

        async def communicate(self):
            return b"Wrong password", None

    async def fake_exec(*args, **kwargs):
        return Proc()

    monkeypatch.setattr(extractor_mod.shutil, "which", lambda name: "7z")
    monkeypatch.setattr(extractor_mod.asyncio, "create_subprocess_exec", fake_exec)

    out = asyncio.run(extractor_mod.extract_archives(input_dir, output_dir, JobState("extract-fail", {}), ["bad"]))

    assert {p.name for p in out} == {"broken.part01.rar", "broken.part02.rar", "note.txt"}
    assert all(p.is_relative_to(output_dir) for p in out)

def test_extract_missing_7z_falls_back_to_original_files(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    (input_dir / "a.zip").write_text("zip")

    monkeypatch.setattr(extractor_mod.shutil, "which", lambda name: None)
    out = asyncio.run(extractor_mod.extract_archives(input_dir, output_dir, JobState("no-7z", {}), ["pw"]))

    assert [p.relative_to(output_dir).as_posix() for p in out] == ["a.zip"]

def test_rar_extract_prefers_unrar(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    (input_dir / "ok.rar").write_text("x")
    calls = []

    class Proc:
        returncode = 0

        async def communicate(self):
            return b"", None

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return Proc()

    monkeypatch.setattr(extractor_mod.shutil, "which", lambda name: name)
    monkeypatch.setattr(extractor_mod.asyncio, "create_subprocess_exec", fake_exec)

    asyncio.run(extractor_mod.extract_archives(input_dir, output_dir, JobState("rar-unrar", {}), ["ok"]))

    assert calls[0][0] == "unrar"

def test_extract_mixed_success_and_fallback_are_staged_together(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    (input_dir / "good.zip").write_text("x")
    (input_dir / "bad.part01.rar").write_text("x")
    (input_dir / "bad.part02.rar").write_text("x")
    (input_dir / "note.txt").write_text("ok")

    class Proc:
        def __init__(self, returncode):
            self.returncode = returncode

        async def communicate(self):
            return (b"" if self.returncode == 0 else b"Wrong password"), None

    async def fake_exec(*args, **kwargs):
        archive = Path(args[-1])
        if archive.name == "good.zip":
            out_arg = next(arg for arg in args if str(arg).startswith("-o"))
            (Path(str(out_arg)[2:]) / "good.jpg").write_text("ok")
            return Proc(0)
        return Proc(2)

    monkeypatch.setattr(extractor_mod.shutil, "which", lambda name: "7z" if name == "7z" else None)
    monkeypatch.setattr(extractor_mod.asyncio, "create_subprocess_exec", fake_exec)

    out = asyncio.run(extractor_mod.extract_archives(input_dir, output_dir, JobState("mixed", {}), ["bad"]))

    assert {p.relative_to(output_dir).as_posix() for p in out} == {"good/good.jpg", "bad.part01.rar", "bad.part02.rar", "note.txt"}

def test_archive_extract_keeps_parent_folder_and_archive_folder(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    (input_dir / "testA").mkdir(parents=True)
    (input_dir / "testA" / "testZip.rar").write_text("x")
    (input_dir / "testA" / "img1.png").write_text("ok")

    class Proc:
        returncode = 0

        async def communicate(self):
            return b"", None

    async def fake_exec(*args, **kwargs):
        target = Path(args[-1]) if args[0] == "unrar" else Path(next(arg for arg in args if str(arg).startswith("-o"))[2:])
        target.mkdir(parents=True, exist_ok=True)
        (target / "unzipped.png").write_text("ok")
        return Proc()

    monkeypatch.setattr(extractor_mod.shutil, "which", lambda name: name)
    monkeypatch.setattr(extractor_mod.asyncio, "create_subprocess_exec", fake_exec)

    out = asyncio.run(extractor_mod.extract_archives(input_dir, output_dir, JobState("paths", {}), ["ok"]))

    assert {p.relative_to(output_dir).as_posix() for p in out} == {"testA/testZip/unzipped.png", "testA/img1.png"}

def test_extract_without_archives_stages_input_files(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    (input_dir / "folder").mkdir()
    (input_dir / "folder" / "note.txt").write_text("ok")

    monkeypatch.setattr(extractor_mod.shutil, "which", lambda name: "7z")

    out = asyncio.run(extractor_mod.extract_archives(input_dir, output_dir, JobState("plain", {}), None))

    assert [p.relative_to(output_dir).as_posix() for p in out] == ["folder/note.txt"]

def test_optimize_queue_unzip_fallback_uploads_archive_and_passwords(monkeypatch):
    class Source:
        async def download_file(self, credentials, file_ref, local_dir: Path, progress: JobState):
            path = local_dir / file_ref["name"]
            path.write_text("zip")
            return path

    dst = UploadRecorder()
    old = dict(PROVIDERS)
    seen_passwords = []

    async def fake_extract(input_dir, output_dir, progress, password=None, delete_archive=False):
        seen_passwords.append(password)
        output_dir.mkdir(parents=True, exist_ok=True)
        out = output_dir / next(input_dir.glob("*.zip")).name
        out.write_text("zip")
        return [out]

    def fake_optimize(input_dir, output_dir, options, job_state, cancel_check=None):
        for path in input_dir.rglob("*"):
            if path.is_file():
                target = output_dir / path.relative_to(input_dir)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        return []

    PROVIDERS.update({"fake-source": Source(), "fake-dst": dst})
    monkeypatch.setattr(transfer_job_mod, "extract_archives", fake_extract)
    monkeypatch.setattr(image_optimizer, "optimize_directory", fake_optimize)
    try:
        job = JobState("opt-unzip-fallback", {
            "source": {"provider": "fake-source", "items": [
                {"type": "file", "id": "a", "name": "a.zip"},
                {"type": "file", "id": "b", "name": "b.zip"},
            ]},
            "target": {"provider": "fake-dst", "folder": {"id": "/", "path": "/"}},
            "options": {"cleanupAfterFinish": False, "optimize_image": True, "extract": True, "archive_passwords": ["pw1", "pw2"], "replace": True},
        })
        asyncio.run(run_transfer(job))
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(old)
        __import__("src.utils.temp_storage", fromlist=["cleanup_job"]).cleanup_job("opt-unzip-fallback")

    assert job.status == "completed", job.error
    assert seen_passwords == [["pw1", "pw2"], ["pw1", "pw2"]]
    assert [call[1].name for call in dst.calls] == ["a.zip", "b.zip"]

def test_drive_mount_download_and_upload(tmp_path, monkeypatch):
    mount = tmp_path / "MyDrive"
    mount.mkdir()
    source = mount / "in.txt"
    source.write_text("ok")
    monkeypatch.setattr(drive_mod, "DRIVE_MOUNT", mount)
    provider = DriveProvider()
    job = JobState("drive-mount", {})

    downloaded = asyncio.run(provider.download_file({"mount": True}, {"path": "/in.txt"}, tmp_path / "input", job))
    assert downloaded.read_text() == "ok"

    uploaded = asyncio.run(provider.upload_file({"mount": True}, downloaded, {"path": "/out", "relative_path": "copy.txt"}, job))
    assert uploaded["path"] == "out/copy.txt"
    assert (mount / "out" / "copy.txt").read_text() == "ok"

def test_drive_mount_required(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_mod, "DRIVE_MOUNT", tmp_path / "missing")
    provider = DriveProvider()

    try:
        asyncio.run(provider.download_file({"mount": True}, {"path": "/x.txt"}, tmp_path, JobState("drive-missing", {})))
    except ProviderFailure as exc:
        assert exc.code == "DRIVE_NOT_MOUNTED"
    else:
        raise AssertionError("expected DRIVE_NOT_MOUNTED")

def test_drive_mount_source_id_without_token_fails_with_path_hint(tmp_path, monkeypatch):
    mount = tmp_path / "MyDrive"
    mount.mkdir()
    monkeypatch.setattr(drive_mod, "DRIVE_MOUNT", mount)
    provider = DriveProvider()

    try:
        asyncio.run(provider.download_file({"mount": True}, {"id": "drive-file-id", "path": "id:drive-file-id"}, tmp_path, JobState("drive-id", {})))
    except ProviderFailure as exc:
        assert exc.code == "SOURCE_FILE_NOT_FOUND"
        assert "MyDrive path" in exc.message
    else:
        raise AssertionError("expected SOURCE_FILE_NOT_FOUND")

def test_provider_folder_locks_are_loop_local():
    async def lock(provider):
        return provider._lock()

    for provider in (DriveProvider(), TeraBoxProvider()):
        first = asyncio.run(lock(provider))
        second = asyncio.run(lock(provider))
        assert first is not second

def test_drive_api_mode_validate_does_not_require_mount(monkeypatch, tmp_path):
    monkeypatch.setattr(drive_mod, "DRIVE_MOUNT", tmp_path / "missing")

    class Response:
        status_code = 200

        def json(self):
            return {"user": {"emailAddress": "a@example.com"}}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, method, url, **kwargs):
            return Response()

    monkeypatch.setattr(drive_mod.httpx, "AsyncClient", Client)

    out = asyncio.run(DriveProvider().validate_credentials({"access_token": "A", "mount": False}))

    assert out["ok"] is True

def test_drive_api_download_uses_media_endpoint(monkeypatch, tmp_path):
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"id": "file1", "name": "a.txt", "mimeType": "text/plain"}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, method, url, **kwargs):
            return Response()

    async def fake_stream_download(url, dest, progress, *, headers=None, phase="download"):
        calls.append((url, headers))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("ok")
        return dest

    monkeypatch.setattr(drive_mod.httpx, "AsyncClient", Client)
    monkeypatch.setattr(drive_mod, "stream_download", fake_stream_download)

    out = asyncio.run(DriveProvider().download_file({"access_token": "A", "mount": False}, {"id": "file1"}, tmp_path, JobState("drive-api-download", {})))

    assert out.read_text() == "ok"
    assert calls == [(f"{drive_mod.DRIVE_API}/files/file1?alt=media&supportsAllDrives=true", {"Authorization": "Bearer A"})]

def test_drive_api_upload_uses_resumable_endpoint(monkeypatch, tmp_path):
    calls = []

    class InitResponse:
        status_code = 200
        headers = {"Location": "https://upload.test/session"}

    class DoneResponse:
        status_code = 200
        headers = {}

        def json(self):
            return {"id": "file1", "name": "a.txt"}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, method, url, **kwargs):
            calls.append((method, url))
            return InitResponse()

        async def put(self, url, **kwargs):
            calls.append(("PUT", url))
            return DoneResponse()

    monkeypatch.setattr(drive_mod.httpx, "AsyncClient", Client)
    path = tmp_path / "a.txt"
    path.write_text("ok")

    out = asyncio.run(DriveProvider().upload_file({"access_token": "A", "mount": False}, path, {"id": "root"}, JobState("drive-api-upload", {})))

    assert out["id"] == "file1"
    assert calls == [("POST", f"{drive_mod.DRIVE_UPLOAD_API}/files"), ("PUT", "https://upload.test/session")]

def test_drive_api_upload_preserves_relative_parent(monkeypatch, tmp_path):
    seen = {}

    class Response:
        status_code = 200
        headers = {"Location": "https://upload.test/session"}
        text = ""

        def __init__(self, payload=None):
            self._payload = payload or {}

        def json(self):
            return self._payload

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, method, url, **kwargs):
            if method == "GET":
                return Response({"files": []})
            if url == f"{drive_mod.DRIVE_API}/files":
                seen["created"] = kwargs["json"]
                return Response({"id": "folderA-id", "name": "folderA"})
            seen["uploaded"] = json.loads(kwargs["content"])
            return Response()

        async def put(self, url, **kwargs):
            return Response({"id": "file1", "name": "image1.jpg"})

    monkeypatch.setattr(drive_mod.httpx, "AsyncClient", Client)
    path = tmp_path / "image1.jpg"
    path.write_text("ok")

    out = asyncio.run(DriveProvider().upload_file({"access_token": "A", "mount": False}, path, {"id": "root", "relative_path": "folderA/image1.jpg"}, JobState("drive-api-folder", {})))

    assert out["id"] == "file1"
    assert seen["created"]["name"] == "folderA"
    assert seen["created"]["parents"] == ["root"]
    assert seen["uploaded"]["name"] == "image1.jpg"
    assert seen["uploaded"]["parents"] == ["folderA-id"]

def test_drive_api_upload_folder_reuses_concurrent_parent(monkeypatch, tmp_path):
    state = {"folder_id": "", "creates": 0}

    class Response:
        status_code = 200
        headers = {"Location": "https://upload.test/session"}
        text = ""

        def __init__(self, payload=None):
            self._payload = payload or {}

        def json(self):
            return self._payload

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, method, url, **kwargs):
            if method == "GET":
                await asyncio.sleep(0.01)
                files = [{"id": state["folder_id"], "name": "folderA"}] if state["folder_id"] else []
                return Response({"files": files})
            if url == f"{drive_mod.DRIVE_API}/files":
                state["creates"] += 1
                state["folder_id"] = "folderA-id"
                return Response({"id": "folderA-id", "name": "folderA"})
            return Response()

        async def put(self, url, **kwargs):
            return Response({"id": "file1", "name": "image.jpg"})

    monkeypatch.setattr(drive_mod.httpx, "AsyncClient", Client)
    root = tmp_path / "root"
    folder = root / "folderA"
    folder.mkdir(parents=True)
    for idx in range(3):
        (folder / f"{idx}.jpg").write_text("ok")

    out = asyncio.run(DriveProvider().upload_folder({"access_token": "A", "mount": False}, root, {"id": "root"}, JobState("drive-api-race", {})))

    assert out["uploaded"] == 3
    assert state["creates"] == 1

def test_drive_web_session_validate_does_not_call_api(tmp_path):
    out = asyncio.run(DriveProvider().validate_credentials({
        "access_token": "SAPISIDHASH old",
        "cookies": {"SAPISID": "s"},
    }))

    assert out == {"ok": True, "authMode": "web_session"}

def test_drive_web_session_large_upload_uses_resumable(monkeypatch, tmp_path):
    calls = []

    class Init:
        status_code = 200
        headers = {"Location": "https://upload.test/session"}

    class Done:
        status_code = 200
        headers = {}

        def json(self):
            return {"id": "file1", "title": "big.bin"}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            calls.append(("POST", url, kwargs.get("params")))
            return Init()

        async def put(self, url, **kwargs):
            calls.append(("PUT", url, kwargs["headers"].get("content-range")))
            return Done()

    monkeypatch.setattr(drive_mod.httpx, "AsyncClient", Client)
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * (drive_mod.WEB_MULTIPART_MAX + 1))

    out = asyncio.run(DriveProvider().upload_file({
        "access_token": "SAPISIDHASH old",
        "cookies": {"SAPISID": "s"},
    }, path, {"id": "root"}, JobState("drive-web-big", {})))

    assert out["id"] == "file1"
    assert calls[0][0] == "POST"
    assert calls[0][2]["uploadType"] == "resumable"
    assert "fields" not in calls[0][2]
    assert calls[1] == ("PUT", "https://upload.test/session", f"bytes 0-{path.stat().st_size - 1}/{path.stat().st_size}")


def test_drive_web_session_upload_maps_slash_to_root(monkeypatch, tmp_path):
    seen = {}

    class Response:
        status_code = 200

        def json(self):
            return {"id": "file1", "title": "a.txt"}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            seen["body"] = kwargs["content"]
            return Response()

    monkeypatch.setattr(drive_mod.httpx, "AsyncClient", Client)
    path = tmp_path / "a.txt"
    path.write_text("ok")

    out = asyncio.run(DriveProvider().upload_file({
        "access_token": "SAPISIDHASH old",
        "cookies": {"SAPISID": "s"},
    }, path, {"id": "/", "path": "/"}, JobState("drive-web-root", {})))

    assert out["id"] == "file1"
    assert b'"parents": [{"id": "root"}]' in seen["body"]

def test_drive_web_session_upload_preserves_relative_parent(monkeypatch, tmp_path):
    seen = {}

    class Response:
        status_code = 200
        headers = {}
        text = ""

        def __init__(self, payload=None):
            self._payload = payload or {}

        def json(self):
            return self._payload

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            return Response({"items": []})

        async def post(self, url, **kwargs):
            if url == f"{drive_mod.DRIVE_WEB_FILES_API}/files":
                seen["created"] = kwargs["json"]
                return Response({"id": "folderA-id", "title": "folderA"})
            seen["body"] = kwargs["content"]
            return Response({"id": "file1", "title": "image1.jpg"})

    monkeypatch.setattr(drive_mod.httpx, "AsyncClient", Client)
    path = tmp_path / "image1.jpg"
    path.write_text("ok")

    out = asyncio.run(DriveProvider().upload_file({
        "access_token": "SAPISIDHASH old",
        "cookies": {"SAPISID": "s"},
    }, path, {"id": "root", "relative_path": "folderA/image1.jpg"}, JobState("drive-web-folder", {})))

    assert out["id"] == "file1"
    assert seen["created"]["title"] == "folderA"
    assert seen["created"]["parents"] == [{"id": "root"}]
    assert b'"parents": [{"id": "folderA-id"}]' in seen["body"]

def test_drive_web_session_upload_folder_reuses_concurrent_parent(monkeypatch, tmp_path):
    state = {"folder_id": "", "creates": 0}

    class Response:
        status_code = 200
        headers = {}
        text = ""

        def __init__(self, payload=None):
            self._payload = payload or {}

        def json(self):
            return self._payload

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            await asyncio.sleep(0.01)
            items = [{"id": state["folder_id"], "title": "folderA"}] if state["folder_id"] else []
            return Response({"items": items})

        async def post(self, url, **kwargs):
            if url == f"{drive_mod.DRIVE_WEB_FILES_API}/files":
                state["creates"] += 1
                state["folder_id"] = "folderA-id"
                return Response({"id": "folderA-id", "title": "folderA"})
            return Response({"id": "file1", "title": "image.jpg"})

    monkeypatch.setattr(drive_mod.httpx, "AsyncClient", Client)
    root = tmp_path / "root"
    folder = root / "folderA"
    folder.mkdir(parents=True)
    for idx in range(3):
        (folder / f"{idx}.jpg").write_text("ok")

    out = asyncio.run(DriveProvider().upload_folder({
        "access_token": "SAPISIDHASH old",
        "cookies": {"SAPISID": "s"},
    }, root, {"id": "root"}, JobState("drive-web-race", {})))

    assert out["uploaded"] == 3
    assert state["creates"] == 1

class OneFileFolderSource:
    async def download_folder(self, credentials, folder_ref, local_dir: Path, progress: JobState):
        path = local_dir / "only.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok")
        return [path]


class UploadRecorder:
    def __init__(self):
        self.calls = []
        self.target_refs = []

    async def upload_file(self, credentials, local_path, target_ref, progress):
        self.calls.append(("file", local_path))
        self.target_refs.append(target_ref)
        return {"ok": True}

    async def upload_folder(self, credentials, local_dir, target_ref, progress):
        self.calls.append(("folder", local_dir))
        self.target_refs.append(target_ref)
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
        self.assertEqual(dst.calls[0][1].name, "input")

    def test_optimized_upload_new_goes_to_results_folder(self):
        class Source:
            async def download_file(self, credentials, file_ref, local_dir: Path, progress: JobState):
                path = local_dir / "a.jpg"
                path.write_bytes(b"x")
                return path

        dst = UploadRecorder()
        old = dict(PROVIDERS)
        old_optimize = image_optimizer.optimize_directory
        def fake_optimize(input_dir, output_dir, options, job_state, cancel_check=None):
            out = output_dir / "a.jpg"
            out.write_bytes(b"x")
            return [{"name": "a.jpg", "original_size": 1, "optimized_size": 1, "status": "ok", "quality": 95}]
        PROVIDERS.update({"fake-source": Source(), "fake-dst": dst})
        image_optimizer.optimize_directory = fake_optimize
        try:
            job = JobState("opt-results", {
                "source": {"provider": "fake-source", "items": [{"type": "file", "name": "a.jpg"}]},
                "target": {"provider": "fake-dst", "folder": {"id": "/photos", "path": "/photos"}},
                "options": {"cleanupAfterFinish": False, "optimize_image": True},
            })
            def confirm():
                for _ in range(1000):
                    if job.status == "waiting_confirmation":
                        job.confirm_action = "upload_new"
                        job.confirm_event.set()
                        return
                    time.sleep(0.01)
            thread = threading.Thread(target=confirm)
            thread.start()
            asyncio.run(run_transfer(job))
            thread.join(timeout=1)
        finally:
            PROVIDERS.clear()
            PROVIDERS.update(old)
            image_optimizer.optimize_directory = old_optimize
            __import__("src.utils.temp_storage", fromlist=["cleanup_job"]).cleanup_job("opt-results")

        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(dst.target_refs[0]["relative_path"], "results/a.jpg")

    def test_optimized_confirmation_timeout_uploads_new(self):
        class Source:
            async def download_file(self, credentials, file_ref, local_dir: Path, progress: JobState):
                path = local_dir / "a.jpg"
                path.write_bytes(b"x")
                return path

        dst = UploadRecorder()
        old = dict(PROVIDERS)
        old_optimize = image_optimizer.optimize_directory
        old_timeout = transfer_job_mod.CONFIRM_TIMEOUT_SECONDS
        def fake_optimize(input_dir, output_dir, options, job_state, cancel_check=None):
            out = output_dir / "a.jpg"
            out.write_bytes(b"x")
            return [{"name": "a.jpg", "original_size": 1, "optimized_size": 1, "status": "ok", "quality": 95}]
        PROVIDERS.update({"fake-source": Source(), "fake-dst": dst})
        image_optimizer.optimize_directory = fake_optimize
        transfer_job_mod.CONFIRM_TIMEOUT_SECONDS = 0.01
        try:
            job = JobState("opt-timeout", {
                "source": {"provider": "fake-source", "items": [{"type": "file", "name": "a.jpg"}]},
                "target": {"provider": "fake-dst", "folder": {}},
                "options": {"cleanupAfterFinish": False, "optimize_image": True, "replace": True},
            })
            asyncio.run(run_transfer(job))
        finally:
            PROVIDERS.clear()
            PROVIDERS.update(old)
            image_optimizer.optimize_directory = old_optimize
            transfer_job_mod.CONFIRM_TIMEOUT_SECONDS = old_timeout
            __import__("src.utils.temp_storage", fromlist=["cleanup_job"]).cleanup_job("opt-timeout")

        self.assertEqual(job.status, "completed", job.error)
        self.assertIn("Confirmation timeout", "\n".join(job.logs))
        self.assertEqual(dst.target_refs[0]["relative_path"], "results/a.jpg")

    def test_optimized_confirmation_timeout_falls_back_to_replace(self):
        class Source:
            async def download_file(self, credentials, file_ref, local_dir: Path, progress: JobState):
                path = local_dir / "a.jpg"
                path.write_bytes(b"x")
                return path

        class Dst(UploadRecorder):
            async def upload_file(self, credentials, local_path, target_ref, progress):
                if not (progress.payload.get("options") or {}).get("replace"):
                    raise ProviderFailure("UPLOAD_FAILED", "results folder unavailable")
                return await super().upload_file(credentials, local_path, target_ref, progress)

        dst = Dst()
        old = dict(PROVIDERS)
        old_optimize = image_optimizer.optimize_directory
        old_timeout = transfer_job_mod.CONFIRM_TIMEOUT_SECONDS
        def fake_optimize(input_dir, output_dir, options, job_state, cancel_check=None):
            out = output_dir / "a.jpg"
            out.write_bytes(b"x")
            return [{"name": "a.jpg", "original_size": 1, "optimized_size": 1, "status": "ok", "quality": 95}]
        PROVIDERS.update({"fake-source": Source(), "fake-dst": dst})
        image_optimizer.optimize_directory = fake_optimize
        transfer_job_mod.CONFIRM_TIMEOUT_SECONDS = 0.01
        try:
            job = JobState("opt-timeout-replace", {
                "source": {"provider": "fake-source", "items": [{"type": "file", "name": "a.jpg"}]},
                "target": {"provider": "fake-dst", "folder": {}},
                "options": {"cleanupAfterFinish": False, "optimize_image": True},
            })
            asyncio.run(run_transfer(job))
        finally:
            PROVIDERS.clear()
            PROVIDERS.update(old)
            image_optimizer.optimize_directory = old_optimize
            transfer_job_mod.CONFIRM_TIMEOUT_SECONDS = old_timeout
            __import__("src.utils.temp_storage", fromlist=["cleanup_job"]).cleanup_job("opt-timeout-replace")

        self.assertEqual(job.status, "completed", job.error)
        self.assertIn("retrying with replace", "\n".join(job.logs))
        self.assertTrue(job.payload["options"]["replace"])
        self.assertEqual(dst.target_refs[0]["relative_path"], "a.jpg")

    def test_optimized_folder_upload_new_goes_inside_selected_folder_results(self):
        class Source:
            async def download_folder(self, credentials, folder_ref, local_dir: Path, progress: JobState):
                path = local_dir / "a.jpg"
                path.write_bytes(b"x")
                return [path]

        dst = UploadRecorder()
        old = dict(PROVIDERS)
        old_optimize = image_optimizer.optimize_directory
        def fake_optimize(input_dir, output_dir, options, job_state, cancel_check=None):
            out = output_dir / "testC" / "a.jpg"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"x")
            return [{"name": "testC/a.jpg", "original_size": 1, "optimized_size": 1, "status": "ok", "quality": 95}]
        PROVIDERS.update({"fake-source": Source(), "fake-dst": dst})
        image_optimizer.optimize_directory = fake_optimize
        try:
            job = JobState("opt-folder-results", {
                "source": {"provider": "fake-source", "items": [{"type": "folder", "name": "testC", "id": "/testA/testC"}]},
                "target": {"provider": "fake-dst", "folder": {"id": "/testA/testC", "path": "/testA/testC"}},
                "options": {"cleanupAfterFinish": False, "optimize_image": True},
            })
            def confirm():
                for _ in range(1000):
                    if job.status == "waiting_confirmation":
                        job.confirm_action = "upload_new"
                        job.confirm_event.set()
                        return
                    time.sleep(0.01)
            thread = threading.Thread(target=confirm)
            thread.start()
            asyncio.run(run_transfer(job))
            thread.join(timeout=1)
        finally:
            PROVIDERS.clear()
            PROVIDERS.update(old)
            image_optimizer.optimize_directory = old_optimize
            __import__("src.utils.temp_storage", fromlist=["cleanup_job"]).cleanup_job("opt-folder-results")

        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(dst.calls[0][1].name, "testC")
        self.assertEqual(dst.target_refs[0]["relative_path"], "results/testC (optimized)")

    def test_optimized_folder_replace_uploads_into_selected_folder_without_nested_folder(self):
        class Source:
            async def download_folder(self, credentials, folder_ref, local_dir: Path, progress: JobState):
                path = local_dir / "a.jpg"
                path.write_bytes(b"x")
                return [path]

        dst = UploadRecorder()
        old = dict(PROVIDERS)
        old_optimize = image_optimizer.optimize_directory
        def fake_optimize(input_dir, output_dir, options, job_state, cancel_check=None):
            out = output_dir / "testC" / "a.jpg"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"x")
            return [{"name": "testC/a.jpg", "original_size": 1, "optimized_size": 1, "status": "ok", "quality": 95}]
        PROVIDERS.update({"fake-source": Source(), "fake-dst": dst})
        image_optimizer.optimize_directory = fake_optimize
        try:
            job = JobState("opt-folder-replace", {
                "source": {"provider": "fake-source", "items": [{"type": "folder", "name": "testC", "id": "/testA/testC"}]},
                "target": {"provider": "fake-dst", "folder": {"id": "/testA/testC", "path": "/testA/testC"}},
                "options": {"cleanupAfterFinish": False, "optimize_image": True},
            })
            def confirm():
                for _ in range(1000):
                    if job.status == "waiting_confirmation":
                        job.confirm_action = "replace"
                        job.confirm_event.set()
                        return
                    time.sleep(0.01)
            thread = threading.Thread(target=confirm)
            thread.start()
            asyncio.run(run_transfer(job))
            thread.join(timeout=1)
        finally:
            PROVIDERS.clear()
            PROVIDERS.update(old)
            image_optimizer.optimize_directory = old_optimize
            __import__("src.utils.temp_storage", fromlist=["cleanup_job"]).cleanup_job("opt-folder-replace")

        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(dst.calls[0][1].name, "testC")
        self.assertNotIn("relative_path", dst.target_refs[0])

    def test_optimized_folder_skips_video_before_download(self):
        class Source(base_mod.BaseProvider):
            def __init__(self):
                self.downloaded = []

            async def validate_credentials(self, credentials):
                return {"ok": True}

            async def list_files(self, credentials, path_or_id):
                return {"items": [
                    {"type": "file", "name": "a.jpg", "id": "a"},
                    {"type": "file", "name": "clip.mp4", "id": "v"},
                ]}

            async def download_file(self, credentials, file_ref, local_path: Path, progress: JobState):
                self.downloaded.append(file_ref["name"])
                path = local_path if local_path.suffix else local_path / file_ref["name"]
                path.write_bytes(b"x")
                return path

            async def upload_file(self, credentials, local_path, target_ref, progress):
                return {"ok": True}

        src = Source()
        dst = UploadRecorder()
        old = dict(PROVIDERS)
        old_optimize = image_optimizer.optimize_directory
        def fake_optimize(input_dir, output_dir, options, job_state, cancel_check=None):
            out = output_dir / "root" / "a.jpg"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"x")
            return [{"name": "root/a.jpg", "original_size": 1, "optimized_size": 1, "status": "ok", "quality": 95}]
        PROVIDERS.update({"fake-source": src, "fake-dst": dst})
        image_optimizer.optimize_directory = fake_optimize
        try:
            job = JobState("opt-folder-skip-video", {
                "source": {"provider": "fake-source", "items": [{"type": "folder", "name": "root", "id": "/root"}]},
                "target": {"provider": "fake-dst", "folder": {}},
                "options": {"cleanupAfterFinish": False, "optimize_image": True},
            })
            def confirm():
                for _ in range(1000):
                    if job.status == "waiting_confirmation":
                        job.confirm_action = "replace"
                        job.confirm_event.set()
                        return
                    time.sleep(0.01)
            thread = threading.Thread(target=confirm)
            thread.start()
            asyncio.run(run_transfer(job))
            thread.join(timeout=1)
        finally:
            PROVIDERS.clear()
            PROVIDERS.update(old)
            image_optimizer.optimize_directory = old_optimize
            __import__("src.utils.temp_storage", fromlist=["cleanup_job"]).cleanup_job("opt-folder-skip-video")

        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(src.downloaded, ["a.jpg"])

    def test_optimize_queue_processes_folders_one_at_a_time(self):
        events = []

        class Source:
            async def download_folder(self, credentials, folder_ref, local_dir: Path, progress: JobState):
                events.append(f"download:{folder_ref['name']}")
                path = local_dir / "a.jpg"
                path.write_bytes(b"x")
                return [path]

        class Dst(UploadRecorder):
            async def upload_folder(self, credentials, local_dir, target_ref, progress):
                events.append(f"upload:{local_dir.name}")
                return await super().upload_folder(credentials, local_dir, target_ref, progress)

        dst = Dst()
        old = dict(PROVIDERS)
        old_optimize = image_optimizer.optimize_directory
        def fake_optimize(input_dir, output_dir, options, job_state, cancel_check=None):
            name = next(p.name for p in input_dir.iterdir() if p.is_dir())
            events.append(f"opt:{name}")
            out = output_dir / name / "a.jpg"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"x")
            return [{"name": f"{name}/a.jpg", "original_size": 1, "optimized_size": 1, "status": "ok", "quality": 95}]
        PROVIDERS.update({"fake-source": Source(), "fake-dst": dst})
        image_optimizer.optimize_directory = fake_optimize
        try:
            job = JobState("opt-queue-batches", {
                "source": {"provider": "fake-source", "items": [
                    {"type": "folder", "name": "A", "id": "/A"},
                    {"type": "folder", "name": "B", "id": "/B"},
                ]},
                "target": {"provider": "fake-dst", "folder": {"id": "/", "path": "/"}},
                "options": {"cleanupAfterFinish": False, "optimize_image": True},
            })
            def confirm():
                for _ in range(1000):
                    if job.status == "waiting_confirmation":
                        job.confirm_action = "upload_new"
                        job.confirm_event.set()
                        return
                    time.sleep(0.01)
            thread = threading.Thread(target=confirm)
            thread.start()
            asyncio.run(run_transfer(job))
            thread.join(timeout=1)
        finally:
            PROVIDERS.clear()
            PROVIDERS.update(old)
            image_optimizer.optimize_directory = old_optimize
            __import__("src.utils.temp_storage", fromlist=["cleanup_job"]).cleanup_job("opt-queue-batches")

        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(events, ["download:A", "opt:A", "upload:A", "download:B", "opt:B", "upload:B"])
        self.assertEqual([r["relative_path"] for r in dst.target_refs], ["results/A (optimized)", "results/B (optimized)"])
        self.assertEqual(len([line for line in job.logs if "Waiting for confirmation" in line]), 1)

    def test_optimized_upload_failure_waits_for_retry_account(self):
        class Source:
            async def download_file(self, credentials, file_ref, local_dir: Path, progress: JobState):
                path = local_dir / "a.jpg"
                path.write_bytes(b"x")
                return path

        class Dst(UploadRecorder):
            async def upload_file(self, credentials, local_path, target_ref, progress):
                if credentials.get("account") != "b":
                    raise ProviderFailure("UPLOAD_FAILED", "insufficient storage")
                return await super().upload_file(credentials, local_path, target_ref, progress)

        dst = Dst()
        old = dict(PROVIDERS)
        old_optimize = image_optimizer.optimize_directory
        def fake_optimize(input_dir, output_dir, options, job_state, cancel_check=None):
            out = output_dir / "a.jpg"
            out.write_bytes(b"x")
            return [{"name": "a.jpg", "original_size": 1, "optimized_size": 1, "status": "ok", "quality": 95}]
        PROVIDERS.update({"fake-source": Source(), "fake-dst": dst})
        image_optimizer.optimize_directory = fake_optimize
        try:
            job = JobState("opt-upload-retry", {
                "source": {"provider": "fake-source", "items": [{"type": "file", "name": "a.jpg"}]},
                "target": {"provider": "fake-dst", "folder": {}, "credentials": {"account": "a"}},
                "options": {"cleanupAfterFinish": False, "optimize_image": True},
            })
            def confirm():
                for _ in range(1000):
                    if job.status == "waiting_confirmation":
                        job.confirm_action = "upload_new"
                        job.confirm_event.set()
                        break
                    time.sleep(0.01)
                for _ in range(1000):
                    if job.status == "waiting_target_account":
                        job.payload["target"]["credentials"] = {"account": "b"}
                        job.confirm_action = "retry_upload"
                        job.confirm_event.set()
                        return
                    time.sleep(0.01)
            thread = threading.Thread(target=confirm)
            thread.start()
            asyncio.run(run_transfer(job))
            thread.join(timeout=1)
        finally:
            PROVIDERS.clear()
            PROVIDERS.update(old)
            image_optimizer.optimize_directory = old_optimize
            __import__("src.utils.temp_storage", fromlist=["cleanup_job"]).cleanup_job("opt-upload-retry")

        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(len(dst.calls), 1)

    def test_optimized_batches_cleanup_only_uploaded_item_on_failure(self):
        from src.utils.temp_storage import cleanup_job, job_dirs

        class Source:
            async def download_file(self, credentials, file_ref, local_dir: Path, progress: JobState):
                path = local_dir / f"{file_ref['id']}.jpg"
                path.write_bytes(b"x")
                return path

        class Dst(UploadRecorder):
            async def upload_file(self, credentials, local_path, target_ref, progress):
                if local_path.name == "b.jpg":
                    raise ProviderFailure("OTHER", "boom")
                return await super().upload_file(credentials, local_path, target_ref, progress)

        dst = Dst()
        old = dict(PROVIDERS)
        old_optimize = image_optimizer.optimize_directory
        def fake_optimize(input_dir, output_dir, options, job_state, cancel_check=None):
            src = next(input_dir.glob("*.jpg"))
            out = output_dir / src.name
            out.write_bytes(b"x")
            return [{"name": src.name, "original_size": 1, "optimized_size": 1, "status": "ok", "quality": 95}]
        PROVIDERS.update({"fake-source": Source(), "fake-dst": dst})
        image_optimizer.optimize_directory = fake_optimize
        try:
            job = JobState("opt-cleanup-fail", {
                "source": {"provider": "fake-source", "items": [
                    {"type": "file", "id": "a", "name": "a.jpg"},
                    {"type": "file", "id": "b", "name": "b.jpg"},
                ]},
                "target": {"provider": "fake-dst", "folder": {}},
                "options": {"cleanupAfterFinish": True, "optimize_image": True},
            })
            def confirm():
                for _ in range(1000):
                    if job.status == "waiting_confirmation":
                        job.confirm_action = "upload_new"
                        job.confirm_event.set()
                        return
                    time.sleep(0.01)
            thread = threading.Thread(target=confirm)
            thread.start()
            dirs = job_dirs("opt-cleanup-fail")
            asyncio.run(run_transfer(job))
            thread.join(timeout=1)
            self.assertEqual(job.status, "failed")
            self.assertFalse((dirs["input"] / "batch-0").exists())
            self.assertFalse((dirs["output"] / "batch-0").exists())
            self.assertTrue((dirs["input"] / "batch-1").exists())
            self.assertTrue((dirs["output"] / "batch-1").exists())
            self.assertEqual(job.completed_items, [{"provider": "fake-source", "id": "a", "path": "a"}])
        finally:
            PROVIDERS.clear()
            PROVIDERS.update(old)
            image_optimizer.optimize_directory = old_optimize
            cleanup_job("opt-cleanup-fail")

    def test_folder_source_uses_path_basename(self):
        from src.utils.temp_storage import job_dirs

        src = PathCapturingFolderSource()
        dst = UploadRecorder()
        old = dict(PROVIDERS)
        PROVIDERS.update({"fake-source": src, "fake-dst": dst})
        try:
            job = JobState("terabox-folder-path", {
                "source": {"provider": "fake-source", "items": [{"type": "folder", "name": "/ZIP HOME/九言 zip/Coser@九言 - 2026年02月月票2 卡芙卡自拍", "id": "/ZIP HOME/九言 zip/Coser@九言 - 2026年02月月票2 卡芙卡自拍"}]},
                "target": {"provider": "fake-dst", "folder": {}},
                "options": {"cleanupAfterFinish": False},
            })
            dirs = job_dirs("terabox-folder-path")
            asyncio.run(run_transfer(job))
            self.assertEqual(job.status, "completed", job.error)
            self.assertEqual(src.dirs[0], dirs["input"] / "Coser@九言 - 2026年02月月票2 卡芙卡自拍")
            self.assertEqual(dst.calls[0], ("folder", dirs["input"]))
            self.assertEqual(dst.target_refs[0], {})
        finally:
            PROVIDERS.clear()
            PROVIDERS.update(old)
            __import__("src.utils.temp_storage", fromlist=["cleanup_job"]).cleanup_job("terabox-folder-path")

    def test_progress_phases_skip_extract_when_disabled(self):
        job = JobState("no-extract", {"options": {"extract": False}})
        self.assertEqual(job.view()["phases"], ["download", "upload"])

    def test_progress_totals_are_cumulative_for_folder_files(self):
        job = JobState("folder-progress", {})
        job._tick_at -= 2
        job.add_bytes(5, 5, "download", "a.bin")
        job._tick_at -= 2
        job.add_bytes(7, 7, "download", "b.bin")

        view = job.view()
        self.assertEqual(view["bytesDone"], 12)
        self.assertEqual(view["bytesTotal"], 12)
        self.assertEqual(view["bytesOverallDone"], 12)
        self.assertEqual(view["bytesOverallTotal"], 12)
        self.assertEqual(job.progress.download, 100)
        self.assertGreater(job.speed, 0)

    def test_progress_overall_counts_download_and_upload(self):
        job = JobState("overall-progress", {})
        job.add_bytes(5, 10, "download", "a.bin")
        job.add_bytes(5, 10, "upload", "a.bin")

        view = job.view()
        self.assertEqual(view["bytesDone"], 5)
        self.assertEqual(view["bytesTotal"], 10)
        self.assertEqual(view["bytesOverallDone"], 10)
        self.assertEqual(view["bytesOverallTotal"], 20)

    def test_stream_download_resumes_after_incomplete_body(self):
        class Stream:
            def __init__(self, calls):
                self.calls = calls
                self.status_code = 206 if len(calls) == 2 else 200
                self.headers = {"content-length": "3" if self.status_code == 206 else "6"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def raise_for_status(self):
                return None

            async def aiter_bytes(self, size):
                if self.status_code == 200:
                    yield b"abc"
                    raise base_mod.httpx.RemoteProtocolError("peer closed connection without sending complete message body")
                yield b"def"

        class Client:
            def __init__(self, *args, **kwargs):
                return None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def stream(self, method, url, headers):
                calls.append(headers)
                return Stream(calls)

        async def no_sleep(delay):
            sleeps.append(delay)
            return None

        calls = []
        sleeps = []
        old_client = base_mod.httpx.AsyncClient
        old_sleep = base_mod.asyncio.sleep
        base_mod.httpx.AsyncClient = Client
        base_mod.asyncio.sleep = no_sleep
        try:
            with __import__("tempfile").TemporaryDirectory() as tmp:
                dest = Path(tmp) / "file.bin"
                asyncio.run(base_mod.stream_download("https://example.test/file", dest, JobState("resume", {})))
                self.assertEqual(dest.read_bytes(), b"abcdef")
        finally:
            base_mod.httpx.AsyncClient = old_client
            base_mod.asyncio.sleep = old_sleep

        self.assertEqual(calls[1]["Range"], "bytes=3-")
        self.assertEqual(sleeps, [])

    def test_folder_downloads_are_concurrent_and_bounded(self):
        class Provider(base_mod.BaseProvider):
            name = "parallel"

            def __init__(self):
                self.active = 0
                self.max_active = 0

            async def validate_credentials(self, credentials):
                return {"ok": True}

            async def list_files(self, credentials, path_or_id):
                return {"items": [{"id": str(i), "name": f"{i}.txt", "type": "file"} for i in range(4)]}

            async def download_file(self, credentials, file_ref, local_path, progress):
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                await asyncio.sleep(0.01)
                self.active -= 1
                local_path.parent.mkdir(parents=True, exist_ok=True)
                local_path.write_text(str(file_ref["id"]))
                return local_path

            async def upload_file(self, credentials, local_path, target_ref, progress):
                return {"ok": True}

        old = base_mod.FOLDER_DOWNLOAD_CONCURRENCY
        base_mod.FOLDER_DOWNLOAD_CONCURRENCY = 2
        try:
            with __import__("tempfile").TemporaryDirectory() as tmp:
                provider = Provider()
                saved = asyncio.run(provider.download_folder({}, {"id": "/"}, Path(tmp), JobState("download-parallel", {})))
        finally:
            base_mod.FOLDER_DOWNLOAD_CONCURRENCY = old

        self.assertEqual(provider.max_active, 2)
        self.assertEqual([p.name for p in saved], ["0.txt", "1.txt", "2.txt", "3.txt"])

    def test_selected_files_downloads_are_concurrent_and_bounded(self):
        class Source(base_mod.BaseProvider):
            name = "selected-parallel"

            def __init__(self):
                self.active = 0
                self.max_active = 0

            async def validate_credentials(self, credentials):
                return {"ok": True}

            async def download_file(self, credentials, file_ref, local_path, progress):
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                await asyncio.sleep(0.01)
                self.active -= 1
                out = local_path / f"{file_ref['id']}.txt"
                out.write_text(str(file_ref["id"]))
                return out

            async def upload_file(self, credentials, local_path, target_ref, progress):
                return {"ok": True}

        src = Source()
        dst = UploadRecorder()
        old_providers = dict(PROVIDERS)
        old_concurrency = __import__("src.jobs.transfer_job", fromlist=["FOLDER_DOWNLOAD_CONCURRENCY"])
        old_value = old_concurrency.FOLDER_DOWNLOAD_CONCURRENCY
        old_concurrency.FOLDER_DOWNLOAD_CONCURRENCY = 2
        PROVIDERS.update({"selected-parallel": src, "fake-dst": dst})
        try:
            job = JobState("selected-download-parallel", {
                "source": {"provider": "selected-parallel", "items": [{"type": "file", "id": str(i), "name": f"{i}.txt"} for i in range(4)]},
                "target": {"provider": "fake-dst", "folder": {}},
                "options": {"cleanupAfterFinish": True},
            })
            asyncio.run(run_transfer(job))
        finally:
            PROVIDERS.clear()
            PROVIDERS.update(old_providers)
            old_concurrency.FOLDER_DOWNLOAD_CONCURRENCY = old_value

        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(src.max_active, 2)
        self.assertEqual(len(dst.calls), 1)

    def test_folder_uploads_are_concurrent_and_bounded(self):
        class Provider(base_mod.BaseProvider):
            name = "parallel"

            def __init__(self):
                self.active = 0
                self.max_active = 0

            async def validate_credentials(self, credentials):
                return {"ok": True}

            async def download_file(self, credentials, file_ref, local_path, progress):
                return local_path

            async def upload_file(self, credentials, local_path, target_ref, progress):
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                await asyncio.sleep(0.01)
                self.active -= 1
                if local_path.name == "2.txt":
                    raise ProviderFailure("UPLOAD_FAILED", "duplicated")
                return {"name": local_path.name}

        old = base_mod.FOLDER_UPLOAD_CONCURRENCY
        base_mod.FOLDER_UPLOAD_CONCURRENCY = 2
        try:
            with __import__("tempfile").TemporaryDirectory() as tmp:
                root = Path(tmp)
                for i in range(4):
                    (root / f"{i}.txt").write_text(str(i))
                provider = Provider()
                out = asyncio.run(provider.upload_folder({}, root, {}, JobState("upload-parallel", {})))
        finally:
            base_mod.FOLDER_UPLOAD_CONCURRENCY = old

        self.assertEqual(provider.max_active, 2)
        self.assertEqual(out["uploaded"], 3)
        self.assertEqual(out["skipped"], 1)

    def test_terabox_upload_falls_back_to_lower_concurrency(self):
        class Provider(TeraBoxProvider):
            def __init__(self):
                self.concurrency = []

            async def _ensure_relative_parent(self, s, parent, relative_path):
                return "/"

            async def _precreate_upload(self, s, remote_path, parent, size, hashes, **kwargs):
                return {"uploadid": "up1"}

            async def _locate_upload_hosts(self, s):
                return ["https://upload.test"]

            async def _upload_parts(self, s, host, local_path, remote_path, upload_id, size, mime, progress, concurrency):
                self.concurrency.append(concurrency)
                if concurrency == 32:
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

        self.assertEqual(provider.concurrency[:2], [32, 16])
        self.assertEqual(job.progress.upload, 100)

    def test_terabox_reuses_existing_results_folder(self):
        class Session:
            base = "https://www.terabox.com"

            def params(self, **extra):
                return extra

            def headers(self):
                return {}

            async def request_json(self, method, url, *, context, **kwargs):
                self.calls.append((method, context, kwargs.get("data") or {}))
                if context == "list /photos":
                    return {"list": [{"isdir": 1, "server_filename": "results", "path": "/photos/results"}]}
                if context == "list /photos/results":
                    return {"list": []}
                return {}

        s = Session()
        s.calls = []
        parent = asyncio.run(TeraBoxProvider()._ensure_relative_parent(s, "/photos", "results/a.jpg"))

        self.assertEqual(parent, "/photos/results")
        self.assertFalse(any(call[1].startswith("create folder /photos/results") for call in s.calls))

    def test_terabox_results_folder_creation_is_serialized(self):
        class Session:
            base = "https://www.terabox.com"

            def __init__(self):
                self.created = False
                self.creates = 0

            def params(self, **extra):
                return extra

            def headers(self):
                return {}

            async def request_json(self, method, url, *, context, **kwargs):
                await asyncio.sleep(0.01)
                if context == "list /photos" and self.created:
                    return {"list": [{"isdir": 1, "server_filename": "results", "path": "/photos/results"}]}
                if context == "create folder /photos/results":
                    self.created = True
                    self.creates += 1
                    return {}
                return {"list": []}

        async def run():
            s = Session()
            p = TeraBoxProvider()
            parents = await asyncio.gather(*(p._ensure_relative_parent(s, "/photos", "results/a.jpg") for _ in range(3)))
            return s.creates, parents

        creates, parents = asyncio.run(run())

        self.assertEqual(creates, 1)
        self.assertEqual(parents, ["/photos/results", "/photos/results", "/photos/results"])

    def test_pikpak_upload_waits_for_task_when_oss_params_missing(self):
        class Session:
            calls = 0

            async def req(self, method, url, **kwargs):
                self.calls += 1
                return {"id": "task1", "phase": "PHASE_TYPE_COMPLETE"}

        job = JobState("pikpak-task", {})
        out = asyncio.run(PikPakProvider()._wait_upload_task(Session(), "task1", job, timeout=1, poll=0))

        self.assertEqual(out["phase"], "PHASE_TYPE_COMPLETE")
        self.assertEqual(job.progress.upload, 100)

    def test_pikpak_upload_no_oss_params_waits_before_success(self):
        class Session:
            def __init__(self, credentials):
                self.credentials = credentials
                self.requests = []
                seen.append(self)

            async def req(self, method, url, **kwargs):
                self.requests.append((method, url))
                if method == "POST":
                    return {"file": {"params": {"task_id": "task1"}}, "task": {"id": "task1"}}
                return {"id": "task1", "phase": "PHASE_TYPE_COMPLETE"}

        class Provider(PikPakProvider):
            async def _ensure_relative_parent(self, s, parent_id, relative_path):
                return ""

        seen = []
        old = pikpak_mod.PikPakSession
        pikpak_mod.PikPakSession = Session
        try:
            with __import__("tempfile").TemporaryDirectory() as tmp:
                path = Path(tmp) / "a.txt"
                path.write_text("ok")
                job = JobState("pikpak-upload", {})
                out = asyncio.run(Provider().upload_file({"access_token": "t"}, path, {}, job))
        finally:
            pikpak_mod.PikPakSession = old

        self.assertEqual(out["task"]["phase"], "PHASE_TYPE_COMPLETE")
        self.assertEqual(job.status, "pending")
        self.assertEqual(job.progress.upload, 100)
        self.assertEqual([req[0] for req in seen[0].requests], ["POST", "GET"])

    def test_pikpak_upload_task_error_fails(self):
        class Session:
            async def req(self, method, url, **kwargs):
                return {"id": "task1", "phase": "PHASE_TYPE_ERROR"}

        with self.assertRaises(ProviderFailure) as ctx:
            asyncio.run(PikPakProvider()._wait_upload_task(Session(), "task1", JobState("pikpak-task-error", {}), timeout=1, poll=0))

        self.assertEqual(ctx.exception.code, "UPLOAD_FAILED")


class PathCapturingFolderSource:
    def __init__(self):
        self.dirs = []

    async def download_folder(self, credentials, folder_ref, local_dir: Path, progress: JobState):
        self.dirs.append(local_dir)
        local_dir.mkdir(parents=True, exist_ok=True)
        path = local_dir / "clip.mp4"
        path.write_text("ok")
        return [path]


class TransferJobPathSafetyTests(TestCase):
    def test_absolute_folder_name_stays_inside_job_input(self):
        from src.utils.temp_storage import job_dirs

        src = PathCapturingFolderSource()
        dst = UploadRecorder()
        old = dict(PROVIDERS)
        PROVIDERS.update({"fake-source": src, "fake-dst": dst})
        try:
            job = JobState("abs-folder-name", {
                "source": {"provider": "fake-source", "items": [{"type": "folder", "name": "/Shiroi", "id": "/Shiroi"}]},
                "target": {"provider": "fake-dst", "folder": {}},
                "options": {"cleanupAfterFinish": False},
            })
            dirs = job_dirs("abs-folder-name")
            asyncio.run(run_transfer(job))
            self.assertEqual(job.status, "completed", job.error)
            self.assertEqual(src.dirs[0], dirs["input"] / "Shiroi")
            self.assertTrue(src.dirs[0].is_relative_to(dirs["input"]))
            self.assertEqual(dst.calls[0], ("folder", dirs["input"]))
        finally:
            PROVIDERS.clear()
            PROVIDERS.update(old)
            __import__("src.utils.temp_storage", fromlist=["cleanup_job"]).cleanup_job("abs-folder-name")

    def test_empty_upload_root_fails_instead_of_reporting_success(self):
        class EmptySource:
            async def download_folder(self, credentials, folder_ref, local_dir: Path, progress: JobState):
                local_dir.mkdir(parents=True, exist_ok=True)
                ghost = local_dir / "gone.mp4"
                return [ghost]

        dst = UploadRecorder()
        old = dict(PROVIDERS)
        PROVIDERS.update({"fake-source": EmptySource(), "fake-dst": dst})
        try:
            job = JobState("empty-upload-root", {
                "source": {"provider": "fake-source", "items": [{"type": "folder", "name": "root"}]},
                "target": {"provider": "fake-dst", "folder": {}},
                "options": {"cleanupAfterFinish": True},
            })
            asyncio.run(run_transfer(job))
        finally:
            PROVIDERS.clear()
            PROVIDERS.update(old)

        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error["code"], "DOWNLOAD_FAILED")
        self.assertEqual(dst.calls, [])

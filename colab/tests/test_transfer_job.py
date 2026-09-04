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
from src.providers import terabox as terabox_mod
from src.providers import drive as drive_mod
from src.providers.drive import DriveProvider
from src.providers.links import LinksProvider

def test_colab_download_concurrency_default_is_cdn_friendly():
    from src.config import FOLDER_DOWNLOAD_CONCURRENCY

    assert FOLDER_DOWNLOAD_CONCURRENCY == 12

def test_links_provider_downloads_inside_directory_path(tmp_path, monkeypatch):
    provider = LinksProvider()

    async def no_deps():
        return None

    async def fake_download(url, dest_dir, name, progress):
        out = dest_dir / (name or "file.bin")
        out.write_text("ok")
        return [out]

    monkeypatch.setattr(provider, "_ensure_deps", no_deps)
    monkeypatch.setattr(provider, "_download_aria2", fake_download)

    out = asyncio.run(provider.download_file({}, {"id": "https://example.com/a.bin", "name": "a.bin"}, tmp_path, JobState("links-dir", {})))

    assert out == tmp_path / "a.bin"
    assert out.read_text() == "ok"

def test_links_provider_resolves_mediafire_before_aria2(tmp_path, monkeypatch):
    provider = LinksProvider()
    seen = {}

    class Response:
        text = '<a href="https://download1.mediafire.com/key/fileid/real%20file.rar" id="downloadButton">Download</a>'

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, *args, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            seen["page"] = url
            return Response()

    async def no_deps():
        return None

    async def fake_aria2(url, dest_dir, name, progress):
        seen.update({"url": url, "name": name})
        out = dest_dir / name
        out.write_text("ok")
        return [out]

    monkeypatch.setattr(provider, "_ensure_deps", no_deps)
    monkeypatch.setattr("src.providers.links.httpx.AsyncClient", Client)
    monkeypatch.setattr(provider, "_download_aria2", fake_aria2)

    out = asyncio.run(provider.download_file({}, {"id": "https://www.mediafire.com/file/id/name.rar/file", "name": "file"}, tmp_path, JobState("mediafire", {})))

    assert seen["url"].startswith("https://download1.mediafire.com/")
    assert seen["name"] == "real file.rar"
    assert out.name == "real file.rar"

def test_links_provider_treats_gofile_direct_download_as_direct(tmp_path, monkeypatch):
    provider = LinksProvider()
    seen = {}

    async def no_deps():
        return None

    async def fake_aria2(url, dest_dir, name, progress):
        seen["url"] = url
        out = dest_dir / (name or "file.rar")
        out.write_text("ok")
        return [out]

    monkeypatch.setattr(provider, "_ensure_deps", no_deps)
    monkeypatch.setattr(provider, "_download_aria2", fake_aria2)

    url = "https://store-na-phx-5.gofile.io/download/web/39d1667c/file.rar"
    out = asyncio.run(provider.download_file({}, {"id": url, "name": "file.rar"}, tmp_path, JobState("gofile-direct", {})))

    assert seen["url"] == url
    assert out.name == "file.rar"

def test_links_provider_rejects_gofile_page_with_direct_link_hint(tmp_path, monkeypatch):
    provider = LinksProvider()

    async def no_deps():
        return None

    monkeypatch.setattr(provider, "_ensure_deps", no_deps)

    try:
        asyncio.run(provider.download_file({}, {"id": "https://gofile.io/d/abc123", "name": "abc123"}, tmp_path, JobState("gofile-page", {})))
    except ProviderFailure as exc:
        assert exc.code == "SOURCE_FILE_NOT_FOUND"
        assert "store-*.gofile.io/download/web/" in exc.message
    else:
        raise AssertionError("expected ProviderFailure")

def test_links_provider_routes_magnet_and_torrent_to_aria2_torrent(tmp_path, monkeypatch):
    provider = LinksProvider()
    seen = []

    async def no_deps():
        return None

    async def fake_torrent(url, dest_dir, progress):
        seen.append(url)
        out = dest_dir / f"file{len(seen)}.bin"
        out.write_text("ok")
        return [out]

    monkeypatch.setattr(provider, "_ensure_deps", no_deps)
    monkeypatch.setattr(provider, "_download_torrent", fake_torrent)

    asyncio.run(provider.download_file({}, {"id": "magnet:?xt=urn:btih:abc", "name": "magnet"}, tmp_path, JobState("magnet", {})))
    asyncio.run(provider.download_file({}, {"id": "https://example.com/archive.torrent", "name": "archive.torrent"}, tmp_path, JobState("torrent", {})))

    assert seen == ["magnet:?xt=urn:btih:abc", "https://example.com/archive.torrent"]

def test_links_provider_reads_aria2_carriage_return_progress(tmp_path, monkeypatch):
    provider = LinksProvider()
    out = tmp_path / "done.bin"
    out.write_text("ok")

    class Stdout:
        def __init__(self):
            self.chunks = [
                b"[#abc 1MiB/4MiB(25%) CN:1 DL:1MiB]\r",
                b"[#abc 3MiB/4MiB(75%) CN:1 DL:2MiB]\r",
                b"",
            ]

        async def read(self, _size):
            return self.chunks.pop(0)

    class Proc:
        stdout = Stdout()
        returncode = 0

        async def wait(self):
            return None

    async def fake_exec(*args, **kwargs):
        return Proc()

    monkeypatch.setattr("src.providers.links.asyncio.create_subprocess_exec", fake_exec)
    job = JobState("aria2-cr", {})

    asyncio.run(provider._run_aria2_cmd(["aria2c", "magnet:?xt=urn:btih:abc"], tmp_path, job))

    assert job.progress.download == 75
    assert job.bytes_done == 3 * 1024 * 1024
    assert job.bytes_total == 4 * 1024 * 1024

def test_links_source_uploads_all_landed_files(tmp_path, monkeypatch):
    dirs = {"input": tmp_path / "input", "output": tmp_path / "output"}
    dirs["input"].mkdir()
    dirs["output"].mkdir()
    uploaded = []

    class LinkSource:
        async def download_file(self, credentials, item, local_path, progress):
            (local_path / "one.txt").write_text("1")
            (local_path / "nested").mkdir(exist_ok=True)
            (local_path / "nested" / "two.txt").write_text("2")
            progress.files_downloaded += 1
            return local_path / "one.txt"

    class Target:
        async def upload_folder(self, credentials, local_dir, target_ref, progress):
            uploaded.extend(p.relative_to(local_dir).as_posix() for p in local_dir.rglob("*") if p.is_file())
            progress.files_to_upload = len(uploaded)
            progress.files_uploaded = len(uploaded)
            return {"ok": True}

    monkeypatch.setattr(transfer_job_mod, "job_dirs", lambda job_id: dirs)
    monkeypatch.setitem(transfer_job_mod.PROVIDERS, "links", LinkSource())
    monkeypatch.setitem(transfer_job_mod.PROVIDERS, "drive", Target())

    payload = {
        "source": {"provider": "links", "items": [{"id": "https://example.com/archive.torrent", "type": "file"}]},
        "target": {"provider": "drive", "credentials": {}, "folder": {}},
        "options": {"cleanupAfterFinish": False},
    }
    job = JobState("links-all", payload)

    asyncio.run(run_transfer(job))

    assert job.status == "completed"
    assert set(uploaded) == {"nested/two.txt", "one.txt"}

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
    assert "-p-" in calls[0]

def test_extract_tries_no_password_before_candidates(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    (input_dir / "locked.zip").write_text("x")
    calls = []

    class Proc:
        def __init__(self, returncode):
            self.returncode = returncode

        async def communicate(self):
            return (b"" if self.returncode == 0 else b"Wrong password"), None

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        out_arg = next(arg for arg in args if str(arg).startswith("-o"))
        if not any(str(arg).startswith("-p") for arg in args):
            return Proc(2)
        Path(str(out_arg)[2:]).mkdir(parents=True, exist_ok=True)
        (Path(str(out_arg)[2:]) / "ok.jpg").write_text("ok")
        return Proc(0)

    monkeypatch.setattr(extractor_mod.shutil, "which", lambda name: "7z")
    monkeypatch.setattr(extractor_mod.asyncio, "create_subprocess_exec", fake_exec)

    job = JobState("pw-order", {})
    asyncio.run(extractor_mod.extract_archives(input_dir, output_dir, job, ["pw1", "pw2"]))

    assert not any(str(arg).startswith("-p") for arg in calls[0])
    assert "-ppw1" in calls[1]
    assert all("Archive password candidates" not in line for line in job.logs)

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

def test_archive_extract_flattens_same_name_root_folder(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    (input_dir / "folderA.rar").write_text("x")

    class Proc:
        returncode = 0

        async def communicate(self):
            return b"", None

    async def fake_exec(*args, **kwargs):
        target = Path(args[-1]) if args[0] == "unrar" else Path(next(arg for arg in args if str(arg).startswith("-o"))[2:])
        (target / "folderA").mkdir(parents=True, exist_ok=True)
        (target / "folderA" / "file.txt").write_text("ok")
        return Proc()

    monkeypatch.setattr(extractor_mod.shutil, "which", lambda name: name)
    monkeypatch.setattr(extractor_mod.asyncio, "create_subprocess_exec", fake_exec)

    out = asyncio.run(extractor_mod.extract_archives(input_dir, output_dir, JobState("flatten", {}), None))

    assert [p.relative_to(output_dir).as_posix() for p in out] == ["folderA/file.txt"]

def test_extract_without_archives_stages_input_files(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    (input_dir / "folder").mkdir()
    (input_dir / "folder" / "note.txt").write_text("ok")

    monkeypatch.setattr(extractor_mod.shutil, "which", lambda name: "7z")

    out = asyncio.run(extractor_mod.extract_archives(input_dir, output_dir, JobState("plain", {}), None))

    assert [p.relative_to(output_dir).as_posix() for p in out] == ["folder/note.txt"]

def test_optimize_extract_folder_download_keeps_archive_parts(tmp_path):
    class Provider(base_mod.BaseProvider):
        async def validate_credentials(self, credentials):
            return {"ok": True}

        async def list_files(self, credentials, path_or_id):
            return {"items": [
                {"type": "file", "id": "a1", "name": "set.part1.rar"},
                {"type": "file", "id": "a2", "name": "set.part2.rar"},
                {"type": "file", "id": "note", "name": "note.txt"},
            ]}

        async def download_file(self, credentials, file_ref, local_path: Path, progress: JobState):
            local_path.write_text("x")
            return local_path

        async def upload_file(self, credentials, local_path, target_ref, progress):
            return {"ok": True}

    job = JobState("archive-parts", {"options": {"optimize_image": True, "extract": True}})

    out = asyncio.run(Provider().download_folder({}, {"id": "/", "name": "root"}, tmp_path, job))

    assert {p.name for p in out} == {"set.part1.rar", "set.part2.rar"}
    assert any("note.txt" in line and "ignored by image optimizer" in line for line in job.logs)
    assert not any("set.part" in line and "ignored by image optimizer" in line for line in job.logs)

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
        out = []
        for archive in sorted(input_dir.glob("*.zip")):
            target = output_dir / archive.name
            target.write_text("zip")
            out.append(target)
        return out

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
    # A multi-file queue is one optimize pass, so extraction runs once for the whole batch.
    assert seen_passwords == [["pw1", "pw2"]]
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
        return base_mod.dict_lock(provider._folder_locks(provider), ("root", "a"))

    for provider in (DriveProvider(), TeraBoxProvider()):
        first = asyncio.run(lock(provider))
        second = asyncio.run(lock(provider))
        assert first is not second

def test_provider_folder_locks_are_per_key():
    async def locks(provider):
        store = provider._folder_locks(provider)
        return base_mod.dict_lock(store, ("root", "a")), base_mod.dict_lock(store, ("root", "b")), base_mod.dict_lock(store, ("root", "a"))

    for provider in (DriveProvider(), TeraBoxProvider()):
        first, other, again = asyncio.run(locks(provider))
        assert first is not other
        assert first is again

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
    monkeypatch.setattr(drive_mod, "API_MULTIPART_MAX", 1)
    path = tmp_path / "a.txt"
    path.write_text("ok")

    out = asyncio.run(DriveProvider().upload_file({"access_token": "A", "mount": False}, path, {"id": "root"}, JobState("drive-api-upload", {})))

    assert out["id"] == "file1"
    assert calls == [("POST", f"{drive_mod.DRIVE_UPLOAD_API}/files"), ("PUT", "https://upload.test/session")]

def test_drive_api_upload_uses_multipart_for_small_files(monkeypatch, tmp_path):
    calls = []
    seen = {}

    class Response:
        status_code = 200
        headers = {}
        text = ""

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
            calls.append((method, url, (kwargs.get("params") or {}).get("uploadType")))
            seen["body"] = kwargs["content"]
            seen["type"] = (kwargs.get("headers") or {}).get("Content-Type", "")
            return Response()

        async def put(self, url, **kwargs):
            calls.append(("PUT", url, None))
            return Response()

    monkeypatch.setattr(drive_mod.httpx, "AsyncClient", Client)
    path = tmp_path / "a.txt"
    path.write_text("ok")

    out = asyncio.run(DriveProvider().upload_file({"access_token": "A", "mount": False}, path, {"id": "root"}, JobState("drive-api-small", {})))

    assert out["id"] == "file1"
    assert calls == [("POST", f"{drive_mod.DRIVE_UPLOAD_API}/files", "multipart")]
    assert seen["type"].startswith("multipart/related; boundary=")
    assert isinstance(seen["body"], bytes)
    assert b'"parents": ["root"]' in seen["body"]
    assert seen["body"].endswith(b"\r\n\r\nok\r\n--" + seen["type"].split("boundary=")[1].encode() + b"--\r\n")

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
    monkeypatch.setattr(drive_mod, "API_MULTIPART_MAX", 1)
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
    def test_optimize_multiple_files_uses_one_optimize_pass(self):
        class Source:
            def __init__(self):
                self.downloaded = []

            async def download_file(self, credentials, file_ref, local_dir: Path, progress: JobState):
                self.downloaded.append(file_ref["name"])
                path = local_dir / file_ref["name"]
                path.write_bytes(b"x")
                return path

        src = Source()
        dst = UploadRecorder()
        optimize_calls = []
        old = dict(PROVIDERS)
        old_optimize = image_optimizer.optimize_directory
        def fake_optimize(input_dir, output_dir, options, job_state, cancel_check=None):
            optimize_calls.append(sorted(p.name for p in input_dir.glob("*.jpg")))
            output_dir.mkdir(parents=True, exist_ok=True)
            for name in optimize_calls[-1]:
                (output_dir / name).write_bytes(b"x")
            return [{"name": name, "original_size": 1, "optimized_size": 1, "status": "ok", "quality": 95} for name in optimize_calls[-1]]
        PROVIDERS.update({"fake-source": src, "fake-dst": dst})
        image_optimizer.optimize_directory = fake_optimize
        try:
            job = JobState("opt-many-files-one-pass", {
                "source": {"provider": "fake-source", "items": [
                    {"type": "file", "id": "a", "name": "a.jpg"},
                    {"type": "file", "id": "b", "name": "b.jpg"},
                    {"type": "file", "id": "c", "name": "c.jpg"},
                ]},
                "target": {"provider": "fake-dst", "folder": {}},
                "options": {"cleanupAfterFinish": False, "optimize_image": True, "confirm_action": "upload_new"},
            })
            asyncio.run(run_transfer(job))
        finally:
            PROVIDERS.clear()
            PROVIDERS.update(old)
            image_optimizer.optimize_directory = old_optimize
            __import__("src.utils.temp_storage", fromlist=["cleanup_job"]).cleanup_job("opt-many-files-one-pass")

        self.assertEqual(src.downloaded, ["a.jpg", "b.jpg", "c.jpg"])
        self.assertEqual(optimize_calls, [["a.jpg", "b.jpg", "c.jpg"]])
        self.assertEqual(job.status, "completed", job.error)

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
        self.assertEqual(dst.calls[0][1].name, "a.jpg")
        self.assertEqual(dst.target_refs[0]["relative_path"], "results/testC (optimized)/a.jpg")

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
        self.assertEqual(dst.calls[0][1].name, "a.jpg")
        self.assertEqual(dst.target_refs[0]["relative_path"], "a.jpg")

    def test_optimized_folder_skips_non_candidates_before_download(self):
        class Source(base_mod.BaseProvider):
            def __init__(self):
                self.downloaded = []

            async def validate_credentials(self, credentials):
                return {"ok": True}

            async def list_files(self, credentials, path_or_id):
                return {"items": [
                    {"type": "file", "name": "large.jpg", "id": "a", "size": 4 * 1024 * 1024},
                    {"type": "file", "name": "small.jpg", "id": "s", "size": 1024},
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
            out = output_dir / "root" / "large.jpg"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"x")
            return [{"name": "root/large.jpg", "original_size": 1, "optimized_size": 1, "status": "ok", "quality": 95}]
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
        self.assertEqual(src.downloaded, ["large.jpg"])
        self.assertIn("[1/1] Uploaded: large.jpg", job.logs)

    def test_optimize_queue_processes_folders_one_at_a_time(self):
        events = []

        class Source:
            async def download_folder(self, credentials, folder_ref, local_dir: Path, progress: JobState):
                events.append(f"download:{folder_ref['name']}")
                path = local_dir / "a.jpg"
                path.write_bytes(b"x")
                return [path]

        class Dst(UploadRecorder):
            async def upload_file(self, credentials, local_path, target_ref, progress):
                events.append(f"upload:{local_path.parent.name}")
                return await super().upload_file(credentials, local_path, target_ref, progress)

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
        self.assertEqual([r["relative_path"] for r in dst.target_refs], ["results/A (optimized)/a.jpg", "results/B (optimized)/a.jpg"])
        self.assertEqual(len([line for line in job.logs if "Waiting for confirmation" in line]), 1)

    def test_optimize_queue_processes_mixed_provider_accounts_in_one_job(self):
        class Provider:
            def __init__(self):
                self.downloads = []
                self.uploads = []

            async def download_file(self, credentials, file_ref, local_dir: Path, progress: JobState):
                self.downloads.append((credentials.get("account"), file_ref["name"]))
                path = local_dir / file_ref["name"]
                path.write_bytes(b"x")
                return path

            async def upload_file(self, credentials, local_path, target_ref, progress):
                self.uploads.append((credentials.get("account"), local_path.name, target_ref))
                return {"ok": True}

        p1 = Provider()
        p2 = Provider()
        old = dict(PROVIDERS)
        old_optimize = image_optimizer.optimize_directory
        def fake_optimize(input_dir, output_dir, options, job_state, cancel_check=None):
            src = next(input_dir.rglob("*.jpg"))
            out = output_dir / src.name
            out.write_bytes(b"x")
            return [{"name": src.name, "original_size": 1, "optimized_size": 1, "status": "ok", "quality": 95}]
        PROVIDERS.update({"p1": p1, "p2": p2})
        image_optimizer.optimize_directory = fake_optimize
        try:
            job = JobState("opt-mixed-scope", {
                "source": {"provider": "p1", "accountId": "a", "items": [
                    {"type": "file", "name": "a.jpg", "id": "/a/a.jpg", "path": "/a/a.jpg", "provider": "p1", "accountId": "a", "credentials": {"account": "a"}},
                    {"type": "file", "name": "b.jpg", "id": "/b/b.jpg", "path": "/b/b.jpg", "provider": "p2", "accountId": "b", "credentials": {"account": "b"}},
                ]},
                "target": {"provider": "p1", "accountId": "a", "folder": {"id": "/", "path": "/"}, "credentials": {"account": "a"}},
                "options": {"cleanupAfterFinish": False, "optimize_image": True, "confirm_action": "upload_new"},
            })
            asyncio.run(run_transfer(job))
        finally:
            PROVIDERS.clear()
            PROVIDERS.update(old)
            image_optimizer.optimize_directory = old_optimize
            __import__("src.utils.temp_storage", fromlist=["cleanup_job"]).cleanup_job("opt-mixed-scope")

        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(p1.downloads, [("a", "a.jpg")])
        self.assertEqual(p2.downloads, [("b", "b.jpg")])
        self.assertEqual([u[0] for u in p1.uploads], ["a"])
        self.assertEqual([u[0] for u in p2.uploads], ["b"])
        self.assertEqual([u[2]["id"] for u in [*p1.uploads, *p2.uploads]], ["/a", "/b"])

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

    def test_optimized_folder_upload_retries_only_failed_file(self):
        events = []

        class Source:
            async def download_folder(self, credentials, folder_ref, local_dir: Path, progress: JobState):
                for name in ("a.jpg", "b.jpg"):
                    path = local_dir / name
                    path.write_bytes(b"x")
                return list(local_dir.glob("*.jpg"))

        class Dst:
            async def upload_file(self, credentials, local_path, target_ref, progress):
                events.append((credentials.get("account"), local_path.name))
                if local_path.name == "b.jpg" and credentials.get("account") != "b":
                    raise ProviderFailure("UPLOAD_FAILED", "part 0 failed HTTP 403")
                return {"ok": True}

        old = dict(PROVIDERS)
        old_optimize = image_optimizer.optimize_directory
        def fake_optimize(input_dir, output_dir, options, job_state, cancel_check=None):
            results = []
            for src in sorted(input_dir.rglob("*.jpg")):
                out = output_dir / src.relative_to(input_dir)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"x")
                results.append({"name": out.name, "original_size": 1, "optimized_size": 1, "status": "ok", "quality": 95})
            return results
        PROVIDERS.update({"fake-source": Source(), "fake-dst": Dst()})
        image_optimizer.optimize_directory = fake_optimize
        try:
            job = JobState("opt-folder-file-retry", {
                "source": {"provider": "fake-source", "items": [{"type": "folder", "name": "root", "id": "/root"}]},
                "target": {"provider": "fake-dst", "folder": {}, "credentials": {"account": "a"}},
                "options": {"cleanupAfterFinish": False, "optimize_image": True},
            })
            def confirm():
                for _ in range(1000):
                    if job.status == "waiting_confirmation":
                        job.confirm_action = "replace"
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
            __import__("src.utils.temp_storage", fromlist=["cleanup_job"]).cleanup_job("opt-folder-file-retry")

        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(events, [("a", "a.jpg"), ("a", "b.jpg"), ("b", "b.jpg")])

    def test_optimized_batches_cleanup_only_uploaded_item_on_failure(self):
        from src.utils.temp_storage import cleanup_job, job_dirs

        class Source:
            async def download_folder(self, credentials, folder_ref, local_dir: Path, progress: JobState):
                path = local_dir / f"{folder_ref['id']}.jpg"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x")
                return [path]

        class Dst(UploadRecorder):
            async def upload_file(self, credentials, local_path, target_ref, progress):
                if local_path.name == "b.jpg":
                    raise ProviderFailure("OTHER", "boom")
                return await super().upload_file(credentials, local_path, target_ref, progress)

        dst = Dst()
        old = dict(PROVIDERS)
        old_optimize = image_optimizer.optimize_directory
        def fake_optimize(input_dir, output_dir, options, job_state, cancel_check=None):
            src = next(input_dir.rglob("*.jpg"))
            out = output_dir / src.name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"x")
            return [{"name": src.name, "original_size": 1, "optimized_size": 1, "status": "ok", "quality": 95}]
        PROVIDERS.update({"fake-source": Source(), "fake-dst": dst})
        image_optimizer.optimize_directory = fake_optimize
        try:
            job = JobState("opt-cleanup-fail", {
                "source": {"provider": "fake-source", "items": [
                    {"type": "folder", "id": "a", "name": "a"},
                    {"type": "folder", "id": "b", "name": "b"},
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

    def test_stream_download_rejects_provider_json_error_as_image(self):
        body = b'{"errmsg":"need verify","request_id":9143768342077911970,"errno":400141}'

        class Stream:
            status_code = 200
            headers = {"content-length": str(len(body))}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def raise_for_status(self):
                return None

            async def aiter_bytes(self, size):
                yield body

        class Client:
            def __init__(self, *args, **kwargs):
                return None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def stream(self, method, url, headers):
                return Stream()

        old_client = base_mod.httpx.AsyncClient
        base_mod.httpx.AsyncClient = Client
        try:
            with __import__("tempfile").TemporaryDirectory() as tmp:
                dest = Path(tmp) / "bad.jpg"
                with self.assertRaises(ProviderFailure) as ctx:
                    asyncio.run(base_mod.stream_download("https://example.test/file", dest, JobState("json-error", {})))
                self.assertEqual(ctx.exception.code, "PROVIDER_NEEDS_VERIFY")
                self.assertEqual(ctx.exception.details["errno"], 400141)
                self.assertFalse(dest.exists())
                self.assertFalse((Path(tmp) / "bad.jpg.part").exists())
        finally:
            base_mod.httpx.AsyncClient = old_client

    def test_stream_download_verifies_and_retries_on_need_verify(self):
        error_body = b'{"errmsg":"need verify","errno":400141}'
        urls: list[str] = []

        class Stream:
            def __init__(self, url):
                self.url = url
                self.body = error_body if url.endswith("/first") else b"realbytes"
                self.status_code = 200
                self.headers = {"content-length": str(len(self.body))}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def raise_for_status(self):
                return None

            async def aiter_bytes(self, size):
                yield self.body

        class Client:
            def __init__(self, *args, **kwargs):
                return None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def stream(self, method, url, headers):
                urls.append(url)
                return Stream(url)

        verified: list[int] = []

        async def on_verify(round_index):
            verified.append(round_index)
            return {"url": "https://example.test/second", "headers": {"Cookie": "fresh=1"}}

        old_client = base_mod.httpx.AsyncClient
        base_mod.httpx.AsyncClient = Client
        try:
            with __import__("tempfile").TemporaryDirectory() as tmp:
                dest = Path(tmp) / "photo.jpg"
                job = JobState("verify-recovers", {})
                asyncio.run(base_mod.stream_download("https://example.test/first", dest, job, on_verify=on_verify))
                self.assertEqual(dest.read_bytes(), b"realbytes")
        finally:
            base_mod.httpx.AsyncClient = old_client

        self.assertEqual(verified, [0])
        self.assertEqual(urls, ["https://example.test/first", "https://example.test/second"])
        self.assertTrue(any("[VERIFY 1/2]" in line for line in job.logs), job.logs)

    def test_download_with_retry_waits_20s_three_times_then_raises(self):
        sleeps: list[float] = []
        attempts: list[int] = []

        async def no_sleep(delay):
            sleeps.append(delay)
            return None

        async def always_needs_verify():
            attempts.append(1)
            raise ProviderFailure("PROVIDER_NEEDS_VERIFY", "need verify")

        job = JobState("retry-then-skip", {})
        old_sleep = base_mod.asyncio.sleep
        base_mod.asyncio.sleep = no_sleep
        try:
            with self.assertRaises(ProviderFailure) as ctx:
                asyncio.run(base_mod.download_with_retry(always_needs_verify, progress=job, label="a.jpg"))
        finally:
            base_mod.asyncio.sleep = old_sleep

        self.assertEqual(ctx.exception.code, "PROVIDER_NEEDS_VERIFY")
        self.assertEqual(len(attempts), 4)  # first try + 3 retries
        self.assertEqual(sum(sleeps), 60.0)  # 3 x 20s, sliced for cancel checks
        self.assertEqual(len([line for line in job.logs if "[RETRY" in line]), 3)

    def test_download_with_retry_does_not_retry_dead_credentials(self):
        attempts: list[int] = []

        async def dead_session():
            attempts.append(1)
            raise ProviderFailure("INVALID_PROVIDER_CREDENTIALS", "cookie died")

        job = JobState("no-retry", {})
        with self.assertRaises(ProviderFailure) as ctx:
            asyncio.run(base_mod.download_with_retry(dead_session, progress=job, label="a.jpg"))
        self.assertEqual(ctx.exception.code, "INVALID_PROVIDER_CREDENTIALS")
        self.assertEqual(len(attempts), 1)

    def test_folder_download_skips_file_that_keeps_needing_verify(self):
        class Provider(base_mod.BaseProvider):
            name = "verify-folder"

            async def validate_credentials(self, credentials):
                return {"ok": True}

            async def list_files(self, credentials, path_or_id):
                return {"items": [{"id": "ok.jpg", "name": "ok.jpg", "type": "file"}, {"id": "bad.jpg", "name": "bad.jpg", "type": "file"}]}

            async def download_file(self, credentials, file_ref, local_path, progress):
                if file_ref["id"] == "bad.jpg":
                    raise ProviderFailure("PROVIDER_NEEDS_VERIFY", "need verify")
                local_path.parent.mkdir(parents=True, exist_ok=True)
                local_path.write_text("ok")
                return local_path

            async def upload_file(self, credentials, local_path, target_ref, progress):
                return {"ok": True}

        async def no_sleep(delay):
            return None

        old_sleep = base_mod.asyncio.sleep
        base_mod.asyncio.sleep = no_sleep
        try:
            with __import__("tempfile").TemporaryDirectory() as tmp:
                job = JobState("folder-skip", {})
                saved = asyncio.run(Provider().download_folder({}, {"id": "/"}, Path(tmp), job))
        finally:
            base_mod.asyncio.sleep = old_sleep

        self.assertEqual([p.name for p in saved], ["ok.jpg"])
        self.assertEqual(job.files_failed, 1)
        self.assertEqual([f["name"] for f in job.failed_files], ["bad.jpg"])
        self.assertTrue(any("[SKIP] Download failed after retries" in line for line in job.logs), job.logs)

    def test_failing_item_is_skipped_and_kept_in_queue(self):
        class Source:
            async def download_file(self, credentials, file_ref, local_dir: Path, progress: JobState):
                if file_ref["id"] == "bad":
                    raise ProviderFailure("PROVIDER_NEEDS_VERIFY", "need verify")
                path = local_dir / file_ref["name"]
                path.write_bytes(b"x")
                return path

        async def no_sleep(delay):
            return None

        dst = UploadRecorder()
        old = dict(PROVIDERS)
        old_sleep = base_mod.asyncio.sleep
        PROVIDERS.update({"fake-source": Source(), "fake-dst": dst})
        base_mod.asyncio.sleep = no_sleep
        try:
            job = JobState("skip-keeps-queue", {
                "source": {"provider": "fake-source", "items": [
                    {"type": "file", "id": "bad", "name": "bad.jpg"},
                    {"type": "file", "id": "good", "name": "good.jpg"},
                ]},
                "target": {"provider": "fake-dst", "folder": {}},
                "options": {"cleanupAfterFinish": False},
            })
            asyncio.run(run_transfer(job))
        finally:
            PROVIDERS.clear()
            PROVIDERS.update(old)
            base_mod.asyncio.sleep = old_sleep
            __import__("src.utils.temp_storage", fromlist=["cleanup_job"]).cleanup_job("skip-keeps-queue")

        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual([call[1].name for call in dst.calls], ["good.jpg"])
        self.assertEqual([entry["id"] for entry in job.failed_items], ["bad"])
        self.assertEqual([entry["id"] for entry in job.completed_items], ["good"])
        view = job.view()
        self.assertEqual([entry["id"] for entry in view["failedItems"]], ["bad"])
        self.assertTrue(any("Kept in queue" in line for line in job.logs), job.logs)

    def test_every_item_failing_fails_the_job_without_clearing_queue(self):
        class Source:
            async def download_file(self, credentials, file_ref, local_dir: Path, progress: JobState):
                raise ProviderFailure("PROVIDER_NEEDS_VERIFY", "need verify")

        async def no_sleep(delay):
            return None

        old = dict(PROVIDERS)
        old_sleep = base_mod.asyncio.sleep
        PROVIDERS.update({"fake-source": Source(), "fake-dst": UploadRecorder()})
        base_mod.asyncio.sleep = no_sleep
        try:
            job = JobState("skip-all", {
                "source": {"provider": "fake-source", "items": [
                    {"type": "file", "id": "a", "name": "a.jpg"},
                    {"type": "file", "id": "b", "name": "b.jpg"},
                ]},
                "target": {"provider": "fake-dst", "folder": {}},
                "options": {"cleanupAfterFinish": False},
            })
            asyncio.run(run_transfer(job))
        finally:
            PROVIDERS.clear()
            PROVIDERS.update(old)
            base_mod.asyncio.sleep = old_sleep
            __import__("src.utils.temp_storage", fromlist=["cleanup_job"]).cleanup_job("skip-all")

        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error["code"], "DOWNLOAD_FAILED")
        self.assertEqual(job.completed_items, [])
        self.assertEqual(sorted(entry["id"] for entry in job.failed_items), ["a", "b"])


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

    def test_optimized_output_uploads_are_concurrent_and_recover_once(self):
        events = []
        waits = []

        class Provider:
            active = 0
            max_active = 0

            async def upload_file(self, credentials, local_path, target_ref, progress):
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                try:
                    events.append((credentials.get("account"), local_path.name))
                    await asyncio.sleep(0.01)
                    if credentials.get("account") != "b":
                        raise ProviderFailure("UPLOAD_FAILED", "quota")
                    return {"ok": True}
                finally:
                    self.active -= 1

        async def fake_wait(job, target, exc):
            waits.append(exc.message)
            await asyncio.sleep(0.01)
            target.clear()
            target.update({"credentials": {"account": "b"}, "folder": {}})

        old_wait = transfer_job_mod._wait_for_retry_account
        old_concurrency = transfer_job_mod.UPLOAD_FILE_CONCURRENCY
        transfer_job_mod._wait_for_retry_account = fake_wait
        transfer_job_mod.UPLOAD_FILE_CONCURRENCY = 2
        try:
            with __import__("tempfile").TemporaryDirectory() as tmp:
                root = Path(tmp)
                for name in ("a.jpg", "b.jpg", "c.jpg", "d.jpg"):
                    (root / name).write_bytes(b"x")
                provider = Provider()
                job = JobState("optimized-parallel-retry", {})
                asyncio.run(transfer_job_mod._upload_outputs(job, {"credentials": {"account": "a"}, "folder": {}}, {}, provider, root, {"type": "folder"}))
        finally:
            transfer_job_mod._wait_for_retry_account = old_wait
            transfer_job_mod.UPLOAD_FILE_CONCURRENCY = old_concurrency

        self.assertEqual(provider.max_active, 2)
        self.assertEqual(waits, ["quota"])
        self.assertEqual(job.files_uploaded, 4)
        self.assertEqual([event for event in events if event[0] == "a"], [("a", "a.jpg"), ("a", "b.jpg")])

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
                path.write_bytes(b"x" * (terabox_mod.PART * 2 + 1))
                provider = Provider()
                job = JobState("upload", {})
                asyncio.run(provider.upload_file({"cookies": {"ndus": "x"}}, path, {"id": "/"}, job))
                single = Provider()
                single_path = Path(tmp) / "b.bin"
                single_path.write_bytes(b"x" * 9)
                with self.assertRaises(ProviderFailure):
                    asyncio.run(single.upload_file({"cookies": {"ndus": "x"}}, single_path, {"id": "/"}, JobState("upload-single", {})))
        finally:
            TeraBoxSession.ready = old_ready
            TeraBoxSession.request_json = old_request_json

        self.assertEqual(provider.concurrency[:2], [32, 16])
        self.assertEqual(job.progress.upload, 100)
        # One part cannot be split further: retrying at lower concurrency would repeat the same request.
        self.assertEqual(single.concurrency, [32])

    def test_terabox_precreate_rate_limit_raises_rate_limited(self):
        class Response:
            status_code = 200
            text = ""
            cookies = {}

            def json(self):
                return {"errno": 31034}

        class Session:
            base = "https://www.terabox.com"
            jstoken = "j"
            cookies = {}

            def params(self, **extra):
                return extra

            def headers(self):
                return {}

            def client(self):
                return self

            async def post(self, *args, **kwargs):
                return Response()

        with self.assertRaises(ProviderFailure) as ctx:
            asyncio.run(TeraBoxProvider()._precreate_upload(Session(), "/a.bin", "/", 1, {"chunks": ["x"], "file": "f", "slice": "s", "crc32": 1}))

        self.assertEqual(ctx.exception.code, "PROVIDER_RATE_LIMITED")

    def test_terabox_one_off_sessions_are_not_tracked(self):
        tracked = []
        closed = []

        class Response:
            status_code = 200
            text = ""
            cookies = {}

            def json(self):
                return {"data": {}, "list": []}

        class Client:
            is_closed = False

            def __init__(self, *args, **kwargs):
                pass

            async def request(self, method, url, **kwargs):
                return Response()

            async def aclose(self):
                self.is_closed = True
                closed.append(True)

        old_client = terabox_mod.httpx.AsyncClient
        old_track = terabox_mod.track_client
        terabox_mod.httpx.AsyncClient = Client
        terabox_mod.track_client = lambda client: tracked.append(client) or client
        try:
            out = asyncio.run(TeraBoxProvider().list_files({"cookies": {"ndus": "x"}, "jstoken": "j", "bdstoken": "b"}, "/"))
        finally:
            terabox_mod.httpx.AsyncClient = old_client
            terabox_mod.track_client = old_track

        self.assertEqual(out["items"], [])
        self.assertEqual(tracked, [])
        self.assertEqual(closed, [True])

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

    def test_terabox_refreshes_token_and_retries_on_need_verify_errno(self):
        bodies = [{"errno": 4000023, "errmsg": "need verify"}, {"errno": 0, "list": []}]
        sent: list[dict] = []

        class Response:
            status_code = 200
            cookies = {}

            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        class Client:
            is_closed = False

            async def request(self, method, url, **kwargs):
                sent.append(dict(kwargs.get("params") or {}))
                return Response(bodies[len(sent) - 1])

        s = TeraBoxSession({"cookies": {"ndus": "x"}, "jstoken": "stale", "bdstoken": "b"})
        s._api_client = Client()
        refreshed: list[bool] = []

        async def bootstrap(*, force=False):
            refreshed.append(force)
            s.jstoken = "fresh"

        s.bootstrap_tokens = bootstrap
        out = asyncio.run(s.request_json("GET", "https://www.terabox.com/api/list", context="list /", params=s.params()))

        self.assertEqual(out["list"], [])
        self.assertEqual(refreshed, [True])
        self.assertEqual([p.get("jsToken") for p in sent], ["stale", "fresh"])

    def test_terabox_persistent_verify_surfaces_needs_verify_code(self):
        class Response:
            status_code = 200
            cookies = {}

            def json(self):
                return {"errno": 400141, "errmsg": "need verify"}

        class Client:
            is_closed = False

            async def request(self, method, url, **kwargs):
                return Response()

        s = TeraBoxSession({"cookies": {"ndus": "x"}, "jstoken": "j", "bdstoken": "b"})
        s._api_client = Client()

        async def bootstrap(*, force=False):
            return None

        s.bootstrap_tokens = bootstrap
        with self.assertRaises(ProviderFailure) as ctx:
            asyncio.run(s.request_json("GET", "https://www.terabox.com/api/filemetas", context="filemetas /a.jpg", params=s.params()))
        self.assertEqual(ctx.exception.code, "PROVIDER_NEEDS_VERIFY")

    def test_terabox_download_sends_referer_and_verify_refetches_dlink(self):
        captured: dict = {}
        dlink_calls: list[str] = []

        class Provider(TeraBoxProvider):
            async def _session(self, credentials):
                return session

            async def _dlink(self, s, path):
                dlink_calls.append(path)
                return {"dlink": f"https://dm-d.terabox.com/file/{len(dlink_calls)}", "server_filename": "a.jpg", "src_location": "kul"}

        class Session:
            base = "https://www.terabox.com"
            cookies = {"ndus": "abc"}

            async def bootstrap_tokens(self, *, force=False):
                captured["forced"] = force

        session = Session()

        async def fake_stream(url, dest, progress, *, headers=None, phase="download", on_verify=None):
            captured["url"] = url
            captured["headers"] = dict(headers or {})
            captured["fresh"] = await on_verify(0)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"x")
            return dest

        old_stream = terabox_mod.stream_download
        terabox_mod.stream_download = fake_stream
        try:
            with __import__("tempfile").TemporaryDirectory() as tmp:
                asyncio.run(Provider().download_file({}, {"path": "/a.jpg", "name": "a.jpg"}, Path(tmp), JobState("tb-dl", {})))
        finally:
            terabox_mod.stream_download = old_stream

        self.assertEqual(captured["headers"]["Referer"], "https://www.terabox.com/")
        self.assertEqual(captured["headers"]["Cookie"], "ndus=abc")
        self.assertTrue(captured["forced"])
        self.assertEqual(dlink_calls, ["/a.jpg", "/a.jpg"])
        # Round 0 retries a freshly issued dlink rather than replaying the stale one.
        self.assertNotEqual(captured["fresh"]["url"], captured["url"])
        self.assertIn("terabox.com", captured["fresh"]["url"])
        self.assertEqual(captured["fresh"]["headers"]["Referer"], "https://www.terabox.com/")

    def test_dlink_candidates_mirror_known_edges(self):
        urls = terabox_mod._dlink_candidates("https://dm-d.terabox.com/file/abc?x=1", {"src_location": "kul"})
        self.assertEqual(urls[0], "https://dm-d.terabox.com/file/abc?x=1")
        self.assertGreater(len(urls), 1)
        self.assertTrue(all(url.endswith("/file/abc?x=1") for url in urls))
        self.assertIn("https://kul-d.terabox.com/file/abc?x=1", urls)

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

    def test_confirmation_strategy_replace_directly(self):
        class OneFileSource:
            async def download_file(self, credentials, file_ref, local_path: Path, progress: JobState):
                path = local_path / file_ref["name"]
                path.write_bytes(b"image data")
                return path

        dst = UploadRecorder()
        old = dict(PROVIDERS)
        old_optimize = image_optimizer.optimize_directory
        def fake_optimize(input_dir, output_dir, options, job_state, cancel_check=None):
            out = output_dir / "pic.jpg"
            out.write_bytes(b"opt data")
            return [{"name": "pic.jpg", "original_size": 10, "optimized_size": 8, "status": "ok"}]

        PROVIDERS.update({"fake-source": OneFileSource(), "fake-dst": dst})
        image_optimizer.optimize_directory = fake_optimize
        try:
            job = JobState("opt-strategy-replace", {
                "source": {"provider": "fake-source", "items": [{"type": "file", "name": "pic.jpg", "id": "f1"}]},
                "target": {"provider": "fake-dst", "folder": {}},
                "options": {"cleanupAfterFinish": False, "optimize_image": True, "confirm_action": "replace"},
            })
            asyncio.run(run_transfer(job))
        finally:
            PROVIDERS.clear()
            PROVIDERS.update(old)
            image_optimizer.optimize_directory = old_optimize
            __import__("src.utils.temp_storage", fromlist=["cleanup_job"]).cleanup_job("opt-strategy-replace")

        self.assertEqual(job.status, "completed", job.error)
        self.assertTrue(job.payload["options"].get("replace"))

    def test_confirmation_strategy_auto_upload_new(self):
        class OneFileSource:
            async def download_file(self, credentials, file_ref, local_path: Path, progress: JobState):
                path = local_path / file_ref["name"]
                path.write_bytes(b"image data")
                return path

        dst = UploadRecorder()
        old = dict(PROVIDERS)
        old_optimize = image_optimizer.optimize_directory
        def fake_optimize(input_dir, output_dir, options, job_state, cancel_check=None):
            out = output_dir / "pic.jpg"
            out.write_bytes(b"opt data")
            return [{"name": "pic.jpg", "original_size": 10, "optimized_size": 8, "status": "ok"}]

        PROVIDERS.update({"fake-source": OneFileSource(), "fake-dst": dst})
        image_optimizer.optimize_directory = fake_optimize
        try:
            job = JobState("opt-strategy-upload-new", {
                "source": {"provider": "fake-source", "items": [{"type": "file", "name": "pic.jpg", "id": "f1"}]},
                "target": {"provider": "fake-dst", "folder": {}},
                "options": {"cleanupAfterFinish": False, "optimize_image": True, "confirm_action": "upload_new"},
            })
            asyncio.run(run_transfer(job))
        finally:
            PROVIDERS.clear()
            PROVIDERS.update(old)
            image_optimizer.optimize_directory = old_optimize
            __import__("src.utils.temp_storage", fromlist=["cleanup_job"]).cleanup_job("opt-strategy-upload-new")

        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(job.payload["options"].get("upload_prefix"), "results")


def test_relay_monitor_swallows_dead_socket_and_stop_cancels_tasks():
    """A dropped relay socket must not surface as an unretrieved task exception."""
    from src import relay_client

    class DeadSocket:
        async def send(self, _raw):
            raise ConnectionResetError("no close frame received or sent")

    class Manager:
        def __init__(self, job):
            self.jobs = {job.job_id: job}

        def get(self, job_id):
            return self.jobs.get(job_id)

    job = JobState("relay-monitor", {})
    job.status = "running"

    async def scenario():
        # The monitor returns quietly instead of raising into an unawaited task.
        await relay_client._monitor(DeadSocket(), Manager(job), job.job_id)

        monitors = {"a": asyncio.create_task(asyncio.sleep(30)), "b": asyncio.create_task(asyncio.sleep(30))}
        relay_client._stop_monitors(monitors)
        await asyncio.sleep(0)
        return monitors

    tasks = asyncio.run(scenario())
    assert tasks == {}

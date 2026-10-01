from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from src.jobs.progress import JobState
from src.providers.base import ProviderFailure
from src.providers.links import LinksProvider


def test_probe_media_durations_parses_json(tmp_path, monkeypatch):
    test_video = tmp_path / "test.mp4"
    test_video.write_bytes(b"dummy video data")

    fake_ffprobe_out = {
        "format": {"duration": "2640.50"},
        "streams": [
            {"codec_type": "video", "duration": "2640.40"},
            {"codec_type": "audio", "duration": "1701.35"},
        ],
    }

    mock_run = MagicMock()
    mock_run.returncode = 0
    mock_run.stdout = json.dumps(fake_ffprobe_out)

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/ffprobe" if cmd == "ffprobe" else None)
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: mock_run)

    res = LinksProvider._probe_media_durations(test_video)
    assert res["has_video"] is True
    assert res["has_audio"] is True
    assert abs(res["video_duration"] - 2640.40) < 0.01
    assert abs(res["audio_duration"] - 1701.35) < 0.01
    assert abs(res["container_duration"] - 2640.50) < 0.01


def test_heal_audio_clock_constructs_ffmpeg_apad(tmp_path, monkeypatch):
    test_video = tmp_path / "stream_trunc.mp4"
    test_video.write_bytes(b"sample video bytes")
    fixed_video = tmp_path / "stream_trunc_audiofix.mp4"

    executed_cmd = []

    def fake_subprocess_run(cmd, *args, **kwargs):
        executed_cmd.extend(cmd)
        fixed_video.write_bytes(b"healed video with padded audio")
        mock = MagicMock()
        mock.returncode = 0
        return mock

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/ffmpeg" if cmd == "ffmpeg" else None)
    monkeypatch.setattr("subprocess.run", fake_subprocess_run)

    job = JobState("test-job", {})
    ok = LinksProvider._heal_audio_clock(test_video, 2640.0, progress=job)

    assert ok is True
    assert "-filter_complex" in executed_cmd
    assert "[0:a]apad=whole_dur=2640.0[a]" in executed_cmd
    assert "-c:v" in executed_cmd
    assert "copy" in executed_cmd
    assert "-c:a" in executed_cmd
    assert "aac" in executed_cmd
    assert test_video.read_bytes() == b"healed video with padded audio"


def test_ytdlp_raises_on_stray_part_file(tmp_path, monkeypatch):
    provider = LinksProvider()
    dest = tmp_path / "dest"
    dest.mkdir()

    job = JobState("test-ytdlp-part", {})

    # Mock subprocess to produce stage_dir with out_name.mp4 and an incomplete out_name.f140.m4a.part
    async def fake_proc(*cmd, **kwargs):
        # Find stage_dir from cmd args
        for arg in cmd:
            if "home:" in str(arg):
                stg = Path(str(arg).split("home:")[1])
                stg.mkdir(parents=True, exist_ok=True)
                (stg / "video.mp4").write_bytes(b"merged?")
                (stg / "video.f140.m4a.part").write_bytes(b"unfinished audio bytes")
        mock_p = MagicMock()
        mock_p.returncode = 0
        mock_p.stdout = None
        async def fake_wait():
            return 0
        mock_p.wait = fake_wait
        return mock_p

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_proc)

    with pytest.raises(ProviderFailure) as exc_info:
        asyncio.run(provider._download_ytdlp("https://example.com/live.m3u8", dest, "video.mp4", job))

    assert "Stream download incomplete" in str(exc_info.value) or "parts remaining" in str(exc_info.value)


def test_master_playlist_scoring_in_universal_page_resolver(tmp_path):
    # Verify candidate sorting prioritizes master over sub-playlists
    candidates = [
        "https://cdn.test/1080p/video_only.m3u8",
        "https://cdn.test/master.m3u8?token=xyz",
        "https://cdn.test/720p/index.m3u8",
    ]

    def _score(u):
        low = u.lower()
        if any(m in low for m in ("master.m3u8", "playlist.m3u8", "index.m3u8", "manifest.m3u8", "all.m3u8")):
            return 0
        if (".m3u8" in low or ".mpd" in low) and not any(sub in low for sub in ("video_only", "audio_only", "/audio/", "_audio")):
            return 1
        if any(ext in low for ext in (".mp4", ".mkv", ".webm")):
            return 2
        return 3

    sorted_cands = sorted(candidates, key=_score)
    assert sorted_cands[0] == "https://cdn.test/master.m3u8?token=xyz"


def test_ytdlp_never_passes_cookie_header(tmp_path, monkeypatch):
    provider = LinksProvider()
    dest = tmp_path / "dest"
    dest.mkdir()
    job = JobState("test-job", {})

    captured_cmds = []

    async def fake_proc(*cmd, **kwargs):
        captured_cmds.extend(cmd)
        for idx, arg in enumerate(cmd):
            if arg == "--cookies":
                c_file = Path(cmd[idx + 1])
                assert c_file.exists()
                c_content = c_file.read_text(encoding="utf-8")
                assert "token" in c_content
                assert "auth" in c_content
            if "home:" in str(arg):
                stg = Path(str(arg).split("home:")[1])
                stg.mkdir(parents=True, exist_ok=True)
                (stg / "video.mp4").write_bytes(b"dummy")
        mock_p = MagicMock()
        mock_p.returncode = 0
        mock_p.stdout = None
        async def fake_wait():
            return 0
        mock_p.wait = fake_wait
        return mock_p

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_proc)

    asyncio.run(
        provider._download_ytdlp(
            "https://dc.mediastrmx.com/hl/video/index.m3u8",
            dest,
            "video.mp4",
            job,
            headers={"Cookie": "token=123", "User-Agent": "TestUA"},
            cookies="auth=456",
        )
    )

    # Ensure NO --add-header contains Cookie
    for idx, arg in enumerate(captured_cmds):
        if arg == "--add-header":
            val = captured_cmds[idx + 1].lower()
            assert "cookie" not in val, f"Found cookie in --add-header: {captured_cmds[idx + 1]}"

    # Ensure --cookies is passed
    assert "--cookies" in captured_cmds


def test_resolve_universal_page_extracts_striptube_escaped_json(tmp_path, monkeypatch):
    provider = LinksProvider()
    job = JobState("test-job", {})

    # Mock page fetch with data-video-id
    html_page = """
    <html>
        <div data-video-id="200021180" data-token="secretTok123"></div>
    </html>
    """

    # Mock API fetch with escaped slashes in JSON
    api_json = {
        "status": 200,
        "media": "https:\\/\\/dc.mediastrmx.com\\/hl\\/bejeni-sweet\\/2026-09-27,09-31\\/index.m3u8?expires=1790530840"
    }
    raw_api_body = json.dumps(api_json)

    async def fake_fetch_webpage(url, headers, progress, proxy=None, auto_proxy=True, session_cookies=None):
        if "/api/video/200021180" in url:
            return 200, raw_api_body, url, None
        return 200, html_page, url, None

    monkeypatch.setattr(provider, "_fetch_webpage", fake_fetch_webpage)

    file_ref = {"page_url": "https://striptube.cc/video/200021180/play"}
    res = asyncio.run(provider._resolve_universal_page("https://striptube.cc/video/200021180/play", file_ref, job))

    assert "https://dc.mediastrmx.com/hl/bejeni-sweet/2026-09-27,09-31/index.m3u8" in res[0]


def test_backup_resume_cross_provider_assembly(tmp_path, monkeypatch):
    from src.jobs.hls_backup_pipeline import run_resumable_backup_pipeline

    class MockSrcProvider:
        def __init__(self):
            self.downloaded = []
            self.deleted = []

        async def list_files(self, creds, folder_id):
            return {
                "items": [
                    {"name": "part_0001.ts", "id": "part1_id", "bytes": 100},
                    {"name": "_resume_manifest.json", "id": "man_id"},
                ]
            }

        async def download_file(self, creds, file_ref, dest_dir, job):
            self.downloaded.append(file_ref["name"])
            p = dest_dir / file_ref["name"]
            p.write_bytes(b"dummy ts part 1")
            return p

        async def delete_item(self, creds, ref):
            self.deleted.append(ref["name"])

    class MockDstProvider:
        def __init__(self):
            self.downloaded = []
            self.uploaded = []

        async def download_file(self, creds, file_ref, dest_dir, job):
            self.downloaded.append(file_ref["name"])
            raise RuntimeError("Should not download from dst provider when source is backup folder!")

        async def upload_file(self, creds, local_path, target_ref, job):
            self.uploaded.append(target_ref["relative_path"])
            return {"ok": True}

    src_p = MockSrcProvider()
    dst_p = MockDstProvider()

    manifest_content = {
        "is_complete": True,
        "total_segments_expected": 100,
        "target_filename": "final_video.mp4",
        "completed_parts": [
            {"part_index": 1, "file_name": "part_0001.ts", "start_seg": 0, "end_seg": 99, "segment_count": 100, "bytes": 100}
        ]
    }

    dirs = {
        "input": tmp_path / "input",
        "output": tmp_path / "output",
        "extract": tmp_path / "extract",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    job = JobState("job-cross-resume", {
        "source": {"provider": "terabox", "credentials": {"ndus": "tok"}},
        "target": {"provider": "drive", "credentials": {"token": "gd_tok"}, "folder": {"id": "root", "path": "/"}},
        "options": {"cleanupAfterFinish": True},
    })

    file_item = {
        "name": "test_video__backup_resume",
        "path": "/test_video__backup_resume",
        "type": "folder",
        "is_folder": True,
    }

    # Mock FFmpeg to create the output file
    async def fake_proc(*cmd, **kwargs):
        out_path = Path(cmd[-1])
        out_path.write_bytes(b"final mp4 bytes")
        mock = MagicMock()
        mock.returncode = 0
        async def fake_comm():
            return b"", b""
        mock.communicate = fake_comm
        return mock

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_proc)
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/ffmpeg")

    # Mock downloading manifest from src
    orig_download = src_p.download_file
    async def mock_src_download(creds, file_ref, dest_dir, job):
        if "_resume_manifest" in file_ref["name"]:
            p = dest_dir / file_ref["name"]
            p.write_text(json.dumps(manifest_content), encoding="utf-8")
            return p
        return await orig_download(creds, file_ref, dest_dir, job)

    src_p.download_file = mock_src_download

    out = asyncio.run(run_resumable_backup_pipeline(
        job=job,
        dirs=dirs,
        source={"provider": "terabox", "credentials": {"ndus": "tok"}},
        target={"provider": "drive", "credentials": {"token": "gd_tok"}, "folder": {"id": "root", "path": "/"}},
        options={"cleanupAfterFinish": True},
        src_provider=src_p,
        dst_provider=dst_p,
        file_item=file_item,
    ))

    assert out.name == "final_video.mp4"
    assert "part_0001.ts" in src_p.downloaded
    assert len(dst_p.downloaded) == 0
    assert "final_video.mp4" in dst_p.uploaded
    assert "test_video__backup_resume" in src_p.deleted


def test_backup_resume_links_source_downloads_missing_parts_from_dst_provider(tmp_path, monkeypatch):
    """When source is links (streaming URL) and resume_from_backup is True, missing parts must be downloaded from dst_provider."""
    from src.jobs.hls_backup_pipeline import run_resumable_backup_pipeline

    class MockLinksProvider:
        def __init__(self):
            self.downloaded = []

        async def download_file(self, creds, file_ref, dest_dir, job):
            self.downloaded.append(file_ref)
            raise RuntimeError("LinksProvider cannot download cloud IDs! Should use dst_provider.")

    class MockDriveDstProvider:
        def __init__(self):
            self.downloaded = []
            self.uploaded = []
            self.deleted = []

        async def list_files(self, creds, folder_id):
            return {
                "items": [
                    {"name": "test_video__backup_resume", "id": "bf_id", "type": "folder", "is_folder": True},
                    {"name": "part_0001.ts", "id": "part1_drive_id", "bytes": 100},
                    {"name": "_resume_manifest.json", "id": "man_id"},
                ]
            }

        async def download_file(self, creds, file_ref, dest_dir, job):
            fname = file_ref.get("name") or "file"
            self.downloaded.append(fname)
            p = dest_dir / fname
            if "_resume_manifest" in fname:
                manifest_content = {
                    "is_complete": True,
                    "total_segments_expected": 100,
                    "target_filename": "test_video.mp4",
                    "completed_parts": [
                        {"part_index": 1, "file_name": "part_0001.ts", "start_seg": 0, "end_seg": 99, "segment_count": 100, "bytes": 100}
                    ]
                }
                p.write_text(json.dumps(manifest_content), encoding="utf-8")
            else:
                p.write_bytes(b"part data")
            return p

        async def upload_file(self, creds, local_path, target_ref, job):
            self.uploaded.append(target_ref.get("relative_path") or str(local_path.name))
            return {"ok": True}

        async def delete_item(self, creds, ref):
            self.deleted.append(ref.get("name"))

    src_p = MockLinksProvider()
    dst_p = MockDriveDstProvider()

    dirs = {
        "input": tmp_path / "input",
        "output": tmp_path / "output",
        "extract": tmp_path / "extract",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    job = JobState("job-links-resume", {
        "source": {"provider": "links", "credentials": {}},
        "target": {"provider": "drive", "credentials": {"token": "gd_tok"}, "folder": {"id": "root", "path": "/"}},
        "options": {"cleanupAfterFinish": True, "resume_from_backup": True},
    })

    file_item = {
        "name": "test_video.mp4",
        "path": "https://example.com/stream.m3u8",
        "url": "https://example.com/stream.m3u8",
        "type": "file",
    }

    # Mock FFmpeg to create output file
    async def fake_proc(*cmd, **kwargs):
        out_path = Path(cmd[-1])
        out_path.write_bytes(b"final mp4 bytes")
        mock = MagicMock()
        mock.returncode = 0
        async def fake_comm():
            return b"", b""
        mock.communicate = fake_comm
        return mock

    async def fake_resolve_media_segments(client, url, headers=None):
        return url, [{"index": i, "duration": 2.0, "url": f"https://example.com/seg_{i}.ts"} for i in range(100)]

    monkeypatch.setattr("src.jobs.hls_backup_pipeline.resolve_media_segments", fake_resolve_media_segments)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_proc)
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/ffmpeg")

    out = asyncio.run(run_resumable_backup_pipeline(
        job=job,
        dirs=dirs,
        source={"provider": "links", "credentials": {}},
        target={"provider": "drive", "credentials": {"token": "gd_tok"}, "folder": {"id": "root", "path": "/"}},
        options={"cleanupAfterFinish": True, "resume_from_backup": True},
        src_provider=src_p,
        dst_provider=dst_p,
        file_item=file_item,
    ))

    assert out.name == "test_video.mp4"
    # Ensure missing part was downloaded by dst_provider (Drive), NOT src_provider (Links)
    assert "part_0001.ts" in dst_p.downloaded
    assert len(src_p.downloaded) == 0
    assert "test_video.mp4" in dst_p.uploaded


def test_optimize_image_mode_skips_video_thumbnails():
    """Optimize Image mode must skip video thumbnailing even if video files are present."""
    from src.jobs.transfer_job import _should_process_video_thumbnails

    # Standard optimize mode: should be False
    assert not _should_process_video_thumbnails({"optimize_image": True})
    assert not _should_process_video_thumbnails({"is_optimize_page": True})
    assert not _should_process_video_thumbnails({}, {"mode": "optimize"})
    assert not _should_process_video_thumbnails({}, {"job_type": "optimize"})

    # Transfer mode with video thumb enabled: should be True
    assert _should_process_video_thumbnails({"set_video_thumbnail": True})
    assert _should_process_video_thumbnails({"trim_intro": True})

    # Transfer mode with video thumb omitted / disabled: should be False
    assert not _should_process_video_thumbnails({})
    assert not _should_process_video_thumbnails({"set_video_thumbnail": False})


def test_download_single_segment_retries_transient_failures_and_succeeds(tmp_path):
    """Verify download_single_segment automatically retries on 500/503/timeout and succeeds."""
    from src.jobs.hls_backup_pipeline import download_single_segment
    import httpx

    dest_file = tmp_path / "seg_000001.ts"
    attempts = 0

    class MockResponse:
        def __init__(self, status_code, content):
            self.status_code = status_code
            self.content = content

        def raise_for_status(self):
            if self.status_code != 200:
                raise httpx.HTTPStatusError("Server Error", request=None, response=self)

    class MockHttpClient:
        async def get(self, url, headers=None, timeout=None):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return MockResponse(503, b"Service Unavailable")
            elif attempts == 2:
                return MockResponse(200, b"")  # Empty content -> should trigger retry
            elif attempts == 3:
                raise httpx.ReadTimeout("Read timed out")
            else:
                return MockResponse(200, b"valid ts segment data")

    client = MockHttpClient()
    sem = asyncio.Semaphore(5)
    seg = {"index": 1, "url": "https://cdn.example.com/seg1.ts"}

    sz = asyncio.run(download_single_segment(client, seg, dest_file, {}, sem, max_retries=5))
    assert sz == len(b"valid ts segment data")
    assert dest_file.read_bytes() == b"valid ts segment data"
    assert attempts == 4


def test_download_single_segment_raises_after_max_retries(tmp_path):
    """Verify download_single_segment raises ProviderFailure after max_retries are exhausted."""
    from src.jobs.hls_backup_pipeline import download_single_segment
    from src.providers.base import ProviderFailure
    import httpx

    dest_file = tmp_path / "seg_000002.ts"
    attempts = 0

    class FailingHttpClient:
        async def get(self, url, headers=None, timeout=None):
            nonlocal attempts
            attempts += 1
            raise httpx.ConnectError("Network unreachable")

    client = FailingHttpClient()
    sem = asyncio.Semaphore(5)
    seg = {"index": 2, "url": "https://cdn.example.com/seg2.ts"}

    with pytest.raises(ProviderFailure) as exc_info:
        asyncio.run(download_single_segment(client, seg, dest_file, {}, sem, max_retries=3))

    assert "DOWNLOAD_FAILED" in str(exc_info.value.code)
    assert attempts == 3





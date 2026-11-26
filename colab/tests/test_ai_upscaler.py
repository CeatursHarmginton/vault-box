from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

COLAB_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = COLAB_ROOT.parent
if str(COLAB_ROOT) not in sys.path:
    sys.path.insert(0, str(COLAB_ROOT))

from src.jobs.progress import JobCancelled, JobState
from src.jobs.transfer_job import run_transfer
from src.providers import PROVIDERS, drive as drive_mod
from src.providers.drive import DriveProvider
from src.providers.pikpak import PikPakProvider
from src.providers.terabox import TeraBoxProvider
from src.utils import ai_upscaler, image_optimizer, media_converter
from src.utils.image_optimizer import is_image_already_optimized, optimize_directory


class DummyJob:
    def __init__(self) -> None:
        self.logs: list[str] = []
        self.current_file: str = ""
        self.progress = SimpleNamespace(optimize=0.0)
        self.updated_at: float = 0.0

    def set(self, **kwargs) -> None:
        if "current_file" in kwargs:
            self.current_file = str(kwargs["current_file"])
        self.updated_at += 1.0

    def log(self, message: str) -> None:
        self.logs.append(str(message))


# ============================================================================
# 1. Naming, Auto-Model Heuristics, Target Dimensions, & Skip Thresholds
# ============================================================================

def test_upscaled_naming_and_suffix_detection():
    assert ai_upscaler.build_upscaled_video_name("movie.mp4") == "movie-upscaled.mp4"
    assert ai_upscaler.build_upscaled_video_name("action_clip.mkv") == "action_clip-upscaled.mp4"
    assert ai_upscaler.build_upscaled_video_name("already-upscaled.mp4") == "already-upscaled.mp4"
    assert ai_upscaler.build_upscaled_video_name("MOVIE-UPSCALED.MKV") == "MOVIE-UPSCALED.mp4"

    # Never include model name or resolution in video filename
    name = ai_upscaler.build_upscaled_video_name("episode_01.avi")
    assert name == "episode_01-upscaled.mp4"
    assert "1080p" not in name
    assert "SPAN" not in name

    assert ai_upscaler.is_upscaled_filename("movie-upscaled.mp4") is True
    assert ai_upscaler.is_upscaled_filename("/folder/CLIP-UPSCALED.mkv") is True
    assert ai_upscaler.is_upscaled_filename("movie.mp4") is False
    assert ai_upscaler.is_upscaled_filename("upscaled_intro.mp4") is False


def test_choose_auto_model_heuristics():
    # 1. Very low-res 360p -> 1080p (scale_needed = 3.0 >= 2.6) on T4 & >1500 frames -> realesr-general-x4v3
    key, dn, reason = ai_upscaler.choose_auto_model(640, 360, "1080p", 3000, 0.08, "Tesla T4", 0.4)
    assert key == "realesr-general-x4v3"
    assert dn == 0.35
    assert "4x" in reason

    # 2. Very low-res 360p -> 1080p on A100 (or short <= 1500 frames) -> 4xNomos8kSC
    key_big, _, _ = ai_upscaler.choose_auto_model(640, 360, "1080p", 3000, 0.08, "NVIDIA A100", 0.4)
    assert key_big == "4xNomos8kSC"

    # 3. Very long video (>90000 frames on T4) -> 2xNomosUni_compact_otf_medium
    key_long, _, _ = ai_upscaler.choose_auto_model(1280, 720, "1080p", 95000, 0.09, "Tesla T4", 0.4)
    assert key_long == "2xNomosUni_compact_otf_medium"

    # 4. Heavily compressed low-BPP (<0.075) -> 2xNomosUni_span_multijpg_ldl
    key_low_bpp, _, _ = ai_upscaler.choose_auto_model(1280, 720, "1080p", 3000, 0.04, "Tesla T4", 0.4)
    assert key_low_bpp == "2xNomosUni_span_multijpg_ldl"

    # 5. Standard live action 720p -> 1080p -> 2xLiveActionV1_SPAN
    key_std, _, _ = ai_upscaler.choose_auto_model(1280, 720, "1080p", 3000, 0.09, "Tesla T4", 0.4)
    assert key_std == "2xLiveActionV1_SPAN"


def test_compute_target_dims_and_target_skip_check():
    # Landscape 16:9
    assert ai_upscaler.compute_target_dims(640, 360, 2, "720p") == (1280, 720)
    assert ai_upscaler.compute_target_dims(1280, 720, 2, "1080p") == (1920, 1080)
    assert ai_upscaler.compute_target_dims(640, 360, 4, "Native Model Scale (2x/4x)") == (2560, 1440)

    # Portrait 9:16
    assert ai_upscaler.compute_target_dims(360, 640, 2, "720p") == (720, 1280)
    assert ai_upscaler.compute_target_dims(720, 1280, 2, "1080p") == (1080, 1920)

    # Even dimensions guaranteed on odd ratios
    out_w, out_h = ai_upscaler.compute_target_dims(641, 361, 2, "1080p")
    assert out_w % 2 == 0 and out_h % 2 == 0

    # Target skip checks: skip if short side >= target_resolution
    assert ai_upscaler.is_already_at_or_above_target(1920, 1080, "1080p") is True
    assert ai_upscaler.is_already_at_or_above_target(2560, 1440, "1080p") is True
    assert ai_upscaler.is_already_at_or_above_target(1080, 1920, "1080p") is True
    assert ai_upscaler.is_already_at_or_above_target(1280, 720, "720p") is True
    assert ai_upscaler.is_already_at_or_above_target(1280, 720, "1080p") is False
    assert ai_upscaler.is_already_at_or_above_target(1920, 1080, "Native Model Scale (2x/4x)") is False


# ============================================================================
# 2. Metadata Tagging & Upscale Engine Execution (Images + Videos)
# ============================================================================

def _install_dummy_2x_spandrel_loader(monkeypatch):
    import torch

    class Dummy2xNet(torch.nn.Module):
        def forward(self, x):
            return torch.nn.functional.interpolate(x, scale_factor=2, mode="bilinear", align_corners=False)

    def fake_load_spandrel_model(model_key, device, use_fp16, denoise_strength, log_fn=None):
        net = Dummy2xNet().to(device).eval()
        if use_fp16 and device.type == "cuda":
            net = net.half()
        return net, 2

    monkeypatch.setattr(ai_upscaler, "ensure_upscale_dependencies", lambda log_fn=None: None)
    monkeypatch.setattr(ai_upscaler, "load_spandrel_model", fake_load_spandrel_model)


def test_image_upscale_rgb_rgba_tag_and_target_skip(tmp_path, monkeypatch):
    pytest.importorskip("torch")
    _install_dummy_2x_spandrel_loader(monkeypatch)

    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    in_dir.mkdir()
    out_dir.mkdir()

    # 1. Small RGB JPEG (320x180 -> 1280x720 for 720p target)
    rgb_path = in_dir / "photo_small.jpg"
    Image.new("RGB", (320, 180), color=(40, 120, 200)).save(rgb_path, "JPEG", quality=90)

    # 2. Small RGBA PNG with transparency (200x200 -> 720x720 for 720p target)
    rgba_path = in_dir / "badge_alpha.png"
    Image.new("RGBA", (200, 200), color=(255, 80, 40, 128)).save(rgba_path, "PNG")

    # 3. Already >= 720p image (1600x900) -> MUST be skipped!
    big_path = in_dir / "already_hd.jpg"
    Image.new("RGB", (1600, 900), color=(10, 20, 30)).save(big_path, "JPEG", quality=90)

    job = DummyJob()
    opts = {
        "optimize_image": True,
        "upscale": True,
        "target_resolution": "720p",
        "model_preset": "auto (Tự động chọn tối ưu)",
        "allow_cpu_upscale": True,
        "clean_temp_and_cache": False,
    }
    results = optimize_directory(in_dir, out_dir, opts, job)
    by_src = {r["source_name"]: r for r in results}

    assert by_src["photo_small.jpg"]["status"] == "Success (Upscaled)"
    assert by_src["badge_alpha.png"]["status"] == "Success (Upscaled)"
    assert by_src["already_hd.jpg"]["status"].startswith("Skipped")
    assert not (out_dir / "already_hd.jpg").exists()

    out_rgb = out_dir / "photo_small.jpg"
    out_rgba = out_dir / "badge_alpha.png"
    assert out_rgb.is_file()
    assert out_rgba.is_file()

    with Image.open(out_rgb) as im_rgb:
        assert im_rgb.size == (1280, 720)
    with Image.open(out_rgba) as im_rgba:
        assert im_rgba.size == (720, 720)
        assert im_rgba.mode == "RGBA"

    # Both outputs carry VaultBox_Optimized metadata tag
    assert is_image_already_optimized(out_rgb) is True
    assert is_image_already_optimized(out_rgba) is True

    # Re-running upscale on the tagged output skips it automatically even if renamed!
    rerun_in = tmp_path / "rerun_in"
    rerun_out = tmp_path / "rerun_out"
    rerun_in.mkdir()
    rerun_out.mkdir()
    renamed_tagged = rerun_in / "renamed_plain_photo.jpg"
    shutil.copy2(out_rgb, renamed_tagged)
    # Resize header? No, keep metadata intact on smaller 320x180 tagged image to test metadata skip specifically:
    tiny_tagged = rerun_in / "tiny_tagged.png"
    Image.new("RGB", (160, 120), color=(1, 2, 3)).save(tiny_tagged, "PNG")
    image_optimizer.embed_optimization_metadata(tiny_tagged)

    rerun_results = optimize_directory(rerun_in, rerun_out, opts, DummyJob())
    for r in rerun_results:
        assert r["status"].startswith("Skipped")
    assert not any(rerun_out.rglob("*"))


@pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg and ffprobe required for synthetic video upscale test",
)
def test_video_upscale_suffix_metadata_tag_skip_and_cancel(tmp_path, monkeypatch):
    pytest.importorskip("torch")
    _install_dummy_2x_spandrel_loader(monkeypatch)

    in_dir = tmp_path / "vid_in"
    out_dir = tmp_path / "vid_out"
    in_dir.mkdir()
    out_dir.mkdir()

    # Create tiny 320x180 6-frame MP4 with AAC audio
    low_vid = in_dir / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=320x180:rate=12:duration=0.5",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            str(low_vid),
        ],
        check=True,
    )

    # Create already-720p video -> MUST be skipped when target_resolution == "720p"
    hd_vid = in_dir / "already720.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=blue:size=1280x720:rate=10:duration=0.3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(hd_vid),
        ],
        check=True,
    )

    job = DummyJob()
    opts = {
        "optimize_image": True,
        "upscale": True,
        "target_resolution": "720p",
        "encoder_choice": "libx264 (CPU)",
        "size_profile": "Tiết kiệm dung lượng (Nhẹ nhất)",
        "torch_compile_mode": "off (Tắt)",
        "batch_size": 2,
        "allow_cpu_upscale": True,
        "clean_temp_and_cache": False,
    }
    results = optimize_directory(in_dir, out_dir, opts, job)
    by_src = {r["source_name"]: r for r in results}

    # 1. Output filename uses ONLY -upscaled.mp4
    assert by_src["clip.mp4"]["status"] == "Success (Upscaled)"
    assert by_src["clip.mp4"]["name"] == "clip-upscaled.mp4"
    upscaled_mp4 = out_dir / "clip-upscaled.mp4"
    assert upscaled_mp4.is_file()
    assert job.progress.optimize == 100

    # 2. Verify output dimensions == 1280x720 & audio preserved & VaultBox_Optimized:true tag embedded
    meta = ai_upscaler.probe_video(upscaled_mp4)
    assert (meta["width"], meta["height"]) == (1280, 720)
    assert meta["has_audio"] is True
    assert ai_upscaler.is_video_already_optimized(upscaled_mp4) is True

    # 3. Already >= 720p video was skipped without producing output
    assert by_src["already720.mp4"]["status"].startswith("Skipped")
    assert not (out_dir / "already720-upscaled.mp4").exists()

    # 4. Even if clip-upscaled.mp4 is renamed to plain.mp4 (and target set to 1080p),
    # VaultBox_Optimized:true tag causes automatic skip on next run!
    rerun_in = tmp_path / "vid_rerun_in"
    rerun_out = tmp_path / "vid_rerun_out"
    rerun_in.mkdir()
    rerun_out.mkdir()
    renamed_vid = rerun_in / "plain_without_suffix.mp4"
    shutil.copy2(upscaled_mp4, renamed_vid)

    opts_1080 = {**opts, "target_resolution": "1080p"}
    rerun_res = optimize_directory(rerun_in, rerun_out, opts_1080, DummyJob())
    assert len(rerun_res) == 1
    assert rerun_res[0]["status"] == "Skipped (Already optimized)"
    assert not any(rerun_out.rglob("*"))

    # 5. Downscale mode ALWAYS skips both -upscaled filename AND VaultBox_Optimized tagged video
    down_in = tmp_path / "down_in"
    down_out = tmp_path / "down_out"
    down_in.mkdir()
    down_out.mkdir()
    shutil.copy2(upscaled_mp4, down_in / "plain_tagged.mp4")
    shutil.copy2(low_vid, down_in / "untouched-upscaled.mp4")
    down_res = optimize_directory(down_in, down_out, {"optimize_image": True, "upscale": False}, DummyJob())
    assert all(r["status"] == "Skipped (Upscaled video)" for r in down_res)
    assert not any(down_out.rglob("*"))

    # 6. Instant cancellation cleans up temporary files
    cancel_in = tmp_path / "cancel_in"
    cancel_out = tmp_path / "cancel_out"
    cancel_in.mkdir()
    cancel_out.mkdir()
    shutil.copy2(low_vid, cancel_in / "cancel_me.mp4")
    calls = {"n": 0}

    def cancel_soon():
        calls["n"] += 1
        if calls["n"] >= 3:
            raise JobCancelled()

    with pytest.raises(JobCancelled):
        optimize_directory(cancel_in, cancel_out, opts, DummyJob(), cancel_check=cancel_soon)
    assert not list(cancel_out.rglob("*.tmp.mp4"))


# ============================================================================
# 3. Cloud Provider Overwrite (with -upscaled suffix) & Upload As New & Skip
# ============================================================================

def test_terabox_replace_file_renames_to_upscaled_and_deletes_old(tmp_path, monkeypatch):
    provider = TeraBoxProvider()
    local_upscaled = tmp_path / "movie-upscaled.mp4"
    local_upscaled.write_bytes(b"upscaled-video-bytes")

    uploaded_calls = []
    deleted_calls = []

    async def fake_upload_file(credentials, local_path, target_folder, progress):
        uploaded_calls.append((local_path.name, dict(target_folder), progress.payload.get("options", {}).get("replace")))
        return {"ok": True}

    async def fake_delete_file(credentials, file_ref):
        deleted_calls.append(dict(file_ref))
        return {"ok": True}

    monkeypatch.setattr(provider, "upload_file", fake_upload_file)
    monkeypatch.setattr(provider, "delete_file", fake_delete_file)

    job = JobState("tb-up-replace", {"options": {}})
    res = asyncio.run(
        provider.replace_file(
            {"ndus": "tok"},
            local_upscaled,
            {"path": "/movies/movie.mkv", "name": "movie-upscaled.mp4"},
            job,
        )
    )

    assert res["ok"] is True
    assert res["new_name"] == "movie-upscaled.mp4"
    assert res["renamed"] is True
    assert uploaded_calls == [("movie-upscaled.mp4", {"id": "/movies", "relative_path": "movie-upscaled.mp4"}, True)]
    assert deleted_calls == [{"path": "/movies/movie.mkv"}]


def test_drive_mount_replace_file_writes_upscaled_suffix_and_removes_original(tmp_path, monkeypatch):
    mount_root = tmp_path / "MyDrive"
    videos_dir = mount_root / "Videos"
    videos_dir.mkdir(parents=True)
    orig_file = videos_dir / "sample.mkv"
    orig_file.write_bytes(b"old-mkv")

    local_upscaled = tmp_path / "sample-upscaled.mp4"
    local_upscaled.write_bytes(b"new-upscaled-mp4")

    monkeypatch.setattr(drive_mod, "DRIVE_MOUNT", mount_root)
    provider = DriveProvider()
    job = JobState("drv-mount-replace", {"options": {"replace": True}})

    res = asyncio.run(
        provider.replace_file(
            {"mount": True},
            local_upscaled,
            {"id": "Videos/sample.mkv", "path": "Videos/sample.mkv", "name": "sample-upscaled.mp4"},
            job,
        )
    )

    assert not orig_file.exists()
    assert (videos_dir / "sample-upscaled.mp4").read_bytes() == b"new-upscaled-mp4"
    assert res["name"] == "sample-upscaled.mp4"
    assert res["path"] == "Videos/sample-upscaled.mp4"


def test_run_transfer_upscale_overwrite_and_upload_new_and_all_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP_DIR", str(tmp_path / "work"))

    class FakeCloudProvider:
        def __init__(self) -> None:
            self.replaced: list[tuple[str, dict]] = []
            self.uploaded: list[tuple[str, dict]] = []
            self.downloaded: list[str] = []

        async def download_file(self, credentials, file_ref, dest_dir, progress):
            name = file_ref["name"]
            self.downloaded.append(name)
            p = dest_dir / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"raw-video")
            progress.add_bytes(9, 9, "download", name)
            return p

        async def upload_file(self, credentials, local_path, target_folder, progress):
            self.uploaded.append((local_path.name, dict(target_folder)))
            progress.add_bytes(local_path.stat().st_size, local_path.stat().st_size, "upload", local_path.name)
            return {"id": f"new-{local_path.name}", "name": local_path.name}

        async def replace_file(self, credentials, local_path, source_ref, progress):
            self.replaced.append((local_path.name, dict(source_ref)))
            progress.add_bytes(local_path.stat().st_size, local_path.stat().st_size, "upload", local_path.name)
            return {"id": source_ref.get("id"), "name": source_ref.get("name")}

    fake_prov = FakeCloudProvider()
    monkeypatch.setitem(PROVIDERS, "fakecloud", fake_prov)

    def fake_upscale_directory(input_dir, output_dir, options, job_state, cancel_check=None):
        results = []
        for p in sorted(input_dir.rglob("*")):
            if not p.is_file():
                continue
            if "already1080" in p.stem:
                results.append({
                    "name": p.name,
                    "source_name": p.name,
                    "original_size": p.stat().st_size,
                    "optimized_size": p.stat().st_size,
                    "status": "Skipped (Already >= 1080p)",
                    "quality": "1920x1080",
                })
                continue
            out_name = ai_upscaler.build_upscaled_video_name(p.name)
            out_p = output_dir / out_name
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_bytes(b"upscaled-mp4-data")
            results.append({
                "name": out_name,
                "source_name": p.name,
                "original_size": p.stat().st_size,
                "optimized_size": out_p.stat().st_size,
                "status": "Success (Upscaled)",
                "quality": "1080p · 2xLiveActionV1_SPAN",
            })
        return results

    monkeypatch.setattr(ai_upscaler, "upscale_directory", fake_upscale_directory)

    # Case A: Overwrite (confirm_action="replace") -> replaces original clip.mkv with clip-upscaled.mp4
    payload_replace = {
        "mode": "optimize",
        "is_optimize_page": True,
        "source": {
            "provider": "fakecloud",
            "credentials": {"ok": True},
            "items": [{"id": "vid-1", "path": "/folder/clip.mkv", "name": "clip.mkv", "type": "file", "size": 9}],
        },
        "target": {
            "provider": "fakecloud",
            "credentials": {"ok": True},
            "folder": {"id": "/folder", "path": "/folder"},
        },
        "options": {
            "is_optimize_page": True,
            "optimize_image": True,
            "upscale": True,
            "target_resolution": "1080p",
            "confirm_action": "replace",
            "replace": True,
            "cleanupAfterFinish": False,
        },
    }
    job_a = JobState("job-up-replace", payload_replace)
    asyncio.run(run_transfer(job_a))
    assert job_a.status == "completed"
    assert len(fake_prov.replaced) == 1
    assert fake_prov.replaced[0][0] == "clip-upscaled.mp4"
    assert fake_prov.replaced[0][1]["id"] == "vid-1"
    assert fake_prov.replaced[0][1]["name"] == "clip-upscaled.mp4"

    # Case B: Upload As New (confirm_action="upload_new") -> uploads results/clip-upscaled.mp4
    fake_prov.uploaded.clear()
    payload_new = {
        **payload_replace,
        "options": {
            **payload_replace["options"],
            "confirm_action": "upload_new",
            "replace": False,
            "_auto_confirm_upload_new": True,
        },
    }
    job_b = JobState("job-up-new", payload_new)
    asyncio.run(run_transfer(job_b))
    assert job_b.status == "completed"
    assert len(fake_prov.uploaded) == 1
    assert fake_prov.uploaded[0][0] == "clip-upscaled.mp4"
    assert fake_prov.uploaded[0][1]["relative_path"] == "results/clip-upscaled.mp4"

    # Case C: Video already >= 1080p -> skipped cleanly without UPLOAD_FAILED
    fake_prov.uploaded.clear()
    fake_prov.replaced.clear()
    payload_skip_res = {
        **payload_replace,
        "source": {
            "provider": "fakecloud",
            "credentials": {"ok": True},
            "items": [{"id": "vid-hd", "path": "/folder/already1080.mp4", "name": "already1080.mp4", "type": "file", "size": 9}],
        },
    }
    job_c = JobState("job-up-skip-hd", payload_skip_res)
    asyncio.run(run_transfer(job_c))
    assert job_c.status == "completed"
    assert job_c.files_skipped >= 1
    assert fake_prov.uploaded == []
    assert fake_prov.replaced == []

    # Case D: Downscale mode ALWAYS skips -upscaled videos even when Auto Convert Video is enabled!
    fake_prov.downloaded.clear()
    payload_downscale_skip = {
        "mode": "optimize",
        "is_optimize_page": True,
        "source": {
            "provider": "fakecloud",
            "credentials": {"ok": True},
            "items": [
                {"id": "v-up-1", "path": "/folder/movie-upscaled.avi", "name": "movie-upscaled.avi", "type": "file", "size": 5000000},
                {"id": "v-up-2", "path": "/folder/show-upscaled.mkv", "name": "show-upscaled.mkv", "type": "file", "size": 5000000},
            ],
        },
        "target": {
            "provider": "fakecloud",
            "credentials": {"ok": True},
            "folder": {"id": "/folder", "path": "/folder"},
        },
        "options": {
            "is_optimize_page": True,
            "optimize_image": True,
            "upscale": False,
            "auto_convert": {
                "enabled": True,
                "video": {"enabled": True, "input_mode": "all", "output_formats": ["mp4"], "target_format": "mp4"},
                "image": {"enabled": False},
            },
            "confirm_action": "replace",
            "cleanupAfterFinish": False,
        },
    }
    job_d = JobState("job-downscale-skip-upscaled", payload_downscale_skip)
    asyncio.run(run_transfer(job_d))
    assert job_d.status == "completed"
    assert fake_prov.downloaded == []
    assert job_d.files_skipped == 2


# ============================================================================
# 4. Notebook Launcher & Frontend JS Integration Verification
# ============================================================================

@pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg and ffprobe required for MP4 metadata tagging test",
)
def test_video_optimization_metadata_tag_persists_across_rename(tmp_path):
    tagged_mp4 = tmp_path / "sample-upscaled.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=green:size=320x180:rate=10:duration=0.3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            *ai_upscaler.embed_video_optimization_metadata_args(),
            str(tagged_mp4),
        ],
        check=True,
    )
    assert ai_upscaler.is_video_already_optimized(tagged_mp4) is True

    # Rename removing -upscaled suffix -> still detected as already optimized via metadata!
    renamed = tmp_path / "renamed_without_suffix.mp4"
    shutil.copy2(tagged_mp4, renamed)
    assert ai_upscaler.is_upscaled_filename(renamed.name) is False
    assert ai_upscaler.is_video_already_optimized(renamed) is True

    # Downscale mode skips renamed video via metadata tag too!
    down_out = tmp_path / "down_out"
    down_out.mkdir()
    down_res = optimize_directory(tmp_path, down_out, {"optimize_image": True, "upscale": False}, DummyJob())
    assert all(r["status"] == "Skipped (Upscaled video)" for r in down_res)
    assert not any(down_out.rglob("*"))


def test_server_launcher_notebook_has_ai_subtitles_and_ai_upscale_options():
    nb_path = COLAB_ROOT / "server_launcher.ipynb"
    nb = json.loads(nb_path.read_text(encoding="utf-8"))
    all_code = "\n".join("".join(c.get("source", [])) for c in nb["cells"] if c.get("cell_type") == "code")
    cell3_src = "".join(nb["cells"][3]["source"])
    cell4_src = "".join(nb["cells"][4]["source"])

    assert "install_ai_models" not in all_code
    assert 'install_ai_subtitles = False #@param {type:"boolean"}' in cell3_src
    assert 'install_ai_upscale = False #@param {type:"boolean"}' in cell3_src
    assert "spandrel" in cell3_src
    assert "safetensors" in cell3_src
    assert "einops" in cell3_src
    assert "/usr/lib64-nvidia" in cell3_src
    assert "/usr/lib64-nvidia" in cell4_src


def test_frontend_upscale_options_and_candidate_rules_via_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")

    js_path = REPO_ROOT / "frontend" / "static" / "js" / "colab_transfer.js"
    # 1. Syntax check entire colab_transfer.js
    subprocess.run([node, "--check", str(js_path)], check=True)

    js = js_path.read_text(encoding="utf-8")
    # 2. Verify all 1:1 notebook option controls exist in Optimize UI
    for dom_id in (
        "modeDownscaleBtn",
        "modeUpscaleBtn",
        "downscaleSettingsGroup",
        "upscaleSettingsGroup",
        "optUpscaleModelPreset",
        "optUpscaleTargetRes",
        "optUpscaleResourcePct",
        "optUpscaleSizeProfile",
        "optUpscaleEncoder",
        "optUpscaleTorchCompile",
        "optUpscaleBatchSize",
        "optUpscaleDenoise",
        "optUpscaleStartTime",
        "optUpscalePreviewSec",
        "optUpscaleUseFp16",
        "optUpscaleCleanTemp",
        "optUpscaleDeleteSource",
        "optUpscaleOverwriteOutput",
    ):
        assert f'id="{dom_id}"' in js, f"Missing control {dom_id} in colab_transfer.js"

    # 3. Extract and execute candidate functions in Node
    start = js.index("function hasUpscaledSuffix(item)")
    end = js.index("function parentRemotePath(path)", start)
    snippet = js[start:end]

    test_script = f"""
    const OPT_IMAGE_EXTENSIONS = new Set(["jpg", "jpeg", "png", "webp", "bmp", "tiff", "tif"]);
    function isVideoFile(item) {{
      const n = String(item?.name || item?.path || "").toLowerCase();
      return /\\.(mp4|mkv|avi|mov|webm|m4v|ts)$/.test(n);
    }}
    function isImageFile(item) {{
      const n = String(item?.name || item?.path || "").toLowerCase();
      return /\\.(jpg|jpeg|png|webp|bmp|tiff|tif|avif)$/.test(n);
    }}
    function isArchiveFile(item) {{
      const n = String(item?.name || item?.path || "").toLowerCase();
      return /\\.(zip|rar|7z|tar|gz)$/.test(n);
    }}
    {snippet}

    const vidPlain = {{ name: "movie.mp4", size: 50 * 1024 * 1024 }};
    const vidUpscaled = {{ name: "movie-upscaled.mp4", size: 80 * 1024 * 1024 }};
    const vidUpscaledAvi = {{ name: "clip-upscaled.avi", size: 20 * 1024 * 1024 }};
    const imgLarge = {{ name: "photo.jpg", size: 6 * 1024 * 1024 }};
    const imgSmall = {{ name: "icon.png", size: 200 * 1024 }};

    // Upscale mode: accepts plain video + any image regardless of size, skips -upscaled
    const upOpts = {{ upscale: true, optimize_mode: "upscale", min_target_mb: 1, max_target_mb: 9999 }};
    if (!isOptimizeCandidate(vidPlain, upOpts)) throw new Error("Upscale should accept plain video");
    if (!isOptimizeCandidate(imgLarge, upOpts)) throw new Error("Upscale should accept image > 1MB");
    if (!isOptimizeCandidate(imgSmall, upOpts)) throw new Error("Upscale should accept small image");
    if (isOptimizeInput(vidUpscaled, upOpts)) throw new Error("Upscale must skip -upscaled video");

    // Downscale mode: ALWAYS skips -upscaled video even when Auto Convert Video is enabled!
    const downOpts = {{
      upscale: false,
      optimize_mode: "downscale",
      min_target_mb: 1,
      max_target_mb: 3,
      autoConvert: {{ enabled: true, video: {{ enabled: true, input_mode: "all", output_formats: ["mp4"] }} }}
    }};
    if (isOptimizeInput(vidUpscaled, downOpts)) throw new Error("Downscale must skip movie-upscaled.mp4");
    if (isOptimizeInput(vidUpscaledAvi, downOpts)) throw new Error("Downscale must skip clip-upscaled.avi even with autoConvert");
    if (!isOptimizeCandidate(imgLarge, downOpts)) throw new Error("Downscale should accept image > max_target_mb");
    if (isOptimizeCandidate(imgSmall, downOpts)) throw new Error("Downscale should not compress small image");
    """
    subprocess.run([node, "-e", test_script], check=True)

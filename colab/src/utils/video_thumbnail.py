from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..jobs.progress import JobState

# In-memory thumbnail bytes cache: str(local_path.resolve()) -> bytes (JPEG)
_THUMBNAIL_BYTES_CACHE: dict[str, bytes] = {}


def get_cached_thumbnail(local_path: Path) -> bytes | None:
    """Retrieve in-memory cached JPEG thumbnail bytes for a file, if available."""
    try:
        return _THUMBNAIL_BYTES_CACHE.get(str(local_path.resolve()))
    except Exception:
        return None


def set_cached_thumbnail(local_path: Path, data: bytes) -> None:
    """Store JPEG thumbnail bytes in memory for upload handlers (e.g. Google Drive contentHints)."""
    try:
        _THUMBNAIL_BYTES_CACHE[str(local_path.resolve())] = data
    except Exception:
        pass


def clear_cached_thumbnails() -> None:
    """Clear in-memory thumbnail cache."""
    _THUMBNAIL_BYTES_CACHE.clear()


def probe_video(video_path: Path) -> dict[str, Any] | None:
    """Run ffprobe to get video duration and stream info."""
    if not shutil.which("ffprobe"):
        return None
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration:stream=index,codec_type,codec_name,disposition",
            "-of", "json",
            str(video_path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            return None
        return json.loads(res.stdout)
    except Exception:
        return None


def calculate_thumbnail_timestamp(
    duration: float,
    custom_offset: float | None = None,
    custom_percent: float | None = None,
) -> float:
    """
    Calculate optimal frame timestamp deep within video to bypass identical intros/logos/bumpers.

    - Custom offset/percent if provided by user options.
    - Duration <= 5s: seek to 50%
    - Duration <= 30s: seek to 30%
    - Duration <= 120s: seek to max(15s, 20%)
    - Duration > 120s: seek to max(30s, min(duration * 15%, 300s))
    """
    if custom_offset is not None and 0 < custom_offset < duration:
        return custom_offset
    if custom_percent is not None and 0 < custom_percent < 1.0:
        return duration * custom_percent

    if duration <= 5.0:
        return max(0.5, duration * 0.5)
    elif duration <= 30.0:
        return max(2.0, duration * 0.3)
    elif duration <= 120.0:
        return max(15.0, min(duration * 0.2, duration - 5.0))
    else:
        # Default for longer videos: 15% depth, minimum 30s in to skip intro/sponsor/bumper, max 5 minutes in
        target = duration * 0.15
        target = max(30.0, min(target, 300.0))
        return min(target, duration - 10.0)


def is_frame_bright_enough(image_path: Path, min_brightness: float = 18.0) -> bool:
    """Check if the extracted JPEG frame is bright enough to avoid black/dark fade frames."""
    try:
        from PIL import Image, ImageStat
        with Image.open(image_path) as img:
            stat = ImageStat.Stat(img.convert("L"))
            mean_brightness = stat.mean[0]
            return mean_brightness >= min_brightness
    except Exception:
        # If PIL is unavailable, default to True so extraction proceeds
        return True


def extract_deep_frame(
    video_path: Path,
    duration: float,
    out_thumb: Path,
    options: dict[str, Any],
) -> float | None:
    """
    Extract a JPEG frame at deep timestamp using ffmpeg fast seek.
    If frame is dark or black fade, retries at further timestamps.
    Returns the timestamp used or None if failed.
    """
    if not shutil.which("ffmpeg"):
        return None

    custom_offset = options.get("thumbnail_offset_sec")
    custom_percent = options.get("thumbnail_percent")
    try:
        custom_offset = float(custom_offset) if custom_offset is not None else None
    except (ValueError, TypeError):
        custom_offset = None
    try:
        custom_percent = float(custom_percent) if custom_percent is not None else None
    except (ValueError, TypeError):
        custom_percent = None

    base_ts = calculate_thumbnail_timestamp(duration, custom_offset, custom_percent)

    # Candidate timestamps to try if first one is too dark
    candidates = [base_ts]
    if base_ts + 15.0 < duration - 5.0:
        candidates.append(base_ts + 15.0)
    if base_ts + 35.0 < duration - 5.0:
        candidates.append(base_ts + 35.0)
    if duration * 0.4 < duration - 5.0 and duration * 0.4 not in candidates:
        candidates.append(duration * 0.4)

    for ts in candidates:
        try:
            # -ss before -i for fast keyframe seek
            cmd = [
                "ffmpeg", "-y",
                "-ss", f"{ts:.2f}",
                "-i", str(video_path),
                "-vframes", "1",
                "-q:v", "2",
                str(out_thumb),
            ]
            res = subprocess.run(cmd, capture_output=True, timeout=20)
            if res.returncode == 0 and out_thumb.exists() and out_thumb.stat().st_size > 0:
                if is_frame_bright_enough(out_thumb):
                    return ts
                # If dark, loop to next candidate
        except Exception:
            continue

    # If all candidates were deemed dark or timed out, but out_thumb exists, use it anyway
    if out_thumb.exists() and out_thumb.stat().st_size > 0:
        return candidates[0]
    return None


def embed_thumbnail(video_path: Path, thumb_path: Path, progress: JobState | None = None) -> bool:
    """
    Embed the thumbnail into video container as attached_pic without re-encoding video.
    Supports MP4, M4V, MOV, and MKV.
    """
    suffix = video_path.suffix.lower()
    temp_dir = video_path.parent
    temp_out = temp_dir / f".thumb_mux_{os.getpid()}_{video_path.name}"

    try:
        if suffix in (".mp4", ".m4v", ".mov"):
            # Map primary video, all audios, all subtitles, and the new thumbnail as v:1
            cmd = [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-i", str(thumb_path),
                "-map", "0:v:0",
                "-map", "0:a?",
                "-map", "0:s?",
                "-map", "1:v:0",
                "-c", "copy",
                "-disposition:v:1", "attached_pic",
                "-movflags", "+faststart",
                str(temp_out),
            ]
        elif suffix == ".mkv":
            cmd = [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-attach", str(thumb_path),
                "-metadata:s:t", "mimetype=image/jpeg",
                "-metadata:s:t", "filename=cover.jpg",
                "-c", "copy",
                str(temp_out),
            ]
        else:
            return False

        res = subprocess.run(cmd, capture_output=True, timeout=120)
        if res.returncode == 0 and temp_out.exists() and temp_out.stat().st_size > 0:
            # Overwrite original atomically
            shutil.move(str(temp_out), str(video_path))
            return True
        else:
            if progress:
                err_snippet = (res.stderr.decode("utf-8", errors="ignore") if res.stderr else "")[-300:]
                progress.log(f"[Thumbnail] ffmpeg mux warning: {err_snippet.strip()}")
            return False
    except Exception as exc:
        if progress:
            progress.log(f"[Thumbnail] embed failed: {exc}")
        return False
    finally:
        if temp_out.exists():
            try:
                temp_out.unlink()
            except Exception:
                pass


def process_video_thumbnails(
    files: list[Path],
    options: dict[str, Any],
    progress: JobState | None = None,
) -> list[Path]:
    """
    Process downloaded files:
    For each video (.mp4, .m4v, .mov, .mkv), extracts a representative deep frame,
    embeds it into the container as attached_pic, and caches thumbnail bytes for Drive API upload.
    Runs fast with zero re-encoding (-c copy).
    """
    if not options.get("set_video_thumbnail", True):
        return files

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        if progress:
            progress.log("[Thumbnail] ffmpeg/ffprobe not found; skipping video thumbnailing.")
        return files

    supported_exts = {".mp4", ".m4v", ".mov", ".mkv"}
    video_files = [p for p in files if p.suffix.lower() in supported_exts and p.is_file()]

    if not video_files:
        return files

    if progress:
        progress.log(f"[Thumbnail] Auto-generating deep-frame thumbnails for {len(video_files)} video file(s)...")

    with tempfile.TemporaryDirectory(prefix="vb_thumb_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        for idx, video_path in enumerate(video_files, 1):
            if progress:
                progress.check_cancelled()
            try:
                probe = probe_video(video_path)
                if not probe:
                    continue

                format_info = probe.get("format") or {}
                duration_str = format_info.get("duration")
                if not duration_str:
                    continue
                duration = float(duration_str)
                if duration < 3.0:
                    continue

                thumb_file = tmp_dir / f"thumb_{idx}.jpg"
                used_ts = extract_deep_frame(video_path, duration, thumb_file, options)
                if not used_ts or not thumb_file.exists():
                    continue

                # Read thumbnail bytes for in-memory cache (used by Google Drive upload)
                thumb_bytes = thumb_file.read_bytes()
                set_cached_thumbnail(video_path, thumb_bytes)

                # Embed thumbnail into video container
                success = embed_thumbnail(video_path, thumb_file, progress)
                if success and progress:
                    mins = int(used_ts // 60)
                    secs = int(used_ts % 60)
                    progress.log(f"[Thumbnail] Set deep frame thumbnail at {mins:02d}:{secs:02d} ({used_ts:.1f}s) for {video_path.name}")
            except Exception as exc:
                if progress:
                    progress.log(f"[Thumbnail] Failed for {video_path.name}: {exc}")
                continue

    return files

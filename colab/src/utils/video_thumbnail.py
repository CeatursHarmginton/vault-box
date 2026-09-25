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

    - Custom offset if provided by user (seconds).
    - Percentage-based: defaults to 20.0% of total video duration.
    - Accepts custom_percent in range 0.0-1.0 (e.g. 0.20) or 1.0-100.0 (e.g. 20.0).
    - Clamped safely to guarantee skipping intro while avoiding outro/credits.
    """
    if custom_offset is not None and 0 < custom_offset < duration:
        return custom_offset

    pct = 0.20  # Default 20%
    if custom_percent is not None:
        try:
            val = float(custom_percent)
            if val > 1.0:
                pct = val / 100.0
            elif val > 0.0:
                pct = val
        except (ValueError, TypeError):
            pct = 0.20

    pct = max(0.01, min(pct, 0.95))
    target = duration * pct

    # Guardrails: skip intro while avoiding outro
    if duration > 60.0:
        target = max(15.0, target)
    elif duration > 15.0:
        target = max(5.0, target)

    if duration > 20.0:
        target = min(target, duration - 5.0)
    elif duration > 3.0:
        target = min(target, duration - 1.0)
    else:
        target = duration * 0.5

    return round(target, 2)


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
    for delta in (15.0, 35.0):
        alt = base_ts + delta
        if alt < duration - 3.0 and alt > base_ts + 2.0:
            candidates.append(round(alt, 2))
    alt_pct = duration * 0.35
    if alt_pct < duration - 3.0 and alt_pct > base_ts + 5.0 and round(alt_pct, 2) not in candidates:
        candidates.append(round(alt_pct, 2))

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
            os.replace(str(temp_out), str(video_path))
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
    For each video, extracts a representative deep frame at configured percentage (default 20%),
    embeds it into the container as attached_pic (for MP4/MKV/MOV/M4V), and caches thumbnail
    bytes for cloud upload metadata (e.g. Google Drive contentHints).
    Runs fast with zero re-encoding (-c copy).
    """
    if not options.get("set_video_thumbnail", True):
        return files

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        if progress:
            progress.log("[Thumbnail] ffmpeg/ffprobe not found; skipping video thumbnailing.")
        return files

    supported_exts = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".ts"}
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

                # Embed thumbnail into video container (MP4, M4V, MOV, MKV)
                success = embed_thumbnail(video_path, thumb_file, progress)
                if progress:
                    mins = int(used_ts // 60)
                    secs = int(used_ts % 60)
                    pct_val = (used_ts / duration) * 100.0
                    action_desc = "embedded cover" if success else "cached for Drive"
                    progress.log(f"[Thumbnail] Set deep frame ({pct_val:.0f}%, {mins:02d}:{secs:02d} / {used_ts:.1f}s, {action_desc}) for {video_path.name}")
            except Exception as exc:
                if progress:
                    progress.log(f"[Thumbnail] Failed for {video_path.name}: {exc}")
                continue

    return files

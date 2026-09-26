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


def trim_video_intro(
    video_path: Path,
    trim_seconds: float = 8.0,
    progress: JobState | None = None,
) -> bool:
    """
    Losslessly trim the first N seconds (intro/bumper/logo) of a video file using ffmpeg -c copy.
    Runs in milliseconds without re-encoding.
    """
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return False

    suffix = video_path.suffix.lower()
    if suffix not in (".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".ts"):
        return False

    temp_dir = video_path.parent
    temp_out = temp_dir / f".trim_{os.getpid()}_{video_path.name}"

    try:
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{trim_seconds:.2f}",
            "-i", str(video_path),
            "-map", "0:v:0",
            "-map", "0:a?",
            "-map", "0:s?",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
        ]
        if suffix in (".mp4", ".m4v", ".mov"):
            cmd.extend(["-movflags", "+faststart"])
        cmd.append(str(temp_out))

        res = subprocess.run(cmd, capture_output=True, timeout=180)
        if res.returncode == 0 and temp_out.exists() and temp_out.stat().st_size > 0:
            os.replace(str(temp_out), str(video_path))
            return True
        else:
            if progress:
                err_snippet = (res.stderr.decode("utf-8", errors="ignore") if res.stderr else "")[-300:]
                progress.log(f"[Intro] ffmpeg trim warning: {err_snippet.strip()}")
            return False
    except Exception as exc:
        if progress:
            progress.log(f"[Intro] trim failed for {video_path.name}: {exc}")
        return False
    finally:
        if temp_out.exists():
            try:
                temp_out.unlink()
            except Exception:
                pass


def prepend_cover_frame(
    video_path: Path,
    thumb_path: Path,
    duration_sec: float = 0.5,
    progress: JobState | None = None,
) -> bool:
    """
    Prepend a short (0.5s) video clip of the thumbnail image to the beginning of the video
    using ffmpeg concat demuxer without re-encoding the main video.
    Forces Google Drive's transcoder to pick the thumbnail image as the video thumbnail.
    """
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return False

    suffix = video_path.suffix.lower()
    if suffix not in (".mp4", ".m4v", ".mov", ".mkv"):
        return False

    temp_dir = video_path.parent
    temp_out = temp_dir / f".prep_mux_{os.getpid()}_{video_path.name}"

    try:
        # Probe video and audio stream parameters
        cmd_probe = [
            "ffprobe", "-v", "error",
            "-show_streams",
            "-of", "json",
            str(video_path),
        ]
        res = subprocess.run(cmd_probe, capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            return False
        probe = json.loads(res.stdout)
        streams = probe.get("streams") or []
        v_stream = next((s for s in streams if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic", 0) == 0), None)
        if not v_stream:
            return False
        a_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

        w = v_stream.get("width")
        h = v_stream.get("height")
        if not w or not h:
            return False

        # Match dimensions to even numbers (libx264 requirement)
        w = int(w)
        h = int(h)
        if w % 2 != 0:
            w -= 1
        if h % 2 != 0:
            h -= 1

        pix_fmt = v_stream.get("pix_fmt") or "yuv420p"
        if pix_fmt not in ("yuv420p", "yuvj420p"):
            pix_fmt = "yuv420p"

        fps = v_stream.get("r_frame_rate") or "30/1"
        codec_name = (v_stream.get("codec_name") or "h264").lower()
        encoder = "libx265" if "hevc" in codec_name or "h265" in codec_name else "libx264"

        with tempfile.TemporaryDirectory(prefix="vb_prep_") as td_str:
            td = Path(td_str)
            intro_clip = td / f"intro{suffix}"
            dur_str = f"{duration_sec:.2f}"

            cmd_intro = [
                "ffmpeg", "-y",
                "-loop", "1", "-t", dur_str, "-i", str(thumb_path),
            ]
            if a_stream:
                sr = str(a_stream.get("sample_rate") or "44100")
                ch = int(a_stream.get("channels") or 2)
                cl = "mono" if ch == 1 else "stereo"
                cmd_intro.extend([
                    "-f", "lavfi", "-t", dur_str, "-i", f"anullsrc=r={sr}:cl={cl}",
                ])

            cmd_intro.extend([
                "-c:v", encoder,
                "-preset", "ultrafast",
                "-pix_fmt", pix_fmt,
                "-r", str(fps),
                "-s", f"{w}x{h}",
            ])

            if a_stream:
                cmd_intro.extend(["-c:a", "aac", "-b:a", "128k"])
            else:
                cmd_intro.append("-an")

            cmd_intro.extend(["-movflags", "+faststart", str(intro_clip)])
            res_intro = subprocess.run(cmd_intro, capture_output=True, timeout=30)
            if res_intro.returncode != 0 or not intro_clip.exists() or intro_clip.stat().st_size == 0:
                if progress:
                    err_msg = (res_intro.stderr.decode("utf-8", errors="ignore") if res_intro.stderr else "")[-200:]
                    progress.log(f"[Thumbnail] Intro clip encode warning: {err_msg.strip()}")
                return False

            list_txt = td / "list.txt"
            with open(list_txt, "w", encoding="utf-8") as f:
                f.write(f"file '{intro_clip.as_posix()}'\n")
                f.write(f"file '{video_path.as_posix()}'\n")

            cmd_concat = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0", "-i", str(list_txt),
                "-c", "copy",
                "-movflags", "+faststart",
                str(temp_out),
            ]
            res_concat = subprocess.run(cmd_concat, capture_output=True, timeout=180)
            if res_concat.returncode == 0 and temp_out.exists() and temp_out.stat().st_size > 0:
                os.replace(str(temp_out), str(video_path))
                return True
            else:
                if progress:
                    err_msg = (res_concat.stderr.decode("utf-8", errors="ignore") if res_concat.stderr else "")[-200:]
                    progress.log(f"[Thumbnail] Concat warning: {err_msg.strip()}")
                return False
    except Exception as exc:
        if progress:
            progress.log(f"[Thumbnail] Prepend cover warning: {exc}")
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
    1. Optionally trims intro/bumper (e.g. first 8s) if trim_intro option is True.
    2. For each video, extracts a representative deep frame at configured percentage (default 20%),
       embeds it into the container as attached_pic (for MP4/MKV/MOV/M4V), and caches thumbnail
       bytes for cloud upload metadata (e.g. Google Drive contentHints).
    3. Runs fast with zero re-encoding (-c copy).
    """
    if not options.get("set_video_thumbnail", True) and not options.get("trim_intro", False):
        return files

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        if progress:
            progress.log("[Thumbnail] ffmpeg/ffprobe not found; skipping video processing.")
        return files

    supported_exts = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".ts"}
    video_files = [p for p in files if p.suffix.lower() in supported_exts and p.is_file()]

    if not video_files:
        return files

    if progress:
        progress.log(f"[Thumbnail] Processing {len(video_files)} video file(s)...")

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

                # 1. Optionally trim intro if enabled
                if options.get("trim_intro", False):
                    try:
                        trim_sec = float(options.get("trim_intro_sec", 8.0))
                    except (ValueError, TypeError):
                        trim_sec = 8.0
                    if trim_sec > 0 and duration > trim_sec + 5.0:
                        trimmed = trim_video_intro(video_path, trim_sec, progress)
                        if trimmed:
                            if progress:
                                progress.log(f"[Intro] Cut {trim_sec:.1f}s intro from {video_path.name}")
                            # Re-probe duration after trimming
                            probe = probe_video(video_path)
                            if probe:
                                duration_str = (probe.get("format") or {}).get("duration")
                                if duration_str:
                                    duration = float(duration_str)

                # Skip thumbnailing if user explicitly disabled set_video_thumbnail
                if not options.get("set_video_thumbnail", True):
                    continue

                thumb_file = tmp_dir / f"thumb_{idx}.jpg"
                used_ts = extract_deep_frame(video_path, duration, thumb_file, options)
                if not used_ts or not thumb_file.exists():
                    continue

                # Read thumbnail bytes for in-memory cache (used by Google Drive upload)
                thumb_bytes = thumb_file.read_bytes()
                set_cached_thumbnail(video_path, thumb_bytes)

                prepended = False
                if options.get("prepend_thumbnail_frame", False):
                    prepended = prepend_cover_frame(video_path, thumb_file, duration_sec=0.5, progress=progress)

                # Embed thumbnail into video container (MP4, M4V, MOV, MKV)
                success = embed_thumbnail(video_path, thumb_file, progress)
                if progress:
                    mins = int(used_ts // 60)
                    secs = int(used_ts % 60)
                    pct_val = (used_ts / duration) * 100.0
                    actions = []
                    if prepended:
                        actions.append("0.5s prepended for Drive")
                    if success:
                        actions.append("embedded cover")
                    else:
                        actions.append("cached for Drive")
                    action_desc = ", ".join(actions)
                    progress.log(f"[Thumbnail] Set deep frame ({pct_val:.0f}%, {mins:02d}:{secs:02d} / {used_ts:.1f}s, {action_desc}) for {video_path.name}")
            except Exception as exc:
                if progress:
                    progress.log(f"[Thumbnail] Failed for {video_path.name}: {exc}")
                continue

    return files

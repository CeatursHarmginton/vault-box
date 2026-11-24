from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)

try:
    from PIL import Image as PILImage, ImageOps
    PIL_AVAILABLE = True
except ImportError:
    PILImage = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]
    PIL_AVAILABLE = False

try:
    import pyvips
    PYVIPS_AVAILABLE = True
except ImportError:
    pyvips = None  # type: ignore[assignment]
    PYVIPS_AVAILABLE = False

IMAGE_FORMAT_ALIASES: dict[str, str] = {
    "jpeg": "jpg",
    "jfif": "jpg",
    "pjpeg": "jpg",
    "pjp": "jpg",
    "tif": "tiff",
    "heif": "heic",
}

VIDEO_FORMAT_ALIASES: dict[str, str] = {
    "mpg": "mpeg",
    "m2ts": "ts",
    "mts": "ts",
    "3g2": "3gp",
}

ALL_FORMAT_ALIASES: dict[str, str] = {
    **IMAGE_FORMAT_ALIASES,
    **VIDEO_FORMAT_ALIASES,
}

IMAGE_EXTENSIONS: set[str] = {
    ".jpg", ".jpeg", ".jfif", ".pjpeg", ".pjp",
    ".png", ".webp", ".bmp", ".gif",
    ".tiff", ".tif", ".avif", ".heic", ".heif", ".ico",
}

VIDEO_EXTENSIONS: set[str] = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v",
    ".ts", ".m2ts", ".mts", ".flv", ".wmv",
    ".3gp", ".3g2", ".mpeg", ".mpg", ".ogv", ".vob", ".asf", ".f4v",
}

STREAM_VIDEO_EXTENSIONS: set[str] = {".m3u8", ".mpd"}

ARCHIVE_EXTENSIONS: set[str] = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".iso", ".zst",
}

AUDIO_EXTENSIONS: set[str] = {
    ".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus", ".wma",
}

DOCUMENT_EXTENSIONS: set[str] = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".srt", ".ass", ".vtt", ".epub",
}

SUPPORTED_IMAGE_OUTPUT_FORMATS: tuple[str, ...] = ("jpg", "png", "webp", "avif", "bmp", "tiff")
SUPPORTED_VIDEO_OUTPUT_FORMATS: tuple[str, ...] = ("mp4", "mkv", "webm", "mov")

PIL_FORMAT_BY_EXT: dict[str, str] = {
    "jpg": "JPEG",
    "png": "PNG",
    "webp": "WEBP",
    "bmp": "BMP",
    "tiff": "TIFF",
    "avif": "AVIF",
}

_NVENC_CHECK_LOCK = threading.Lock()
_NVENC_AVAILABLE: bool | None = None


def normalize_format_ext(value: Any, category: str | None = None) -> str:
    """Normalize a format or extension (e.g. '.JPEG' -> 'jpg', 'MP4' -> 'mp4')."""
    raw = str(value or "").strip().lower().lstrip(".")
    if not raw:
        return ""
    if category == "image":
        return IMAGE_FORMAT_ALIASES.get(raw, raw)
    if category == "video":
        return VIDEO_FORMAT_ALIASES.get(raw, raw)
    return ALL_FORMAT_ALIASES.get(raw, raw)


def normalize_format_list(values: Any, category: str | None = None) -> list[str]:
    """Parse a list or comma/space-separated string of formats into deduplicated canonical names."""
    if values is None:
        return []
    raw_items: list[Any]
    if isinstance(values, str):
        raw_items = [tok.strip() for tok in values.replace(";", ",").replace(" ", ",").split(",") if tok.strip()]
    elif isinstance(values, (list, tuple, set)):
        raw_items = list(values)
    else:
        return []

    seen: set[str] = set()
    result: list[str] = []
    for item in raw_items:
        norm = normalize_format_ext(item, category=category)
        if norm and norm not in seen:
            seen.add(norm)
            result.append(norm)
    return result


def extract_item_filename(item_or_name: Any) -> str:
    """Extract best-effort filename (with extension) from a source item dict, Path, or string."""
    if isinstance(item_or_name, Path):
        return item_or_name.name
    if isinstance(item_or_name, str):
        clean = item_or_name.strip()
        if "://" in clean:
            clean = unquote(urlsplit(clean).path or "")
        return PurePosixPath(clean.replace("\\", "/")).name

    if isinstance(item_or_name, dict):
        for key in ("name", "server_filename", "filename", "_relative_path"):
            val = str(item_or_name.get(key) or "").strip()
            if val and "." in PurePosixPath(val.replace("\\", "/")).name:
                return PurePosixPath(val.replace("\\", "/")).name
        for key in ("path", "url", "id"):
            val = str(item_or_name.get(key) or "").strip()
            if not val:
                continue
            if "://" in val:
                url_path = unquote(urlsplit(val).path or "")
                cand = PurePosixPath(url_path.replace("\\", "/")).name
            else:
                cand = PurePosixPath(val.replace("\\", "/")).name
            if cand and "." in cand:
                return cand
        fallback = str(item_or_name.get("name") or item_or_name.get("server_filename") or "").strip()
        return PurePosixPath(fallback.replace("\\", "/")).name
    return ""


def extract_item_ext(item_or_name: Any, category: str | None = None) -> tuple[str, str]:
    """Return (raw_ext_without_dot, canonical_ext_without_dot) for an item or filename."""
    fname = extract_item_filename(item_or_name)
    suffix = PurePosixPath(fname).suffix.lower().lstrip(".") if fname else ""
    if not suffix and isinstance(item_or_name, dict):
        item_type = str(item_or_name.get("type") or "").strip().lower()
        if item_type == "hls":
            return "m3u8", "m3u8"
        if item_type == "dash":
            return "mpd", "mpd"
    return suffix, normalize_format_ext(suffix, category=category)


def detect_file_category(item_or_name: Any) -> str:
    """Classify a file or source item into 'image', 'video', 'archive', 'audio', 'document', or 'other'."""
    raw_ext, _ = extract_item_ext(item_or_name)
    dot_ext = f".{raw_ext}" if raw_ext else ""
    if dot_ext in IMAGE_EXTENSIONS:
        return "image"
    if dot_ext in VIDEO_EXTENSIONS or dot_ext in STREAM_VIDEO_EXTENSIONS:
        return "video"
    if dot_ext in ARCHIVE_EXTENSIONS:
        return "archive"
    if dot_ext in AUDIO_EXTENSIONS:
        return "audio"
    if dot_ext in DOCUMENT_EXTENSIONS:
        return "document"

    if isinstance(item_or_name, dict):
        mime = str(
            item_or_name.get("mimeType")
            or item_or_name.get("mime_type")
            or (item_or_name.get("meta") or {}).get("mimeType")
            or ""
        ).lower()
        if mime.startswith("image/"):
            return "image"
        if mime.startswith("video/") or "mpegurl" in mime or "dash+xml" in mime:
            return "video"
        if mime.startswith("audio/"):
            return "audio"
        item_type = str(item_or_name.get("type") or "").lower()
        if item_type in ("hls", "dash", "video", "stream"):
            return "video"
        raw_url = str(item_or_name.get("url") or item_or_name.get("path") or item_or_name.get("id") or "").lower()
        if any(tok in raw_url for tok in (".m3u8", ".mpd", "/hls/")):
            return "video"
    return "other"


# ============================================================================
# 1. Download File Filter
# ============================================================================

@dataclass
class FileFilterConfig:
    enabled: bool = False
    mode: str = "all"        # "all" | "include" | "except"
    category: str = "all"    # "all" | "video" | "image" | "archive" | "audio" | "document"
    formats: list[str] = field(default_factory=list)


def parse_file_filter(options: dict[str, Any] | None) -> FileFilterConfig:
    opts = options or {}
    raw = opts.get("file_filter")
    if raw is None:
        raw = opts.get("fileFilter")

    if not isinstance(raw, dict):
        mode = str(opts.get("file_filter_mode") or opts.get("fileFilterMode") or "all").strip().lower()
        category = str(opts.get("file_filter_category") or opts.get("fileFilterCategory") or "all").strip().lower()
        formats = normalize_format_list(opts.get("file_filter_formats") or opts.get("fileFilterFormats"))
        explicit_enabled = opts.get("file_filter_enabled")
        if explicit_enabled is None:
            explicit_enabled = opts.get("fileFilterEnabled")
    else:
        mode = str(raw.get("mode") or "all").strip().lower()
        category = str(raw.get("category") or "all").strip().lower()
        formats = normalize_format_list(raw.get("formats"))
        explicit_enabled = raw.get("enabled")

    if mode not in ("all", "include", "except"):
        mode = "all"
    if mode == "exclude":
        mode = "except"
    if category not in ("all", "video", "image", "archive", "audio", "document"):
        category = "all"

    if explicit_enabled is False:
        return FileFilterConfig(enabled=False, mode="all", category="all", formats=[])

    is_active = (mode != "all" and (bool(formats) or category != "all")) or (category != "all")
    return FileFilterConfig(
        enabled=is_active,
        mode=mode,
        category=category,
        formats=formats,
    )


def is_file_filter_enabled(options: dict[str, Any] | None) -> bool:
    return parse_file_filter(options).enabled


def should_download_file_item(item_or_name: Any, options: dict[str, Any] | None) -> bool:
    """
    Evaluate whether a file item should be downloaded according to `options['file_filter']`.
    Folders always return True so their contents can be listed and filtered individually.
    Default ('all' mode, 'all' category) returns True for every file.
    """
    if isinstance(item_or_name, dict):
        if (
            item_or_name.get("type") == "folder"
            or item_or_name.get("is_folder")
            or item_or_name.get("isdir")
        ):
            return True
        f_name_raw = str(item_or_name.get("name") or item_or_name.get("path") or item_or_name.get("id") or "")
        if f_name_raw.endswith("__backup_resume"):
            return True

    cfg = parse_file_filter(options)
    if not cfg.enabled:
        return True

    file_cat = detect_file_category(item_or_name)
    raw_ext, norm_ext = extract_item_ext(item_or_name)

    if cfg.category != "all" and file_cat != cfg.category:
        return False

    if cfg.mode == "all":
        return True

    fmt_set = set(cfg.formats)
    if not fmt_set:
        return True

    candidates = {x for x in (raw_ext, norm_ext) if x}
    matched = bool(candidates & fmt_set)

    if cfg.mode == "include":
        return matched
    if cfg.mode == "except":
        return not matched
    return True


# ============================================================================
# 2. Auto Convert Rules (Unified for Image & Video)
# ============================================================================

@dataclass
class CategoryConvertRule:
    category: str
    enabled: bool = False
    input_mode: str = "all"                  # "all" | "include" | "except"
    input_formats: list[str] = field(default_factory=list)
    output_formats: list[str] = field(default_factory=list)
    target_format: str = ""

    def should_convert_ext(self, raw_Or_norm_ext: str) -> tuple[bool, str | None, str]:
        """
        Evaluate a file extension within this category.
        Returns (should_convert, target_format_or_none, reason).
        """
        if not self.enabled:
            return False, None, "disabled"
        if not self.output_formats:
            return False, None, "no_output_format"

        ext = normalize_format_ext(raw_Or_norm_ext, category=self.category)
        if not ext:
            return False, None, "unknown_extension"

        accepted_outputs = [normalize_format_ext(fmt, category=self.category) for fmt in self.output_formats if fmt]
        if not accepted_outputs:
            return False, None, "no_output_format"

        # Rule: If a file already matches one of the selected output formats, skip conversion.
        if ext in accepted_outputs:
            return False, None, f"already_accepted:{ext}"

        # Always exclude gif from video conversion (user requirement: all types to mp4 except gif)
        if self.category == "video" and ext == "gif":
            return False, None, "excluded:gif"

        if self.input_mode == "include":
            inc_set = {normalize_format_ext(f, category=self.category) for f in self.input_formats if f}
            if ext not in inc_set:
                return False, None, f"not_included:{ext}"
        elif self.input_mode == "except":
            exc_set = {normalize_format_ext(f, category=self.category) for f in self.input_formats if f}
            if ext in exc_set:
                return False, None, f"excluded:{ext}"

        target = normalize_format_ext(self.target_format, category=self.category)
        if not target or target not in accepted_outputs:
            target = accepted_outputs[0]

        if ext == target:
            return False, None, f"already_accepted:{ext}"

        return True, target, "convert"


def _parse_single_category_rule(
    category: str,
    raw_rule: Any,
    supported_outputs: tuple[str, ...],
    default_output: str,
) -> CategoryConvertRule:
    if not isinstance(raw_rule, dict):
        if category == "video":
            return CategoryConvertRule(
                category=category,
                enabled=False,
                input_mode="except",
                input_formats=["gif"],
                output_formats=[default_output],
                target_format=default_output,
            )
        return CategoryConvertRule(
            category=category,
            enabled=False,
            input_mode="all",
            input_formats=[],
            output_formats=[default_output],
            target_format=default_output,
        )

    enabled = bool(raw_rule.get("enabled", False))
    default_mode = "except" if category == "video" else "all"
    input_mode = str(raw_rule.get("input_mode") or raw_rule.get("inputMode") or default_mode).strip().lower()
    if input_mode == "exclude":
        input_mode = "except"
    if input_mode not in ("all", "include", "except"):
        input_mode = default_mode

    raw_input_fmts = raw_rule.get("input_formats") if "input_formats" in raw_rule else raw_rule.get("inputFormats")
    input_formats = normalize_format_list(
        raw_input_fmts,
        category=category,
    )
    if category == "video" and input_mode == "except" and raw_input_fmts is None and not input_formats:
        input_formats = ["gif"]

    raw_outputs = (
        raw_rule.get("output_formats")
        if "output_formats" in raw_rule
        else raw_rule.get("outputFormats")
    )
    parsed_outputs = [
        fmt for fmt in normalize_format_list(raw_outputs, category=category)
        if fmt in supported_outputs
    ]
    if not parsed_outputs and enabled:
        single_target = normalize_format_ext(
            raw_rule.get("target_format") or raw_rule.get("targetFormat") or default_output,
            category=category,
        )
        if single_target in supported_outputs:
            parsed_outputs = [single_target]
        else:
            parsed_outputs = [default_output]
    elif not parsed_outputs:
        parsed_outputs = [default_output]

    target_fmt = normalize_format_ext(
        raw_rule.get("target_format") or raw_rule.get("targetFormat") or "",
        category=category,
    )
    if not target_fmt or target_fmt not in parsed_outputs:
        target_fmt = parsed_outputs[0]

    return CategoryConvertRule(
        category=category,
        enabled=enabled,
        input_mode=input_mode,
        input_formats=input_formats,
        output_formats=parsed_outputs,
        target_format=target_fmt,
    )


def parse_auto_convert_config(options: dict[str, Any] | None) -> dict[str, CategoryConvertRule]:
    opts = options or {}
    raw = opts.get("auto_convert")
    if raw is None:
        raw = opts.get("autoConvert")

    if isinstance(raw, dict):
        master_enabled = raw.get("enabled")
        is_master_on = master_enabled is True or str(master_enabled).strip().lower() in ("true", "1")
        if not is_master_on:
            return {
                "image": CategoryConvertRule("image", enabled=False, input_mode="all", input_formats=[], output_formats=["jpg"], target_format="jpg"),
                "video": CategoryConvertRule("video", enabled=False, input_mode="except", input_formats=["gif"], output_formats=["mp4"], target_format="mp4"),
            }
        img_raw = raw.get("image") or raw.get("images") or {}
        vid_raw = raw.get("video") or raw.get("videos") or {}
        img_rule = _parse_single_category_rule("image", img_raw, SUPPORTED_IMAGE_OUTPUT_FORMATS, "jpg")
        vid_rule = _parse_single_category_rule("video", vid_raw, SUPPORTED_VIDEO_OUTPUT_FORMATS, "mp4")
        return {"image": img_rule, "video": vid_rule}

    return {
        "image": CategoryConvertRule("image", enabled=False, input_mode="all", input_formats=[], output_formats=["jpg"], target_format="jpg"),
        "video": CategoryConvertRule("video", enabled=False, input_mode="except", input_formats=["gif"], output_formats=["mp4"], target_format="mp4"),
    }


def is_auto_convert_enabled(options: dict[str, Any] | None) -> bool:
    cfg = parse_auto_convert_config(options)
    return bool(cfg["image"].enabled or cfg["video"].enabled)


def resolve_conversion_decision(item_or_path: Any, options: dict[str, Any] | None) -> tuple[str, bool, str | None, str]:
    """
    Determine conversion action for a file or item.
    Returns: (category, should_convert, target_format_or_none, reason)
    """
    cat = detect_file_category(item_or_path)
    if cat not in ("image", "video"):
        return cat, False, None, "unsupported_category"

    _, norm_ext = extract_item_ext(item_or_path, category=cat)
    if cat == "video" and norm_ext in ("m3u8", "mpd"):
        # Streaming manifests are first assembled to .mp4 before file conversion applies
        norm_ext = "mp4"

    cfg = parse_auto_convert_config(options)
    rule = cfg[cat]
    should_conv, target_fmt, reason = rule.should_convert_ext(norm_ext)
    return cat, should_conv, target_fmt, reason


def resolve_conversion_target(item_or_path: Any, options: dict[str, Any] | None) -> str | None:
    """Return target output format (e.g. 'mp4', 'jpg') if item_or_path will be converted, else None."""
    _, should_conv, target_fmt, _ = resolve_conversion_decision(item_or_path, options)
    return target_fmt if should_conv else None


# ============================================================================
# 3. Validation Before Transferring
# ============================================================================

def validate_converted_image(src_path: Path, out_path: Path, target_fmt: str) -> tuple[bool, str]:
    """Validate that a converted image exists, is non-empty, decodes cleanly, and matches target_fmt."""
    if not out_path.is_file():
        return False, "Converted image file does not exist"
    try:
        out_size = out_path.stat().st_size
    except OSError as exc:
        return False, f"Cannot stat converted image: {exc}"
    if out_size <= 0:
        return False, "Converted image file is empty (0 bytes)"

    norm_target = normalize_format_ext(target_fmt, category="image")
    expected_pil_fmt = PIL_FORMAT_BY_EXT.get(norm_target)

    if PIL_AVAILABLE and PILImage is not None:
        try:
            with PILImage.open(out_path) as img:
                actual_fmt = (img.format or "").upper()
                w, h = img.size
                if w <= 0 or h <= 0:
                    return False, f"Invalid converted image dimensions ({w}x{h})"
                if expected_pil_fmt and actual_fmt and actual_fmt != expected_pil_fmt:
                    return False, f"Converted image format mismatch: expected {expected_pil_fmt}, got {actual_fmt}"
                img.verify()
            # Full pixel decode verification to detect truncated/corrupted image streams
            with PILImage.open(out_path) as img_decode:
                img_decode.load()
            return True, "ok"
        except Exception as pil_exc:
            if norm_target != "avif":
                return False, f"Image decode validation failed: {pil_exc}"

    if shutil.which("ffprobe") and shutil.which("ffmpeg"):
        try:
            probe_cmd = [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,width,height",
                "-of", "json",
                str(out_path),
            ]
            res = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)
            if res.returncode != 0:
                return False, f"ffprobe validation failed: {res.stderr.strip()[:200]}"
            data = json.loads(res.stdout or "{}")
            streams = data.get("streams") or []
            if not streams:
                return False, "No image stream found in converted file"
            w = int(streams[0].get("width") or 0)
            h = int(streams[0].get("height") or 0)
            if w <= 0 or h <= 0:
                return False, f"Invalid image dimensions ({w}x{h})"
            dec_cmd = ["ffmpeg", "-v", "error", "-i", str(out_path), "-f", "null", "-"]
            dec_res = subprocess.run(dec_cmd, capture_output=True, text=True, timeout=30)
            if dec_res.returncode != 0:
                return False, f"ffmpeg image decode check failed: {dec_res.stderr.strip()[:200]}"
            return True, "ok"
        except Exception as exc:
            return False, f"Image validation error: {exc}"

    return False, "No image validation backend (Pillow or FFmpeg) available"


def _probe_media_json(media_path: Path, timeout: int = 30) -> dict[str, Any] | None:
    if not shutil.which("ffprobe"):
        return None
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=format_name,duration,size:stream=index,codec_type,codec_name,width,height,disposition",
            "-of", "json",
            str(media_path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0:
            return None
        return json.loads(res.stdout or "{}")
    except Exception:
        return None


def validate_converted_video(
    src_path: Path,
    out_path: Path,
    target_fmt: str,
    src_probe: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Validate that a converted video exists, is non-empty, has intact streams/duration, and decodes cleanly."""
    if not out_path.is_file():
        return False, "Converted video file does not exist"
    try:
        out_size = out_path.stat().st_size
    except OSError as exc:
        return False, f"Cannot stat converted video: {exc}"
    if out_size <= 0:
        return False, "Converted video file is empty (0 bytes)"

    if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
        return False, "ffprobe/ffmpeg not available to validate converted video"

    out_probe = _probe_media_json(out_path)
    if not out_probe:
        return False, "ffprobe failed to read converted video container"

    norm_target = normalize_format_ext(target_fmt, category="video")
    fmt_info = out_probe.get("format") or {}
    format_names = {tok.strip().lower() for tok in str(fmt_info.get("format_name") or "").split(",") if tok.strip()}

    expected_format_tokens: dict[str, set[str]] = {
        "mp4": {"mp4", "mov"},
        "mov": {"mov", "mp4"},
        "mkv": {"matroska", "webm"},
        "webm": {"webm", "matroska"},
    }
    expected_tokens = expected_format_tokens.get(norm_target, {norm_target})
    if format_names and not (format_names & expected_tokens):
        return False, f"Container format mismatch: expected {norm_target}, got {','.join(sorted(format_names))}"

    out_streams = out_probe.get("streams") or []
    out_video_streams = [
        s for s in out_streams
        if s.get("codec_type") == "video" and (s.get("disposition") or {}).get("attached_pic", 0) == 0
    ]
    out_audio_streams = [s for s in out_streams if s.get("codec_type") == "audio"]

    if src_probe is None:
        src_probe = _probe_media_json(src_path)

    src_streams = (src_probe or {}).get("streams") or []
    src_has_video = any(
        s.get("codec_type") == "video" and (s.get("disposition") or {}).get("attached_pic", 0) == 0
        for s in src_streams
    )
    src_has_audio = any(s.get("codec_type") == "audio" for s in src_streams)

    if src_has_video or not src_streams:
        if not out_video_streams:
            return False, "Converted video is missing primary video stream"
        v0 = out_video_streams[0]
        w = int(v0.get("width") or 0)
        h = int(v0.get("height") or 0)
        if w <= 0 or h <= 0:
            return False, f"Invalid converted video dimensions ({w}x{h})"

    if src_has_audio and not out_audio_streams:
        return False, "Converted video is missing audio stream present in source"

    try:
        src_dur = float(((src_probe or {}).get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        src_dur = 0.0
    try:
        out_dur = float(fmt_info.get("duration") or 0.0)
    except (TypeError, ValueError):
        out_dur = 0.0

    if src_dur > 1.0:
        if out_dur <= 0.0:
            return False, "Converted video reports zero/invalid duration"
        tolerance = max(2.5, src_dur * 0.05)
        if abs(out_dur - src_dur) > tolerance:
            return False, f"Converted video duration mismatch (source={src_dur:.2f}s, converted={out_dur:.2f}s)"

    # Decode first second of output to guarantee streams are decodable and not corrupt
    try:
        dec_cmd = [
            "ffmpeg", "-v", "error",
            "-i", str(out_path),
            "-t", "1",
            "-f", "null", "-",
        ]
        dec_res = subprocess.run(dec_cmd, capture_output=True, text=True, timeout=45)
        if dec_res.returncode != 0:
            err_tail = (dec_res.stderr or "").strip()[-240:]
            return False, f"Decoded frame check failed: {err_tail or 'ffmpeg decode error'}"
    except Exception as exc:
        return False, f"Video decode verification failed: {exc}"

    return True, "ok"


# ============================================================================
# 4. Image & Video Conversion Execution (Non-Destructive)
# ============================================================================

def convert_image_file(
    src_path: Path,
    dest_path: Path,
    target_fmt: str,
    quality: int = 92,
) -> tuple[bool, str]:
    """
    Convert `src_path` to `dest_path` in `target_fmt` without modifying `src_path`.
    Validates output before atomically placing it at `dest_path`.
    """
    norm_target = normalize_format_ext(target_fmt, category="image")
    if norm_target not in SUPPORTED_IMAGE_OUTPUT_FORMATS:
        return False, f"Unsupported target image format: {target_fmt}"

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_out = dest_path.parent / f".conv_img_{os.getpid()}_{threading.get_ident()}_{dest_path.name}"
    temp_out.unlink(missing_ok=True)
    q = max(1, min(100, int(quality or 92)))

    converted = False
    last_err = ""

    if PIL_AVAILABLE and PILImage is not None:
        try:
            with PILImage.open(src_path) as img:
                if ImageOps is not None:
                    try:
                        img = ImageOps.exif_transpose(img)
                    except Exception:
                        pass
                pil_fmt = PIL_FORMAT_BY_EXT[norm_target]
                save_kwargs: dict[str, Any] = {}
                if norm_target in ("jpg", "bmp"):
                    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                        rgba = img.convert("RGBA")
                        bg = PILImage.new("RGB", rgba.size, (255, 255, 255))
                        bg.paste(rgba, mask=rgba.split()[3])
                        img = bg
                    elif img.mode != "RGB":
                        img = img.convert("RGB")
                    if norm_target == "jpg":
                        save_kwargs = {"quality": q, "optimize": True}
                elif norm_target == "png":
                    if img.mode not in ("RGB", "RGBA", "L", "LA", "P"):
                        img = img.convert("RGBA" if "A" in img.mode else "RGB")
                    save_kwargs = {"optimize": True}
                elif norm_target == "webp":
                    if img.mode not in ("RGB", "RGBA"):
                        img = img.convert("RGBA" if ("A" in img.mode or "transparency" in img.info) else "RGB")
                    save_kwargs = {"quality": q}
                elif norm_target == "tiff":
                    save_kwargs = {"compression": "tiff_deflate"}
                elif norm_target == "avif":
                    save_kwargs = {"quality": q}

                img.save(temp_out, format=pil_fmt, **save_kwargs)
                converted = True
        except Exception as exc:
            last_err = str(exc)
            temp_out.unlink(missing_ok=True)

    if not converted and PYVIPS_AVAILABLE and pyvips is not None:
        try:
            vimg = pyvips.Image.new_from_file(str(src_path))
            if norm_target in ("jpg", "webp", "avif"):
                vimg.write_to_file(str(temp_out), Q=q)
            else:
                vimg.write_to_file(str(temp_out))
            converted = True
        except Exception as exc:
            last_err = str(exc)
            temp_out.unlink(missing_ok=True)

    if not converted and shutil.which("ffmpeg"):
        try:
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src_path), "-frames:v", "1"]
            if norm_target == "jpg":
                cmd.extend(["-q:v", "2"])
            elif norm_target == "webp":
                cmd.extend(["-quality", str(q)])
            cmd.append(str(temp_out))
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if res.returncode == 0 and temp_out.is_file():
                converted = True
            else:
                last_err = (res.stderr or "").strip()[-240:] or "ffmpeg image convert failed"
                temp_out.unlink(missing_ok=True)
        except Exception as exc:
            last_err = str(exc)
            temp_out.unlink(missing_ok=True)

    if not converted:
        temp_out.unlink(missing_ok=True)
        return False, f"Image conversion failed: {last_err or 'no capable image encoder'}"

    try:
        valid, reason = validate_converted_image(src_path, temp_out, norm_target)
        if not valid:
            temp_out.unlink(missing_ok=True)
            return False, reason
        os.replace(str(temp_out), str(dest_path))
        return True, "ok"
    except Exception as exc:
        temp_out.unlink(missing_ok=True)
        return False, f"Failed finalizing converted image: {exc}"


def _has_nvenc_h264() -> bool:
    global _NVENC_AVAILABLE
    with _NVENC_CHECK_LOCK:
        if _NVENC_AVAILABLE is not None:
            return _NVENC_AVAILABLE
        if not shutil.which("ffmpeg") or not shutil.which("nvidia-smi"):
            _NVENC_AVAILABLE = False
            return False
        try:
            cmd = [
                "ffmpeg", "-v", "error",
                "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1",
                "-c:v", "h264_nvenc",
                "-f", "null", "-",
            ]
            res = subprocess.run(cmd, capture_output=True, timeout=10)
            _NVENC_AVAILABLE = (res.returncode == 0)
        except Exception:
            _NVENC_AVAILABLE = False
        return _NVENC_AVAILABLE


def _run_ffmpeg_cancellable(
    cmd: list[str],
    cancel_check: Callable[[], None] | None = None,
    timeout_sec: float = 7200.0,
) -> tuple[int, str]:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    start = time.monotonic()
    try:
        while True:
            if cancel_check is not None:
                try:
                    cancel_check()
                except BaseException:
                    proc.kill()
                    proc.wait(timeout=5)
                    raise
            if time.monotonic() - start > timeout_sec:
                proc.kill()
                proc.wait(timeout=5)
                return -1, f"FFmpeg timed out after {int(timeout_sec)}s"
            ret = proc.poll()
            if ret is not None:
                stderr_out = proc.stderr.read() if proc.stderr else ""
                return ret, stderr_out
            time.sleep(0.25)
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass


def _build_remux_command(
    src_path: Path,
    temp_out: Path,
    target_fmt: str,
    src_probe: dict[str, Any] | None,
) -> list[str] | None:
    """Build a fast stream-copy (or video-copy + audio-transcode) command when codecs are compatible."""
    if not src_probe:
        return None
    streams = src_probe.get("streams") or []
    v_stream = next(
        (s for s in streams if s.get("codec_type") == "video" and (s.get("disposition") or {}).get("attached_pic", 0) == 0),
        None,
    )
    if not v_stream:
        return None
    a_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    v_codec = str(v_stream.get("codec_name") or "").lower()
    a_codec = str(a_stream.get("codec_name") or "").lower() if a_stream else None
    src_ext = src_path.suffix.lower()

    if target_fmt in ("mp4", "mov"):
        compat_v = {"h264", "hevc", "mpeg4", "av1"}
        compat_a = {"aac", "mp3", "ac3", "eac3", "alac"}
        if target_fmt == "mov":
            compat_a = compat_a | {"pcm_s16le", "pcm_s24le"}
        if v_codec not in compat_v:
            return None
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src_path), "-map", "0:v:0"]
        if a_stream:
            cmd.extend(["-map", "0:a:0"])
        cmd.extend(["-c:v", "copy"])
        if v_codec == "hevc":
            cmd.extend(["-tag:v", "hvc1"])
        if a_stream:
            if a_codec in compat_a:
                cmd.extend(["-c:a", "copy"])
                if a_codec == "aac" and src_ext in (".ts", ".m2ts", ".mts"):
                    cmd.extend(["-bsf:a", "aac_adtstoasc"])
            else:
                cmd.extend(["-c:a", "aac", "-b:a", "192k"])
        cmd.extend(["-movflags", "+faststart", str(temp_out)])
        return cmd

    if target_fmt == "mkv":
        compat_v_mkv = {"h264", "hevc", "vp8", "vp9", "av1", "mpeg4", "mpeg2video"}
        if v_codec not in compat_v_mkv:
            return None
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src_path), "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?", "-c", "copy", str(temp_out)]
        return cmd

    if target_fmt == "webm":
        compat_v_webm = {"vp8", "vp9", "av1"}
        compat_a_webm = {None, "opus", "vorbis"}
        if v_codec not in compat_v_webm:
            return None
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src_path), "-map", "0:v:0"]
        if a_stream:
            cmd.extend(["-map", "0:a:0"])
        cmd.extend(["-c:v", "copy"])
        if a_stream:
            if a_codec in compat_a_webm:
                cmd.extend(["-c:a", "copy"])
            else:
                cmd.extend(["-c:a", "libopus", "-b:a", "128k"])
        cmd.append(str(temp_out))
        return cmd

    return None


def _build_transcode_commands(
    src_path: Path,
    temp_out: Path,
    target_fmt: str,
    src_probe: dict[str, Any] | None,
) -> list[list[str]]:
    """Return ordered fallback transcode commands (GPU first if available, then CPU)."""
    streams = (src_probe or {}).get("streams") or []
    has_audio = any(s.get("codec_type") == "audio" for s in streams) or not src_probe

    commands: list[list[str]] = []
    even_scale = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

    if target_fmt in ("mp4", "mkv", "mov"):
        extra_mux = ["-movflags", "+faststart"] if target_fmt in ("mp4", "mov") else []
        if _has_nvenc_h264():
            gpu_cmd = [
                "ffmpeg", "-y", "-v", "error",
                "-i", str(src_path),
                "-map", "0:v:0",
            ]
            if has_audio:
                gpu_cmd.extend(["-map", "0:a:0?"])
            gpu_cmd.extend([
                "-vf", even_scale,
                "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "21", "-pix_fmt", "yuv420p",
            ])
            if has_audio:
                gpu_cmd.extend(["-c:a", "aac", "-b:a", "192k"])
            gpu_cmd.extend([*extra_mux, str(temp_out)])
            commands.append(gpu_cmd)

        cpu_cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(src_path),
            "-map", "0:v:0",
        ]
        if has_audio:
            cpu_cmd.extend(["-map", "0:a:0?"])
        cpu_cmd.extend([
            "-vf", even_scale,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
        ])
        if has_audio:
            cpu_cmd.extend(["-c:a", "aac", "-b:a", "192k"])
        cpu_cmd.extend([*extra_mux, str(temp_out)])
        commands.append(cpu_cmd)
        return commands

    if target_fmt == "webm":
        vp9_cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(src_path),
            "-map", "0:v:0",
        ]
        if has_audio:
            vp9_cmd.extend(["-map", "0:a:0?"])
        vp9_cmd.extend([
            "-vf", even_scale,
            "-c:v", "libvpx-vp9", "-crf", "31", "-b:v", "0", "-deadline", "good", "-cpu-used", "4", "-row-mt", "1", "-pix_fmt", "yuv420p",
        ])
        if has_audio:
            vp9_cmd.extend(["-c:a", "libopus", "-b:a", "128k"])
        vp9_cmd.append(str(temp_out))
        commands.append(vp9_cmd)

        vp8_cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(src_path),
            "-map", "0:v:0",
        ]
        if has_audio:
            vp8_cmd.extend(["-map", "0:a:0?"])
        vp8_cmd.extend([
            "-vf", even_scale,
            "-c:v", "libvpx", "-crf", "10", "-b:v", "2M", "-pix_fmt", "yuv420p",
        ])
        if has_audio:
            vp8_cmd.extend(["-c:a", "libvorbis", "-q:a", "4"])
        vp8_cmd.append(str(temp_out))
        commands.append(vp8_cmd)
        return commands

    return commands


def convert_video_file(
    src_path: Path,
    dest_path: Path,
    target_fmt: str,
    cancel_check: Callable[[], None] | None = None,
) -> tuple[bool, str]:
    """
    Convert `src_path` to `dest_path` in `target_fmt` without modifying `src_path`.
    Tries lossless stream remux first when codecs are compatible, then falls back to transcode.
    Validates output container, streams, duration, and decodability before placing `dest_path`.
    """
    norm_target = normalize_format_ext(target_fmt, category="video")
    if norm_target not in SUPPORTED_VIDEO_OUTPUT_FORMATS:
        return False, f"Unsupported target video format: {target_fmt}"
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return False, "ffmpeg/ffprobe is required for video conversion"

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_out = dest_path.parent / f".conv_vid_{os.getpid()}_{threading.get_ident()}_{dest_path.name}"
    temp_out.unlink(missing_ok=True)

    src_probe = _probe_media_json(src_path)
    last_err = ""

    # 1. Fast stream remux attempt when video codec is compatible with target container
    remux_cmd = _build_remux_command(src_path, temp_out, norm_target, src_probe)
    if remux_cmd is not None:
        ret, stderr_out = _run_ffmpeg_cancellable(remux_cmd, cancel_check=cancel_check)
        if ret == 0 and temp_out.is_file() and temp_out.stat().st_size > 0:
            valid, reason = validate_converted_video(src_path, temp_out, norm_target, src_probe=src_probe)
            if valid:
                os.replace(str(temp_out), str(dest_path))
                return True, "remuxed"
            last_err = reason
        else:
            last_err = (stderr_out or "").strip()[-240:]
        temp_out.unlink(missing_ok=True)

    # 2. Full transcode fallback
    for transcode_cmd in _build_transcode_commands(src_path, temp_out, norm_target, src_probe):
        temp_out.unlink(missing_ok=True)
        ret, stderr_out = _run_ffmpeg_cancellable(transcode_cmd, cancel_check=cancel_check)
        if ret == 0 and temp_out.is_file() and temp_out.stat().st_size > 0:
            valid, reason = validate_converted_video(src_path, temp_out, norm_target, src_probe=src_probe)
            if valid:
                os.replace(str(temp_out), str(dest_path))
                return True, "transcoded"
            last_err = reason
        else:
            last_err = (stderr_out or "").strip()[-240:]
        temp_out.unlink(missing_ok=True)

    return False, f"Video conversion failed: {last_err or 'ffmpeg error'}"


# ============================================================================
# 5. Batch Directory Conversion Orchestrator
# ============================================================================

def _is_inside_dir(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def _propagate_source_ref(job_state: Any, src_path: Path, dest_path: Path, converted_name: str | None = None) -> None:
    if job_state is None:
        return
    refs = getattr(job_state, "_source_refs", None)
    if not isinstance(refs, dict):
        return
    src_key = str(src_path.resolve())
    ref = refs.get(src_key)
    if isinstance(ref, dict):
        refs[str(dest_path.resolve())] = dict(ref)
        if converted_name and hasattr(job_state, "item_timings") and isinstance(job_state.item_timings, dict):
            for t_val in job_state.item_timings.values():
                if isinstance(t_val, dict) and t_val.get("name") in (src_path.name, ref.get("name")):
                    t_val["outputName"] = converted_name


def _allocate_unique_rel_path(desired_rel: PurePosixPath, reserved_lower: set[str], src_ext: str) -> PurePosixPath:
    cand_key = desired_rel.as_posix().casefold()
    if cand_key not in reserved_lower:
        reserved_lower.add(cand_key)
        return desired_rel

    parent = desired_rel.parent
    stem = desired_rel.stem
    suffix = desired_rel.suffix
    clean_src = src_ext.lstrip(".")
    if clean_src:
        tagged = (parent / f"{stem}_{clean_src}{suffix}") if str(parent) != "." else PurePosixPath(f"{stem}_{clean_src}{suffix}")
        if tagged.as_posix().casefold() not in reserved_lower:
            reserved_lower.add(tagged.as_posix().casefold())
            return tagged

    idx = 1
    while True:
        numbered = (parent / f"{stem}_{idx}{suffix}") if str(parent) != "." else PurePosixPath(f"{stem}_{idx}{suffix}")
        if numbered.as_posix().casefold() not in reserved_lower:
            reserved_lower.add(numbered.as_posix().casefold())
            return numbered
        idx += 1


def convert_directory(
    input_dir: Path,
    output_dir: Path,
    options: dict[str, Any],
    job_state: Any = None,
    cancel_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """
    Batch-process all files under `input_dir`, writing validated converted outputs and passthrough
    files into `output_dir` while strictly preserving original files inside `input_dir`.

    Returns:
      {
        "outputs": list[Path],
        "converted": list[dict[str, Any]],
        "skipped": list[dict[str, Any]],
        "failed": list[dict[str, Any]],
      }
    """
    if input_dir.resolve() == output_dir.resolve():
        raise ValueError("Auto Convert output_dir must be distinct from input_dir to preserve original files")

    output_dir.mkdir(parents=True, exist_ok=True)
    all_files = sorted(
        p for p in input_dir.rglob("*")
        if p.is_file()
        and not _is_inside_dir(p, output_dir)
        and not p.name.startswith(".conv_")
        and not p.name.endswith((".part", ".aria2", ".ytdl", ".tmp"))
    )

    passthrough_plan: list[tuple[Path, PurePosixPath, str]] = []
    image_tasks: list[tuple[Path, PurePosixPath, str]] = []
    video_tasks: list[tuple[Path, PurePosixPath, str]] = []
    reserved_rel_lower: set[str] = set()

    # First pass: identify passthrough files vs conversion candidates, reserving passthrough names first
    pending_conversions: list[tuple[Path, PurePosixPath, str, str]] = []
    for src_path in all_files:
        rel_posix = PurePosixPath(src_path.relative_to(input_dir).as_posix())
        cat, should_conv, target_fmt, reason = resolve_conversion_decision(src_path, options)
        if should_conv and target_fmt:
            pending_conversions.append((src_path, rel_posix, cat, target_fmt))
        else:
            reserved_rel_lower.add(rel_posix.as_posix().casefold())
            passthrough_plan.append((src_path, rel_posix, reason))

    for src_path, rel_posix, cat, target_fmt in pending_conversions:
        parent = rel_posix.parent
        new_name = f"{rel_posix.stem}.{target_fmt}"
        desired_rel = (parent / new_name) if str(parent) != "." else PurePosixPath(new_name)
        final_rel = _allocate_unique_rel_path(desired_rel, reserved_rel_lower, src_path.suffix)
        if cat == "image":
            image_tasks.append((src_path, final_rel, target_fmt))
        else:
            video_tasks.append((src_path, final_rel, target_fmt))

    total_to_convert = len(image_tasks) + len(video_tasks)
    done_lock = threading.Lock()
    done_count = 0

    def _log(msg: str) -> None:
        if job_state is not None and hasattr(job_state, "log"):
            job_state.log(msg)

    def _update_progress(current_name: str = "") -> None:
        if job_state is None:
            return
        if current_name and hasattr(job_state, "set"):
            job_state.set(current_file=current_name)
        if total_to_convert > 0 and hasattr(job_state, "progress"):
            pct = min(100.0, (done_count / total_to_convert) * 100.0)
            setattr(job_state.progress, "convert", pct)
            job_state.updated_at = time.time()

    converted_records: list[dict[str, Any]] = []
    skipped_records: list[dict[str, Any]] = []
    failed_records: list[dict[str, Any]] = []
    staged_outputs: list[Path] = []

    if total_to_convert > 0:
        _log(f"[Auto-Convert] Batch processing {total_to_convert} file(s) ({len(image_tasks)} image, {len(video_tasks)} video)...")
        _update_progress()

    # 1. Copy passthrough files (preserving originals in input_dir)
    for src_path, rel_posix, reason in passthrough_plan:
        if cancel_check is not None:
            cancel_check()
        dest_path = output_dir / Path(rel_posix.as_posix())
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dest_path)
        _propagate_source_ref(job_state, src_path, dest_path)
        staged_outputs.append(dest_path)
        if reason.startswith("already_accepted:"):
            fmt_label = reason.split(":", 1)[1].upper()
            _log(f"[Auto-Convert] Skipped (already accepted {fmt_label}): {rel_posix.as_posix()}")
        skipped_records.append({
            "source_name": rel_posix.as_posix(),
            "output_name": rel_posix.as_posix(),
            "reason": reason,
        })

    # 2. Batch convert images in parallel
    if image_tasks:
        img_workers = min(len(image_tasks), max(1, min(8, (os.cpu_count() or 2))))
        try:
            req_quality = int(float(options.get("convert_image_quality") or options.get("quality") or 92))
        except (TypeError, ValueError):
            req_quality = 92

        def _run_image_one(task: tuple[Path, PurePosixPath, str]) -> tuple[Path, PurePosixPath, str, bool, str, Path]:
            src_p, out_rel, tgt_fmt = task
            if cancel_check is not None:
                cancel_check()
            dest_p = output_dir / Path(out_rel.as_posix())
            ok, msg = convert_image_file(src_p, dest_p, tgt_fmt, quality=req_quality)
            return src_p, out_rel, tgt_fmt, ok, msg, dest_p

        with ThreadPoolExecutor(max_workers=img_workers) as pool:
            futures = [pool.submit(_run_image_one, t) for t in image_tasks]
            for fut in as_completed(futures):
                src_p, out_rel, tgt_fmt, ok, msg, dest_p = fut.result()
                src_rel = src_p.relative_to(input_dir).as_posix()
                with done_lock:
                    done_count += 1
                    cur_idx = done_count
                _update_progress(out_rel.name)
                if ok and dest_p.is_file():
                    _propagate_source_ref(job_state, src_p, dest_p, converted_name=out_rel.name)
                    staged_outputs.append(dest_p)
                    converted_records.append({
                        "category": "image",
                        "source_name": src_rel,
                        "output_name": out_rel.as_posix(),
                        "target_format": tgt_fmt,
                        "original_size": src_p.stat().st_size,
                        "converted_size": dest_p.stat().st_size,
                        "validated": True,
                    })
                    _log(f"[Auto-Convert] [{cur_idx}/{total_to_convert}] Image converted & validated: {src_rel} -> {out_rel.as_posix()}")
                else:
                    dest_p.unlink(missing_ok=True)
                    failed_records.append({
                        "category": "image",
                        "source_path": str(src_p.resolve()),
                        "source_name": src_rel,
                        "target_format": tgt_fmt,
                        "error": msg,
                    })
                    _log(f"[Auto-Convert] [{cur_idx}/{total_to_convert}] [ERROR] Image conversion/validation failed for {src_rel}: {msg}")

    # 3. Batch convert videos
    if video_tasks:
        vid_workers = min(len(video_tasks), max(1, int(options.get("convert_video_workers") or 2)))

        def _run_video_one(task: tuple[Path, PurePosixPath, str]) -> tuple[Path, PurePosixPath, str, bool, str, Path]:
            src_p, out_rel, tgt_fmt = task
            if cancel_check is not None:
                cancel_check()
            if job_state is not None and hasattr(job_state, "set"):
                job_state.set(current_file=src_p.name)
            dest_p = output_dir / Path(out_rel.as_posix())
            ok, msg = convert_video_file(src_p, dest_p, tgt_fmt, cancel_check=cancel_check)
            return src_p, out_rel, tgt_fmt, ok, msg, dest_p

        with ThreadPoolExecutor(max_workers=vid_workers) as pool:
            futures = [pool.submit(_run_video_one, t) for t in video_tasks]
            for fut in as_completed(futures):
                src_p, out_rel, tgt_fmt, ok, msg, dest_p = fut.result()
                src_rel = src_p.relative_to(input_dir).as_posix()
                with done_lock:
                    done_count += 1
                    cur_idx = done_count
                _update_progress(out_rel.name)
                if ok and dest_p.is_file():
                    _propagate_source_ref(job_state, src_p, dest_p, converted_name=out_rel.name)
                    staged_outputs.append(dest_p)
                    converted_records.append({
                        "category": "video",
                        "source_name": src_rel,
                        "output_name": out_rel.as_posix(),
                        "target_format": tgt_fmt,
                        "mode": msg,
                        "original_size": src_p.stat().st_size,
                        "converted_size": dest_p.stat().st_size,
                        "validated": True,
                    })
                    _log(f"[Auto-Convert] [{cur_idx}/{total_to_convert}] Video converted ({msg}) & validated: {src_rel} -> {out_rel.as_posix()}")
                else:
                    dest_p.unlink(missing_ok=True)
                    failed_records.append({
                        "category": "video",
                        "source_path": str(src_p.resolve()),
                        "source_name": src_rel,
                        "target_format": tgt_fmt,
                        "error": msg,
                    })
                    _log(f"[Auto-Convert] [{cur_idx}/{total_to_convert}] [ERROR] Video conversion/validation failed for {src_rel}: {msg}")

    if total_to_convert == 0 and job_state is not None and hasattr(job_state, "progress"):
        setattr(job_state.progress, "convert", 100.0)

    staged_outputs.sort()
    return {
        "outputs": [p for p in staged_outputs if p.is_file()],
        "converted": converted_records,
        "skipped": skipped_records,
        "failed": failed_records,
    }

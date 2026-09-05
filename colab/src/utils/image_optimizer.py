from __future__ import annotations

import os
import shutil
import subprocess
import logging
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import groupby
from pathlib import Path
from threading import Lock
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Attempt to load PIL
try:
    from PIL import Image as PILImage
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# Attempt to load pyvips
try:
    import pyvips
    PYVIPS_AVAILABLE = True
except ImportError:
    PYVIPS_AVAILABLE = False

VIPS_CLI_AVAILABLE = shutil.which("vips") is not None
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".ts", ".3gp", ".flv", ".mpeg", ".mpg", ".wmv"}
# Hard floor for JPEG quality: below this the artefacts are worse than an oversized file,
# so the descent stops here even when the size target is still not met.
MIN_QUALITY = 65

OPTIMIZED_TAG_KEY = "VaultBox_Optimized"
OPTIMIZED_TAG_VALUE = "true"
OPTIMIZED_EXIF_DESC = "VaultBox_Optimized:true"
EXIF_IMAGE_DESCRIPTION = 0x010E
EXIF_USER_COMMENT = 0x9286

def clamp_quality(value: Any, default: int = 95) -> int:
    try:
        q = int(float(value))
    except (TypeError, ValueError):
        q = default
    return max(MIN_QUALITY, min(100, q))

def is_image_already_optimized(path: Path) -> bool:
    """
    Fast metadata inspection to check if the image has already been optimized.
    Only inspects headers without decoding image pixels.
    """
    if not PIL_AVAILABLE or not path.is_file():
        return False
    try:
        with PILImage.open(path) as img:
            fmt = (img.format or "").upper()
            if fmt == "PNG":
                if img.info.get(OPTIMIZED_TAG_KEY) in ("true", "1", OPTIMIZED_TAG_VALUE):
                    return True
                if img.info.get("Description") in (OPTIMIZED_TAG_VALUE, OPTIMIZED_EXIF_DESC):
                    return True
                if hasattr(img, "text") and img.text.get(OPTIMIZED_TAG_KEY) in ("true", "1", OPTIMIZED_TAG_VALUE):
                    return True
            else:
                exif = img.getexif()
                if exif:
                    desc = exif.get(EXIF_IMAGE_DESCRIPTION)
                    if desc and ("VaultBox_Optimized" in str(desc) or "already-optimized" in str(desc)):
                        return True
                    comment = exif.get(EXIF_USER_COMMENT)
                    if comment and ("VaultBox_Optimized" in str(comment) or "already-optimized" in str(comment)):
                        return True
                comment = img.info.get("comment")
                if comment and ("VaultBox_Optimized" in str(comment) or "already-optimized" in str(comment)):
                    return True
    except Exception:
        return False
    return False

def embed_optimization_metadata(file_path: Path) -> bool:
    """
    Embeds optimization metadata into an existing image file.
    """
    if not PIL_AVAILABLE or not file_path.is_file():
        return False
    try:
        with PILImage.open(file_path) as img:
            fmt = (img.format or "").upper()
            if fmt == "PNG":
                from PIL import PngImagePlugin
                png_info = PngImagePlugin.PngInfo()
                for k, v in img.info.items():
                    if isinstance(k, str) and isinstance(v, str):
                        png_info.add_text(k, v)
                png_info.add_text(OPTIMIZED_TAG_KEY, OPTIMIZED_TAG_VALUE)
                img.save(file_path, format="PNG", pnginfo=png_info)
                return True
            elif fmt in ("JPEG", "WEBP", "TIFF"):
                exif = img.getexif()
                exif[EXIF_IMAGE_DESCRIPTION] = OPTIMIZED_EXIF_DESC
                img.save(file_path, format=fmt, exif=exif)
                return True
    except Exception as e:
        logger.warning("Failed to embed optimization metadata in %s: %s", file_path, e)
        return False
    return False

def should_optimize_image_file(path: Path, options: dict[str, Any]) -> bool:
    suffix = path.suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        return False
    if not options.get("force_reoptimize", False) and is_image_already_optimized(path):
        return False
    if suffix not in (".jpg", ".jpeg"):
        return True
    size = path.stat().st_size
    min_target = int(float(options.get("min_target_mb", 1.0)) * 1024 * 1024)
    max_target = int(float(options.get("max_target_mb", 3.0)) * 1024 * 1024)
    return size < min_target if options.get("upscale") else size > max_target

def _optimize_workers(options: dict[str, Any], image_count: int) -> int:
    try:
        requested = int(float(options.get("optimize_workers") or 0))
    except (TypeError, ValueError):
        requested = 0
    if requested > 0:
        return min(image_count, max(1, min(64, requested)))
    cpu = os.cpu_count() or 2
    if options.get("upscale") and shutil.which("realesrgan-ncnn-vulkan"):
        return min(image_count, min(4, max(2, cpu // 2)))
    return min(image_count, min(32, max(2, cpu - 1)))

def compress_image(src_path: Path, dest_path: Path, q: int, scale: float = 1.0) -> bool:
    """
    Compresses an image to JPEG format using pyvips, PIL, or vips CLI fallback.
    Quality is clamped to MIN_QUALITY..100 — no caller may encode below the floor.
    Embeds optimization metadata into the output image.
    Returns True if successful, False otherwise.
    """
    q = clamp_quality(q)
    if PYVIPS_AVAILABLE:
        try:
            img = pyvips.Image.new_from_file(str(src_path))
            if scale < 1.0:
                img = img.resize(scale)
            img.set_type(pyvips.GValue.gstr_type, "image-description", OPTIMIZED_EXIF_DESC)
            img.write_to_file(str(dest_path), Q=q, optimize_coding=True)
            return True
        except Exception as e:
            logger.warning(f"pyvips compression failed: {e}. Trying fallback.")
            
    if PIL_AVAILABLE:
        try:
            img = PILImage.open(src_path)
            if scale < 1.0:
                w, h = img.size
                img = img.resize((int(w * scale), int(h * scale)), PILImage.Resampling.LANCZOS)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            exif = img.getexif()
            exif[EXIF_IMAGE_DESCRIPTION] = OPTIMIZED_EXIF_DESC
            img.save(dest_path, "JPEG", quality=q, optimize=True, exif=exif)
            return True
        except Exception as e:
            logger.error(f"PIL fallback failed: {e}")

    if VIPS_CLI_AVAILABLE:
        try:
            if scale < 1.0:
                cmd = ["vips", "resize", str(src_path), f"{dest_path}[Q={q},strip,optimize_coding]", str(scale)]
            else:
                cmd = ["vips", "copy", str(src_path), f"{dest_path}[Q={q},strip,optimize_coding]"]
            res = subprocess.run(cmd, capture_output=True)
            if res.returncode == 0:
                return True
            else:
                logger.warning(f"vips CLI failed: {res.stderr.decode(errors='ignore')}")
        except Exception as e:
            logger.warning(f"vips CLI run failed: {e}")
            
    return False

def copy_or_convert_image(src_path: Path, dest_path: Path, q: int) -> None:
    if src_path.suffix.lower() in (".jpg", ".jpeg") and dest_path.suffix.lower() in (".jpg", ".jpeg"):
        shutil.copy2(src_path, dest_path)
        return
    if not compress_image(src_path, dest_path, q):
        raise RuntimeError(f"Failed to convert image to JPEG: {src_path}")

def copy_original_image(src_path: Path, dest_dir: Path) -> Path:
    dest_path = dest_dir / src_path.name
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_path, dest_path)
    return dest_path

def _valid_optimized_output(src_path: Path, out_path: Path, scale: float) -> bool:
    if not out_path.exists() or out_path.stat().st_size <= 0:
        return False
    if not PIL_AVAILABLE:
        return out_path.stat().st_size >= 1024
    try:
        with PILImage.open(src_path) as src, PILImage.open(out_path) as out:
            out.verify()
            expected = (max(1, int(src.size[0] * scale)), max(1, int(src.size[1] * scale)))
            return out.size == expected
    except Exception:
        return False

def optimize_image_file(src_path: Path, dest_dir: Path, options: dict[str, Any], adaptive_quality: int) -> tuple[Path, int, str]:
    """
    Optimizes a single image file based on size target.
    Returns: (output_path, final_quality, status_message)
    """
    quality = clamp_quality(options.get("quality", 85), 85)

    # 0. Skip if already optimized and force_reoptimize is not set
    if not options.get("force_reoptimize", False) and is_image_already_optimized(src_path):
        dest_path = copy_original_image(src_path, dest_dir)
        return dest_path, quality, "Giữ nguyên (Đã tối ưu)"

    min_target = int(float(options.get("min_target_mb", 1.0)) * 1024 * 1024)
    max_target = int(float(options.get("max_target_mb", 3.0)) * 1024 * 1024)
    start_quality = clamp_quality(options.get("start_quality", 95))
    auto_size = options.get("auto_size", True)
    scale = float(options.get("resolution_scale", 1.0))
    
    size = src_path.stat().st_size
    dest_ext = ".jpg" if src_path.suffix.lower() not in (".jpg", ".jpeg") else src_path.suffix.lower()
    # Use stem + ext to avoid string-replace collision (e.g. "image.png.png")
    dest_path = dest_dir / f"{src_path.stem}{dest_ext}"
    
    # Ensure destination parent exists
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    
    # 1. Skip if already within range
    if min_target <= size <= max_target:
        copy_or_convert_image(src_path, dest_path, quality)
        if dest_path.suffix.lower() != src_path.suffix.lower() and not _valid_optimized_output(src_path, dest_path, 1.0):
            dest_path = copy_original_image(src_path, dest_dir)
        return dest_path, quality, "Giữ nguyên"
        
    # 2. Upscale if too small (Real-ESRGAN placeholder or keep as-is)
    if size < min_target:
        if options.get("upscale") and shutil.which("realesrgan-ncnn-vulkan"):
            outscale = int(options.get("outscale", 3))
            with tempfile.TemporaryDirectory() as tmp_dir:
                output_file = Path(tmp_dir) / f"{src_path.stem}_out{src_path.suffix}"
                try:
                    cmd = ["realesrgan-ncnn-vulkan", "-i", str(src_path), "-o", str(output_file), "-s", str(outscale)]
                    res = subprocess.run(cmd, capture_output=True)
                    if res.returncode == 0 and output_file.exists():
                        copy_or_convert_image(output_file, dest_path, quality)
                        if not _valid_optimized_output(src_path, dest_path, float(outscale)):
                            dest_path = copy_original_image(src_path, dest_dir)
                            return dest_path, quality, "Giữ nguyên (Lỗi upscale)"
                        return dest_path, quality, "Thành công (Upscaled)"
                except Exception as e:
                    logger.warning(f"Real-ESRGAN failed: {e}")
            
        copy_or_convert_image(src_path, dest_path, quality)
        if dest_path.suffix.lower() != src_path.suffix.lower() and not _valid_optimized_output(src_path, dest_path, 1.0):
            dest_path = copy_original_image(src_path, dest_dir)
        return dest_path, quality, "Giữ nguyên (Upscale tắt hoặc lỗi)"

    # 3. Compress if larger than max_target
    q = adaptive_quality if auto_size else quality
    q = clamp_quality(q, quality)
    final_q = q
    
    temp_fd, temp_path_str = tempfile.mkstemp(suffix=dest_ext)
    os.close(temp_fd)
    temp_path = Path(temp_path_str)
    
    best_temp_fd, best_temp_str = tempfile.mkstemp(suffix=dest_ext)
    os.close(best_temp_fd)
    best_temp = Path(best_temp_str)
    best_size = size  # Track best compressed size (start at original)
    best_q = q
    invalid_output_seen = False
    
    try:
        while q >= MIN_QUALITY:
            if not compress_image(src_path, temp_path, q, scale):
                break
            temp_size = temp_path.stat().st_size
            if not _valid_optimized_output(src_path, temp_path, scale):
                logger.warning("Skipping invalid optimized image: %s quality=%s size=%s", src_path.name, q, temp_size)
                invalid_output_seen = True
                if not auto_size:
                    break
                q -= 5
                continue
            
            # Track best compressed result (smallest)
            if temp_size < best_size:
                shutil.copy2(temp_path, best_temp)
                best_size = temp_size
                best_q = q
            
            if not auto_size or temp_size <= max_target:
                final_q = q
                shutil.copy2(temp_path, dest_path)
                return dest_path, final_q, "Thành công (Compressed)"
            q -= 5
            
        # Loop exhausted — use best compressed result if smaller than original
        if best_size < size:
            shutil.copy2(best_temp, dest_path)
            final_q = best_q
            status = "Thành công (Compressed)"
        else:
            if invalid_output_seen:
                dest_path = copy_original_image(src_path, dest_dir)
            else:
                copy_or_convert_image(src_path, dest_path, quality)
            status = "Giữ nguyên (Lỗi nén)"
    finally:
        temp_path.unlink(missing_ok=True)
        best_temp.unlink(missing_ok=True)
        
    return dest_path, final_q, status

def _is_inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False

def optimize_directory(
    input_dir: Path,
    output_dir: Path,
    options: dict[str, Any],
    job_state: Any,
    cancel_check: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    """
    Scans the input directory, processes all files, and outputs to the output directory.
    Non-image files are copied as-is.
    Groups images by folder and resets adaptive quality per folder (like fiximg_vips.ps1).
    """
    all_files = [p for p in input_dir.rglob("*") if p.is_file() and not _is_inside(p, output_dir)]
    
    # Separate images and non-images
    images = []
    passthrough = []
    for p in all_files:
        if should_optimize_image_file(p, options):
            images.append(p)
        else:
            passthrough.append(p)
            
    # Group images by folder, sort by folder path, then by size within each folder
    # This exactly replicates fiximg_vips.ps1 which groups by folder, resets
    # adaptiveQualityStart per folder, and sorts by size within each group
    images.sort(key=lambda x: (str(x.parent), x.stat().st_size))
    folder_groups: list[tuple[str, list[Path]]] = []
    for folder_key, group in groupby(images, key=lambda x: str(x.parent)):
        folder_groups.append((folder_key, list(group)))
    
    start_quality = clamp_quality(options.get("start_quality", 95))
    max_target = int(float(options.get("max_target_mb", 3.0)) * 1024 * 1024)
    
    results: list[dict[str, Any]] = []
    total_images = len(images)
    processed = 0
    workers = _optimize_workers(options, total_images) if total_images else 1

    def set_optimize_progress(done: int) -> None:
        if total_images and hasattr(job_state, "progress"):
            setattr(job_state.progress, "optimize", min(100, done / total_images * 100))
            job_state.updated_at = time.time()

    if workers > 1 and not options.get("auto_size", True):
        job_state.log(f"[TỐI ƯU] Xử lý song song: {workers} worker(s)")

        def run_one(index: int, p: Path) -> tuple[int, dict[str, Any]]:
            if cancel_check:
                cancel_check()
            relative_path = p.relative_to(input_dir)
            job_state.set(current_file=str(relative_path))
            job_state.log(f"Đang tối ưu ảnh ({index + 1}/{total_images}): {p.name}")
            orig_size = p.stat().st_size
            out_path, final_q, status = optimize_image_file(p, output_dir / relative_path.parent, options, start_quality)
            return index, {
                "name": str(out_path.relative_to(output_dir)),
                "source_name": str(relative_path),
                "original_size": orig_size,
                "optimized_size": out_path.stat().st_size,
                "status": status,
                "quality": final_q
            }

        ordered: list[dict[str, Any] | None] = [None] * total_images
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(run_one, i, p) for i, p in enumerate(images)]
            for future in as_completed(futures):
                index, item = future.result()
                ordered[index] = item
                processed += 1
                set_optimize_progress(processed)
        results.extend(item for item in ordered if item is not None)

        for p in passthrough:
            if cancel_check:
                cancel_check()
            relative_path = p.relative_to(input_dir)
            target_path = output_dir / relative_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target_path)

        return results

    counter_lock = Lock()

    def process_group(folder_key: str, folder_images: list[Path]) -> list[dict[str, Any]]:
        nonlocal processed
        adaptive_quality = start_quality
        job_state.log(f"[THƯ MỤC] Đang xử lý: {folder_key} (Quality bắt đầu={adaptive_quality})")
        group_results: list[dict[str, Any]] = []
        for p in folder_images:
            if cancel_check:
                cancel_check()
            with counter_lock:
                processed += 1
                current = processed
            relative_path = p.relative_to(input_dir)
            job_state.set(current_file=str(relative_path))
            job_state.log(f"Đang tối ưu ảnh ({current}/{total_images}): {p.name}")
            orig_size = p.stat().st_size
            out_path, final_q, status = optimize_image_file(p, output_dir / relative_path.parent, options, adaptive_quality)
            new_size = out_path.stat().st_size
            if options.get("auto_size", True) and (start_quality - final_q) >= 10 and new_size >= 0.8 * max_target:
                new_start = min(start_quality, max(MIN_QUALITY, final_q + 5))
                if new_start < adaptive_quality:
                    job_state.log(f"[TỐI ƯU] Auto quality start giảm từ {adaptive_quality} xuống {new_start} dựa trên ảnh trước.")
                    adaptive_quality = new_start
            group_results.append({
                "name": str(out_path.relative_to(output_dir)),
                "source_name": str(relative_path),
                "original_size": orig_size,
                "optimized_size": new_size,
                "status": status,
                "quality": final_q
            })
            set_optimize_progress(current)
        return group_results

    if workers > 1 and len(folder_groups) > 1:
        group_workers = min(workers, len(folder_groups))
        job_state.log(f"[TỐI ƯU] Xử lý song song: {group_workers} folder worker(s)")
        ordered_groups: list[list[dict[str, Any]] | None] = [None] * len(folder_groups)
        with ThreadPoolExecutor(max_workers=group_workers) as executor:
            futures = [executor.submit(process_group, folder_key, folder_images) for folder_key, folder_images in folder_groups]
            for index, future in enumerate(futures):
                ordered_groups[index] = future.result()
        for group_results in ordered_groups:
            results.extend(group_results or [])
    else:
        for folder_key, folder_images in folder_groups:
            results.extend(process_group(folder_key, folder_images))
    
    # Copy files that should pass through unchanged.
    for p in passthrough:
        if cancel_check:
            cancel_check()
        relative_path = p.relative_to(input_dir)
        target_path = output_dir / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target_path)
        
    return results

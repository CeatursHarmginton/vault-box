from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlparse, unquote, parse_qs

import httpx

from ..providers import PROVIDERS
from ..providers.base import ProviderFailure, safe_name
from .progress import JobState

try:
    from ..config import IS_KAGGLE
except Exception:
    IS_KAGGLE = bool(
        os.environ.get("KAGGLE_KERNEL_RUN_TYPE")
        or os.environ.get("KAGGLE_CONTAINER_NAME")
        or os.environ.get("KAGGLE_URL_BASE")
        or Path("/kaggle").exists()
    )

DEFAULT_CHUNK_SEGMENTS = 150
DEFAULT_CHUNK_MB = 250
DEFAULT_SEGMENT_CONCURRENCY = 6


def _get_disk_free(dir_path: Path) -> int:
    """Safely return free disk space in bytes for given directory path."""
    try:
        check_p = dir_path if dir_path.exists() else dir_path.parent
        while not check_p.exists() and check_p.parent != check_p:
            check_p = check_p.parent
        return shutil.disk_usage(check_p).free
    except Exception:
        return 10 * 1024 * 1024 * 1024


def _emergency_cleanup_disk(
    work_dir: Path | None = None,
    parts_dir: Path | None = None,
    uploaded_names: set[str] | None = None,
) -> int:
    """Free as much local disk space as possible when disk is tight or [Errno 28] occurs."""
    freed = 0
    import gc
    gc.collect()

    if parts_dir and parts_dir.is_dir():
        for p in list(parts_dir.glob("part_*.ts")):
            if uploaded_names is None or p.name in uploaded_names:
                try:
                    sz = p.stat().st_size
                    p.unlink(missing_ok=True)
                    freed += sz
                except Exception:
                    pass

    if work_dir and work_dir.is_dir():
        for f in list(work_dir.glob("concat_*.txt")) + list(work_dir.glob("*.tmp")):
            try:
                sz = f.stat().st_size
                f.unlink(missing_ok=True)
                freed += sz
            except Exception:
                pass

    gc.collect()
    return freed


def should_skip_merge_due_to_space(
    work_dir: Path,
    total_segments: int,
    completed_parts: list[dict[str, Any]],
    accumulated_bytes: int = 0,
    downloaded_segs: int = 0,
    chunk_segments: int = DEFAULT_CHUNK_SEGMENTS,
    chunk_mb: int = DEFAULT_CHUNK_MB,
    is_kaggle: bool = False,
    options: dict[str, Any] | None = None,
) -> tuple[bool, str, int, int]:
    """
    Determine if final assembly video merge should be skipped due to limited disk space (e.g. on Kaggle),
    and whether local parts should be deleted immediately after upload to prevent running out of space (Errno 28).

    Condition:
    - If user explicitly requested skip_merge / skip_final_assembly / parts_only / low_disk_mode.
    - If total estimated size of all parts exceeds 50% of available free disk space (or free disk < 2x estimated size + buffer),
      local disk cannot hold both all parts and the merged video file simultaneously.
    - On Kaggle environment where disk quota is strictly limited and shared across files in the batch.
    """
    opts = options or {}
    if opts.get("skip_final_assembly") or opts.get("skip_merge") or opts.get("parts_only") or opts.get("low_disk_mode"):
        return True, "Chế độ bỏ qua ghép video được chỉ định bởi tùy chọn", 0, _get_disk_free(work_dir)

    free_disk = _get_disk_free(work_dir)

    known_bytes = sum(cp.get("bytes", 0) for cp in completed_parts) + accumulated_bytes
    known_segs = sum(cp.get("segment_count", 0) for cp in completed_parts) + downloaded_segs

    if known_segs > 0 and known_bytes > 0:
        avg_seg = known_bytes / known_segs
        estimated_total_bytes = int(total_segments * avg_seg) if total_segments > 0 else known_bytes
    else:
        num_parts = max(1, (total_segments + chunk_segments - 1) // chunk_segments) if total_segments > 0 else 1
        estimated_total_bytes = num_parts * chunk_mb * 1024 * 1024

    half_free = int(free_disk * 0.5)
    required_space = (estimated_total_bytes * 2) + (1024 * 1024 * 1024)

    if estimated_total_bytes > half_free:
        est_mb = estimated_total_bytes / (1024 * 1024)
        free_mb = free_disk / (1024 * 1024)
        return (
            True,
            f"Tổng dung lượng phân đoạn ước tính (~{est_mb:.1f} MB) vượt quá 50% đĩa trống ({free_mb:.1f} MB)",
            estimated_total_bytes,
            free_disk,
        )

    if required_space > free_disk:
        req_mb = required_space / (1024 * 1024)
        free_mb = free_disk / (1024 * 1024)
        return (
            True,
            f"Dung lượng đĩa trống ({free_mb:.1f} MB) không đủ cho cả phân đoạn và video ghép (~{req_mb:.1f} MB)",
            estimated_total_bytes,
            free_disk,
        )

    if is_kaggle:
        if estimated_total_bytes > (free_disk * 0.4) or free_disk < (3 * 1024 * 1024 * 1024):
            est_mb = estimated_total_bytes / (1024 * 1024)
            free_mb = free_disk / (1024 * 1024)
            return (
                True,
                f"Môi trường Kaggle với đĩa trống hạn chế ({free_mb:.1f} MB, video ~{est_mb:.1f} MB)",
                estimated_total_bytes,
                free_disk,
            )

    return False, "", estimated_total_bytes, free_disk



def parse_m3u8_playlist(content: str, base_url: str) -> tuple[str | None, list[dict[str, Any]]]:
    """
    Parse an HLS playlist (Master or Media).
    Returns:
      (master_variant_url_if_any, list_of_segments)
      Each segment dict: {"index": int, "duration": float, "url": str}
    """
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        return None, []

    # Check if Master Playlist
    is_master = any(line.startswith("#EXT-X-STREAM-INF") for line in lines)
    if is_master:
        best_bw = -1
        best_url = None
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
                bw_match = re.search(r"BANDWIDTH=(\d+)", line)
                bw = int(bw_match.group(1)) if bw_match else 0
                target_url = lines[i + 1]
                if not target_url.startswith("#") and bw > best_bw:
                    best_bw = bw
                    best_url = urljoin(base_url, target_url)
        return best_url, []

    # Media Playlist
    segments: list[dict[str, Any]] = []
    seg_idx = 0
    cur_duration = 0.0
    for line in lines:
        if line.startswith("#EXTINF:"):
            m = re.search(r"#EXTINF:([0-9.]+)", line)
            if m:
                cur_duration = float(m.group(1))
        elif not line.startswith("#"):
            seg_url = urljoin(base_url, line)
            segments.append({
                "index": seg_idx,
                "duration": cur_duration,
                "url": seg_url,
            })
            seg_idx += 1
            cur_duration = 0.0

    return None, segments


async def resolve_media_segments(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Fetch and recursively resolve media playlist to extract all segment URLs."""
    cur_url = url
    for _ in range(4):
        resp = await client.get(cur_url, headers=headers)
        resp.raise_for_status()
        content = resp.text
        variant_url, segments = parse_m3u8_playlist(content, cur_url)
        if variant_url:
            cur_url = variant_url
            continue
        if segments:
            return cur_url, segments
        break
    return cur_url, []


def generate_backup_folder_name(raw_name: str) -> str:
    clean = safe_name(re.sub(r'[,;:*?"<>|/\\`\s]+', '_', raw_name).strip('_'))
    stem = Path(clean).stem or clean
    return f"{stem}__backup_resume"


def generate_final_video_name(raw_name: str) -> str:
    clean = safe_name(re.sub(r'[,;:*?"<>|/\\`\s]+', '_', raw_name).strip('_'))
    stem = Path(clean).stem or clean
    if clean.lower().endswith((".m3u8", ".mpd", ".ts")):
        clean = stem
    if not clean.lower().endswith((".mp4", ".mkv", ".webm", ".ts")):
        clean += ".mp4"
    return clean


async def download_single_segment(
    client: httpx.AsyncClient,
    seg: dict[str, Any],
    dest_path: Path,
    headers: dict[str, str] | None,
    sem: asyncio.Semaphore,
    progress: JobState | None = None,
    proxy: str | None = None,
    max_retries: int = 10,
) -> int:
    """Download a single TS segment with robust retry, rate accounting, and exponential backoff."""
    def _safe_write(p: Path, b: bytes) -> None:
        try:
            p.write_bytes(b)
        except OSError as w_err:
            if w_err.errno in (28, 122) or "no space" in str(w_err).lower():
                _emergency_cleanup_disk(p.parent.parent, p.parent.parent / "parts")
                p.write_bytes(b)
            else:
                raise

    async with sem:
        if progress:
            progress.check_cancelled()
        last_error: Exception | None = None
        for attempt in range(max_retries):
            if progress:
                progress.check_cancelled()
            try:
                if proxy and "socks" in proxy.lower():
                    from curl_cffi import requests as cffi_requests
                    s = cffi_requests.Session(impersonate="chrome")
                    c_resp = await asyncio.to_thread(s.get, seg["url"], headers=headers, proxy=proxy, timeout=25)
                    if c_resp.status_code == 200:
                        data = c_resp.content
                        if not data:
                            raise ValueError(f"Segment {seg.get('index')} received empty (0 bytes) content")
                        _safe_write(dest_path, data)
                        return len(data)
                    else:
                        c_resp.raise_for_status()
                else:
                    resp = await client.get(seg["url"], headers=headers, timeout=25.0)
                    if resp.status_code == 200:
                        data = resp.content
                        if not data:
                            raise ValueError(f"Segment {seg.get('index')} received empty (0 bytes) content")
                        _safe_write(dest_path, data)
                        return len(data)
                    else:
                        resp.raise_for_status()
            except Exception as dl_err:
                last_error = dl_err
                err_str = str(dl_err).lower()

                # If token expired or forbidden, fast-fail after attempt >= 1 so caller can renew token immediately
                is_auth_error = any(code in err_str for code in ("401", "403", "404", "410", "expired", "forbidden"))
                if is_auth_error and attempt >= 1:
                    raise

                if "socks" in err_str and proxy:
                    try:
                        from curl_cffi import requests as cffi_requests
                        s = cffi_requests.Session(impersonate="chrome")
                        c_resp = await asyncio.to_thread(s.get, seg["url"], headers=headers, proxy=proxy, timeout=25)
                        if c_resp.status_code == 200:
                            data = c_resp.content
                            if data:
                                _safe_write(dest_path, data)
                                return len(data)
                    except Exception:
                        pass

                is_disk_full = "no space" in err_str or "errno 28" in err_str or (isinstance(dl_err, OSError) and getattr(dl_err, "errno", None) in (28, 122))
                if is_disk_full:
                    _emergency_cleanup_disk(dest_path.parent.parent, dest_path.parent.parent / "parts")

                if attempt >= max_retries - 1:
                    raise ProviderFailure("DOWNLOAD_FAILED", f"Failed to download segment {seg.get('index')} after {max_retries} attempts: {dl_err}") from dl_err

                # Backoff strategy: longer wait for rate limits/overloads (429, 503, 502, 504)
                if any(c in err_str for c in ("429", "503", "502", "504", "rate", "busy", "limit")):
                    backoff = min(30.0, 3.0 * (attempt + 1))
                elif is_disk_full:
                    backoff = min(10.0, 1.0 * (attempt + 1))
                else:
                    backoff = min(20.0, 1.5 * (attempt + 1))

                if (attempt >= 2 or is_auth_error or is_disk_full) and progress:
                    progress.log(f"[Download-Retry] Segment {seg.get('index')} thử lại lần {attempt + 2}/{max_retries} sau {backoff:.1f}s (Lỗi: {dl_err})")

                await asyncio.sleep(backoff)

        raise ProviderFailure("DOWNLOAD_FAILED", f"Failed to download segment {seg.get('index')}: {last_error}")


def merge_segments_to_part(seg_paths: list[Path], output_part: Path) -> int:
    """Losslessly concatenate MPEG-TS segments into a single part file."""
    output_part.parent.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    with output_part.open("wb") as outfile:
        for p in seg_paths:
            if p.is_file():
                sz = p.stat().st_size
                with p.open("rb") as infile:
                    shutil.copyfileobj(infile, outfile, length=1024 * 1024)
                total_bytes += sz
    return total_bytes


def _has_sufficient_disk(dir_path: Path, min_bytes: int = 1024 * 1024 * 1024) -> bool:
    """Check if free disk space in the specified directory exceeds min_bytes (default 1GB)."""
    try:
        free_bytes = shutil.disk_usage(dir_path).free
        return free_bytes > min_bytes
    except Exception:
        return True


def _is_local_part_valid(local_file: Path, expected_bytes: int | None = None) -> bool:
    """Check if local part file exists and is valid (non-empty and matching byte size if known)."""
    if not local_file.is_file():
        return False
    size = local_file.stat().st_size
    if size <= 0:
        return False
    if expected_bytes and expected_bytes > 0:
        return size == expected_bytes
    return True


async def _find_and_load_best_manifest(
    provider: Any,
    creds: dict[str, Any],
    items: list[dict[str, Any]],
    work_dir: Path,
    job: JobState,
) -> dict[str, Any] | None:
    """
    Search all candidate _resume_manifest*.json files in remote items.
    If multiple exist (e.g. on Google Drive), download all candidates, evaluate their progress
    (is_complete, last_synced_segment, completed_parts count), choose the latest/most complete,
    and clean up the stale manifest files.
    """
    manifest_items = [
        it for it in items
        if str(it.get("name") or "") == "_resume_manifest.json"
        or (str(it.get("name") or "").startswith("_resume_manifest") and str(it.get("name") or "").endswith(".json"))
    ]
    if not manifest_items:
        return None

    if len(manifest_items) == 1:
        m_item = manifest_items[0]
        cand_dl_dir = work_dir / "_m_cand_0"
        cand_dl_dir.mkdir(parents=True, exist_ok=True)
        try:
            await provider.download_file(creds, m_item, cand_dl_dir, job)
            dl_files = list(cand_dl_dir.glob("*.json"))
            if not dl_files:
                dl_files = [work_dir / str(m_item.get("name") or "_resume_manifest.json")]
            for actual_dl in dl_files:
                if actual_dl.is_file():
                    try:
                        data = json.loads(actual_dl.read_text(encoding="utf-8"))
                        if isinstance(data, dict):
                            return data
                    except Exception:
                        pass
        except Exception as err:
            job.log(f"[Backup-Resume] Lưu ý khi đọc manifest: {err}")
            return None
        finally:
            shutil.rmtree(cand_dl_dir, ignore_errors=True)

    # Multiple manifests found (common in Google Drive due to duplicate file names)
    job.log(f"[Backup-Resume] Phát hiện {len(manifest_items)} file manifest trên Cloud. Đang phân tích để chọn bản ghi mới nhất...")
    best_data: dict[str, Any] | None = None
    best_item: dict[str, Any] | None = None
    best_score: int = -1

    for idx, m_item in enumerate(manifest_items):
        cand_dl_dir = work_dir / f"_m_cand_{idx}"
        cand_dl_dir.mkdir(parents=True, exist_ok=True)
        try:
            await provider.download_file(creds, m_item, cand_dl_dir, job)
            dl_files = list(cand_dl_dir.glob("*.json"))
            if not dl_files:
                dl_files = [work_dir / str(m_item.get("name") or "_resume_manifest.json")]
            for actual_dl in dl_files:
                if actual_dl.is_file():
                    try:
                        m_data = json.loads(actual_dl.read_text(encoding="utf-8"))
                        if isinstance(m_data, dict):
                            parts_count = len(m_data.get("completed_parts") or [])
                            last_seg = int(m_data.get("last_synced_segment") or -1)
                            is_done = 1 if m_data.get("is_complete") else 0
                            score = (is_done * 1_000_000_000) + (max(last_seg, 0) * 1_000) + parts_count
                            if score > best_score:
                                best_score = score
                                best_data = m_data
                                best_item = m_item
                    except Exception:
                        pass
        except Exception as m_err:
            job.log(f"[Backup-Resume] Bỏ qua file manifest lỗi ({m_item.get('name')}): {m_err}")
        finally:
            shutil.rmtree(cand_dl_dir, ignore_errors=True)

    if best_data and best_item:
        p_len = len(best_data.get("completed_parts") or [])
        l_seg = best_data.get("last_synced_segment", -1)
        job.log(f"[Backup-Resume] Đã chọn Manifest chính xác nhất ({p_len} parts đã tải, last_seg={l_seg}). Tiến hành dọn dẹp các manifest cũ...")
        stale_items = [
            it for it in manifest_items
            if str(it.get("id") or it.get("path")) != str(best_item.get("id") or best_item.get("path"))
        ]
        for stale in stale_items:
            try:
                if hasattr(provider, "delete_item"):
                    await provider.delete_item(creds, stale)
                elif hasattr(provider, "delete_file"):
                    await provider.delete_file(creds, stale)
            except Exception:
                pass

    return best_data


async def run_resumable_backup_pipeline(
    job: JobState,
    dirs: dict[str, Path],
    source: dict[str, Any],
    target: dict[str, Any],
    options: dict[str, Any],
    src_provider: Any,
    dst_provider: Any,
    file_item: dict[str, Any],
) -> Path:
    """
    Execute Chunked Resumable Backup transfer for an HLS video stream:
    - Slices stream into parts (default 150 segments / 250MB or custom configured)
    - Pipelined parallel upload of each part to remote cloud destination
    - Maintains and updates remote _resume_manifest.json
    - Supports multi-session resumption by skipping already uploaded parts
    - Assembles final MP4 using FFmpeg concat stream copy
    """
    job.log("[Backup-Resume] Khởi động chiến lược truyền tải: Sao lưu phân đoạn để Resume")
    source_url = str(file_item.get("path") or file_item.get("id") or file_item.get("url") or "")
    raw_name = str(file_item.get("name") or unquote(Path(urlparse(source_url).path).name) or "stream_video.mp4")

    # If the source is a local sniffer JSON file (e.g. from Chrome Stream Sniffer extension), parse and merge its full metadata
    sniffer_cand = Path(source_url)
    if not sniffer_cand.is_file() and file_item.get("path"):
        sniffer_cand = Path(str(file_item.get("path")))
    if sniffer_cand.is_file() and sniffer_cand.suffix.lower() == ".json" and not sniffer_cand.name.startswith("_resume_manifest"):
        try:
            s_data = json.loads(sniffer_cand.read_text(encoding="utf-8"))
            if isinstance(s_data, dict):
                job.log(f"[Backup-Resume] Đã nạp dữ liệu sniffer từ file JSON: {sniffer_cand.name}")
                file_item = {**s_data, **file_item}
                source_url = str(s_data.get("url") or s_data.get("path") or source_url)
                if s_data.get("name"):
                    raw_name = str(s_data["name"])
                elif s_data.get("page_title"):
                    raw_name = f"{safe_name(str(s_data['page_title']))}.mp4"
        except Exception as j_err:
            job.log(f"[Backup-Resume] Lưu ý khi đọc file sniffer JSON: {j_err}")

    target_folder = str((target.get("folder") or {}).get("id") or (target.get("folder") or {}).get("path") or "/")
    if target_folder.startswith("id:"):
        target_folder = target_folder[3:].strip()
    target_creds = target.get("credentials") or {}

    chunk_segments = int(options.get("backup_chunk_segments") or DEFAULT_CHUNK_SEGMENTS)
    chunk_mb = int(options.get("backup_chunk_mb") or DEFAULT_CHUNK_MB)
    auto_clean = bool(options.get("backup_auto_clean", True))

    is_source_links = (
        str(source.get("provider") or "").lower() == "links"
        or getattr(src_provider, "__class__", None).__name__ == "LinksProvider"
    )
    is_backup_folder_input = (
        not is_source_links
        and (
            file_item.get("type") == "folder"
            or bool(file_item.get("is_folder"))
            or raw_name.endswith("__backup_resume")
            or source_url.endswith("__backup_resume")
        )
    )

    folder_manifest_data: dict[str, Any] | None = None
    backup_folder_id: str | None = None
    completed_parts: list[dict[str, Any]] = []
    last_synced_seg = -1
    existing_remote_part_names: set[str] = set()
    remote_part_map: dict[str, dict[str, Any]] = {}

    if is_backup_folder_input:
        src_folder_id = str(file_item.get("id") or file_item.get("path") or "")
        if src_folder_id.startswith("id:"):
            src_folder_id = src_folder_id[3:].strip()
        src_folder_name = PurePosixPath(file_item.get("name") or file_item.get("path") or "").name
        backup_folder_name = src_folder_name if src_folder_name.endswith("__backup_resume") else generate_backup_folder_name(src_folder_name)
        backup_folder_id = src_folder_id
        job_work_dir = dirs["input"] / f"backup_pipe_{safe_name(backup_folder_name)}"
        parts_local_dir = job_work_dir / "parts"
        segs_local_dir = job_work_dir / "segments"
        parts_local_dir.mkdir(parents=True, exist_ok=True)
        segs_local_dir.mkdir(parents=True, exist_ok=True)

        job.log(f"[Backup-Resume] Nhận diện thư mục nguồn là Backup Folder: {backup_folder_name} (Đang đọc manifest...)")
        source_creds = file_item.get("credentials") or source.get("credentials") or {}
        try:
            src_listing = await src_provider.list_files(source_creds, src_folder_id)
            items_found = src_listing.get("items") or []
            for it in items_found:
                nm = str(it.get("name") or "")
                if nm.startswith("part_") and nm.endswith(".ts"):
                    existing_remote_part_names.add(nm)
                    remote_part_map[nm] = it
            folder_manifest_data = await _find_and_load_best_manifest(src_provider, source_creds, items_found, job_work_dir, job)
            if not folder_manifest_data and target_folder and hasattr(src_provider, "list_files"):
                try:
                    parent_listing = await src_provider.list_files(source_creds, target_folder)
                    b_item = next((it for it in (parent_listing.get("items") or []) if it.get("name") == backup_folder_name and (it.get("type") == "folder" or it.get("is_folder"))), None)
                    if b_item:
                        backup_folder_id = b_item.get("id") or b_item.get("path") or backup_folder_id
                        src_listing = await src_provider.list_files(source_creds, backup_folder_id)
                        items_found = src_listing.get("items") or []
                        for it in items_found:
                            nm = str(it.get("name") or "")
                            if nm.startswith("part_") and nm.endswith(".ts"):
                                existing_remote_part_names.add(nm)
                                remote_part_map[nm] = it
                        folder_manifest_data = await _find_and_load_best_manifest(src_provider, source_creds, items_found, job_work_dir, job)
                except Exception:
                    pass
        except Exception as scan_err:
            job.log(f"[Backup-Resume] Lưu ý khi đọc manifest từ thư mục backup nguồn: {scan_err}")

        if folder_manifest_data:
            meta_info = folder_manifest_data.get("meta") or {}
            raw_item_saved = folder_manifest_data.get("raw_item") or {}
            file_item = {**raw_item_saved, **file_item, "meta": {**meta_info, **(file_item.get("meta") or {})}}
            for prop in ("page_url", "page_title", "decryption_key", "headers", "cookies"):
                if folder_manifest_data.get(prop) and not file_item.get(prop):
                    file_item[prop] = folder_manifest_data[prop]

            fresh_url = str(
                options.get("fresh_url")
                or options.get("stream_url")
                or file_item.get("fresh_url")
                or meta_info.get("fresh_url")
                or ""
            ).strip()
            source_url = fresh_url or folder_manifest_data.get("original_url") or meta_info.get("original_url") or source_url
            raw_target_name = folder_manifest_data.get("target_filename") or raw_name.replace("__backup_resume", "")
            final_video_name = generate_final_video_name(raw_target_name)
            chunk_segments = int(options.get("backup_chunk_segments") or folder_manifest_data.get("chunk_size_segments") or chunk_segments)
            chunk_mb = int(options.get("backup_chunk_mb") or folder_manifest_data.get("chunk_size_mb") or chunk_mb)
            job.log(f"[Backup-Resume] Đọc manifest thành công: Video đích '{final_video_name}', URL gốc: {source_url[:60]}... (Ngưỡng cắt: {chunk_segments} segs / {chunk_mb} MB)")
        else:
            final_video_name = generate_final_video_name(raw_name.replace("__backup_resume", ""))
    else:
        final_video_name = generate_final_video_name(raw_name)
        backup_folder_name = generate_backup_folder_name(final_video_name)
        job_work_dir = dirs["input"] / f"backup_pipe_{safe_name(backup_folder_name)}"
        parts_local_dir = job_work_dir / "parts"
        segs_local_dir = job_work_dir / "segments"
        parts_local_dir.mkdir(parents=True, exist_ok=True)
        segs_local_dir.mkdir(parents=True, exist_ok=True)

    if folder_manifest_data:
        completed_parts = folder_manifest_data.get("completed_parts") or []
        last_synced_seg = max((cp.get("end_seg", -1) for cp in completed_parts), default=-1)
        existing_remote_part_names.update(cp.get("file_name") for cp in completed_parts if cp.get("file_name"))
        total_segments_expected = int(folder_manifest_data.get("total_segments_expected") or 0)
        is_already_complete = (
            folder_manifest_data.get("is_complete") is True
            or (total_segments_expected > 0 and sum(cp.get("segment_count", 0) for cp in completed_parts) >= total_segments_expected)
        )
    else:
        is_already_complete = False

    if not is_already_complete:
        # 1. Resolve source URL and fetch HLS playlist
        # The raw item URL may be a webpage, an expired CDN token, or a direct m3u8.
        # We must resolve it through the links provider's pipeline (auto-resolve, token refresh)
        # before attempting to parse as HLS.
        meta_info = file_item.get("meta") or {}
        fresh_url = str(
            options.get("fresh_url")
            or options.get("stream_url")
            or file_item.get("fresh_url")
            or meta_info.get("fresh_url")
            or ""
        ).strip()
        if fresh_url:
            source_url = fresh_url
            job.log(f"[Backup-Resume] Sử dụng URL luồng mới được cập nhật: {fresh_url[:80]}...")

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            **(file_item.get("headers") or source.get("headers") or (folder_manifest_data or {}).get("headers") or meta_info.get("headers") or {}),
        }
        cookies_str = str(file_item.get("cookies") or source.get("cookies") or (folder_manifest_data or {}).get("cookies") or meta_info.get("cookies") or "")
        if cookies_str and "cookie" not in {k.lower() for k in headers}:
            headers["Cookie"] = cookies_str

        active_proxy: str | None = headers.pop("_active_proxy", None)

        def _ensure_socks_installed():
            try:
                import socksio
            except ImportError:
                import subprocess, sys
                try:
                    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "socksio", "httpx[socks]"], check=False)
                except Exception:
                    pass

        def _make_http_client(proxy: str | None = None) -> httpx.AsyncClient:
            client_kw: dict[str, Any] = {"follow_redirects": True, "timeout": 25.0}
            if proxy and "socks" not in proxy.lower():
                client_kw["proxy"] = proxy
            return httpx.AsyncClient(**client_kw)

        resolved_url = source_url
        page_url = str(
            options.get("page_url")
            or file_item.get("page_url")
            or meta_info.get("page_url")
            or (folder_manifest_data or {}).get("page_url")
            or ""
        ).strip()
        if not page_url:
            ref_hdr = str(headers.get("Referer") or headers.get("referer") or "").strip()
            if ref_hdr.startswith("http") and ref_hdr.rstrip("/").count("/") >= 3:
                page_url = ref_hdr
        if not page_url:
            orig_cand = str((folder_manifest_data or {}).get("original_url") or meta_info.get("original_url") or "").strip()
            if orig_cand.startswith("http") and not any(ext in orig_cand.lower() for ext in (".m3u8", "/hls/", ".mpd", ".ts")):
                page_url = orig_cand
        if not page_url and source_url.startswith("http") and not any(ext in source_url.lower() for ext in (".m3u8", "/hls/", ".mpd", ".ts")):
            page_url = source_url

        has_webpage = bool(page_url and page_url.startswith(("http://", "https://")) and not any(ext in page_url.lower() for ext in (".m3u8", "/hls/", ".mpd", ".ts")))

        # Resolver: instantiate LinksProvider if src_provider lacks _resolve_universal_page (e.g. when resuming from cloud folder)
        links_resolver = src_provider if hasattr(src_provider, "_resolve_universal_page") else None
        if not links_resolver:
            try:
                from ..providers.links import LinksProvider
                links_resolver = LinksProvider()
            except Exception:
                pass

        if links_resolver and hasattr(links_resolver, "_ensure_deps"):
            try:
                await links_resolver._ensure_deps()
            except Exception:
                pass

        async def _attempt_resolve_stream(target_u: str) -> bool:
            nonlocal resolved_url, active_proxy, cookies_str, page_url
            if not (links_resolver and hasattr(links_resolver, "_resolve_universal_page")):
                return False
            ref_payload = {**file_item, "page_url": page_url or target_u, "headers": headers, "cookies": cookies_str}
            try:
                fresh_res_url, _, fresh_hdrs = await links_resolver._resolve_universal_page(
                    target_u, ref_payload, job, proxy=active_proxy
                )
                if fresh_res_url:
                    resolved_url = fresh_res_url
                    if fresh_hdrs:
                        headers.update(fresh_hdrs)
                        if fresh_hdrs.get("_active_proxy"):
                            active_proxy = fresh_hdrs["_active_proxy"]
                        if "Cookie" in fresh_hdrs:
                            cookies_str = fresh_hdrs["Cookie"]
                            headers["Cookie"] = cookies_str
                    if not page_url and target_u != fresh_res_url and not any(ext in target_u.lower() for ext in (".m3u8", "/hls/", ".ts")):
                        page_url = target_u
                    job.log(f"[Backup-Resume] Đã tự động giải quyết URL luồng mới, cookie và IP mới thành công!")
                    return True
            except Exception as resolve_exc:
                job.log(f"[Backup-Resume] Lưu ý tự động giải quyết URL qua LinksProvider: {resolve_exc}")
            return False

        # Check token expiration timestamp
        is_direct_m3u8 = any(k in source_url.lower() for k in (".m3u8", "/hls/", ".mpd"))
        is_token_expired = False
        try:
            parsed_q = parse_qs(urlparse(source_url).query)
            exp_val = (
                parsed_q.get("expires", [None])[0]
                or parsed_q.get("e", [None])[0]
                or parsed_q.get("exp", [None])[0]
                or parsed_q.get("expire", [None])[0]
            )
            if exp_val and str(exp_val).isdigit() and int(exp_val) < int(time.time()):
                is_token_expired = True
                is_direct_m3u8 = False
                diff_m = max(1, (int(time.time()) - int(exp_val)) // 60)
                job.log(f"[Backup-Resume] Token luồng HLS đã hết hạn {diff_m} phút trước (expires={exp_val}).")
        except Exception:
            pass

        async def _fetch_playlist_with_fallback() -> tuple[str | None, list[dict[str, Any]], Exception | None]:
            nonlocal active_proxy, resolved_url
            last_err = None

            def _try_curl_cffi_playlist(u: str, px: str | None) -> tuple[str | None, list[dict[str, Any]], Exception | None]:
                try:
                    from curl_cffi import requests as cffi_requests
                    s = cffi_requests.Session(impersonate="chrome")
                    cur = u
                    for _ in range(4):
                        r = s.get(cur, headers=headers, proxy=px, timeout=25)
                        r.raise_for_status()
                        var_u, segs = parse_m3u8_playlist(r.text, cur)
                        if var_u:
                            cur = var_u
                            continue
                        if segs:
                            return cur, segs, None
                        break
                    return cur, [], None
                except Exception as cffi_err:
                    return None, [], cffi_err

            # 1. Try with current active_proxy / direct
            try:
                async with _make_http_client(active_proxy) as http_client:
                    media_u, segs = await resolve_media_segments(http_client, resolved_url, headers)
                    if segs:
                        return media_u, segs, None
            except Exception as err:
                last_err = err
                if active_proxy:
                    c_u, c_segs, _ = _try_curl_cffi_playlist(resolved_url, active_proxy)
                    if c_segs:
                        return c_u, c_segs, None

            # 2. If blocked by IP (403/429/503) and token not expired, try Tor proxy like LinksProvider
            if not is_token_expired and links_resolver and hasattr(links_resolver, "_ensure_tor_proxy"):
                is_ip_block = last_err and any(code in str(last_err).lower() for code in ("403", "429", "503", "challenge", "blocked"))
                if is_ip_block:
                    try:
                        job.log(f"[Backup-Resume] Thử truy cập luồng qua IP mới (Tor SOCKS5 proxy) như LinksProvider...")
                        fresh_px = await links_resolver._ensure_tor_proxy(job)
                        if fresh_px:
                            active_proxy = fresh_px
                            try:
                                async with _make_http_client(active_proxy) as http_client:
                                    media_u, segs = await resolve_media_segments(http_client, resolved_url, headers)
                                    if segs:
                                        job.log(f"[Backup-Resume] Lấy luồng HLS thành công qua IP mới!")
                                        return media_u, segs, None
                            except Exception as h_err:
                                last_err = h_err
                                c_u, c_segs, _ = _try_curl_cffi_playlist(resolved_url, active_proxy)
                                if c_segs:
                                    job.log(f"[Backup-Resume] Lấy luồng HLS thành công qua IP mới!")
                                    return c_u, c_segs, None
                    except Exception as px_err:
                        last_err = px_err

            # 3. If we have a canonical webpage URL, auto-resolve fresh stream token from page
            if has_webpage and links_resolver and hasattr(links_resolver, "_resolve_universal_page"):
                job.log(f"[Backup-Resume] Đang tự động làm mới link, cookie và IP từ trang web: {page_url[:80]}...")
                if await _attempt_resolve_stream(page_url):
                    try:
                        async with _make_http_client(active_proxy) as http_client:
                            media_u, segs = await resolve_media_segments(http_client, resolved_url, headers)
                            if segs:
                                return media_u, segs, None
                    except Exception as res_err:
                        last_err = res_err
                        if active_proxy:
                            c_u, c_segs, _ = _try_curl_cffi_playlist(resolved_url, active_proxy)
                            if c_segs:
                                return c_u, c_segs, None

            return None, [], last_err

        if has_webpage and (is_token_expired or not is_direct_m3u8) and links_resolver:
            job.log(f"[Backup-Resume] Đang tự động làm mới link, cookie và IP qua LinksProvider từ trang web: {page_url[:80]}...")
            await _attempt_resolve_stream(page_url)

        job.log(f"[Backup-Resume] Đang phân tích luồng HLS: {resolved_url[:80]}...")
        media_url, all_segments, fetch_err = await _fetch_playlist_with_fallback()

        if not all_segments:
            err_msg = str(fetch_err) if fetch_err else "Không tìm thấy segments trong playlist HLS"
            is_http_expiry = fetch_err and any(code in str(fetch_err) for code in ("404", "403", "410"))
            if is_http_expiry or "expired" in str(fetch_err).lower() or not is_direct_m3u8:
                completed_count = len(completed_parts)
                raise ProviderFailure(
                    "STREAM_TOKEN_EXPIRED",
                    f"Token luồng HLS đã hết hạn trên máy chủ phát (Lỗi 404/403: {err_msg}). "
                    f"Toàn bộ {completed_count} phân đoạn đã sao lưu vẫn an toàn trên Cloud! "
                    f"Để tải tiếp các segment còn lại: Vui lòng dán link m3u8 mới hoặc link trang web vào ô 'URL luồng mới' trong tùy chọn Resume.",
                    {"resolved_url": resolved_url, "completed_parts": completed_count}
                )
            raise ProviderFailure("INVALID_STREAM", f"Không tìm thấy segments trong playlist HLS: {resolved_url[:80]} ({err_msg})")

        total_segments = len(all_segments)
        job.log(f"[Backup-Resume] Playlist hợp lệ: Tổng cộng {total_segments} segments (Ngưỡng cắt: {chunk_segments} segments hoặc {chunk_mb} MB/part)")

        # 2. Check remote target backup folder for existing manifest or parts (Multi-session check)
        try:
            remote_files = await dst_provider.list_files(target_creds, target_folder)
            sub_items = remote_files.get("items") or []
            backup_item = next((it for it in sub_items if (it.get("name") == backup_folder_name and (it.get("type") == "folder" or it.get("is_folder")))), None)
            if backup_item:
                backup_folder_id = backup_item.get("id") or backup_item.get("path")
                parts_listing = await dst_provider.list_files(target_creds, backup_folder_id)
                sub_parts = parts_listing.get("items") or []
                for it in sub_parts:
                    name = str(it.get("name") or "")
                    if name.startswith("part_") and name.endswith(".ts"):
                        existing_remote_part_names.add(name)
                        remote_part_map[name] = it
                job.log(f"[Backup-Resume] Phát hiện thư mục backup từ xa: {len(existing_remote_part_names)} part(s) đã upload trước đó.")

                # Try to restore state from manifest first (most accurate for variable-size parts)
                manifest_loaded = bool(folder_manifest_data)
                if not manifest_loaded:
                    try:
                        manifest_data = await _find_and_load_best_manifest(dst_provider, target_creds, sub_parts, job_work_dir, job)
                        if manifest_data:
                            for cp in manifest_data.get("completed_parts") or []:
                                p_name = cp.get("file_name") or ""
                                if p_name in existing_remote_part_names:
                                    completed_parts.append(cp)
                                    last_synced_seg = max(last_synced_seg, cp.get("end_seg", -1))
                            manifest_loaded = True
                            job.log(f"[Resume-Manifest] Đọc manifest thành công: {len(completed_parts)} part xác nhận.")
                    except Exception as exc:
                        job.log(f"[Resume-Manifest] Không thể đọc manifest, dùng file listing thay thế: {exc}")

                # Fallback: infer from file listing if manifest unavailable or empty
                if not manifest_loaded and not completed_parts:
                    num_parts = (total_segments + chunk_segments - 1) // chunk_segments
                    for p_idx in range(1, num_parts + 1):
                        p_name = f"part_{p_idx:04d}.ts"
                        start_seg = (p_idx - 1) * chunk_segments
                        end_seg = min(p_idx * chunk_segments - 1, total_segments - 1)
                        if p_name in existing_remote_part_names:
                            completed_parts.append({
                                "part_index": p_idx,
                                "file_name": p_name,
                                "start_seg": start_seg,
                                "end_seg": end_seg,
                                "segment_count": end_seg - start_seg + 1,
                            })
                            last_synced_seg = max(last_synced_seg, end_seg)
        except Exception as exc:
            job.log(f"[Backup-Resume] Lưu ý: Kiểm tra remote backup: {exc}")
    else:
        total_segments = int(folder_manifest_data.get("total_segments_expected") or sum(cp.get("segment_count", 0) for cp in completed_parts))
        job.log(f"[Backup-Resume] Toàn bộ {total_segments} segments đã có đủ trên Cloud đích! Tiến hành ghép hoàn chỉnh: {final_video_name}...")

    # Evaluate disk space & merge feasibility (Space-Saver for Kaggle / low-disk)
    env_is_kaggle = IS_KAGGLE or bool(options.get("is_kaggle"))
    skip_final_assembly, skip_reason, estimated_total_bytes, initial_free_disk = should_skip_merge_due_to_space(
        job_work_dir,
        total_segments,
        completed_parts,
        accumulated_bytes=0,
        downloaded_segs=0,
        chunk_segments=chunk_segments,
        chunk_mb=chunk_mb,
        is_kaggle=env_is_kaggle,
        options=options,
    )
    keep_local_parts = (not skip_final_assembly) and (initial_free_disk > (estimated_total_bytes * 2.2) + (1024 * 1024 * 1024))

    if skip_final_assembly:
        job.log(
            f"[Backup-Resume] [Space-Saver] Phát hiện dung lượng đĩa hạn chế ({skip_reason}). "
            f"Kích hoạt chế độ tải an toàn: Từng part sẽ upload ngay lên Cloud và xóa bản local. "
            f"Bỏ qua bước ghép video (Final Assembly) trên runtime để chống tràn bộ nhớ (Errno 28)."
        )

    if completed_parts:
        remote_list_summary = ", ".join(sorted(existing_remote_part_names)) if len(existing_remote_part_names) <= 5 else f"{len(existing_remote_part_names)} parts"
        job.log(f"[Trace] Đã phát hiện {len(existing_remote_part_names)} part(s) trên Cloud đích ({remote_list_summary}). Bỏ qua {len(completed_parts)} part đã hoàn thành.")
        job.log(f"[Resume-Hit] Khôi phục phiên! Bỏ qua {len(completed_parts)} part đã có. Tiếp tục từ segment {last_synced_seg + 1}/{total_segments}")
    else:
        job.log("[Backup-Resume] Bắt đầu phiên mới từ segment 0.")

    # 3. Pipeline Slicing & Concurrent Upload
    seg_sem = asyncio.Semaphore(DEFAULT_SEGMENT_CONCURRENCY)
    active_upload_tasks: list[asyncio.Task] = []
    manifest_remote_path: str | None = None  # Track remote manifest path for deletion

    part_idx = 1
    cur_batch_segments: list[dict[str, Any]] = []
    cur_batch_paths: list[Path] = []
    accumulated_bytes = 0
    total_downloaded_segs = 0

    async def _resolve_backup_folder_id() -> str | None:
        nonlocal backup_folder_id
        if backup_folder_id:
            return backup_folder_id
        try:
            remote_files = await dst_provider.list_files(target_creds, target_folder)
            sub_items = remote_files.get("items") or []
            b_item = next((it for it in sub_items if (it.get("name") == backup_folder_name and (it.get("type") == "folder" or it.get("is_folder")))), None)
            if b_item:
                backup_folder_id = b_item.get("id") or b_item.get("path")
                return backup_folder_id
        except Exception:
            pass
        return None

    async def _clean_duplicate_manifests(items_to_check: list[dict[str, Any]] | None = None, keep_id: str | None = None) -> None:
        """Delete any duplicate _resume_manifest*.json files except the active manifest."""
        bf_id = await _resolve_backup_folder_id()
        if not bf_id:
            return
        try:
            cand_items = items_to_check
            if cand_items is None:
                listing = await dst_provider.list_files(target_creds, bf_id)
                cand_items = listing.get("items") or []
            for it in cand_items:
                fname = str(it.get("name") or "")
                fid = it.get("id") or it.get("path")
                if fid and str(fid) == str(keep_id):
                    continue
                is_dup = (fname.startswith("_resume_manifest") and fname.endswith(".json") and fname != "_resume_manifest.json") or (keep_id and str(fid) != str(keep_id) and fname == "_resume_manifest.json") or (not keep_id and fname.startswith("_resume_manifest") and fname.endswith(".json"))
                if is_dup:
                    try:
                        if hasattr(dst_provider, "delete_item"):
                            await dst_provider.delete_item(target_creds, it)
                        elif hasattr(dst_provider, "delete_file"):
                            await dst_provider.delete_file(target_creds, it)
                    except Exception:
                        pass
        except Exception:
            pass

    async def _delete_old_manifest() -> None:
        """Delete the existing _resume_manifest*.json files from remote to prevent duplicates."""
        await _clean_duplicate_manifests()

    async def _upload_manifest() -> None:
        """Build and upload _resume_manifest.json to remote backup folder (with replace)."""
        is_done = (sum(cp.get("segment_count", 0) for cp in completed_parts) >= total_segments) if total_segments else False
        manifest = {
            "version": 1,
            "transfer_strategy": "backup_resume",
            "original_url": resolved_url or source_url,
            "page_url": page_url or file_item.get("page_url") or (file_item.get("meta") or {}).get("page_url") or "",
            "page_title": file_item.get("page_title") or (file_item.get("meta") or {}).get("page_title") or "",
            "headers": {k: v for k, v in headers.items() if k.lower() in ("user-agent", "referer", "cookie", "origin")},
            "cookies": cookies_str,
            "decryption_key": file_item.get("decryption_key") or (file_item.get("meta") or {}).get("decryption_key"),
            "source_provider": source.get("provider"),
            "target_filename": final_video_name,
            "chunk_size_segments": chunk_segments,
            "chunk_size_mb": chunk_mb,
            "total_segments_expected": total_segments,
            "completed_parts": sorted(completed_parts, key=lambda x: x["part_index"]),
            "last_synced_segment": max((cp["end_seg"] for cp in completed_parts), default=-1),
            "is_complete": is_done,
            "all_parts_uploaded": is_done,
            "final_assembly_skipped": bool(skip_final_assembly),
            "meta": {
                **(file_item.get("meta") or {}),
                "page_url": page_url or file_item.get("page_url") or "",
                "page_title": file_item.get("page_title") or (file_item.get("meta") or {}).get("page_title") or "",
                "original_url": resolved_url or source_url,
                "headers": {k: v for k, v in headers.items() if k.lower() in ("user-agent", "referer", "cookie", "origin")},
                "cookies": cookies_str,
            },
            "raw_item": {k: v for k, v in file_item.items() if k not in ("credentials",)},
        }
        manifest_path = job_work_dir / "_resume_manifest.json"
        job_work_dir.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

        bf_id = await _resolve_backup_folder_id()
        existing_manifests = []
        if bf_id:
            try:
                listing = await dst_provider.list_files(target_creds, bf_id)
                for it in listing.get("items") or []:
                    fname = str(it.get("name") or "")
                    if fname == "_resume_manifest.json" or (fname.startswith("_resume_manifest") and fname.endswith(".json")):
                        existing_manifests.append(it)
            except Exception:
                pass

        exact_manifest = next((it for it in existing_manifests if str(it.get("name") or "") == "_resume_manifest.json"), None)
        target_manifest = exact_manifest or (existing_manifests[0] if existing_manifests else None)

        # Force replace mode in options for providers that check options["replace"] (e.g. TeraBox rtype=3)
        orig_replace = (job.payload.get("options") or {}).get("replace")
        if "options" not in job.payload:
            job.payload["options"] = {}
        job.payload["options"]["replace"] = True

        replaced = False
        try:
            if target_manifest and hasattr(dst_provider, "replace_file"):
                try:
                    await dst_provider.replace_file(target_creds, manifest_path, target_manifest, job)
                    replaced = True
                except Exception as rep_err:
                    job.log(f"[Manifest] replace_file: {rep_err}, falling back to upload_file with replace=True")

            if not replaced:
                manifest_ref = {
                    "id": target_folder,
                    "path": target_folder,
                    "relative_path": f"{backup_folder_name}/_resume_manifest.json",
                }
                upload_res = await dst_provider.upload_file(target_creds, manifest_path, manifest_ref, job)
                uploaded_fid = (upload_res.get("id") or upload_res.get("path")) if isinstance(upload_res, dict) else None
                await _clean_duplicate_manifests(existing_manifests, keep_id=uploaded_fid)
        except Exception as exc:
            job.log(f"[Manifest] Cảnh báo: Không thể upload manifest: {exc}")
        finally:
            if orig_replace is None:
                job.payload["options"].pop("replace", None)
            else:
                job.payload["options"]["replace"] = orig_replace
            manifest_path.unlink(missing_ok=True)

        try:
            bf_id = await _resolve_backup_folder_id()
            if bf_id:
                fresh_listing = await dst_provider.list_files(target_creds, bf_id)
                fresh_items = fresh_listing.get("items") or []
                exact_m = next((it for it in fresh_items if str(it.get("name") or "") == "_resume_manifest.json"), None)
                active_id = (exact_m.get("id") or exact_m.get("path")) if exact_m else None
                await _clean_duplicate_manifests(fresh_items, keep_id=active_id)
        except Exception:
            pass

    async def _upload_part_worker(part_file: Path, p_meta: dict[str, Any]) -> None:
        """Upload a single part to remote destination, update manifest, and track local part.

        Includes retry-with-session-refresh logic: when the upload fails due to
        an expired/revoked Drive web session (401/403), the provider's session is
        refreshed and the upload retried up to 3 times before giving up.
        """
        p_name = part_file.name
        size_mb = p_meta['bytes'] / (1024 * 1024)

        if p_name in existing_remote_part_names and any(cp.get("file_name") == p_name for cp in completed_parts):
            if not keep_local_parts or skip_final_assembly:
                part_file.unlink(missing_ok=True)
                job.log(f"[Trace] Phân đoạn {p_name} đã tồn tại trên Cloud đích. Đã xóa bản local để giải phóng đĩa.")
            else:
                job.log(f"[Trace] Phân đoạn {p_name} đã tồn tại trên Cloud đích. Bỏ qua upload, giữ bản local để ghép nhanh.")
            return

        job.log(f"[Trace] Phân đoạn mới: {p_name} ({p_meta['segment_count']} segments, {size_mb:.1f} MB) -> Đang upload lên Cloud đích...")
        target_ref = {
            "id": target_folder,
            "path": target_folder,
            "relative_path": f"{backup_folder_name}/{p_name}",
        }

        max_upload_retries = 3
        last_upload_err: Exception | None = None
        for upload_attempt in range(max_upload_retries):
            try:
                res = await dst_provider.upload_file(target_creds, part_file, target_ref, job)
                break  # success
            except ProviderFailure as up_err:
                last_upload_err = up_err
                is_auth = up_err.code == "INVALID_PROVIDER_CREDENTIALS" or "session expired" in str(up_err).lower() or "revoked" in str(up_err).lower()
                is_retryable = is_auth or up_err.code == "UPLOAD_FAILED" and int((up_err.details or {}).get("status") or 0) in (401, 403, 429, 500, 502, 503)

                if not is_retryable or upload_attempt >= max_upload_retries - 1:
                    raise

                job.log(
                    f"[Upload-Retry] Upload {p_name} thất bại (lần {upload_attempt + 1}/{max_upload_retries}): {up_err}. "
                    f"Đang refresh web session và thử lại..."
                )

                # Attempt to refresh the Drive web session on the dst_provider
                if hasattr(dst_provider, "_refresh_web_session"):
                    try:
                        await dst_provider._refresh_web_session(target_creds)
                        job.log(f"[Upload-Retry] Đã refresh Drive web session thành công.")
                    except Exception as ref_err:
                        job.log(f"[Upload-Retry] Refresh web session thất bại: {ref_err}")
                elif hasattr(dst_provider, "_web_request"):
                    # Force a lightweight probe to trigger internal retry/refresh inside _web_request
                    try:
                        await dst_provider.validate_credentials(target_creds)
                        job.log(f"[Upload-Retry] Đã validate lại credentials thành công.")
                    except Exception:
                        pass

                await asyncio.sleep(min(2 ** upload_attempt, 8))
            except (httpx.HTTPError, OSError) as net_err:
                last_upload_err = net_err
                if upload_attempt >= max_upload_retries - 1:
                    raise ProviderFailure("UPLOAD_FAILED", f"Network error uploading {p_name}: {net_err}")
                job.log(f"[Upload-Retry] Lỗi mạng upload {p_name} (lần {upload_attempt + 1}/{max_upload_retries}): {net_err}. Thử lại...")
                await asyncio.sleep(min(2 ** upload_attempt, 8))
        else:
            raise ProviderFailure("UPLOAD_FAILED", f"Upload {p_name} thất bại sau {max_upload_retries} lần thử: {last_upload_err}")

        job.log(f"[Trace] Upload thành công: {p_name}")
        completed_parts.append(p_meta)
        existing_remote_part_names.add(p_name)
        if isinstance(res, dict):
            remote_part_map[p_name] = res

        # Tối ưu giữ lại các file part cục bộ chỉ khi đủ không gian đĩa an toàn cho toàn bộ video + merge
        # Nếu skip_final_assembly (do space > 50% free disk hoặc Kaggle), hoặc free disk < 1GB:
        # Xóa ngay bản local sau khi upload
        should_keep = (
            keep_local_parts
            and not skip_final_assembly
            and _has_sufficient_disk(parts_local_dir, min_bytes=max(1024 * 1024 * 1024, int(estimated_total_bytes * 2)))
        )
        if should_keep:
            job.log(f"[Upload-Pipe] Giữ bản local {p_name} ({size_mb:.1f} MB) để tối ưu ghép nối nhanh ở final stage.")
        else:
            job.log(f"[Disk-Safety] Đã upload {p_name} ({size_mb:.1f} MB) lên Cloud và xóa bản local để giải phóng không gian đĩa.")
            part_file.unlink(missing_ok=True)

        # Always sync manifest after each part — critical for resume safety
        await _upload_manifest()

    if not is_already_complete:
        # Filter segments that need downloading
        remaining_segments = [s for s in all_segments if s["index"] > last_synced_seg]
        total_remaining = len(remaining_segments)

        if total_remaining > 0:
            job.log(f"[Backup-Resume] Cần tải {total_remaining} segments còn lại ({total_remaining * 100 // total_segments}% video)")

        async def _call_download_seg(c, s, dp, h, sm, jb, px):
            import inspect
            sig = inspect.signature(download_single_segment)
            if "proxy" in sig.parameters:
                return await download_single_segment(c, s, dp, h, sm, jb, proxy=px)
            return await download_single_segment(c, s, dp, h, sm, jb)

        dl_client = _make_http_client(active_proxy)
        try:
            for seg in remaining_segments:
                job.check_cancelled()
                seg_file = segs_local_dir / f"seg_{seg['index']:06d}.ts"
                seg_sz = None
                last_seg_err = None

                for recovery_round in range(3):
                    try:
                        seg_sz = await _call_download_seg(dl_client, seg, seg_file, headers, seg_sem, job, active_proxy)
                        break
                    except Exception as dl_err:
                        last_seg_err = dl_err
                        job.check_cancelled()
                        err_str = str(dl_err).lower()
                        is_expired = any(c in err_str for c in ("401", "403", "404", "410", "expired", "token", "forbidden"))
                        is_disk_full = "no space" in err_str or "errno 28" in err_str or (isinstance(dl_err, OSError) and getattr(dl_err, "errno", None) in (28, 122))

                        if is_disk_full:
                            skip_final_assembly = True
                            keep_local_parts = False
                            freed_b = _emergency_cleanup_disk(job_work_dir, parts_local_dir, existing_remote_part_names)
                            job.log(f"[Backup-Resume] [Space-Saver] Phát hiện sự cố đĩa đầy (Errno 28). Đã dọn dẹp khẩn cấp ({freed_b // (1024*1024)} MB giải phóng). Chuyển sang chế độ không giữ part.")

                        job.log(f"[Backup-Resume] Segment {seg['index']} gặp sự cố ({dl_err}). Đang khôi phục kết nối (Vòng {recovery_round + 1}/3)...")

                        # Rebuild http client to clear stale socket/connection pools
                        try:
                            await dl_client.aclose()
                        except Exception:
                            pass
                        dl_client = _make_http_client(active_proxy)

                        renewed = False
                        if links_resolver and hasattr(links_resolver, "_resolve_universal_page"):
                            target_to_resolve = page_url or source_url
                            if target_to_resolve:
                                job.log(f"[Backup-Resume] Segment {seg['index']}: Tự động làm mới link, cookie và IP qua LinksProvider...")
                                renewed = await _attempt_resolve_stream(target_to_resolve)
                        if renewed:
                            try:
                                _, fresh_segs, _ = await _fetch_playlist_with_fallback()
                                if fresh_segs:
                                    fresh_map = {s["index"]: s["url"] for s in fresh_segs}
                                    for rem in remaining_segments:
                                        if rem["index"] in fresh_map:
                                            rem["url"] = fresh_map[rem["index"]]
                                    seg["url"] = fresh_map.get(seg["index"], seg["url"])
                                    job.log(f"[Backup-Resume] Đã cập nhật token mới cho {len(remaining_segments)} segment còn lại. Tiếp tục tải...")
                            except Exception as ref_exc:
                                job.log(f"[Backup-Resume] Cảnh báo cập nhật playlist sau renew: {ref_exc}")

                        await asyncio.sleep(2.0 * (recovery_round + 1))

                if seg_sz is None:
                    raise ProviderFailure("DOWNLOAD_FAILED", f"Failed to download segment {seg['index']} after all recovery rounds: {last_seg_err}") from last_seg_err
                accumulated_bytes += seg_sz
                cur_batch_segments.append(seg)
                cur_batch_paths.append(seg_file)
                total_downloaded_segs += 1

                # Progress logging every 50 segments
                if total_downloaded_segs % 50 == 0 or seg["index"] == total_segments - 1:
                    pct = (last_synced_seg + 1 + total_downloaded_segs) * 100 // total_segments
                    job.log(f"[Download] {last_synced_seg + 1 + total_downloaded_segs}/{total_segments} segments ({pct}%) | {accumulated_bytes // (1024*1024)} MB tích lũy")

                # Check threshold trigger
                hit_seg_threshold = len(cur_batch_segments) >= chunk_segments
                hit_mb_threshold = accumulated_bytes >= (chunk_mb * 1024 * 1024)
                is_last_seg = (seg["index"] == total_segments - 1)

                if hit_seg_threshold or hit_mb_threshold or is_last_seg:
                    # Determine part index — skip indices already used by completed parts
                    while any(cp["part_index"] == part_idx for cp in completed_parts):
                        part_idx += 1

                    part_name = f"part_{part_idx:04d}.ts"
                    part_path = parts_local_dir / part_name
                    part_bytes = merge_segments_to_part(cur_batch_paths, part_path)

                    part_meta = {
                        "part_index": part_idx,
                        "file_name": part_name,
                        "start_seg": cur_batch_segments[0]["index"],
                        "end_seg": cur_batch_segments[-1]["index"],
                        "segment_count": len(cur_batch_segments),
                        "bytes": part_bytes,
                    }

                    # Clean raw segments immediately to free Colab ephemeral disk
                    for p in cur_batch_paths:
                        p.unlink(missing_ok=True)

                    cur_batch_segments.clear()
                    cur_batch_paths.clear()
                    accumulated_bytes = 0

                    # Dynamic space check after each part creation
                    dyn_skip, dyn_reason, _, _ = should_skip_merge_due_to_space(
                        job_work_dir,
                        total_segments,
                        completed_parts,
                        accumulated_bytes=0,
                        downloaded_segs=total_downloaded_segs,
                        chunk_segments=chunk_segments,
                        chunk_mb=chunk_mb,
                        is_kaggle=env_is_kaggle,
                        options=options,
                    )
                    if dyn_skip and not skip_final_assembly:
                        skip_final_assembly = True
                        keep_local_parts = False
                        _emergency_cleanup_disk(job_work_dir, parts_local_dir, existing_remote_part_names)
                        job.log(f"[Backup-Resume] [Space-Saver] Cập nhật dung lượng: {dyn_reason}. Chuyển sang chế độ không giữ part và bỏ qua Final Assembly.")

                    # Launch upload as background task — download continues immediately
                    task = asyncio.create_task(_upload_part_worker(part_path, part_meta))
                    active_upload_tasks.append(task)
                    part_idx += 1

                    # Surface upload errors from completed tasks without blocking
                    done_tasks = [t for t in active_upload_tasks if t.done()]
                    for t in done_tasks:
                        exc = t.exception()
                        if exc:
                            raise exc
                    active_upload_tasks = [t for t in active_upload_tasks if not t.done()]

            # Wait for remaining background uploads after all segments downloaded
            if active_upload_tasks:
                job.log(f"[Upload-Pipe] Đợi {len(active_upload_tasks)} upload task(s) còn lại hoàn tất...")
                results = await asyncio.gather(*active_upload_tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception):
                        raise r
        finally:
            await dl_client.aclose()


    # 4. Check if all segments are covered before Final Assembly
    total_synced = sum(cp.get("segment_count", 0) for cp in completed_parts)
    if total_synced < total_segments:
        job.log(
            f"[Backup-Resume] Phiên này đã sao lưu {total_synced}/{total_segments} segments "
            f"({len(completed_parts)} parts). Dữ liệu an toàn trên Cloud — hãy resume phiên tiếp để hoàn tất."
        )
        shutil.rmtree(job_work_dir, ignore_errors=True)
        # Return None for partial session; caller checks is not None
        return None

    # If skip_final_assembly is active (due to space > 50% free disk, Kaggle limit, or option):
    if skip_final_assembly:
        total_synced_mb = sum(cp.get("bytes", 0) for cp in completed_parts) / (1024 * 1024)
        job.log(
            f"[Backup-Resume] [Space-Saver] 100% segments ({total_segments} segments, {len(completed_parts)} parts, ~{total_synced_mb:.1f} MB) "
            f"đã được sao lưu an toàn và hoàn chỉnh lên Cloud tại thư mục '{backup_folder_name}'! "
            f"Bỏ qua bước ghép video (Final Assembly) trên runtime để tiết kiệm không gian đĩa và tránh lỗi tràn bộ nhớ (Errno 28)."
        )
        await _upload_manifest()
        shutil.rmtree(job_work_dir, ignore_errors=True)
        return job_work_dir / backup_folder_name

    # 5. Final Assembly: Concatenate all parts into final MP4
    job.log(f"[Final-Assembly] 100% segments ({total_segments} segments) đã sao lưu lên Cloud! Bắt đầu ghép file hoàn chỉnh: {final_video_name}...")
    final_output_path = job_work_dir / final_video_name

    # Download missing local parts for final concat
    completed_parts.sort(key=lambda x: x["part_index"])
    concat_list_file = job_work_dir / "concat_parts.txt"
    concat_lines = []

    # Check which parts are already present locally vs need download from Cloud
    local_ready_parts = []
    missing_parts = []
    for cp in completed_parts:
        p_name = cp["file_name"]
        local_p = parts_local_dir / p_name
        exp_bytes = cp.get("bytes")
        if _is_local_part_valid(local_p, exp_bytes):
            local_ready_parts.append(p_name)
        else:
            missing_parts.append(cp)

    parts_provider = src_provider if (is_backup_folder_input and not is_source_links) else dst_provider
    parts_creds = (source.get("credentials") or {}) if (is_backup_folder_input and not is_source_links) else target_creds
    provider_label = "Cloud nguồn" if (is_backup_folder_input and not is_source_links) else "Cloud đích"

    local_summary = ", ".join(local_ready_parts) if len(local_ready_parts) <= 5 else f"{len(local_ready_parts)} parts"
    job.log(
        f"[Final-Assembly] Phân tích phân đoạn: {len(local_ready_parts)}/{len(completed_parts)} part(s) đã có sẵn tại local "
        f"({local_summary}). Chỉ cần tải {len(missing_parts)} part(s) còn thiếu từ {provider_label}..."
    )

    # Re-check if downloading missing parts would exceed disk space
    total_missing_bytes = sum(cp.get("bytes", 0) for cp in missing_parts)
    current_free_disk = _get_disk_free(job_work_dir)
    if total_missing_bytes > 0 and (total_missing_bytes * 2 > current_free_disk or total_missing_bytes > current_free_disk * 0.5):
        total_synced_mb = sum(cp.get("bytes", 0) for cp in completed_parts) / (1024 * 1024)
        job.log(
            f"[Final-Assembly] [Space-Saver] Dung lượng các part cần tải lại (~{total_missing_bytes // (1024*1024)} MB) "
            f"vượt quá không gian đĩa an toàn ({current_free_disk // (1024*1024)} MB). "
            f"Bỏ qua bước ghép video để bảo vệ hệ thống. Toàn bộ {len(completed_parts)} parts (~{total_synced_mb:.1f} MB) đã có đủ trên Cloud tại '{backup_folder_name}'."
        )
        shutil.rmtree(job_work_dir, ignore_errors=True)
        return job_work_dir / backup_folder_name

    # If there are missing parts, ensure we have fresh remote items to download from
    if missing_parts:
        bf_id = backup_folder_id if is_backup_folder_input else await _resolve_backup_folder_id()
        if bf_id:
            try:
                fresh_listing = await parts_provider.list_files(parts_creds, bf_id)
                for it in fresh_listing.get("items") or []:
                    fn = str(it.get("name") or "")
                    if fn.startswith("part_") and fn.endswith(".ts"):
                        remote_part_map[fn] = it
            except Exception:
                pass

    for cp in completed_parts:
        p_name = cp["file_name"]
        local_p = parts_local_dir / p_name
        exp_bytes = cp.get("bytes")
        if not _is_local_part_valid(local_p, exp_bytes):
            size_mb = (exp_bytes or 0) / (1024 * 1024)
            job.log(f"[Final-Assembly] Tải phân đoạn còn thiếu từ {provider_label}: {p_name} ({size_mb:.1f} MB)...")
            part_remote_item = remote_part_map.get(p_name) or {
                "name": p_name,
                "path": f"{backup_folder_name}/{p_name}",
                "relative_path": f"{backup_folder_name}/{p_name}",
            }
            part_downloaded = False
            for part_dl_attempt in range(5):
                try:
                    await parts_provider.download_file(parts_creds, part_remote_item, parts_local_dir, job)
                    if not local_p.is_file():
                        candidates = list(parts_local_dir.glob(f"*{p_name}*"))
                        if candidates:
                            candidates[0].rename(local_p)
                    if local_p.is_file() and local_p.stat().st_size > 0:
                        part_downloaded = True
                        break
                except Exception as p_err:
                    job.log(f"[Final-Assembly] Tải lại part {p_name} lần {part_dl_attempt + 1}/5 thất bại: {p_err}")
                    await asyncio.sleep(2.0 * (part_dl_attempt + 1))

            if not part_downloaded or not local_p.is_file() or local_p.stat().st_size <= 0:
                raise ProviderFailure("DOWNLOAD_FAILED", f"Không thể tải lại part {p_name} từ {provider_label}")
            job.log(f"[Final-Assembly] Tải thành công part: {p_name}")
        else:
            size_mb = local_p.stat().st_size / (1024 * 1024)
            job.log(f"[Final-Assembly] Tận dụng part local có sẵn: {p_name} ({size_mb:.1f} MB) - Bỏ qua tải lại từ Cloud")
        concat_lines.append(f"file '{local_p.resolve().as_posix()}'\n")

    concat_list_file.write_text("".join(concat_lines), encoding="utf-8")

    # Run FFmpeg Concat Demuxer
    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [
        ffmpeg_bin,
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list_file),
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        str(final_output_path),
    ]

    job.log(f"[Final-Assembly] Thực thi FFmpeg lossless stream copy...")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0 or not final_output_path.is_file():
        err_msg = stderr.decode("utf-8", errors="ignore")[-400:]
        raise ProviderFailure("FFMPEG_MERGE_FAILED", f"Lỗi ghép video qua FFmpeg: {err_msg}")

    final_size = final_output_path.stat().st_size
    job.log(f"[Final-Assembly] Ghép thành công video hoàn chỉnh ({final_size} bytes). Đang upload lên Cloud đích...")

    # Upload final MP4 to main target folder
    target_final_ref = {
        "id": target_folder,
        "path": target_folder,
        "relative_path": final_video_name,
    }
    await dst_provider.upload_file(target_creds, final_output_path, target_final_ref, job)
    job.log(f"[Done] Đã lưu thành phẩm {final_video_name} vào thư mục đích trên Cloud!")

    # 6. Cleanup backup folder if auto_clean is enabled
    if auto_clean:
        try:
            clean_provider = src_provider if (is_backup_folder_input and not is_source_links) else dst_provider
            clean_creds = (source.get("credentials") or {}) if (is_backup_folder_input and not is_source_links) else target_creds
            job.log(f"[Cleanup] Tự động dọn dẹp thư mục phân mảnh tạm trên {'Cloud nguồn' if (is_backup_folder_input and not is_source_links) else 'Cloud đích'}: {backup_folder_name}...")
            target_base = str(target_folder or "/").rstrip("/")
            del_path = (str(file_item.get("path") or "") if (is_backup_folder_input and not is_source_links) else "") or f"{target_base}/{backup_folder_name}"
            del_ref = {
                "id": backup_folder_id,
                "path": del_path,
                "name": backup_folder_name,
                "type": "folder",
            }
            if hasattr(clean_provider, "delete_item"):
                await clean_provider.delete_item(clean_creds, del_ref)
            elif hasattr(clean_provider, "delete_file"):
                await clean_provider.delete_file(clean_creds, del_ref)
        except Exception as e:
            job.log(f"[Cleanup] Ghi chú dọn dẹp: {e}")

    # Remove local temp workspace
    shutil.rmtree(job_work_dir, ignore_errors=True)
    return final_output_path


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
        os.environ.get("VAULTBOX_ENV", "").strip().lower() == "kaggle"
        or (
            (
                os.environ.get("KAGGLE_KERNEL_RUN_TYPE")
                or os.environ.get("KAGGLE_CONTAINER_NAME")
                or os.environ.get("KAGGLE_URL_BASE")
                or (Path("/kaggle/working").is_dir() and not Path("/content").is_dir())
            )
            and not (
                "google.colab" in sys.modules
                or os.environ.get("COLAB_RELEASE_TAG")
                or Path("/content").is_dir()
            )
        )
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


def _part_seg_range(cp: dict[str, Any]) -> tuple[int, int] | None:
    """Extract (start_seg, end_seg) from a completed_parts entry if valid."""
    s_raw = cp.get("start_seg")
    e_raw = cp.get("end_seg")
    if s_raw is None or e_raw is None:
        return None
    try:
        s_idx = int(s_raw)
        e_idx = int(e_raw)
    except (TypeError, ValueError):
        return None
    if s_idx < 0 or e_idx < s_idx:
        return None
    return (s_idx, e_idx)


def _part_seg_count(cp: dict[str, Any]) -> int:
    """Return segment count for a part entry, preferring exact [start_seg..end_seg] span."""
    rng = _part_seg_range(cp)
    if rng is not None:
        return rng[1] - rng[0] + 1
    try:
        return max(0, int(cp.get("segment_count") or 0))
    except (TypeError, ValueError):
        return 0


def _covered_segments_from_parts(completed_parts: list[dict[str, Any]]) -> set[int]:
    """Return exact set of segment indices covered by completed_parts."""
    covered: set[int] = set()
    for cp in completed_parts:
        rng = _part_seg_range(cp)
        if rng is not None:
            covered.update(range(rng[0], rng[1] + 1))
    return covered


def _count_synced_segments(
    completed_parts: list[dict[str, Any]],
    skipped_indices: list[int] | set[int] = (),
    total_segments: int = 0,
) -> int:
    """Count unique covered + skipped segments accurately across non-contiguous parts."""
    skip_set: set[int] = set()
    for sk in skipped_indices:
        try:
            sk_i = int(sk)
            if sk_i >= 0:
                skip_set.add(sk_i)
        except (TypeError, ValueError):
            pass
    if completed_parts and all(_part_seg_range(cp) is not None for cp in completed_parts):
        all_cov = _covered_segments_from_parts(completed_parts) | skip_set
        if total_segments > 0:
            return sum(1 for idx in all_cov if 0 <= idx < total_segments)
        return len(all_cov)
    raw_sum = sum(_part_seg_count(cp) for cp in completed_parts) + len(skip_set)
    return min(total_segments, raw_sum) if total_segments > 0 else raw_sum


def _part_sort_key(cp: dict[str, Any]) -> tuple[int, int, int]:
    """Sort parts chronologically by start_seg first (NOT part_index alone), preventing scrambled video when gap parts are filled later."""
    rng = _part_seg_range(cp)
    try:
        p_idx = int(cp.get("part_index") or 0)
    except (TypeError, ValueError):
        p_idx = 0
    if rng is not None:
        return (rng[0], rng[1], p_idx)
    return (p_idx * 1_000_000, p_idx * 1_000_000, p_idx)


def _normalize_completed_parts(
    parts: list[dict[str, Any]] | None,
    valid_remote_names: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Deduplicate, optionally verify against remote files, normalize segment_count, and sort chronologically."""
    seen_names: set[str] = set()
    seen_ranges: set[tuple[int, int]] = set()
    normalized: list[dict[str, Any]] = []
    dropped_missing: list[str] = []
    for raw_cp in parts or []:
        if not isinstance(raw_cp, dict):
            continue
        fname = str(raw_cp.get("file_name") or "").strip()
        if fname:
            if valid_remote_names is not None and fname not in valid_remote_names:
                dropped_missing.append(fname)
                continue
            if fname in seen_names:
                continue
        rng = _part_seg_range(raw_cp)
        if rng is not None and rng in seen_ranges:
            continue
        cp_copy = dict(raw_cp)
        if rng is not None:
            cp_copy["start_seg"] = rng[0]
            cp_copy["end_seg"] = rng[1]
            cp_copy["segment_count"] = rng[1] - rng[0] + 1
            seen_ranges.add(rng)
        if fname:
            seen_names.add(fname)
        normalized.append(cp_copy)
    normalized.sort(key=_part_sort_key)
    return normalized, dropped_missing


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
    (is_complete, unique synced segments, completed_parts count, last_synced_segment), choose the
    most complete, merge any disjoint verified parts from sibling manifests, and clean up stale files.
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
    parsed_candidates: list[dict[str, Any]] = []

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
                            parsed_candidates.append(m_data)
                            cps = m_data.get("completed_parts") or []
                            parts_count = len(cps)
                            synced_segs = _count_synced_segments(cps, m_data.get("skipped_segments") or [])
                            last_seg = int(m_data.get("last_synced_segment") or -1)
                            tot_exp = int(m_data.get("total_segments_expected") or 0)
                            is_done = 1 if (m_data.get("is_complete") and (tot_exp <= 0 or synced_segs >= tot_exp)) else 0
                            score = (is_done * 1_000_000_000_000) + (synced_segs * 1_000_000) + (parts_count * 1_000) + max(last_seg, 0)
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
        # Recover any disjoint part entries recorded in sibling manifests whose part_*.ts exists in items
        actual_part_files = {
            str(it.get("name") or "")
            for it in items
            if str(it.get("name") or "").startswith("part_") and str(it.get("name") or "").endswith(".ts")
        }
        if actual_part_files and len(parsed_candidates) > 1:
            base_parts = list(best_data.get("completed_parts") or [])
            known_names = {str(cp.get("file_name") or "") for cp in base_parts if isinstance(cp, dict)}
            covered_now = _covered_segments_from_parts(base_parts)
            recovered_count = 0
            for cand in parsed_candidates:
                if cand is best_data:
                    continue
                for cp in cand.get("completed_parts") or []:
                    if not isinstance(cp, dict):
                        continue
                    fname = str(cp.get("file_name") or "")
                    rng = _part_seg_range(cp)
                    if not fname or fname in known_names or fname not in actual_part_files or rng is None:
                        continue
                    cand_span = set(range(rng[0], rng[1] + 1))
                    if not (cand_span & covered_now):
                        base_parts.append(dict(cp))
                        known_names.add(fname)
                        covered_now.update(cand_span)
                        recovered_count += 1
            if recovered_count > 0:
                base_parts.sort(key=_part_sort_key)
                best_data["completed_parts"] = base_parts
                best_data["last_synced_segment"] = max((cp.get("end_seg", -1) for cp in base_parts), default=-1)
                job.log(f"[Backup-Resume] Đã khôi phục thêm {recovered_count} part từ các bản ghi manifest song song!")

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


class _HlsItemTelemetry:
    """Tracks live per-stream progress in job.active_files, job.file_sizes, and job.item_timings."""

    def __init__(self, job: Any, source: dict[str, Any], file_item: dict[str, Any], raw_name: str) -> None:
        self.job = job
        relay = file_item.get("relay") if isinstance(file_item.get("relay"), dict) else {}
        meta = file_item.get("meta") if isinstance(file_item.get("meta"), dict) else {}
        orig_prov = str(file_item.get("provider") or meta.get("provider") or source.get("provider") or "").lower()
        orig_acc = str(
            file_item.get("accountId")
            or file_item.get("account_id")
            or meta.get("accountId")
            or meta.get("account_id")
            or source.get("accountId")
            or source.get("account_id")
            or ""
        )
        self.orig_uid = str(
            relay.get("sourcePath")
            or relay.get("sourceId")
            or file_item.get("id")
            or file_item.get("path")
            or file_item.get("url")
            or file_item.get("name")
            or raw_name
        )
        self.track_key = f"{orig_prov}:{orig_acc}:{self.orig_uid}"
        self.raw_name = raw_name
        self.started_at = time.time()
        self._last_tick_at = self.started_at
        self._last_tick_bytes = 0
        self.entry: dict[str, Any] = {
            "name": raw_name,
            "phase": "download",
            "stage": "Initializing HLS pipeline",
            "is_hls": True,
            "started_at": self.started_at,
            "segments_done": 0,
            "segments_total": 0,
            "segments_skipped": 0,
            "parts_uploaded": 0,
            "parts_total": 0,
            "current_part": "",
            "bytes_done": 0,
            "size": 0,
            "speed": 0.0,
        }
        active_files = getattr(job, "active_files", None)
        if isinstance(active_files, dict):
            active_files[self.track_key] = self.entry
            if self.orig_uid and self.orig_uid != self.track_key:
                active_files[self.orig_uid] = self.entry

    def update(
        self,
        *,
        output_name: str | None = None,
        phase: str | None = None,
        stage: str | None = None,
        segments_done: int | None = None,
        segments_total: int | None = None,
        segments_skipped: int | None = None,
        parts_uploaded: int | None = None,
        parts_total: int | None = None,
        current_part: str | None = None,
        bytes_done: int | None = None,
        size: int | None = None,
    ) -> None:
        now = time.time()
        if output_name:
            self.entry["name"] = output_name
        if phase:
            self.entry["phase"] = phase
        if stage is not None:
            self.entry["stage"] = stage
        if segments_done is not None:
            self.entry["segments_done"] = max(0, int(segments_done))
        if segments_total is not None:
            self.entry["segments_total"] = max(0, int(segments_total))
        if segments_skipped is not None:
            self.entry["segments_skipped"] = max(0, int(segments_skipped))
        if parts_uploaded is not None:
            self.entry["parts_uploaded"] = max(0, int(parts_uploaded))
        if parts_total is not None:
            self.entry["parts_total"] = max(0, int(parts_total))
        if current_part is not None:
            self.entry["current_part"] = current_part
        if bytes_done is not None:
            b_done = max(0, int(bytes_done))
            if self._last_tick_bytes == 0 and b_done > 0 and (now - self.started_at) < 2.0:
                # Baseline from pre-existing resumed parts without spiking artificial speed
                self._last_tick_bytes = b_done
                self._last_tick_at = now
            else:
                dt = now - self._last_tick_at
                if dt >= 0.8 and b_done >= self._last_tick_bytes:
                    inst_spd = (b_done - self._last_tick_bytes) / dt
                    prev_spd = float(self.entry.get("speed") or 0.0)
                    self.entry["speed"] = round(inst_spd if prev_spd <= 0 else (prev_spd * 0.4 + inst_spd * 0.6), 1)
                    self._last_tick_at = now
                    self._last_tick_bytes = b_done
            self.entry["bytes_done"] = b_done
        if size is not None and size > 0:
            self.entry["size"] = int(size)
        if hasattr(self.job, "updated_at"):
            self.job.updated_at = now

    def record_completion(
        self,
        *,
        final_bytes: int,
        segments_total: int,
        segments_done: int,
        segments_skipped: int,
        parts_count: int,
        output_name: str,
        space_saved: bool = False,
    ) -> None:
        file_sizes = getattr(self.job, "file_sizes", None)
        if isinstance(file_sizes, dict) and final_bytes > 0:
            file_sizes[self.track_key] = int(final_bytes)
            if self.orig_uid:
                file_sizes[self.orig_uid] = int(final_bytes)
            if output_name:
                file_sizes[output_name] = int(final_bytes)
        timings = getattr(self.job, "item_timings", None)
        if isinstance(timings, dict):
            extra = {
                "segmentsTotal": int(segments_total),
                "segmentsDone": int(segments_done),
                "segmentsSkipped": int(segments_skipped),
                "partsCount": int(parts_count),
                "finalBytes": int(final_bytes),
                "outputName": output_name,
                "spaceSaved": bool(space_saved),
            }
            for tk in (self.track_key, self.orig_uid):
                if not tk:
                    continue
                if isinstance(timings.get(tk), dict):
                    timings[tk].update(extra)
                elif tk == self.track_key:
                    timings[tk] = {"startTime": self.started_at, "status": "active", "name": output_name or self.raw_name, **extra}

    def cleanup(self) -> None:
        active_files = getattr(self.job, "active_files", None)
        if isinstance(active_files, dict):
            active_files.pop(self.track_key, None)
            if self.orig_uid:
                active_files.pop(self.orig_uid, None)


def _get_hls_upload_semaphore(
    job: JobState,
    target: dict[str, Any],
    options: dict[str, Any],
) -> asyncio.Semaphore:
    """Return a job-scoped semaphore capping concurrent HLS part/final uploads."""
    live_target = (
        (job.payload.get("target") if isinstance(getattr(job, "payload", None), dict) else None)
        or target
        or {}
    )
    t_prov = str(live_target.get("provider") or "").strip().lower()
    t_creds = live_target.get("credentials") or {}
    is_drive_web = t_prov == "drive" and (
        t_creds.get("auth_mode") == "web_session"
        or bool(t_creds.get("cookies"))
        or str(t_creds.get("access_token") or t_creds.get("web_access_token") or "").lower().startswith("sapisidhash")
    )
    raw_limit = (
        options.get("upload_concurrency")
        or options.get("uploadConcurrency")
        or options.get("hls_upload_concurrency")
        or 3
    )
    try:
        ul_limit = max(1, min(6, int(raw_limit)))
    except (TypeError, ValueError):
        ul_limit = 3
    if is_drive_web:
        ul_limit = max(1, min(2, ul_limit))
    sem_key = (is_drive_web, ul_limit)
    cached = getattr(job, "_hls_upload_sem_entry", None)
    if isinstance(cached, tuple) and len(cached) == 2 and cached[0] == sem_key:
        return cached[1]
    sem = asyncio.Semaphore(ul_limit)
    job._hls_upload_sem_entry = (sem_key, sem)  # type: ignore[attr-defined]
    return sem


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
    init_url = str(file_item.get("path") or file_item.get("id") or file_item.get("url") or "")
    init_name = str(file_item.get("name") or unquote(Path(urlparse(init_url).path).name) or "stream_video.mp4")
    telemetry = _HlsItemTelemetry(job, source, file_item, init_name)
    try:
        return await _run_resumable_backup_pipeline_core(
            job, dirs, source, target, options, src_provider, dst_provider, file_item, telemetry
        )
    finally:
        telemetry.cleanup()


async def _run_resumable_backup_pipeline_core(
    job: JobState,
    dirs: dict[str, Path],
    source: dict[str, Any],
    target: dict[str, Any],
    options: dict[str, Any],
    src_provider: Any,
    dst_provider: Any,
    file_item: dict[str, Any],
    telemetry: _HlsItemTelemetry,
) -> Path:
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

    initial_dst_prov_name = str(target.get("provider") or "").lower()

    def _live_target_creds() -> dict[str, Any]:
        creds = target.get("credentials")
        return creds if isinstance(creds, dict) else {}

    def _live_target_folder() -> str:
        raw_f = str((target.get("folder") or {}).get("id") or (target.get("folder") or {}).get("path") or "/")
        if raw_f.startswith("id:"):
            raw_f = raw_f[3:].strip()
        return raw_f or "/"

    def _live_dst_provider() -> Any:
        prov_key = str(target.get("provider") or "").lower()
        if prov_key and prov_key != initial_dst_prov_name:
            return PROVIDERS.get(prov_key, dst_provider)
        if dst_provider is not None:
            return dst_provider
        return PROVIDERS.get(prov_key, dst_provider)

    def _live_account_key() -> tuple[str, str, str]:
        creds = _live_target_creds()
        acc = str(
            target.get("accountId")
            or target.get("account_id")
            or creds.get("account_id")
            or creds.get("accountId")
            or creds.get("account")
            or ""
        )
        return (str(target.get("provider") or "").lower(), acc, _live_target_folder())

    target_folder = _live_target_folder()
    target_creds = _live_target_creds()

    chunk_segments = int(options.get("backup_chunk_segments") or DEFAULT_CHUNK_SEGMENTS)
    chunk_mb = int(options.get("backup_chunk_mb") or DEFAULT_CHUNK_MB)
    raw_and_mode = options.get("backup_chunk_and_mode")
    if raw_and_mode is None:
        raw_and_mode = options.get("backupChunkAndMode")
    if raw_and_mode is None and options.get("backup_chunk_mode") is not None:
        raw_and_mode = str(options.get("backup_chunk_mode")).strip().lower() == "and"
    if raw_and_mode is None and options.get("backupChunkMode") is not None:
        raw_and_mode = str(options.get("backupChunkMode")).strip().lower() == "and"
    chunk_and_mode = bool(raw_and_mode)
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
    src_backup_folder_id: str | None = None
    src_listing_succeeded = False
    src_remote_part_map: dict[str, dict[str, Any]] = {}
    dst_remote_part_map: dict[str, dict[str, Any]] = {}
    backup_folder_id: str | None = None
    backup_folder_scope: tuple[str, str, str] = _live_account_key()
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
        src_backup_folder_id = src_folder_id
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
            if items_found:
                src_listing_succeeded = True
            for it in items_found:
                nm = str(it.get("name") or "")
                if nm.startswith("part_") and nm.endswith(".ts"):
                    existing_remote_part_names.add(nm)
                    src_remote_part_map[nm] = it
                    remote_part_map[nm] = it
            folder_manifest_data = await _find_and_load_best_manifest(src_provider, source_creds, items_found, job_work_dir, job)
            if not folder_manifest_data and target_folder and hasattr(src_provider, "list_files"):
                try:
                    parent_listing = await src_provider.list_files(source_creds, target_folder)
                    b_item = next((it for it in (parent_listing.get("items") or []) if it.get("name") == backup_folder_name and (it.get("type") == "folder" or it.get("is_folder"))), None)
                    if b_item:
                        src_backup_folder_id = b_item.get("id") or b_item.get("path") or src_backup_folder_id
                        src_listing = await src_provider.list_files(source_creds, src_backup_folder_id)
                        items_found = src_listing.get("items") or []
                        if items_found:
                            src_listing_succeeded = True
                        for it in items_found:
                            nm = str(it.get("name") or "")
                            if nm.startswith("part_") and nm.endswith(".ts"):
                                existing_remote_part_names.add(nm)
                                src_remote_part_map[nm] = it
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
            if raw_and_mode is None:
                if folder_manifest_data.get("chunk_and_mode") is not None:
                    chunk_and_mode = bool(folder_manifest_data.get("chunk_and_mode"))
                elif folder_manifest_data.get("chunk_condition_mode") is not None:
                    chunk_and_mode = str(folder_manifest_data.get("chunk_condition_mode")).strip().lower() == "and"
            cond_op_label = "VÀ (AND)" if chunk_and_mode else "HOẶC (OR)"
            job.log(f"[Backup-Resume] Đọc manifest thành công: Video đích '{final_video_name}', URL gốc: {source_url[:60]}... (Ngưỡng cắt: {chunk_segments} segs {cond_op_label} {chunk_mb} MB)")
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

    telemetry.update(output_name=final_video_name, phase="download", stage="Preparing backup workspace")

    is_auto_skip = bool(options.get("auto_skip") or options.get("autoSkip"))
    if is_auto_skip and hasattr(_live_dst_provider(), "list_files"):
        try:
            from .transfer_job import wait_for_retry_gate
            await wait_for_retry_gate(job)
            remote_target_listing = await _live_dst_provider().list_files(_live_target_creds(), _live_target_folder())
            remote_target_items = remote_target_listing.get("items") or remote_target_listing.get("files") or []
            target_cand_names = {
                final_video_name.lower(),
                safe_name(final_video_name).lower(),
            }
            raw_clean = safe_name(raw_name).lower()
            if raw_clean and not raw_clean.endswith("__backup_resume") and not raw_clean.endswith((".m3u8", ".mpd")):
                target_cand_names.add(raw_clean)
            existing_complete_video = None
            has_incomplete_backup = False
            for r_it in remote_target_items:
                r_name = str(r_it.get("name") or r_it.get("server_filename") or r_it.get("path") or "").strip()
                r_base = PurePosixPath(r_name.replace("\\", "/")).name
                is_dir = bool(r_it.get("type") == "folder" or r_it.get("is_folder") or r_it.get("isdir"))
                if is_dir:
                    if r_base.lower() == backup_folder_name.lower() or safe_name(r_base).lower() == safe_name(backup_folder_name).lower():
                        has_incomplete_backup = True
                    continue
                if r_base.lower().endswith("__backup_resume") or r_base.lower().startswith("_resume_manifest"):
                    continue
                if r_base.lower() in target_cand_names or safe_name(r_base).lower() in target_cand_names:
                    existing_complete_video = r_it
                    break
            if existing_complete_video is not None:
                job.log(f"[Auto-Skip] Video hoàn chỉnh '{final_video_name}' đã tồn tại ở đích. Bỏ qua tải & ghép nối!")
                file_item["_auto_skipped"] = True
                job.files_skipped += 1
                shutil.rmtree(job_work_dir, ignore_errors=True)
                return None
            if has_incomplete_backup:
                job.log(f"[Auto-Skip] Video '{final_video_name}' ở đích chưa hoàn chỉnh (đang ở dạng backup folder '{backup_folder_name}'). Tiếp tục xử lý resume như bình thường...")
        except Exception as auto_skip_err:
            job.log(f"[Auto-Skip] Lưu ý khi kiểm tra file đích: {auto_skip_err}")

    skip_broken_segments = bool(
        options.get("skip_broken_segments")
        or options.get("skipBrokenSegments")
        or options.get("skip_failed_segments")
        or options.get("skipFailedSegments")
    )
    raw_max_skipped = (
        options.get("max_skipped_segments")
        if options.get("max_skipped_segments") is not None
        else options.get("maxSkippedSegments")
    )
    try:
        max_skipped_segments = int(raw_max_skipped) if raw_max_skipped is not None and str(raw_max_skipped).strip() != "" else 0
    except (ValueError, TypeError):
        max_skipped_segments = 0

    skipped_segment_indices: list[int] = []
    skipped_segments_count = 0

    if folder_manifest_data:
        raw_manifest_parts = folder_manifest_data.get("completed_parts") or []
        valid_src_names = set(src_remote_part_map.keys()) if src_listing_succeeded else None
        prelim_parts, missing_in_src = _normalize_completed_parts(raw_manifest_parts, valid_remote_names=valid_src_names)

        # If any manifest part is missing from source folder, check if it already exists in destination backup folder
        if missing_in_src and hasattr(_live_dst_provider(), "list_files"):
            try:
                from .transfer_job import wait_for_retry_gate
                await wait_for_retry_gate(job)
                live_dst_chk = _live_dst_provider()
                live_creds_chk = _live_target_creds()
                live_folder_chk = _live_target_folder()
                dst_root_listing = await live_dst_chk.list_files(live_creds_chk, live_folder_chk)
                dst_b_item = next(
                    (
                        it for it in (dst_root_listing.get("items") or [])
                        if it.get("name") == backup_folder_name and (it.get("type") == "folder" or it.get("is_folder"))
                    ),
                    None,
                )
                if dst_b_item:
                    backup_folder_id = dst_b_item.get("id") or dst_b_item.get("path")
                    backup_folder_scope = _live_account_key()
                    dst_parts_listing = await live_dst_chk.list_files(live_creds_chk, backup_folder_id)
                    for it in dst_parts_listing.get("items") or []:
                        nm = str(it.get("name") or "")
                        if nm.startswith("part_") and nm.endswith(".ts"):
                            existing_remote_part_names.add(nm)
                            dst_remote_part_map[nm] = it
                            remote_part_map.setdefault(nm, it)
            except Exception:
                pass
            combined_valid_names = (set(src_remote_part_map.keys()) | set(dst_remote_part_map.keys())) if src_listing_succeeded else None
            prelim_parts, missing_in_src = _normalize_completed_parts(raw_manifest_parts, valid_remote_names=combined_valid_names)

        if missing_in_src:
            missing_preview = ", ".join(missing_in_src[:5])
            job.log(f"[Resume-Manifest] Phát hiện {len(missing_in_src)} part ghi trong manifest nhưng không có trên Cloud ({missing_preview}) — sẽ tải bù!")

        completed_parts = prelim_parts
        last_synced_seg = max((cp.get("end_seg", -1) for cp in completed_parts), default=-1)
        if not src_listing_succeeded:
            existing_remote_part_names.update(cp.get("file_name") for cp in completed_parts if cp.get("file_name"))
        for sk in folder_manifest_data.get("skipped_segments") or []:
            if sk not in skipped_segment_indices:
                skipped_segment_indices.append(sk)
        skipped_segments_count = len(skipped_segment_indices)
        total_segments_expected = int(folder_manifest_data.get("total_segments_expected") or 0)
        synced_manifest_segs = _count_synced_segments(completed_parts, skipped_segment_indices, total_segments_expected)
        is_already_complete = (
            not missing_in_src
            and bool(completed_parts)
            and (
                (
                    folder_manifest_data.get("is_complete") is True
                    and (total_segments_expected <= 0 or synced_manifest_segs >= total_segments_expected)
                )
                or (total_segments_expected > 0 and synced_manifest_segs >= total_segments_expected)
            )
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
            from ..utils.http import ensure_socks_support
            ensure_socks_support()

        def _make_http_client(proxy: str | None = None) -> httpx.AsyncClient:
            client_kw: dict[str, Any] = {"follow_redirects": True, "timeout": 25.0}
            if proxy:
                if "socks" in str(proxy).lower():
                    _ensure_socks_installed()
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

        async def _attempt_resolve_stream(target_u: str) -> bool:
            nonlocal resolved_url, active_proxy, cookies_str, page_url
            if not (links_resolver and hasattr(links_resolver, "_resolve_universal_page")):
                return False
            if hasattr(links_resolver, "_ensure_deps"):
                try:
                    await links_resolver._ensure_deps()
                except Exception:
                    pass
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

        telemetry.update(phase="download", stage="Analyzing HLS playlist")
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
        cond_word = "và (AND)" if chunk_and_mode else "hoặc (OR)"
        job.log(f"[Backup-Resume] Playlist hợp lệ: Tổng cộng {total_segments} segments (Ngưỡng cắt: {chunk_segments} segments {cond_word} {chunk_mb} MB/part)")

        # 2. Check remote target backup folder for existing manifest or parts (Multi-session check)
        try:
            from .transfer_job import wait_for_retry_gate
            await wait_for_retry_gate(job)
            live_dst = _live_dst_provider()
            live_creds = _live_target_creds()
            live_folder = _live_target_folder()
            if hasattr(live_dst, "list_files"):
                remote_files = await live_dst.list_files(live_creds, live_folder)
                sub_items = remote_files.get("items") or []
                backup_item = next((it for it in sub_items if (it.get("name") == backup_folder_name and (it.get("type") == "folder" or it.get("is_folder")))), None)
                if backup_item:
                    backup_folder_id = backup_item.get("id") or backup_item.get("path")
                    backup_folder_scope = _live_account_key()
                    parts_listing = await live_dst.list_files(live_creds, backup_folder_id)
                    sub_parts = parts_listing.get("items") or []
                    for it in sub_parts:
                        name = str(it.get("name") or "")
                        if name.startswith("part_") and name.endswith(".ts"):
                            existing_remote_part_names.add(name)
                            dst_remote_part_map[name] = it
                            remote_part_map[name] = it
                    job.log(f"[Backup-Resume] Phát hiện thư mục backup từ xa: {len(existing_remote_part_names)} part(s) đã upload trước đó.")

                    # Try to restore state from manifest first (most accurate for variable-size parts)
                    manifest_loaded = bool(folder_manifest_data)
                    if not manifest_loaded:
                        try:
                            manifest_data = await _find_and_load_best_manifest(live_dst, live_creds, sub_parts, job_work_dir, job)
                            if manifest_data:
                                norm_dst_parts, _ = _normalize_completed_parts(
                                    manifest_data.get("completed_parts") or [],
                                    valid_remote_names=existing_remote_part_names,
                                )
                                completed_parts.extend(norm_dst_parts)
                                completed_parts, _ = _normalize_completed_parts(completed_parts)
                                last_synced_seg = max((cp.get("end_seg", -1) for cp in completed_parts), default=-1)
                                for sk in manifest_data.get("skipped_segments") or []:
                                    if sk not in skipped_segment_indices:
                                        skipped_segment_indices.append(sk)
                                skipped_segments_count = len(skipped_segment_indices)
                                manifest_loaded = True
                                skip_txt = f", {skipped_segments_count} segment đã bỏ qua" if skipped_segments_count > 0 else ""
                                job.log(f"[Resume-Manifest] Đọc manifest thành công: {len(completed_parts)} part xác nhận{skip_txt}.")
                        except Exception as exc:
                            job.log(f"[Resume-Manifest] Không thể đọc manifest, dùng file listing thay thế: {exc}")
                    elif backup_folder_id and backup_folder_id != src_backup_folder_id:
                        # Cross-folder/account resume: merge any additional verified parts from target's manifest
                        try:
                            dst_manifest_data = await _find_and_load_best_manifest(live_dst, live_creds, sub_parts, job_work_dir, job)
                            if dst_manifest_data:
                                cur_cov = _covered_segments_from_parts(completed_parts)
                                cur_names = {str(cp.get("file_name") or "") for cp in completed_parts}
                                merged_extra = 0
                                for cp in dst_manifest_data.get("completed_parts") or []:
                                    p_name = str(cp.get("file_name") or "")
                                    rng = _part_seg_range(cp)
                                    if p_name and p_name not in cur_names and p_name in existing_remote_part_names and rng is not None:
                                        span = set(range(rng[0], rng[1] + 1))
                                        if not (span & cur_cov):
                                            completed_parts.append(dict(cp))
                                            cur_names.add(p_name)
                                            cur_cov.update(span)
                                            merged_extra += 1
                                for sk in dst_manifest_data.get("skipped_segments") or []:
                                    if sk not in skipped_segment_indices:
                                        skipped_segment_indices.append(sk)
                                skipped_segments_count = len(skipped_segment_indices)
                                if merged_extra > 0:
                                    completed_parts, _ = _normalize_completed_parts(completed_parts)
                                    last_synced_seg = max((cp.get("end_seg", -1) for cp in completed_parts), default=-1)
                                    job.log(f"[Resume-Manifest] Hợp nhất thêm {merged_extra} part đã có sẵn trên thư mục đích!")
                        except Exception:
                            pass

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
        total_segments = int(
            folder_manifest_data.get("total_segments_expected")
            or _count_synced_segments(completed_parts, skipped_segment_indices)
        )
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

    # Compute exact segment coverage across all verified parts (supporting non-contiguous / out-of-order parts)
    covered_by_parts = _covered_segments_from_parts(completed_parts)
    if completed_parts and not covered_by_parts and last_synced_seg >= 0:
        covered_by_parts = set(range(0, last_synced_seg + 1))
    covered_all_segments = covered_by_parts | {int(sk) for sk in skipped_segment_indices if isinstance(sk, int) or str(sk).isdigit()}
    remaining_segments: list[dict[str, Any]] = (
        []
        if is_already_complete
        else [s for s in all_segments if s["index"] not in covered_all_segments]
    )

    if completed_parts:
        remote_list_summary = ", ".join(sorted(existing_remote_part_names)) if len(existing_remote_part_names) <= 5 else f"{len(existing_remote_part_names)} parts"
        job.log(f"[Trace] Đã phát hiện {len(existing_remote_part_names)} part(s) trên Cloud đích ({remote_list_summary}). Bỏ qua {len(completed_parts)} part đã hoàn thành.")
        if is_already_complete or not remaining_segments:
            job.log(f"[Resume-Hit] Khôi phục phiên! Toàn bộ {len(completed_parts)} part ({total_segments}/{total_segments} segments) đã có đủ trên Cloud — chuyển sang ghép file!")
        else:
            first_missing_seg = remaining_segments[0]["index"]
            if first_missing_seg <= last_synced_seg:
                synced_now = _count_synced_segments(completed_parts, skipped_segment_indices, total_segments)
                job.log(
                    f"[Resume-Hit] Khôi phục phiên! Giữ nguyên {len(completed_parts)} part đã có ({synced_now}/{total_segments} segments). "
                    f"Phát hiện {len(remaining_segments)} segment bị khuyết giữa chừng (từ segment {first_missing_seg}/{total_segments}) — tiến hành tải bù!"
                )
            else:
                job.log(f"[Resume-Hit] Khôi phục phiên! Bỏ qua {len(completed_parts)} part đã có. Tiếp tục từ segment {first_missing_seg}/{total_segments}")
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
    session_downloaded_bytes = 0
    initial_synced_segs = len(covered_by_parts) if covered_by_parts else sum(_part_seg_count(cp) for cp in completed_parts)
    initial_synced_bytes = sum(cp.get("bytes", 0) for cp in completed_parts)
    in_flight_part_names: list[str] = []
    est_parts_total = max(len(completed_parts), (total_segments + max(1, chunk_segments) - 1) // max(1, chunk_segments)) if total_segments > 0 else 0
    telemetry.update(
        output_name=final_video_name,
        phase="upload" if is_already_complete else "download",
        stage="Ready for Final Assembly" if is_already_complete else "Downloading HLS segments",
        segments_done=min(total_segments, initial_synced_segs + skipped_segments_count),
        segments_total=total_segments,
        segments_skipped=skipped_segments_count,
        parts_uploaded=len(completed_parts),
        parts_total=est_parts_total,
        bytes_done=initial_synced_bytes,
        size=estimated_total_bytes if estimated_total_bytes > 0 else initial_synced_bytes,
    )

    async def _resolve_backup_folder_id() -> str | None:
        nonlocal backup_folder_id, backup_folder_scope
        cur_scope = _live_account_key()
        if backup_folder_scope != cur_scope:
            backup_folder_id = None
            backup_folder_scope = cur_scope
        if backup_folder_id:
            return backup_folder_id
        try:
            from .transfer_job import wait_for_retry_gate
            await wait_for_retry_gate(job)
            remote_files = await _live_dst_provider().list_files(_live_target_creds(), _live_target_folder())
            sub_items = remote_files.get("items") or []
            b_item = next((it for it in sub_items if (it.get("name") == backup_folder_name and (it.get("type") == "folder" or it.get("is_folder")))), None)
            if b_item:
                backup_folder_id = b_item.get("id") or b_item.get("path")
                backup_folder_scope = _live_account_key()
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
            live_dst = _live_dst_provider()
            live_creds = _live_target_creds()
            cand_items = items_to_check
            if cand_items is None:
                listing = await live_dst.list_files(live_creds, bf_id)
                cand_items = listing.get("items") or []
            for it in cand_items:
                fname = str(it.get("name") or "")
                fid = it.get("id") or it.get("path")
                if fid and str(fid) == str(keep_id):
                    continue
                is_dup = (fname.startswith("_resume_manifest") and fname.endswith(".json") and fname != "_resume_manifest.json") or (keep_id and str(fid) != str(keep_id) and fname == "_resume_manifest.json") or (not keep_id and fname.startswith("_resume_manifest") and fname.endswith(".json"))
                if is_dup:
                    try:
                        if hasattr(live_dst, "delete_item"):
                            await live_dst.delete_item(live_creds, it)
                        elif hasattr(live_dst, "delete_file"):
                            await live_dst.delete_file(live_creds, it)
                    except Exception:
                        pass
        except Exception:
            pass

    async def _delete_old_manifest() -> None:
        """Delete the existing _resume_manifest*.json files from remote to prevent duplicates."""
        await _clean_duplicate_manifests()

    manifest_lock = asyncio.Lock()

    async def _upload_manifest() -> None:
        """Build and upload _resume_manifest.json to remote backup folder (with replace)."""
        async with manifest_lock:
            synced_for_manifest = _count_synced_segments(completed_parts, skipped_segment_indices, total_segments)
            is_done = (synced_for_manifest >= total_segments) if total_segments else False
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
                "chunk_and_mode": chunk_and_mode,
                "chunk_condition_mode": "and" if chunk_and_mode else "or",
                "total_segments_expected": total_segments,
                "completed_parts": sorted(completed_parts, key=_part_sort_key),
                "last_synced_segment": max((cp["end_seg"] for cp in completed_parts), default=-1),
                "skipped_segments": list(skipped_segment_indices),
                "skipped_segments_count": len(skipped_segment_indices),
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
            from .transfer_job import _invoke_wait_for_retry_account, wait_for_retry_gate
            for manifest_try in range(2):
                manifest_gen = await wait_for_retry_gate(job)
                job_work_dir.mkdir(parents=True, exist_ok=True)
                manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

                live_dst = _live_dst_provider()
                live_creds = _live_target_creds()
                live_folder = _live_target_folder()

                bf_id = await _resolve_backup_folder_id()
                existing_manifests = []
                if bf_id:
                    try:
                        listing = await live_dst.list_files(live_creds, bf_id)
                        for it in listing.get("items") or []:
                            fname = str(it.get("name") or "")
                            if fname == "_resume_manifest.json" or (fname.startswith("_resume_manifest") and fname.endswith(".json")):
                                existing_manifests.append(it)
                    except Exception:
                        pass

                exact_manifest = next((it for it in existing_manifests if str(it.get("name") or "") == "_resume_manifest.json"), None)
                target_manifest = exact_manifest or (existing_manifests[0] if existing_manifests else None)

                # Force replace mode safely across concurrent streams via job-level depth counter
                if "options" not in job.payload:
                    job.payload["options"] = {}
                rep_depth = int(getattr(job, "_manifest_replace_depth", 0))
                if rep_depth == 0:
                    job._manifest_orig_replace = job.payload["options"].get("replace")  # type: ignore[attr-defined]
                job._manifest_replace_depth = rep_depth + 1  # type: ignore[attr-defined]
                job.payload["options"]["replace"] = True

                replaced = False
                should_retry_manifest = False
                try:
                    if target_manifest and hasattr(live_dst, "replace_file"):
                        try:
                            await live_dst.replace_file(live_creds, manifest_path, target_manifest, job)
                            replaced = True
                        except Exception as rep_err:
                            job.log(f"[Manifest] replace_file: {rep_err}, falling back to upload_file with replace=True")

                    if not replaced:
                        manifest_ref = {
                            "id": live_folder,
                            "path": live_folder,
                            "relative_path": f"{backup_folder_name}/_resume_manifest.json",
                        }
                        upload_res = await live_dst.upload_file(live_creds, manifest_path, manifest_ref, job)
                        uploaded_fid = (upload_res.get("id") or upload_res.get("path")) if isinstance(upload_res, dict) else None
                        await _clean_duplicate_manifests(existing_manifests, keep_id=uploaded_fid)
                except ProviderFailure as pf_exc:
                    if manifest_try == 0 and pf_exc.code in ("INVALID_PROVIDER_CREDENTIALS", "PROVIDER_TOKEN_EXPIRED", "UPLOAD_FAILED"):
                        try:
                            await _invoke_wait_for_retry_account(job, target, pf_exc, attempt_gen=manifest_gen)
                            should_retry_manifest = True
                        except Exception as retry_err:
                            job.log(f"[Manifest] Cảnh báo: Không thể upload manifest: {retry_err}")
                    else:
                        job.log(f"[Manifest] Cảnh báo: Không thể upload manifest: {pf_exc}")
                except Exception as exc:
                    job.log(f"[Manifest] Cảnh báo: Không thể upload manifest: {exc}")
                finally:
                    new_depth = max(0, int(getattr(job, "_manifest_replace_depth", 1)) - 1)
                    job._manifest_replace_depth = new_depth  # type: ignore[attr-defined]
                    if new_depth == 0:
                        saved_rep = getattr(job, "_manifest_orig_replace", None)
                        if saved_rep is None:
                            job.payload["options"].pop("replace", None)
                        else:
                            job.payload["options"]["replace"] = saved_rep
                    manifest_path.unlink(missing_ok=True)

                if not should_retry_manifest:
                    break

            try:
                bf_id = await _resolve_backup_folder_id()
                if bf_id:
                    fresh_listing = await _live_dst_provider().list_files(_live_target_creds(), bf_id)
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
        nonlocal target_creds, target_folder, dst_provider, backup_folder_id
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
        in_flight_part_names.append(p_name)
        telemetry.update(
            current_part=p_name,
            parts_uploaded=len(completed_parts),
            parts_total=max(est_parts_total, len(completed_parts) + len(in_flight_part_names)),
            stage=f"Downloading • Uploading {p_name}",
        )

        from .transfer_job import _invoke_wait_for_retry_account, wait_for_retry_gate

        max_upload_retries = 3
        last_upload_err: Exception | None = None
        upload_attempt = 0
        auto_refreshed_once = False
        while upload_attempt < max_upload_retries:
            upload_attempt += 1
            attempt_gen = await wait_for_retry_gate(job)
            try:
                async with _get_hls_upload_semaphore(job, target, options):
                    target_creds = _live_target_creds()
                    target_folder = _live_target_folder()
                    active_dst = _live_dst_provider()
                    target_ref = {
                        "id": target_folder,
                        "path": target_folder,
                        "relative_path": f"{backup_folder_name}/{p_name}",
                    }
                    res = await active_dst.upload_file(target_creds, part_file, target_ref, job)
                if not getattr(job, "_waiting_for_account", False):
                    job.error = None
                break  # success
            except ProviderFailure as up_err:
                last_upload_err = up_err
                err_text = str(up_err).lower()
                is_auth = (
                    up_err.code in ("INVALID_PROVIDER_CREDENTIALS", "PROVIDER_TOKEN_EXPIRED")
                    or "session expired" in err_text
                    or "revoked" in err_text
                    or "relogin" in err_text
                )
                is_retryable = is_auth or up_err.code == "UPLOAD_FAILED"

                if not is_retryable:
                    raise

                # When is_auth is True or retries are exhausted, prompt user for fresh account/credentials
                if is_auth or upload_attempt >= max_upload_retries:
                    refreshed = False
                    active_dst = _live_dst_provider()
                    if not is_auth and not auto_refreshed_once and hasattr(active_dst, "_refresh_web_session"):
                        auto_refreshed_once = True
                        try:
                            refreshed = await active_dst._refresh_web_session(_live_target_creds())
                        except Exception:
                            refreshed = False
                    if refreshed:
                        upload_attempt = max(0, max_upload_retries - 1)
                        job.log(f"[Upload-Retry] Đã tự làm mới phiên web thành công. Thử lại upload {p_name}...")
                        continue
                    old_scope = _live_account_key()
                    await _invoke_wait_for_retry_account(job, target, up_err, attempt_gen=attempt_gen)
                    target_creds = _live_target_creds()
                    target_folder = _live_target_folder()
                    if _live_account_key() != old_scope:
                        backup_folder_id = None
                    upload_attempt = 0
                    auto_refreshed_once = False
                    job.log(f"[Upload-Retry] Đã nhận thông tin xác thực mới từ hệ thống. Tiếp tục upload {p_name}...")
                    continue

                job.log(
                    f"[Upload-Retry] Upload {p_name} thất bại (lần {upload_attempt}/{max_upload_retries}): {up_err}. "
                    f"Đang thử lại..."
                )
                await asyncio.sleep(min(2 ** (upload_attempt - 1), 8))
            except (httpx.HTTPError, OSError) as net_err:
                last_upload_err = net_err
                if upload_attempt >= max_upload_retries:
                    net_fail = ProviderFailure("UPLOAD_FAILED", f"Network error uploading {p_name}: {net_err}")
                    old_scope = _live_account_key()
                    await _invoke_wait_for_retry_account(job, target, net_fail, attempt_gen=attempt_gen)
                    target_creds = _live_target_creds()
                    target_folder = _live_target_folder()
                    if _live_account_key() != old_scope:
                        backup_folder_id = None
                    upload_attempt = 0
                    auto_refreshed_once = False
                    job.log(f"[Upload-Retry] Đã nhận thông tin xác thực mới sau lỗi mạng. Tiếp tục upload {p_name}...")
                    continue
                job.log(f"[Upload-Retry] Lỗi mạng upload {p_name} (lần {upload_attempt}/{max_upload_retries}): {net_err}. Thử lại...")
                await asyncio.sleep(min(2 ** (upload_attempt - 1), 8))
        else:
            raise ProviderFailure("UPLOAD_FAILED", f"Upload {p_name} thất bại sau {max_upload_retries} lần thử: {last_upload_err}")

        job.log(f"[Trace] Upload thành công: {p_name}")
        completed_parts.append(p_meta)
        existing_remote_part_names.add(p_name)
        if p_name in in_flight_part_names:
            in_flight_part_names.remove(p_name)
        telemetry.update(
            parts_uploaded=len(completed_parts),
            parts_total=max(est_parts_total, len(completed_parts) + len(in_flight_part_names)),
            current_part=in_flight_part_names[-1] if in_flight_part_names else "",
        )
        if isinstance(res, dict):
            dst_remote_part_map[p_name] = res
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
        # Filter segments that need downloading (gap-aware across all covered + skipped segments)
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
        last_applied_fresh_url = str(
            (job.payload.get("options") or options).get("fresh_url")
            or (job.payload.get("options") or options).get("freshUrl")
            or ""
        ).strip()
        try:
            for rem_pos, seg in enumerate(remaining_segments):
                job.check_cancelled()
                if getattr(job, "_waiting_for_account", False) and active_upload_tasks:
                    done_set, _ = await asyncio.wait(active_upload_tasks, return_when=asyncio.FIRST_COMPLETED)
                    for t in done_set:
                        exc = t.exception()
                        if exc:
                            raise exc
                    active_upload_tasks = [t for t in active_upload_tasks if not t.done()]
                live_opts = (job.payload.get("options") if isinstance(getattr(job, "payload", None), dict) else None) or options
                if isinstance(live_opts, dict):
                    try:
                        if live_opts.get("backup_chunk_segments"):
                            chunk_segments = max(1, int(live_opts["backup_chunk_segments"]))
                        if live_opts.get("backup_chunk_mb"):
                            chunk_mb = max(1, int(live_opts["backup_chunk_mb"]))
                    except (TypeError, ValueError):
                        pass
                    live_and = live_opts.get("backup_chunk_and_mode")
                    if live_and is None:
                        live_and = live_opts.get("backupChunkAndMode")
                    if live_and is None and live_opts.get("backup_chunk_mode") is not None:
                        live_and = str(live_opts.get("backup_chunk_mode")).strip().lower() == "and"
                    if live_and is None and live_opts.get("backupChunkMode") is not None:
                        live_and = str(live_opts.get("backupChunkMode")).strip().lower() == "and"
                    if live_and is not None:
                        chunk_and_mode = bool(live_and)
                    skip_broken_segments = bool(
                        live_opts.get("skip_broken_segments")
                        or live_opts.get("skipBrokenSegments")
                        or live_opts.get("skip_failed_segments")
                        or live_opts.get("skipFailedSegments")
                    )
                    raw_live_max_skip = (
                        live_opts.get("max_skipped_segments")
                        if live_opts.get("max_skipped_segments") is not None
                        else live_opts.get("maxSkippedSegments")
                    )
                    try:
                        max_skipped_segments = int(raw_live_max_skip) if raw_live_max_skip is not None and str(raw_live_max_skip).strip() != "" else 0
                    except (ValueError, TypeError):
                        max_skipped_segments = 0
                    if live_opts.get("backup_auto_clean") is not None:
                        auto_clean = bool(live_opts.get("backup_auto_clean"))
                    live_fresh = str(live_opts.get("fresh_url") or live_opts.get("freshUrl") or "").strip()
                    if live_fresh and live_fresh != last_applied_fresh_url:
                        last_applied_fresh_url = live_fresh
                        resolved_url = live_fresh
                        source_url = live_fresh
                        job.log(f"[Live-Options] Phát hiện URL luồng mới vừa được cập nhật trực tiếp: {live_fresh[:80]}...")
                        try:
                            if not any(k in live_fresh.lower() for k in (".m3u8", "/hls/", ".mpd")):
                                await _attempt_resolve_stream(live_fresh)
                            _, fresh_segs_live, _ = await _fetch_playlist_with_fallback()
                            if fresh_segs_live:
                                fresh_map_live = {s["index"]: s["url"] for s in fresh_segs_live}
                                for rem in remaining_segments:
                                    if rem["index"] in fresh_map_live:
                                        rem["url"] = fresh_map_live[rem["index"]]
                                seg["url"] = fresh_map_live.get(seg["index"], seg["url"])
                                job.log(f"[Live-Options] Đã làm mới URL cho {len(remaining_segments)} segments từ URL luồng mới!")
                        except Exception as live_url_exc:
                            job.log(f"[Live-Options] Lưu ý khi áp dụng URL luồng mới: {live_url_exc}")

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
                    can_skip = False
                    if skip_broken_segments:
                        if max_skipped_segments <= 0 or (skipped_segments_count < max_skipped_segments):
                            can_skip = True

                    if can_skip:
                        skipped_segments_count += 1
                        skipped_segment_indices.append(seg["index"])
                        seg_file.unlink(missing_ok=True)
                        limit_str = f"giới hạn {max_skipped_segments}" if max_skipped_segments > 0 else "không giới hạn"
                        job.log(
                            f"[Backup-Resume] [Skip-Segment] Bỏ qua segment lỗi {seg['index']} sau khi đã dùng hết số lần retry. "
                            f"(Đã skip {skipped_segments_count} segment - {limit_str}). Tiếp tục tải các segment khác..."
                        )
                    else:
                        if skip_broken_segments and max_skipped_segments > 0 and skipped_segments_count >= max_skipped_segments:
                            raise ProviderFailure(
                                "DOWNLOAD_FAILED",
                                f"Tải segment {seg['index']} thất bại và đã đạt giới hạn tối đa số segment lỗi cho phép ({max_skipped_segments}): {last_seg_err}"
                            ) from last_seg_err
                        raise ProviderFailure(
                            "DOWNLOAD_FAILED",
                            f"Failed to download segment {seg['index']} after all recovery rounds: {last_seg_err}"
                        ) from last_seg_err
                else:
                    accumulated_bytes += seg_sz
                    session_downloaded_bytes += seg_sz
                    cur_batch_segments.append(seg)
                    cur_batch_paths.append(seg_file)
                    total_downloaded_segs += 1

                if (total_downloaded_segs + skipped_segments_count) % 5 == 0 or seg["index"] == total_segments - 1:
                    segs_now = min(total_segments, initial_synced_segs + total_downloaded_segs + skipped_segments_count)
                    bytes_now = initial_synced_bytes + session_downloaded_bytes
                    est_total_now = (
                        int(bytes_now * (total_segments / max(1, segs_now)))
                        if segs_now >= 3 and total_segments > 0 and bytes_now > 0
                        else max(estimated_total_bytes, bytes_now)
                    )
                    est_parts_total = max(est_parts_total, len(completed_parts) + len(in_flight_part_names), (total_segments + max(1, chunk_segments) - 1) // max(1, chunk_segments))
                    cur_up_part = in_flight_part_names[-1] if in_flight_part_names else ""
                    telemetry.update(
                        phase="download",
                        stage=f"Downloading • Uploading {cur_up_part}" if cur_up_part else "Downloading HLS segments",
                        segments_done=segs_now,
                        segments_total=total_segments,
                        segments_skipped=skipped_segments_count,
                        parts_uploaded=len(completed_parts),
                        parts_total=est_parts_total,
                        current_part=cur_up_part,
                        bytes_done=bytes_now,
                        size=max(bytes_now, est_total_now),
                    )

                # Progress logging every 50 segments or at end of remaining queue
                is_last_rem = (rem_pos == len(remaining_segments) - 1)
                if (total_downloaded_segs + skipped_segments_count) % 50 == 0 or seg["index"] == total_segments - 1 or is_last_rem:
                    done_segs_log = min(total_segments, initial_synced_segs + total_downloaded_segs + skipped_segments_count)
                    pct = done_segs_log * 100 // max(1, total_segments)
                    skip_info = f", {skipped_segments_count} skipped" if skipped_segments_count > 0 else ""
                    job.log(f"[Download] {done_segs_log}/{total_segments} segments ({pct}%{skip_info}) | {accumulated_bytes // (1024*1024)} MB tích lũy")

                # Check threshold trigger (OR by default; AND when chunk_and_mode is enabled) + gap boundary trigger
                hit_seg_threshold = len(cur_batch_segments) >= chunk_segments
                hit_mb_threshold = accumulated_bytes >= (chunk_mb * 1024 * 1024)
                is_last_seg = (seg["index"] == total_segments - 1)
                next_rem_idx = remaining_segments[rem_pos + 1]["index"] if (rem_pos + 1) < len(remaining_segments) else None
                is_gap_span_end = (
                    next_rem_idx is None
                    or any(mid_idx in covered_by_parts for mid_idx in range(seg["index"] + 1, next_rem_idx))
                )
                hit_chunk_threshold = (hit_seg_threshold and hit_mb_threshold) if chunk_and_mode else (hit_seg_threshold or hit_mb_threshold)

                if (hit_chunk_threshold or is_last_seg or is_gap_span_end) and cur_batch_segments:
                    # Determine part index — skip indices/filenames already used by completed, remote, or in-flight parts
                    while (
                        any(cp.get("part_index") == part_idx for cp in completed_parts)
                        or f"part_{part_idx:04d}.ts" in existing_remote_part_names
                        or f"part_{part_idx:04d}.ts" in in_flight_part_names
                    ):
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

                    # Launch upload as background task — apply backpressure so slow/paused uploads do not pile up
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

                    max_inflight_parts = 1 if getattr(job, "_waiting_for_account", False) else 2
                    while len(active_upload_tasks) >= max_inflight_parts:
                        done_set, _ = await asyncio.wait(active_upload_tasks, return_when=asyncio.FIRST_COMPLETED)
                        for t in done_set:
                            exc = t.exception()
                            if exc:
                                raise exc
                        active_upload_tasks = [t for t in active_upload_tasks if not t.done()]
                        max_inflight_parts = 1 if getattr(job, "_waiting_for_account", False) else 2

            if cur_batch_segments:
                while (
                    any(cp.get("part_index") == part_idx for cp in completed_parts)
                    or f"part_{part_idx:04d}.ts" in existing_remote_part_names
                    or f"part_{part_idx:04d}.ts" in in_flight_part_names
                ):
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

                for p in cur_batch_paths:
                    p.unlink(missing_ok=True)

                cur_batch_segments.clear()
                cur_batch_paths.clear()
                accumulated_bytes = 0

                task = asyncio.create_task(_upload_part_worker(part_path, part_meta))
                active_upload_tasks.append(task)
                part_idx += 1

            # Wait for remaining background uploads after all segments downloaded
            if active_upload_tasks:
                telemetry.update(
                    phase="upload",
                    stage=f"Uploading remaining parts ({len(active_upload_tasks)})",
                    segments_done=total_segments,
                    segments_total=total_segments,
                    segments_skipped=skipped_segments_count,
                )
                job.log(f"[Upload-Pipe] Đợi {len(active_upload_tasks)} upload task(s) còn lại hoàn tất...")
                results = await asyncio.gather(*active_upload_tasks, return_exceptions=True)
                active_upload_tasks.clear()
                for r in results:
                    if isinstance(r, BaseException):
                        raise r
        finally:
            pending_uploads = [t for t in active_upload_tasks if not t.done()]
            for t in pending_uploads:
                t.cancel()
            if pending_uploads:
                await asyncio.gather(*pending_uploads, return_exceptions=True)
            await dl_client.aclose()


    # 4. Check if all segments are covered before Final Assembly
    completed_parts, _ = _normalize_completed_parts(completed_parts)
    total_synced = _count_synced_segments(completed_parts, skipped_segment_indices, total_segments)
    if not completed_parts:
        raise ProviderFailure("DOWNLOAD_FAILED", "Không có segment nào tải thành công (toàn bộ segment bị lỗi hoặc bị bỏ qua).")
    if total_synced < total_segments:
        job.log(
            f"[Backup-Resume] Phiên này đã sao lưu {total_synced}/{total_segments} segments "
            f"({len(completed_parts)} parts, {len(skipped_segment_indices)} skipped). Dữ liệu an toàn trên Cloud — hãy resume phiên tiếp để hoàn tất."
        )
        shutil.rmtree(job_work_dir, ignore_errors=True)
        # Return None for partial session; caller checks is not None
        return None

    # If skip_final_assembly is active (due to space > 50% free disk, Kaggle limit, or option):
    if skip_final_assembly:
        total_synced_bytes = sum(cp.get("bytes", 0) for cp in completed_parts)
        total_synced_mb = total_synced_bytes / (1024 * 1024)
        skipped_str = f", {len(skipped_segment_indices)} segment đã bỏ qua" if skipped_segment_indices else ""
        job.log(
            f"[Backup-Resume] [Space-Saver] 100% segments ({total_segments} segments{skipped_str}, {len(completed_parts)} parts, ~{total_synced_mb:.1f} MB) "
            f"đã được sao lưu an toàn và hoàn chỉnh lên Cloud tại thư mục '{backup_folder_name}'! "
            f"Bỏ qua bước ghép video (Final Assembly) trên runtime để tiết kiệm không gian đĩa và tránh lỗi tràn bộ nhớ (Errno 28)."
        )
        await _upload_manifest()
        telemetry.record_completion(
            final_bytes=total_synced_bytes,
            segments_total=total_segments,
            segments_done=max(0, total_segments - len(skipped_segment_indices)),
            segments_skipped=len(skipped_segment_indices),
            parts_count=len(completed_parts),
            output_name=backup_folder_name,
            space_saved=True,
        )
        shutil.rmtree(job_work_dir, ignore_errors=True)
        return job_work_dir / backup_folder_name

    # 5. Final Assembly: Concatenate all parts into final MP4
    total_parts_bytes = sum(cp.get("bytes", 0) for cp in completed_parts)
    telemetry.update(
        phase="upload",
        stage="Final Assembly: verifying parts",
        segments_done=total_segments,
        segments_total=total_segments,
        segments_skipped=len(skipped_segment_indices),
        parts_uploaded=len(completed_parts),
        parts_total=len(completed_parts),
        current_part="",
        bytes_done=total_parts_bytes,
        size=max(total_parts_bytes, estimated_total_bytes),
    )
    skipped_str = f" ({len(skipped_segment_indices)} segment lỗi đã được bỏ qua)" if skipped_segment_indices else ""
    job.log(f"[Final-Assembly] Hoàn tất 100% segments ({total_segments} segments{skipped_str})! Bắt đầu ghép file hoàn chỉnh: {final_video_name}...")
    final_output_path = job_work_dir / final_video_name

    # Sort chronologically by start_seg (critical when gap-fill parts were uploaded with higher part_index)
    completed_parts.sort(key=_part_sort_key)
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

    is_src_parts = bool(is_backup_folder_input and not is_source_links)
    parts_provider = src_provider if is_src_parts else _live_dst_provider()
    parts_creds = (source.get("credentials") or {}) if is_src_parts else _live_target_creds()
    provider_label = "Cloud nguồn" if is_src_parts else "Cloud đích"

    local_summary = ", ".join(local_ready_parts) if len(local_ready_parts) <= 5 else f"{len(local_ready_parts)} parts"
    job.log(
        f"[Final-Assembly] Phân tích phân đoạn: {len(local_ready_parts)}/{len(completed_parts)} part(s) đã có sẵn tại local "
        f"({local_summary}). Chỉ cần tải {len(missing_parts)} part(s) còn thiếu từ {provider_label}..."
    )

    # Re-check if downloading missing parts would exceed disk space
    total_missing_bytes = sum(cp.get("bytes", 0) for cp in missing_parts)
    current_free_disk = _get_disk_free(job_work_dir)
    if total_missing_bytes > 0 and (total_missing_bytes * 2 > current_free_disk or total_missing_bytes > current_free_disk * 0.5):
        total_synced_bytes = sum(cp.get("bytes", 0) for cp in completed_parts)
        total_synced_mb = total_synced_bytes / (1024 * 1024)
        job.log(
            f"[Final-Assembly] [Space-Saver] Dung lượng các part cần tải lại (~{total_missing_bytes // (1024*1024)} MB) "
            f"vượt quá không gian đĩa an toàn ({current_free_disk // (1024*1024)} MB). "
            f"Bỏ qua bước ghép video để bảo vệ hệ thống. Toàn bộ {len(completed_parts)} parts (~{total_synced_mb:.1f} MB) đã có đủ trên Cloud tại '{backup_folder_name}'."
        )
        telemetry.record_completion(
            final_bytes=total_synced_bytes,
            segments_total=total_segments,
            segments_done=max(0, total_segments - len(skipped_segment_indices)),
            segments_skipped=len(skipped_segment_indices),
            parts_count=len(completed_parts),
            output_name=backup_folder_name,
            space_saved=True,
        )
        shutil.rmtree(job_work_dir, ignore_errors=True)
        return job_work_dir / backup_folder_name

    # If there are missing parts, ensure we have fresh remote items to download from
    if missing_parts:
        if is_src_parts and src_backup_folder_id and hasattr(src_provider, "list_files"):
            try:
                fresh_src_listing = await src_provider.list_files(source.get("credentials") or {}, src_backup_folder_id)
                for it in fresh_src_listing.get("items") or []:
                    fn = str(it.get("name") or "")
                    if fn.startswith("part_") and fn.endswith(".ts"):
                        src_remote_part_map[fn] = it
                        remote_part_map[fn] = it
            except Exception:
                pass
        need_dst_lookup = (not is_src_parts) or any(cp["file_name"] not in src_remote_part_map for cp in missing_parts)
        if need_dst_lookup and hasattr(_live_dst_provider(), "list_files"):
            dst_bf_id = await _resolve_backup_folder_id()
            if dst_bf_id:
                try:
                    fresh_dst_listing = await _live_dst_provider().list_files(_live_target_creds(), dst_bf_id)
                    for it in fresh_dst_listing.get("items") or []:
                        fn = str(it.get("name") or "")
                        if fn.startswith("part_") and fn.endswith(".ts"):
                            dst_remote_part_map[fn] = it
                            remote_part_map[fn] = it
                except Exception:
                    pass

    for cp in completed_parts:
        p_name = cp["file_name"]
        local_p = parts_local_dir / p_name
        exp_bytes = cp.get("bytes")
        if not _is_local_part_valid(local_p, exp_bytes):
            size_mb = (exp_bytes or 0) / (1024 * 1024)
            use_dst_for_part = (not is_src_parts) or (p_name not in src_remote_part_map and p_name in dst_remote_part_map)
            cur_part_label = "Cloud đích" if use_dst_for_part else provider_label
            job.log(f"[Final-Assembly] Tải phân đoạn còn thiếu từ {cur_part_label}: {p_name} ({size_mb:.1f} MB)...")
            part_remote_item = (
                (dst_remote_part_map.get(p_name) if use_dst_for_part else src_remote_part_map.get(p_name))
                or remote_part_map.get(p_name)
                or {
                    "name": p_name,
                    "path": f"{backup_folder_name}/{p_name}",
                    "relative_path": f"{backup_folder_name}/{p_name}",
                }
            )
            part_downloaded = False
            for part_dl_attempt in range(5):
                try:
                    if use_dst_for_part:
                        from .transfer_job import wait_for_retry_gate
                        await wait_for_retry_gate(job)
                        dl_prov = _live_dst_provider()
                        dl_creds = _live_target_creds()
                    else:
                        dl_prov = src_provider
                        dl_creds = source.get("credentials") or {}
                    await dl_prov.download_file(dl_creds, part_remote_item, parts_local_dir, job)
                    if not local_p.is_file():
                        candidates = list(parts_local_dir.glob(f"*{p_name}*"))
                        if candidates:
                            candidates[0].rename(local_p)
                    if local_p.is_file() and local_p.stat().st_size > 0:
                        part_downloaded = True
                        break
                except Exception as p_err:
                    job.log(f"[Final-Assembly] Tải lại part {p_name} lần {part_dl_attempt + 1}/5 thất bại: {p_err}")
                    if not use_dst_for_part and p_name in dst_remote_part_map:
                        use_dst_for_part = True
                        part_remote_item = dst_remote_part_map[p_name]
                    await asyncio.sleep(2.0 * (part_dl_attempt + 1))

            if not part_downloaded or not local_p.is_file() or local_p.stat().st_size <= 0:
                raise ProviderFailure("DOWNLOAD_FAILED", f"Không thể tải lại part {p_name} từ {cur_part_label}")
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

    telemetry.update(phase="upload", stage="Merging MP4 via FFmpeg")
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
    live_opts_final = (job.payload.get("options") if isinstance(getattr(job, "payload", None), dict) else None) or options
    if isinstance(live_opts_final, dict) and (live_opts_final.get("set_video_thumbnail") or live_opts_final.get("trim_intro")):
        try:
            from ..utils.video_thumbnail import process_video_thumbnails
            processed = await asyncio.to_thread(process_video_thumbnails, [final_output_path], live_opts_final, job)
            if processed and processed[0].is_file():
                final_output_path = processed[0]
                final_size = final_output_path.stat().st_size
        except Exception as thumb_exc:
            job.log(f"[Thumbnail] Lưu ý xử lý video sau khi ghép: {thumb_exc}")
    telemetry.update(
        phase="upload",
        stage="Uploading final MP4 to Cloud",
        bytes_done=final_size,
        size=final_size,
    )
    job.log(f"[Final-Assembly] Ghép thành công video hoàn chỉnh ({final_size} bytes). Đang upload lên Cloud đích...")

    # Upload final MP4 to main target folder
    from .transfer_job import _invoke_wait_for_retry_account, wait_for_retry_gate
    final_attempt = 0
    while True:
        final_attempt += 1
        attempt_gen = await wait_for_retry_gate(job)
        try:
            async with _get_hls_upload_semaphore(job, target, options):
                target_creds = _live_target_creds()
                target_folder = _live_target_folder()
                active_dst = _live_dst_provider()
                target_final_ref = {
                    "id": target_folder,
                    "path": target_folder,
                    "relative_path": final_video_name,
                }
                await active_dst.upload_file(target_creds, final_output_path, target_final_ref, job)
            if not getattr(job, "_waiting_for_account", False):
                job.error = None
            break
        except ProviderFailure as up_err:
            if up_err.code not in ("UPLOAD_FAILED", "INVALID_PROVIDER_CREDENTIALS", "PROVIDER_TOKEN_EXPIRED"):
                raise
            is_auth_final = up_err.code in ("INVALID_PROVIDER_CREDENTIALS", "PROVIDER_TOKEN_EXPIRED")
            if not is_auth_final and final_attempt < 3:
                job.log(f"[Final-Upload] Upload {final_video_name} tạm lỗi (lần {final_attempt}/3): {up_err}. Đang thử lại...")
                await asyncio.sleep(min(2 ** (final_attempt - 1), 8))
                continue
            old_scope = _live_account_key()
            await _invoke_wait_for_retry_account(job, target, up_err, attempt_gen=attempt_gen)
            if _live_account_key() != old_scope:
                backup_folder_id = None
            final_attempt = 0
        except (httpx.HTTPError, OSError) as net_err:
            if final_attempt < 3:
                job.log(f"[Final-Upload] Lỗi mạng upload {final_video_name} (lần {final_attempt}/3): {net_err}. Đang thử lại...")
                await asyncio.sleep(min(2 ** (final_attempt - 1), 8))
                continue
            net_fail = ProviderFailure("UPLOAD_FAILED", f"Network error uploading {final_video_name}: {net_err}")
            old_scope = _live_account_key()
            await _invoke_wait_for_retry_account(job, target, net_fail, attempt_gen=attempt_gen)
            if _live_account_key() != old_scope:
                backup_folder_id = None
            final_attempt = 0
    job.log(f"[Done] Đã lưu thành phẩm {final_video_name} vào thư mục đích trên Cloud!")
    telemetry.record_completion(
        final_bytes=final_size,
        segments_total=total_segments,
        segments_done=max(0, total_segments - len(skipped_segment_indices)),
        segments_skipped=len(skipped_segment_indices),
        parts_count=len(completed_parts),
        output_name=final_video_name,
        space_saved=False,
    )

    # 6. Cleanup backup folder if auto_clean is enabled
    if auto_clean:
        try:
            is_src_clean = bool(is_backup_folder_input and not is_source_links)
            if not is_src_clean:
                await wait_for_retry_gate(job)
            clean_provider = src_provider if is_src_clean else _live_dst_provider()
            clean_creds = (source.get("credentials") or {}) if is_src_clean else _live_target_creds()
            clean_folder = target_folder if is_src_clean else _live_target_folder()
            clean_bf_id = src_backup_folder_id if is_src_clean else (await _resolve_backup_folder_id())
            job.log(f"[Cleanup] Tự động dọn dẹp thư mục phân mảnh tạm trên {'Cloud nguồn' if is_src_clean else 'Cloud đích'}: {backup_folder_name}...")
            target_base = str(clean_folder or "/").rstrip("/")
            del_path = (str(file_item.get("path") or "") if is_src_clean else "") or f"{target_base}/{backup_folder_name}"
            del_ref = {
                "id": clean_bf_id,
                "path": del_path,
                "name": backup_folder_name,
                "type": "folder",
            }
            if hasattr(clean_provider, "delete_item"):
                await clean_provider.delete_item(clean_creds, del_ref)
            elif hasattr(clean_provider, "delete_file"):
                await clean_provider.delete_file(clean_creds, del_ref)

            # If cross-folder/account resume also created a backup folder on target, clean it up too
            if is_src_clean and backup_folder_id and (backup_folder_id != src_backup_folder_id or _live_dst_provider() is not src_provider):
                try:
                    await wait_for_retry_gate(job)
                    dst_clean_prov = _live_dst_provider()
                    dst_clean_creds = _live_target_creds()
                    dst_base = str(_live_target_folder() or "/").rstrip("/")
                    dst_del_ref = {
                        "id": backup_folder_id,
                        "path": f"{dst_base}/{backup_folder_name}",
                        "name": backup_folder_name,
                        "type": "folder",
                    }
                    if hasattr(dst_clean_prov, "delete_item"):
                        await dst_clean_prov.delete_item(dst_clean_creds, dst_del_ref)
                    elif hasattr(dst_clean_prov, "delete_file"):
                        await dst_clean_prov.delete_file(dst_clean_creds, dst_del_ref)
                except Exception:
                    pass
        except Exception as e:
            job.log(f"[Cleanup] Ghi chú dọn dẹp: {e}")

    # Remove local temp workspace
    shutil.rmtree(job_work_dir, ignore_errors=True)
    return final_output_path


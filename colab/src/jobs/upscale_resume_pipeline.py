from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import re
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from ..providers import PROVIDERS
from ..providers.base import ProviderFailure, safe_name
from ..utils.ai_upscaler import (
    UPSCALED_VIDEO_SUFFIX,
    build_upscale_part_plan,
    build_upscaled_video_name,
    concat_upscaled_video_parts,
    normalize_model_preset_key,
    parse_upscale_options,
    target_short_pixels,
)
from .progress import JobState

try:
    from ..config import IS_KAGGLE
except Exception:
    IS_KAGGLE = False

UPSCALE_RESUME_SUFFIX = "__upscale_resume"


def is_upscale_resume_folder_name(name_or_path: str | Path | Any) -> bool:
    raw = str(name_or_path or "").strip().replace("\\", "/").rstrip("/")
    if not raw:
        return False
    base = raw.rsplit("/", 1)[-1]
    return base.lower().endswith(UPSCALE_RESUME_SUFFIX)


def generate_upscale_resume_folder_name(raw_name: str) -> str:
    clean = safe_name(re.sub(r'[,;:*?"<>|/\\`\s]+', "_", str(raw_name or "video")).strip("_"))
    base = clean
    if base.lower().endswith(UPSCALE_RESUME_SUFFIX):
        return base
    stem = Path(base).stem if "." in base else base
    stem = stem or "video"
    if stem.lower().endswith(UPSCALED_VIDEO_SUFFIX):
        stem = stem[: -len(UPSCALED_VIDEO_SUFFIX)] or stem
    return f"{stem}{UPSCALE_RESUME_SUFFIX}"


def _get_disk_free(dir_path: Path) -> int:
    try:
        cur = dir_path
        while not cur.exists() and cur.parent != cur:
            cur = cur.parent
        return shutil.disk_usage(cur).free
    except Exception:
        return 10 * 1024 * 1024 * 1024


def _is_local_part_valid(local_file: Path, expected_bytes: int | None = None) -> bool:
    if not local_file.is_file():
        return False
    sz = local_file.stat().st_size
    if sz < 1024:
        return False
    if expected_bytes and expected_bytes > 0:
        return sz == expected_bytes
    return True


def _part_sort_key(cp: dict[str, Any]) -> tuple[int, int]:
    try:
        sf = int(cp.get("start_frame") if cp.get("start_frame") is not None else -1)
    except (TypeError, ValueError):
        sf = -1
    try:
        pi = int(cp.get("part_index") or 0)
    except (TypeError, ValueError):
        pi = 0
    return (sf if sf >= 0 else pi * 1_000_000, pi)


def _normalize_upscale_completed_parts(
    parts: list[dict[str, Any]] | None,
    valid_remote_names: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    seen_indices: set[int] = set()
    seen_names: set[str] = set()
    normalized: list[dict[str, Any]] = []
    missing_names: list[str] = []

    for raw_cp in parts or []:
        if not isinstance(raw_cp, dict):
            continue
        fname = str(raw_cp.get("file_name") or "").strip()
        try:
            p_idx = int(raw_cp.get("part_index") or 0)
        except (TypeError, ValueError):
            p_idx = 0
        if p_idx <= 0 or not fname:
            continue
        if valid_remote_names is not None and fname not in valid_remote_names:
            missing_names.append(fname)
            continue
        if p_idx in seen_indices or fname in seen_names:
            continue
        seen_indices.add(p_idx)
        seen_names.add(fname)
        normalized.append(dict(raw_cp))

    normalized.sort(key=_part_sort_key)
    return normalized, missing_names


async def _resolve_nested_folder_id(
    provider: Any,
    creds: dict[str, Any],
    root_folder_id: str,
    rel_parts: list[str],
) -> str | None:
    if not hasattr(provider, "list_files"):
        return None
    cur_id = str(root_folder_id or "/").strip() or "/"
    if cur_id.startswith("id:"):
        cur_id = cur_id[3:].strip() or "/"
    clean_parts = [p for p in rel_parts if p and p not in (".", "..")]
    for part in clean_parts:
        try:
            listing = await provider.list_files(creds, cur_id)
        except Exception:
            return None
        items = (listing or {}).get("items") or (listing or {}).get("files") or []
        cand_norms = {part.lower(), safe_name(part, is_dir=True).lower(), safe_name(part).lower()}
        matched = None
        for it in items:
            if not isinstance(it, dict):
                continue
            is_dir = bool(it.get("type") == "folder" or it.get("is_folder") or it.get("isdir"))
            if not is_dir:
                continue
            raw_nm = str(it.get("name") or it.get("server_filename") or it.get("path") or "").strip()
            base_nm = PurePosixPath(raw_nm.replace("\\", "/")).name
            if base_nm.lower() in cand_norms or safe_name(base_nm, is_dir=True).lower() in cand_norms:
                matched = it
                break
        if matched is None:
            return None
        next_id = str(matched.get("id") or matched.get("path") or "").strip()
        if not next_id:
            return None
        if next_id.startswith("id:"):
            next_id = next_id[3:].strip()
        cur_id = next_id
    return cur_id


async def _find_and_load_best_upscale_manifest(
    provider: Any,
    creds: dict[str, Any],
    items: list[dict[str, Any]],
    work_dir: Path,
    job: JobState,
) -> dict[str, Any] | None:
    manifest_items = [
        it
        for it in items
        if isinstance(it, dict)
        and (
            str(it.get("name") or "") == "_resume_manifest.json"
            or (str(it.get("name") or "").startswith("_resume_manifest") and str(it.get("name") or "").endswith(".json"))
        )
    ]
    if not manifest_items:
        return None

    work_dir.mkdir(parents=True, exist_ok=True)
    parsed_candidates: list[tuple[int, dict[str, Any], dict[str, Any]]] = []

    for idx, m_item in enumerate(manifest_items):
        cand_dir = work_dir / f"_up_m_cand_{idx}"
        cand_dir.mkdir(parents=True, exist_ok=True)
        try:
            await provider.download_file(creds, m_item, cand_dir, job)
            dl_files = list(cand_dir.glob("*.json"))
            for actual_dl in dl_files:
                if actual_dl.is_file():
                    data = json.loads(actual_dl.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and data.get("pipeline") == "upscale_part_resume":
                        cps = data.get("completed_parts") or []
                        tot_p = int(data.get("total_parts") or 0)
                        done_flag = 1 if (data.get("is_complete") and (tot_p <= 0 or len(cps) >= tot_p)) else 0
                        synced_frames = sum(int(cp.get("frame_count") or 0) for cp in cps if isinstance(cp, dict))
                        score = (done_flag * 1_000_000_000) + (len(cps) * 1_000_000) + synced_frames
                        parsed_candidates.append((score, data, m_item))
        except Exception as err:
            job.log(f"[UPSCALE-RESUME] Lưu ý khi đọc manifest ({m_item.get('name')}): {err}")
        finally:
            shutil.rmtree(cand_dir, ignore_errors=True)

    if not parsed_candidates:
        return None

    parsed_candidates.sort(key=lambda t: t[0], reverse=True)
    _, best_data, best_item = parsed_candidates[0]

    # Merge any disjoint verified part files recorded in sibling duplicate manifests (Google Drive edge case)
    actual_part_files = {
        str(it.get("name") or "")
        for it in items
        if isinstance(it, dict)
        and str(it.get("name") or "").startswith("part_")
        and str(it.get("name") or "").endswith(".mp4")
        and int(it.get("size") or it.get("bytes") or 1) > 0
    }
    if actual_part_files and len(parsed_candidates) > 1:
        base_parts = list(best_data.get("completed_parts") or [])
        known_indices = {int(cp.get("part_index") or 0) for cp in base_parts if isinstance(cp, dict)}
        recovered = 0
        for _, cand_data, _ in parsed_candidates[1:]:
            for cp in cand_data.get("completed_parts") or []:
                if not isinstance(cp, dict):
                    continue
                p_idx = int(cp.get("part_index") or 0)
                fname = str(cp.get("file_name") or "")
                if p_idx > 0 and p_idx not in known_indices and fname in actual_part_files:
                    base_parts.append(dict(cp))
                    known_indices.add(p_idx)
                    recovered += 1
        if recovered > 0:
            base_parts.sort(key=_part_sort_key)
            best_data["completed_parts"] = base_parts
            job.log(f"[UPSCALE-RESUME] Khôi phục thêm {recovered} part từ bản ghi manifest song song.")

    if len(manifest_items) > 1:
        best_id = str(best_item.get("id") or best_item.get("path") or "")
        for m_it in manifest_items:
            m_id = str(m_it.get("id") or m_it.get("path") or "")
            if best_id and m_id and m_id != best_id:
                try:
                    if hasattr(provider, "delete_item"):
                        await provider.delete_item(creds, m_it)
                    elif hasattr(provider, "delete_file"):
                        await provider.delete_file(creds, m_it)
                except Exception:
                    pass

    return best_data


def _is_manifest_config_compatible(manifest: dict[str, Any], cfg: dict[str, Any], target_resolution: str) -> tuple[bool, str]:
    """Verify whether an existing resume manifest matches the user's current target resolution & model settings."""
    prev_res = str(manifest.get("target_resolution") or "").strip()
    prev_t_short = target_short_pixels(prev_res)
    cur_t_short = target_short_pixels(target_resolution)
    if prev_t_short != cur_t_short:
        return False, f"Target thay đổi ({prev_res or 'cũ'} -> {target_resolution})"

    cur_norm_preset = normalize_model_preset_key(cfg.get("model_preset") or "auto")
    prev_selected_model = str(manifest.get("selected_model") or "").strip()
    if cur_norm_preset != "auto" and prev_selected_model and cur_norm_preset != prev_selected_model:
        return False, f"Model thay đổi ({prev_selected_model} -> {cur_norm_preset})"

    prev_start = str(manifest.get("start_time") or "00:00:00").strip() or "00:00:00"
    cur_start = str(cfg.get("start_time") or "00:00:00").strip() or "00:00:00"
    prev_prev_sec = float(manifest.get("preview_seconds") or 0.0)
    cur_prev_sec = float(cfg.get("preview_seconds") or 0.0)
    if prev_start != cur_start or abs(prev_prev_sec - cur_prev_sec) > 0.5:
        return False, "Khung cắt thời gian (Start Time / Preview) thay đổi"

    return True, ""


class UpscalePartResumeController:
    """
    Thread-safe bridge between the synchronous GPU video upscaler thread (`process_single_video`)
    and the async Cloud provider event loop (`Google Drive` / `PikPak` / `TeraBox`):
    - Checks Target Cloud for `<stem>__upscale_resume` + `_resume_manifest.json`
    - Skips already-uploaded `part_XXXX.mp4` slices on resume (or starts from Part 1 if none/incompatible)
    - Pipelines background uploads of finished parts while GPU immediately upscales the next part
    - Downloads any missing earlier-session parts before lossless FFmpeg final assembly
    - Defers deleting `<stem>__upscale_resume` on Cloud until AFTER final `<stem>-upscaled.mp4` is uploaded
    """

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        job: JobState,
        source: dict[str, Any],
        target: dict[str, Any],
        options: dict[str, Any],
        dst_provider: Any,
        work_root: Path,
        rel_dir_posix: str = "",
        source_item: dict[str, Any] | None = None,
        src_resume_provider: Any = None,
        src_resume_creds: dict[str, Any] | None = None,
        src_resume_folder_id: str | None = None,
    ) -> None:
        self.loop = loop
        self.job = job
        self.source = source
        self.target = target
        self.options = options
        self.initial_dst_prov_name = str(target.get("provider") or "").lower()
        self.dst_provider = dst_provider
        self.work_root = work_root
        self.rel_dir_posix = str(rel_dir_posix or "").replace("\\", "/").strip("/")
        if self.rel_dir_posix == ".":
            self.rel_dir_posix = ""
        self.source_item = source_item or {}
        self.src_resume_provider = src_resume_provider
        self.src_resume_creds = src_resume_creds or {}
        self.src_resume_folder_id = src_resume_folder_id

        self.resume_folder_name = ""
        self.resume_rel_dir = ""
        self.local_workspace = work_root
        self.parts_local_dir = work_root / "parts"
        self.enabled = False
        self.auto_clean_parts = True
        self.space_saved = False

        self._resume_folder_id: str | None = None
        self._resume_folder_scope: tuple[str, str, str] | None = None
        self._manifest_lock = asyncio.Lock()
        self._upload_sem = asyncio.Semaphore(2)
        self._upload_futures: list[concurrent.futures.Future[Any]] = []

        self.parts_plan: list[dict[str, Any]] = []
        self.completed_parts: list[dict[str, Any]] = []
        self.remote_part_map: dict[str, dict[str, Any]] = {}
        self.src_remote_part_map: dict[str, dict[str, Any]] = {}
        self.existing_remote_names: set[str] = set()
        self.manifest_meta: dict[str, Any] = {}
        self.keep_local_parts = True
        self.estimated_total_bytes = 0

    def _run_sync(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def _live_target_creds(self) -> dict[str, Any]:
        live_t = (
            (self.job.payload.get("target") if isinstance(getattr(self.job, "payload", None), dict) else None)
            or self.target
            or {}
        )
        creds = live_t.get("credentials")
        return creds if isinstance(creds, dict) else (self.target.get("credentials") or {})

    def _live_target_folder(self) -> str:
        live_t = (
            (self.job.payload.get("target") if isinstance(getattr(self.job, "payload", None), dict) else None)
            or self.target
            or {}
        )
        folder = live_t.get("folder") or self.target.get("folder") or {}
        raw_f = str(folder.get("id") or folder.get("path") or "/").strip()
        if raw_f.startswith("id:"):
            raw_f = raw_f[3:].strip()
        return raw_f or "/"

    def _live_dst_provider(self) -> Any:
        live_t = (
            (self.job.payload.get("target") if isinstance(getattr(self.job, "payload", None), dict) else None)
            or self.target
            or {}
        )
        prov_key = str(live_t.get("provider") or self.target.get("provider") or "").lower()
        if prov_key and prov_key != self.initial_dst_prov_name:
            return PROVIDERS.get(prov_key, self.dst_provider)
        return self.dst_provider or PROVIDERS.get(prov_key)

    def _live_scope(self) -> tuple[str, str, str]:
        creds = self._live_target_creds()
        live_t = (
            (self.job.payload.get("target") if isinstance(getattr(self.job, "payload", None), dict) else None)
            or self.target
            or {}
        )
        acc = str(
            live_t.get("accountId")
            or live_t.get("account_id")
            or creds.get("account_id")
            or creds.get("accountId")
            or ""
        )
        return (str(live_t.get("provider") or "").lower(), acc, self._live_target_folder())

    def _rel_parts_to_resume_dir(self) -> list[str]:
        segs = [p for p in self.rel_dir_posix.split("/") if p and p not in (".", "..")]
        segs.append(self.resume_folder_name)
        return segs

    async def _resolve_resume_folder_id(self) -> str | None:
        cur_scope = self._live_scope()
        if self._resume_folder_scope != cur_scope:
            self._resume_folder_id = None
            self._resume_folder_scope = cur_scope
        if self._resume_folder_id:
            return self._resume_folder_id
        from .transfer_job import wait_for_retry_gate
        await wait_for_retry_gate(self.job)
        fid = await _resolve_nested_folder_id(
            self._live_dst_provider(),
            self._live_target_creds(),
            self._live_target_folder(),
            self._rel_parts_to_resume_dir(),
        )
        if fid:
            self._resume_folder_id = fid
            self._resume_folder_scope = cur_scope
        return fid

    async def _clean_duplicate_manifests(self, items_to_check: list[dict[str, Any]] | None = None, keep_id: str | None = None) -> None:
        rf_id = await self._resolve_resume_folder_id()
        if not rf_id:
            return
        try:
            live_dst = self._live_dst_provider()
            live_creds = self._live_target_creds()
            cand_items = items_to_check
            if cand_items is None and hasattr(live_dst, "list_files"):
                listing = await live_dst.list_files(live_creds, rf_id)
                cand_items = (listing or {}).get("items") or []
            for it in cand_items or []:
                fname = str(it.get("name") or "")
                fid = it.get("id") or it.get("path")
                if fid and keep_id and str(fid) == str(keep_id):
                    continue
                is_dup = (
                    (fname.startswith("_resume_manifest") and fname.endswith(".json") and fname != "_resume_manifest.json")
                    or (keep_id and str(fid) != str(keep_id) and fname == "_resume_manifest.json")
                )
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

    async def _delete_existing_resume_folder_on_dst(self) -> None:
        rf_id = await self._resolve_resume_folder_id()
        if not rf_id:
            return
        live_dst = self._live_dst_provider()
        live_creds = self._live_target_creds()
        base_f = str(self._live_target_folder() or "/").rstrip("/")
        del_path = f"{base_f}/{self.resume_rel_dir}".replace("//", "/")
        del_ref = {
            "id": rf_id,
            "path": del_path,
            "name": self.resume_folder_name,
            "type": "folder",
        }
        try:
            if hasattr(live_dst, "delete_item"):
                await live_dst.delete_item(live_creds, del_ref)
            elif hasattr(live_dst, "delete_file"):
                await live_dst.delete_file(live_creds, del_ref)
        except Exception as exc:
            self.job.log(f"[UPSCALE-RESUME] Ghi chú khi dọn folder cũ '{self.resume_folder_name}': {exc}")
        self._resume_folder_id = None

    async def _upload_manifest_async(self) -> None:
        async with self._manifest_lock:
            total_parts = len(self.parts_plan)
            completed_sorted = sorted(self.completed_parts, key=_part_sort_key)
            is_done = total_parts > 0 and len(completed_sorted) >= total_parts
            synced_frames = sum(int(cp.get("frame_count") or 0) for cp in completed_sorted)

            safe_src_item = {
                k: v
                for k, v in (self.source_item or {}).items()
                if k not in ("credentials", "_prefetched_listing")
            }
            manifest = {
                "version": 1,
                "pipeline": "upscale_part_resume",
                "upscale_strategy": "part_resume",
                **self.manifest_meta,
                "total_parts": total_parts,
                "parts_plan": self.parts_plan,
                "completed_parts": completed_sorted,
                "synced_frames": synced_frames,
                "is_complete": is_done,
                "all_parts_uploaded": is_done,
                "updated_at": time.time(),
                "source_provider": self.source.get("provider"),
                "source_account_id": self.source.get("accountId") or self.source.get("account_id"),
                "source_item": safe_src_item,
            }

            manifest_file = self.local_workspace / "_resume_manifest.json"
            from .transfer_job import _invoke_wait_for_retry_account, wait_for_retry_gate

            for attempt in range(2):
                attempt_gen = await wait_for_retry_gate(self.job)
                self.local_workspace.mkdir(parents=True, exist_ok=True)
                manifest_file.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

                live_dst = self._live_dst_provider()
                live_creds = self._live_target_creds()
                live_folder = self._live_target_folder()

                rf_id = await self._resolve_resume_folder_id()
                existing_manifests: list[dict[str, Any]] = []
                if rf_id and hasattr(live_dst, "list_files"):
                    try:
                        listing = await live_dst.list_files(live_creds, rf_id)
                        for it in (listing or {}).get("items") or []:
                            fn = str(it.get("name") or "")
                            if fn == "_resume_manifest.json" or (fn.startswith("_resume_manifest") and fn.endswith(".json")):
                                existing_manifests.append(it)
                    except Exception:
                        pass

                exact_m = next((it for it in existing_manifests if str(it.get("name") or "") == "_resume_manifest.json"), None)
                target_m = exact_m or (existing_manifests[0] if existing_manifests else None)

                if "options" not in self.job.payload:
                    self.job.payload["options"] = {}
                rep_depth = int(getattr(self.job, "_manifest_replace_depth", 0))
                if rep_depth == 0:
                    self.job._manifest_orig_replace = self.job.payload["options"].get("replace")  # type: ignore[attr-defined]
                self.job._manifest_replace_depth = rep_depth + 1  # type: ignore[attr-defined]
                self.job.payload["options"]["replace"] = True

                replaced = False
                retry_manifest = False
                try:
                    if target_m and hasattr(live_dst, "replace_file"):
                        try:
                            await live_dst.replace_file(live_creds, manifest_file, target_m, self.job)
                            replaced = True
                        except Exception:
                            replaced = False

                    if not replaced:
                        manifest_ref = {
                            "id": live_folder,
                            "path": live_folder,
                            "relative_path": f"{self.resume_rel_dir}/_resume_manifest.json",
                        }
                        up_res = await live_dst.upload_file(live_creds, manifest_file, manifest_ref, self.job)
                        up_id = (up_res.get("id") or up_res.get("path")) if isinstance(up_res, dict) else None
                        await self._clean_duplicate_manifests(existing_manifests, keep_id=up_id)
                except ProviderFailure as pf_exc:
                    if attempt == 0 and pf_exc.code in ("INVALID_PROVIDER_CREDENTIALS", "PROVIDER_TOKEN_EXPIRED", "UPLOAD_FAILED"):
                        await _invoke_wait_for_retry_account(self.job, self.target, pf_exc, attempt_gen=attempt_gen)
                        retry_manifest = True
                    else:
                        self.job.log(f"[UPSCALE-RESUME] Cảnh báo ghi manifest: {pf_exc}")
                except Exception as exc:
                    self.job.log(f"[UPSCALE-RESUME] Cảnh báo ghi manifest: {exc}")
                finally:
                    new_depth = max(0, int(getattr(self.job, "_manifest_replace_depth", 1)) - 1)
                    self.job._manifest_replace_depth = new_depth  # type: ignore[attr-defined]
                    if new_depth == 0:
                        saved_rep = getattr(self.job, "_manifest_orig_replace", None)
                        if saved_rep is None:
                            self.job.payload["options"].pop("replace", None)
                        else:
                            self.job.payload["options"]["replace"] = saved_rep
                    manifest_file.unlink(missing_ok=True)

                if not retry_manifest:
                    break

    async def _prepare_plan_async(
        self,
        *,
        cur_input: Path,
        out_filename: str,
        orig_in_w: int,
        orig_in_h: int,
        in_w: int,
        in_h: int,
        did_pre_downscale: bool,
        selected_model: str,
        eff_denoise: float,
        target_resolution: str,
        fps_str: str,
        fps_val: float,
        base_start_sec: float,
        eff_dur: float,
        est_frames: int,
        cfg: dict[str, Any],
    ) -> dict[str, Any]:
        self.auto_clean_parts = bool(cfg.get("upscale_auto_clean_parts", True))
        resume_check = bool(cfg.get("upscale_resume_check", True))
        part_minutes = float(cfg.get("upscale_part_minutes") or 3.0)

        self.resume_folder_name = generate_upscale_resume_folder_name(cur_input.name)
        self.resume_rel_dir = (
            f"{self.rel_dir_posix}/{self.resume_folder_name}".strip("/")
            if self.rel_dir_posix
            else self.resume_folder_name
        )
        self.local_workspace = self.work_root / f"up_resume_{safe_name(self.resume_folder_name)}"
        self.parts_local_dir = self.local_workspace / "parts"
        self.parts_local_dir.mkdir(parents=True, exist_ok=True)

        existing_manifest: dict[str, Any] | None = None
        remote_items_found: list[dict[str, Any]] = []

        # 1. Check explicit source resume folder first (if user picked `*__upscale_resume` folder directly)
        if self.src_resume_folder_id and self.src_resume_provider and hasattr(self.src_resume_provider, "list_files"):
            try:
                src_list = await self.src_resume_provider.list_files(self.src_resume_creds, self.src_resume_folder_id)
                s_items = (src_list or {}).get("items") or []
                for it in s_items:
                    nm = str(it.get("name") or "")
                    if nm.startswith("part_") and nm.endswith(".mp4") and int(it.get("size") or it.get("bytes") or 1) > 0:
                        self.existing_remote_names.add(nm)
                        self.src_remote_part_map[nm] = it
                        self.remote_part_map[nm] = it
                existing_manifest = await _find_and_load_best_upscale_manifest(
                    self.src_resume_provider, self.src_resume_creds, s_items, self.local_workspace, self.job
                )
            except Exception as err:
                self.job.log(f"[UPSCALE-RESUME] Lưu ý đọc folder resume nguồn: {err}")

        # 2. Check destination Cloud resume folder `<target>/<rel>/<stem>__upscale_resume`
        live_dst = self._live_dst_provider()
        if hasattr(live_dst, "list_files"):
            rf_id = await self._resolve_resume_folder_id()
            if rf_id:
                if not resume_check and existing_manifest is None:
                    self.job.log(
                        f"[UPSCALE-RESUME] Tùy chọn Resume đang TẮT -> Dọn dẹp folder tạm cũ '{self.resume_folder_name}' và chạy lại từ đầu."
                    )
                    await self._delete_existing_resume_folder_on_dst()
                else:
                    try:
                        dst_list = await live_dst.list_files(self._live_target_creds(), rf_id)
                        remote_items_found = (dst_list or {}).get("items") or []
                        for it in remote_items_found:
                            nm = str(it.get("name") or "")
                            if nm.startswith("part_") and nm.endswith(".mp4") and int(it.get("size") or it.get("bytes") or 1) > 0:
                                self.existing_remote_names.add(nm)
                                self.remote_part_map[nm] = it
                        if existing_manifest is None:
                            existing_manifest = await _find_and_load_best_upscale_manifest(
                                live_dst, self._live_target_creds(), remote_items_found, self.local_workspace, self.job
                            )
                    except Exception as err:
                        self.job.log(f"[UPSCALE-RESUME] Lưu ý kiểm tra folder resume trên Cloud: {err}")

        # 3. Validate compatibility if an existing manifest was found
        if existing_manifest is not None and resume_check:
            compat_ok, compat_reason = _is_manifest_config_compatible(existing_manifest, cfg, target_resolution)
            if not compat_ok:
                self.job.log(
                    f"[UPSCALE-RESUME] Phát hiện folder '{self.resume_folder_name}' nhưng cấu hình đã đổi ({compat_reason}) -> Làm mới và chạy lại từ Part 1."
                )
                await self._delete_existing_resume_folder_on_dst()
                existing_manifest = None
                self.existing_remote_names.clear()
                self.remote_part_map.clear()

        if existing_manifest is not None and resume_check:
            saved_plan = existing_manifest.get("parts_plan") or []
            if isinstance(saved_plan, list) and len(saved_plan) >= 1:
                self.parts_plan = saved_plan
            else:
                saved_pm = float(existing_manifest.get("part_minutes") or part_minutes)
                self.parts_plan = build_upscale_part_plan(est_frames, fps_val, saved_pm, base_start_sec, eff_dur)

            valid_parts, missing_remote = _normalize_upscale_completed_parts(
                existing_manifest.get("completed_parts") or [],
                valid_remote_names=self.existing_remote_names if (remote_items_found or self.src_remote_part_map) else None,
            )
            if missing_remote:
                self.job.log(
                    f"[UPSCALE-RESUME] Có {len(missing_remote)} part trong manifest không còn trên Cloud ({', '.join(missing_remote[:4])}) -> Sẽ AI upscale bù."
                )
            self.completed_parts = valid_parts

            locked_in_w = int(existing_manifest.get("eff_in_w") or in_w)
            locked_in_h = int(existing_manifest.get("eff_in_h") or in_h)
            locked_pre_dn = bool(existing_manifest.get("did_pre_downscale", did_pre_downscale))
            locked_model = str(existing_manifest.get("selected_model") or selected_model)
            locked_denoise = float(existing_manifest.get("eff_denoise") if existing_manifest.get("eff_denoise") is not None else eff_denoise)
            tot_frames_plan = sum(int(p.get("frame_count") or 0) for p in self.parts_plan) or est_frames

            done_idx_set = {int(cp["part_index"]) for cp in self.completed_parts}
            remaining_parts = [p for p in self.parts_plan if int(p["part_index"]) not in done_idx_set]
            pre_synced_frames = sum(int(cp.get("frame_count") or 0) for cp in self.completed_parts)

            self.manifest_meta = {
                "target_filename": out_filename,
                "source_filename": cur_input.name,
                "orig_in_w": orig_in_w,
                "orig_in_h": orig_in_h,
                "eff_in_w": locked_in_w,
                "eff_in_h": locked_in_h,
                "did_pre_downscale": locked_pre_dn,
                "selected_model": locked_model,
                "eff_denoise": locked_denoise,
                "target_resolution": target_resolution,
                "fps_str": fps_str,
                "fps_val": fps_val,
                "start_time": cfg.get("start_time", "00:00:00"),
                "preview_seconds": float(cfg.get("preview_seconds") or 0.0),
                "part_minutes": float(existing_manifest.get("part_minutes") or part_minutes),
                "total_frames": tot_frames_plan,
            }
            self.enabled = True
            if remaining_parts:
                next_p = remaining_parts[0]["part_index"]
                self.job.log(
                    f"[UPSCALE-RESUME] Khôi phục từ '{self.resume_folder_name}': Đã có {len(self.completed_parts)}/{len(self.parts_plan)} parts "
                    f"({pre_synced_frames}/{tot_frames_plan} frames) -> Tiếp tục từ Part {next_p}!"
                )
            else:
                self.job.log(
                    f"[UPSCALE-RESUME] Đã có đủ 100% ({len(self.completed_parts)}/{len(self.parts_plan)} parts) trên Cloud -> Chuyển thẳng sang ghép file MP4 đích!"
                )
            return {
                "enabled": True,
                "locked_in_w": locked_in_w,
                "locked_in_h": locked_in_h,
                "locked_pre_downscale": locked_pre_dn,
                "locked_model": locked_model,
                "locked_denoise": locked_denoise,
                "total_frames": tot_frames_plan,
                "parts_plan": self.parts_plan,
                "remaining_parts": remaining_parts,
                "pre_synced_frames": pre_synced_frames,
                "parts_local_dir": self.parts_local_dir,
            }

        # 4. Fresh start: build deterministic part plan
        self.parts_plan = build_upscale_part_plan(est_frames, fps_val, part_minutes, base_start_sec, eff_dur)
        if len(self.parts_plan) <= 1:
            # Video is shorter than 1 part window and has no existing resume folder -> run fast direct 1-pass
            self.enabled = False
            shutil.rmtree(self.local_workspace, ignore_errors=True)
            return {"enabled": False}

        tot_frames_plan = sum(int(p.get("frame_count") or 0) for p in self.parts_plan) or est_frames
        self.manifest_meta = {
            "target_filename": out_filename,
            "source_filename": cur_input.name,
            "orig_in_w": orig_in_w,
            "orig_in_h": orig_in_h,
            "eff_in_w": in_w,
            "eff_in_h": in_h,
            "did_pre_downscale": did_pre_downscale,
            "selected_model": selected_model,
            "eff_denoise": eff_denoise,
            "target_resolution": target_resolution,
            "fps_str": fps_str,
            "fps_val": fps_val,
            "start_time": cfg.get("start_time", "00:00:00"),
            "preview_seconds": float(cfg.get("preview_seconds") or 0.0),
            "part_minutes": part_minutes,
            "total_frames": tot_frames_plan,
        }
        self.enabled = True
        self.job.log(
            f"[UPSCALE-RESUME] Chưa có folder '{self.resume_folder_name}' trên Cloud -> Khởi tạo mới {len(self.parts_plan)} parts "
            f"(~{part_minutes:g} phút/part) và lưu tiến trình liên tục."
        )
        await self._upload_manifest_async()
        return {
            "enabled": True,
            "locked_in_w": in_w,
            "locked_in_h": in_h,
            "locked_pre_downscale": did_pre_downscale,
            "locked_model": selected_model,
            "locked_denoise": eff_denoise,
            "total_frames": tot_frames_plan,
            "parts_plan": self.parts_plan,
            "remaining_parts": list(self.parts_plan),
            "pre_synced_frames": 0,
            "parts_local_dir": self.parts_local_dir,
        }

    def prepare_plan(self, **kwargs: Any) -> dict[str, Any]:
        return self._run_sync(self._prepare_plan_async(**kwargs))

    async def _upload_part_and_manifest_async(
        self,
        part_file: Path,
        part_spec: dict[str, Any],
        actual_frames: int,
    ) -> None:
        from .transfer_job import _invoke_wait_for_retry_account, wait_for_retry_gate

        p_name = part_file.name
        p_idx = int(part_spec["part_index"])
        p_bytes = part_file.stat().st_size
        size_mb = p_bytes / (1024 * 1024)
        tot_p = len(self.parts_plan)

        max_retries = 3
        attempt = 0
        auto_refreshed_once = False
        res: Any = None
        while attempt < max_retries:
            attempt += 1
            attempt_gen = await wait_for_retry_gate(self.job)
            try:
                async with self._upload_sem:
                    live_dst = self._live_dst_provider()
                    live_creds = self._live_target_creds()
                    live_folder = self._live_target_folder()
                    target_ref = {
                        "id": live_folder,
                        "path": live_folder,
                        "relative_path": f"{self.resume_rel_dir}/{p_name}",
                    }
                    res = await live_dst.upload_file(live_creds, part_file, target_ref, self.job)
                if not getattr(self.job, "_waiting_for_account", False):
                    self.job.error = None
                break
            except ProviderFailure as up_err:
                err_txt = str(up_err).lower()
                is_auth = (
                    up_err.code in ("INVALID_PROVIDER_CREDENTIALS", "PROVIDER_TOKEN_EXPIRED")
                    or "session expired" in err_txt
                    or "revoked" in err_txt
                    or "relogin" in err_txt
                )
                if not (is_auth or up_err.code == "UPLOAD_FAILED"):
                    raise
                if is_auth or attempt >= max_retries:
                    live_dst = self._live_dst_provider()
                    refreshed = False
                    if not is_auth and not auto_refreshed_once and hasattr(live_dst, "_refresh_web_session"):
                        auto_refreshed_once = True
                        try:
                            refreshed = await live_dst._refresh_web_session(self._live_target_creds())
                        except Exception:
                            refreshed = False
                    if refreshed:
                        attempt = max(0, max_retries - 1)
                        continue
                    old_scope = self._live_scope()
                    await _invoke_wait_for_retry_account(self.job, self.target, up_err, attempt_gen=attempt_gen)
                    if self._live_scope() != old_scope:
                        self._resume_folder_id = None
                    attempt = 0
                    auto_refreshed_once = False
                    continue
                await asyncio.sleep(min(2 ** (attempt - 1), 6))
            except (httpx.HTTPError, OSError) as net_err:
                if attempt >= max_retries:
                    net_pf = ProviderFailure("UPLOAD_FAILED", f"Network error uploading {p_name}: {net_err}")
                    old_scope = self._live_scope()
                    await _invoke_wait_for_retry_account(self.job, self.target, net_pf, attempt_gen=attempt_gen)
                    if self._live_scope() != old_scope:
                        self._resume_folder_id = None
                    attempt = 0
                    continue
                await asyncio.sleep(min(2 ** (attempt - 1), 6))

        cp_entry = {
            "part_index": p_idx,
            "file_name": p_name,
            "start_frame": int(part_spec.get("start_frame") or 0),
            "end_frame": int(part_spec.get("end_frame") or actual_frames),
            "frame_count": int(actual_frames or part_spec.get("frame_count") or 0),
            "start_sec": float(part_spec.get("start_sec") or 0.0),
            "duration_sec": float(part_spec.get("duration_sec") or 0.0),
            "bytes": p_bytes,
        }
        async with self._manifest_lock:
            self.completed_parts = [cp for cp in self.completed_parts if int(cp.get("part_index") or 0) != p_idx]
            self.completed_parts.append(cp_entry)
            self.completed_parts.sort(key=_part_sort_key)
            self.existing_remote_names.add(p_name)
            if isinstance(res, dict):
                self.remote_part_map[p_name] = res

        # Estimate total bytes across all parts to decide whether local part copy can stay cached for fast merge
        done_cnt = max(1, len(self.completed_parts))
        known_b = sum(int(cp.get("bytes") or 0) for cp in self.completed_parts)
        self.estimated_total_bytes = int((known_b / done_cnt) * max(1, tot_p))
        free_disk = _get_disk_free(self.parts_local_dir)
        safe_keep = (
            self.keep_local_parts
            and not IS_KAGGLE
            and free_disk > max(3 * 1024 * 1024 * 1024, int(self.estimated_total_bytes * 2.2))
        )
        if not safe_keep:
            self.keep_local_parts = False
            for old_p in list(self.parts_local_dir.glob("part_*.mp4")):
                if old_p.name in self.existing_remote_names:
                    old_p.unlink(missing_ok=True)
            self.job.log(
                f"[UPSCALE-RESUME] Đã sao lưu Part {p_idx}/{tot_p} ({p_name}, {size_mb:.1f} MB) lên Cloud & giải phóng bản tạm local."
            )
        else:
            self.job.log(
                f"[UPSCALE-RESUME] Đã sao lưu Part {p_idx}/{tot_p} ({p_name}, {size_mb:.1f} MB) lên Cloud."
            )

        await self._upload_manifest_async()

    def _reap_completed_uploads(self) -> None:
        still_running: list[concurrent.futures.Future[Any]] = []
        for fut in self._upload_futures:
            if fut.done():
                exc = fut.exception()
                if exc is not None:
                    raise exc
            else:
                still_running.append(fut)
        self._upload_futures = still_running

    def on_part_completed(self, part_file: Path, part_spec: dict[str, Any], actual_frames: int) -> None:
        self._reap_completed_uploads()
        max_inflight = 1 if getattr(self.job, "_waiting_for_account", False) else 2
        while len(self._upload_futures) >= max_inflight:
            done_futs, _ = concurrent.futures.wait(
                self._upload_futures, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for df in done_futs:
                exc = df.exception()
                if exc is not None:
                    raise exc
            self._upload_futures = [f for f in self._upload_futures if not f.done()]
            max_inflight = 1 if getattr(self.job, "_waiting_for_account", False) else 2

        fut = asyncio.run_coroutine_threadsafe(
            self._upload_part_and_manifest_async(part_file, part_spec, actual_frames),
            self.loop,
        )
        self._upload_futures.append(fut)

    async def _finalize_parts_async(self) -> tuple[list[Path], bool]:
        self.completed_parts, _ = _normalize_upscale_completed_parts(self.completed_parts)
        tot_p = len(self.parts_plan)
        if len(self.completed_parts) < tot_p:
            raise ProviderFailure(
                "UPLOAD_FAILED",
                f"Chưa đủ số part để ghép nối ({len(self.completed_parts)}/{tot_p} parts).",
            )

        missing_cps: list[dict[str, Any]] = []
        for cp in self.completed_parts:
            p_name = str(cp["file_name"])
            local_p = self.parts_local_dir / p_name
            if not _is_local_part_valid(local_p, cp.get("bytes")):
                missing_cps.append(cp)

        total_parts_bytes = sum(int(cp.get("bytes") or 0) for cp in self.completed_parts)
        missing_bytes = sum(int(cp.get("bytes") or 0) for cp in missing_cps)
        free_disk = _get_disk_free(self.local_workspace)

        # Space-Saver protection if free disk cannot hold downloaded parts + concatenated final MP4
        if (missing_bytes + total_parts_bytes + 512 * 1024 * 1024) > free_disk:
            self.space_saved = True
            await self._upload_manifest_async()
            self.job.log(
                f"[UPSCALE-RESUME] [Space-Saver] Đĩa tạm Colab còn {free_disk // (1024 * 1024)} MB không đủ ghép file tổng "
                f"(~{total_parts_bytes // (1024 * 1024)} MB). Đã giữ nguyên trọn bộ {tot_p} parts an toàn tại '{self.resume_folder_name}'!"
            )
            return [], True

        if missing_cps:
            self.job.log(
                f"[UPSCALE-RESUME] Tải lại {len(missing_cps)}/{tot_p} part từ phiên trước trên Cloud để ghép file MP4 hoàn chỉnh..."
            )
            live_dst = self._live_dst_provider()
            if hasattr(live_dst, "list_files"):
                rf_id = await self._resolve_resume_folder_id()
                if rf_id:
                    try:
                        listing = await live_dst.list_files(self._live_target_creds(), rf_id)
                        for it in (listing or {}).get("items") or []:
                            fn = str(it.get("name") or "")
                            if fn.startswith("part_") and fn.endswith(".mp4"):
                                self.remote_part_map[fn] = it
                    except Exception:
                        pass

            from .transfer_job import wait_for_retry_gate
            for cp in missing_cps:
                p_name = str(cp["file_name"])
                local_p = self.parts_local_dir / p_name
                use_src = bool(p_name in self.src_remote_part_map and p_name not in self.remote_part_map)
                part_ref = (
                    (self.src_remote_part_map.get(p_name) if use_src else self.remote_part_map.get(p_name))
                    or {
                        "name": p_name,
                        "path": f"{self.resume_rel_dir}/{p_name}",
                        "relative_path": f"{self.resume_rel_dir}/{p_name}",
                    }
                )
                downloaded_ok = False
                for dl_try in range(4):
                    try:
                        if use_src and self.src_resume_provider:
                            await self.src_resume_provider.download_file(self.src_resume_creds, part_ref, self.parts_local_dir, self.job)
                        else:
                            await wait_for_retry_gate(self.job)
                            await self._live_dst_provider().download_file(
                                self._live_target_creds(), part_ref, self.parts_local_dir, self.job
                            )
                        if not local_p.is_file():
                            cands = list(self.parts_local_dir.glob(f"*{p_name}*"))
                            if cands:
                                cands[0].rename(local_p)
                        if local_p.is_file() and local_p.stat().st_size >= 1024:
                            downloaded_ok = True
                            break
                    except Exception as dl_err:
                        self.job.log(f"[UPSCALE-RESUME] Thử tải lại {p_name} ({dl_try + 1}/4): {dl_err}")
                        await asyncio.sleep(1.5 * (dl_try + 1))
                if not downloaded_ok:
                    raise ProviderFailure("DOWNLOAD_FAILED", f"Không thể tải lại {p_name} từ Cloud để ghép video.")

        ordered_paths = [self.parts_local_dir / str(cp["file_name"]) for cp in self.completed_parts]
        return ordered_paths, False

    def finalize_parts(self) -> tuple[list[Path], bool]:
        for fut in list(self._upload_futures):
            fut.result()
        self._upload_futures.clear()
        return self._run_sync(self._finalize_parts_async())

    def cleanup_local_workspace(self) -> None:
        shutil.rmtree(self.local_workspace, ignore_errors=True)

    async def cleanup_remote_resume_folder(self) -> None:
        """Delete `<stem>__upscale_resume` on Cloud ONLY after the final `-upscaled.mp4` is uploaded."""
        if not self.enabled or not self.auto_clean_parts or self.space_saved:
            return
        from .transfer_job import wait_for_retry_gate
        try:
            await wait_for_retry_gate(self.job)
            await self._delete_existing_resume_folder_on_dst()
            if self.src_resume_folder_id and self.src_resume_provider:
                src_del_ref = {
                    "id": self.src_resume_folder_id,
                    "path": str(self.source_item.get("path") or self.resume_folder_name),
                    "name": self.resume_folder_name,
                    "type": "folder",
                }
                try:
                    if hasattr(self.src_resume_provider, "delete_item"):
                        await self.src_resume_provider.delete_item(self.src_resume_creds, src_del_ref)
                    elif hasattr(self.src_resume_provider, "delete_file"):
                        await self.src_resume_provider.delete_file(self.src_resume_creds, src_del_ref)
                except Exception:
                    pass
            self.job.log(f"[CLEANUP] Đã tự động dọn dẹp folder tạm '{self.resume_folder_name}' trên Cloud sau khi lưu MP4 hoàn chỉnh.")
        except Exception as err:
            self.job.log(f"[CLEANUP] Lưu ý khi dọn folder tạm '{self.resume_folder_name}': {err}")


async def inspect_upscale_resume_folder_input(
    job: JobState,
    work_dir: Path,
    source: dict[str, Any],
    src_provider: Any,
    folder_item: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]]]:
    """
    Inspect a `*__upscale_resume` folder selected directly by the user:
    Returns `(manifest_data, original_source_item_if_any, remote_items)`.
    """
    folder_id = str(folder_item.get("id") or folder_item.get("path") or "").strip()
    if folder_id.startswith("id:"):
        folder_id = folder_id[3:].strip()
    creds = folder_item.get("credentials") or source.get("credentials") or {}
    if not folder_id or not hasattr(src_provider, "list_files"):
        return None, None, []
    try:
        listing = await src_provider.list_files(creds, folder_id)
        items = (listing or {}).get("items") or []
        manifest = await _find_and_load_best_upscale_manifest(src_provider, creds, items, work_dir, job)
        if not manifest:
            return None, None, items
        src_item = manifest.get("source_item") if isinstance(manifest.get("source_item"), dict) else None
        return manifest, src_item, items
    except Exception as exc:
        job.log(f"[UPSCALE-RESUME] Lưu ý khi đọc folder resume '{folder_item.get('name')}': {exc}")
        return None, None, []


async def assemble_completed_upscale_resume_folder(
    job: JobState,
    work_dir: Path,
    output_dir: Path,
    src_provider: Any,
    src_creds: dict[str, Any],
    manifest: dict[str, Any],
    remote_items: list[dict[str, Any]],
    options: dict[str, Any],
) -> Path:
    """Assemble `<stem>-upscaled.mp4` directly from a 100%-complete `*__upscale_resume` folder without needing source video."""
    cfg = parse_upscale_options(options)
    parts_dir = work_dir / "parts_direct_assemble"
    parts_dir.mkdir(parents=True, exist_ok=True)
    remote_map = {
        str(it.get("name") or ""): it
        for it in remote_items
        if isinstance(it, dict) and str(it.get("name") or "").startswith("part_")
    }
    completed_parts, missing = _normalize_upscale_completed_parts(
        manifest.get("completed_parts") or [],
        valid_remote_names=set(remote_map.keys()) if remote_map else None,
    )
    if missing or not completed_parts:
        raise ProviderFailure("SOURCE_FILE_NOT_FOUND", f"Folder resume thiếu phân đoạn: {', '.join(missing[:5])}")

    local_parts: list[Path] = []
    for cp in completed_parts:
        p_name = str(cp["file_name"])
        local_p = parts_dir / p_name
        ref = remote_map.get(p_name) or {"name": p_name}
        await src_provider.download_file(src_creds, ref, parts_dir, job)
        if not local_p.is_file():
            cands = list(parts_dir.glob(f"*{p_name}*"))
            if cands:
                cands[0].rename(local_p)
        if not local_p.is_file():
            raise ProviderFailure("DOWNLOAD_FAILED", f"Không thể tải {p_name} từ folder resume.")
        local_parts.append(local_p)

    out_name = build_upscaled_video_name(str(manifest.get("target_filename") or manifest.get("source_filename") or "video.mp4"))
    final_out = output_dir / out_name
    concat_upscaled_video_parts(
        local_parts,
        final_out,
        audio_source=None,
        preview_seconds=float(cfg.get("preview_seconds") or 0.0),
        work_dir=work_dir,
    )
    shutil.rmtree(parts_dir, ignore_errors=True)
    return final_out

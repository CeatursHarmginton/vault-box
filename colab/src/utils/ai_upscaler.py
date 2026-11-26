from __future__ import annotations

import ctypes
import gc
import importlib.util
import json
import logging
import os
import pathlib
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from ..config import BASE_DIR
from .image_optimizer import (
    IMAGE_EXTENSIONS,
    OPTIMIZED_EXIF_DESC,
    OPTIMIZED_TAG_KEY,
    VIDEO_EXTENSIONS,
    embed_optimization_metadata,
    is_image_already_optimized,
)

logger = logging.getLogger(__name__)

UPSCALED_VIDEO_SUFFIX = "-upscaled"

MODEL_PRESET_OPTIONS: tuple[str, ...] = (
    "auto (Tự động chọn tối ưu)",
    "2xLiveActionV1_SPAN",
    "2xNomosUni_span_multijpg_ldl",
    "2xNomosUni_compact_otf_medium",
    "realesr-general-x4v3",
    "2xNomosUni_esrgan_multijpg",
    "4xNomos8kSC",
    "RealESRGAN_x2plus",
)

TARGET_RESOLUTION_OPTIONS: tuple[str, ...] = (
    "720p",
    "1080p",
    "Native Model Scale (2x/4x)",
)

SIZE_PROFILE_OPTIONS: tuple[str, ...] = (
    "Tiết kiệm dung lượng (Nhẹ nhất)",
    "Chuẩn HD cân bằng (Khuyên dùng)",
    "Chất lượng lưu trữ (File lớn)",
)

ENCODER_CHOICE_OPTIONS: tuple[str, ...] = (
    "auto (GPU h264_nvenc tương thích mọi máy)",
    "GPU hevc_nvenc (H.265 nhẹ hơn ~35%)",
    "libx264 (CPU)",
)

TORCH_COMPILE_OPTIONS: tuple[str, ...] = (
    "auto (Bật cho video >= 1500 frames)",
    "on (Luôn bật)",
    "off (Tắt)",
)

HEAVY_MODELS = {"2xNomosUni_esrgan_multijpg", "4xNomos8kSC", "RealESRGAN_x2plus"}

MODEL_ZOO: dict[str, dict[str, str]] = {
    "2xLiveActionV1_SPAN": {
        "url": "https://raw.githubusercontent.com/jcj83429/upscaling/f73a3a02874360ec6ced18f8bdd8e43b5d7bba57/2xLiveActionV1_SPAN/2xLiveActionV1_SPAN_490000.pth",
        "filename": "2xLiveActionV1_SPAN.pth",
    },
    "2xNomosUni_span_multijpg_ldl": {
        "url": "https://huggingface.co/Phips/2xNomosUni_span_multijpg_ldl/resolve/main/2xNomosUni_span_multijpg_ldl.safetensors",
        "filename": "2xNomosUni_span_multijpg_ldl.safetensors",
    },
    "2xNomosUni_compact_otf_medium": {
        "url": "https://huggingface.co/Phips/2xNomosUni_compact_otf_medium/resolve/main/2xNomosUni_compact_otf_medium.safetensors",
        "filename": "2xNomosUni_compact_otf_medium.safetensors",
    },
    "realesr-general-x4v3": {
        "url": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-x4v3.pth",
        "filename": "realesr-general-x4v3.pth",
        "wdn_url": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-wdn-x4v3.pth",
        "wdn_filename": "realesr-general-wdn-x4v3.pth",
    },
    "2xNomosUni_esrgan_multijpg": {
        "url": "https://huggingface.co/Phips/2xNomosUni_esrgan_multijpg/resolve/main/2xNomosUni_esrgan_multijpg.safetensors",
        "filename": "2xNomosUni_esrgan_multijpg.safetensors",
    },
    "4xNomos8kSC": {
        "url": "https://github.com/Phhofm/models/raw/main/4xNomos8kSC/4xNomos8kSC.pth",
        "filename": "4xNomos8kSC.pth",
    },
    "RealESRGAN_x2plus": {
        "url": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth",
        "filename": "RealESRGAN_x2plus.pth",
    },
}

DEFAULT_WEIGHTS_DIR = BASE_DIR / "upscale_weights"
_PIP_INSTALL_LOCK = threading.Lock()


def ensure_nvidia_lib_paths() -> None:
    """Unlock hidden Google Colab NVIDIA driver libraries (/usr/lib64-nvidia) for FFmpeg NVENC/NVDEC."""
    if os.name == "nt":
        return
    for nv_dir in ("/usr/lib64-nvidia", "/usr/local/nvidia/lib64"):
        if os.path.isdir(nv_dir) and nv_dir not in os.environ.get("LD_LIBRARY_PATH", ""):
            os.environ["LD_LIBRARY_PATH"] = f"{nv_dir}:{os.environ.get('LD_LIBRARY_PATH', '')}".strip(":")


def is_upscaled_filename(name_or_path: str | Path | Any) -> bool:
    """Return True if filename stem ends with '-upscaled' (case-insensitive)."""
    raw = str(name_or_path or "").strip().replace("\\", "/")
    if not raw:
        return False
    base = raw.rsplit("/", 1)[-1]
    if not base:
        return False
    stem = Path(base).stem if "." in base else base
    return stem.lower().endswith(UPSCALED_VIDEO_SUFFIX)


def build_upscaled_video_name(name_or_path: str | Path) -> str:
    """Build `<stem>-upscaled.mp4` without duplicating `-upscaled` or embedding model names."""
    raw = str(name_or_path or "video").strip().replace("\\", "/")
    base = raw.rsplit("/", 1)[-1] or "video"
    stem = Path(base).stem if "." in base else base
    stem = stem.strip() or "video"
    if stem.lower().endswith(UPSCALED_VIDEO_SUFFIX):
        return f"{stem}.mp4"
    return f"{stem}{UPSCALED_VIDEO_SUFFIX}.mp4"


def embed_video_optimization_metadata_args() -> list[str]:
    """FFmpeg metadata arguments embedding `VaultBox_Optimized:true` into output video container."""
    return [
        "-metadata", f"comment={OPTIMIZED_EXIF_DESC}",
        "-metadata", f"description={OPTIMIZED_EXIF_DESC}",
    ]


def is_video_already_optimized(video_path: Path) -> bool:
    """
    Fast header inspection to check if a video was already upscaled/optimized by VaultBox.
    Checks `-upscaled` filename suffix, container binary header/tail, and ffprobe format tags
    without decoding video frames.
    """
    if is_upscaled_filename(video_path.name):
        return True
    if not video_path.is_file():
        return False
    try:
        file_size = video_path.stat().st_size
    except OSError:
        return False
    if file_size <= 0:
        return False

    # 1. Sub-millisecond binary atom scan on head & tail (catches +faststart MP4/MOV/MKV immediately)
    marker = OPTIMIZED_TAG_KEY.encode("utf-8")
    scan_window = 256 * 1024
    try:
        with video_path.open("rb") as fh:
            head = fh.read(min(file_size, scan_window))
            if marker in head:
                return True
            if file_size > scan_window:
                fh.seek(max(0, file_size - scan_window))
                tail = fh.read(scan_window)
                if marker in tail:
                    return True
    except Exception:
        pass

    # 2. Container format tag inspection via ffprobe (handles non-UTF8 / compressed MKV tags)
    if shutil.which("ffprobe"):
        try:
            cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "format_tags=comment,description,title",
                "-of", "json",
                str(video_path),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode == 0 and res.stdout:
                data = json.loads(res.stdout)
                tags = (data.get("format") or {}).get("tags") or {}
                for val in tags.values():
                    s_val = str(val or "")
                    if OPTIMIZED_TAG_KEY in s_val or "already-optimized" in s_val:
                        return True
        except Exception:
            pass

    return False


_GPU_PROBE_CACHE: dict[str, Any] | None = None
_TPU_INIT_LOCK = threading.Lock()
_TPU_RUNTIME_CACHE: dict[str, Any] | None = None
_TPU_MODEL_CACHE: dict[tuple[str, float, str], Any] = {}
_TPU_COMPILED_SHAPES: set[tuple[Any, ...]] = set()


def detect_tpu_hardware() -> dict[str, Any]:
    """
    Passive, zero-lock Google Cloud / Colab TPU hardware detector.
    Never calls `xm.xla_device()` so `/dev/vfio/0` is never locked during health checks.
    """
    acc_type = (os.environ.get("TPU_ACCELERATOR_TYPE") or "").strip()
    has_env = bool(
        acc_type
        or os.environ.get("COLAB_TPU_ADDR")
        or os.environ.get("TPU_NAME")
        or os.environ.get("TPU_WORKER_ID")
    )
    has_pci = False
    has_dev = False
    if os.name != "nt":
        try:
            pci_root = Path("/sys/bus/pci/devices")
            if pci_root.is_dir():
                has_pci = any(
                    p.is_file() and p.read_text(encoding="utf-8", errors="ignore").strip().lower() == "0x1ae0"
                    for p in pci_root.glob("*/vendor")
                )
        except Exception:
            has_pci = False
        try:
            dev_root = Path("/dev")
            vfio_root = Path("/dev/vfio")
            has_accel = dev_root.is_dir() and any(dev_root.glob("accel*"))
            has_vfio_grp = (
                has_pci
                and vfio_root.is_dir()
                and any(p.name.isdigit() for p in vfio_root.glob("*"))
            )
            has_dev = bool(has_accel or has_vfio_grp)
        except Exception:
            has_dev = False

    has_tpu = bool(has_env or has_pci or has_dev)
    if not has_tpu:
        return {"hasTpu": False, "tpuName": "", "tpuHbmGb": 0.0, "acceleratorType": ""}

    low_acc = acc_type.lower()
    if "v5litepod" in low_acc or "v5e" in low_acc or not acc_type:
        suffix = acc_type.split("-", 1)[1] if "-" in acc_type else "1"
        tpu_name = f"TPU v5e-{suffix}"
        hbm_gb = 16.0
    elif "v6e" in low_acc:
        tpu_name = f"TPU {acc_type}"
        hbm_gb = 32.0
    elif "v4" in low_acc:
        tpu_name = f"TPU {acc_type}"
        hbm_gb = 32.0
    elif "v3" in low_acc:
        tpu_name = f"TPU {acc_type}"
        hbm_gb = 16.0
    elif "v2" in low_acc:
        tpu_name = f"TPU {acc_type}"
        hbm_gb = 8.0
    else:
        tpu_name = f"TPU {acc_type}"
        hbm_gb = 16.0

    return {
        "hasTpu": True,
        "tpuName": tpu_name,
        "tpuHbmGb": hbm_gb,
        "acceleratorType": acc_type or "v5litepod-1",
    }


def get_ai_upscale_status() -> dict[str, Any]:
    """Return non-blocking status of AI Upscale packages and GPU/TPU availability for worker health."""
    global _GPU_PROBE_CACHE
    spandrel_ok = importlib.util.find_spec("spandrel") is not None
    safetensors_ok = importlib.util.find_spec("safetensors") is not None
    torch_xla_ok = importlib.util.find_spec("torch_xla") is not None
    if _GPU_PROBE_CACHE is None:
        has_gpu = False
        gpu_name = ""
        vram_gb = 0.0
        if shutil.which("nvidia-smi"):
            try:
                out = subprocess.check_output(
                    ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                    text=True,
                    timeout=4,
                ).strip()
                if out:
                    parts = [p.strip() for p in out.splitlines()[0].split(",")]
                    has_gpu = True
                    gpu_name = parts[0]
                    if len(parts) > 1:
                        vram_gb = round(float(parts[1]) / 1024.0, 1)
            except Exception:
                pass
        _GPU_PROBE_CACHE = {"hasGpu": has_gpu, "gpuName": gpu_name, "vramGb": vram_gb}

    has_gpu = bool(_GPU_PROBE_CACHE["hasGpu"])
    tpu_info = {"hasTpu": False, "tpuName": "", "tpuHbmGb": 0.0} if has_gpu else detect_tpu_hardware()
    has_tpu = bool(tpu_info["hasTpu"])
    accelerator = "gpu" if has_gpu else ("tpu" if has_tpu else "cpu")

    return {
        "installed": bool(spandrel_ok and safetensors_ok),
        "hasGpu": has_gpu,
        "gpuName": str(_GPU_PROBE_CACHE["gpuName"]),
        "vramGb": float(_GPU_PROBE_CACHE["vramGb"]),
        "hasTpu": has_tpu,
        "tpuName": str(tpu_info["tpuName"]),
        "tpuHbmGb": float(tpu_info["tpuHbmGb"]),
        "torchXlaInstalled": bool(torch_xla_ok),
        "accelerator": accelerator,
        "models": list(MODEL_PRESET_OPTIONS),
    }


def _emit_upscale_progress(job_state: Any, *, merge: bool = True, **fields: Any) -> None:
    if job_state is None:
        return
    if hasattr(job_state, "set_upscale_progress"):
        job_state.set_upscale_progress(fields, merge=merge)
    else:
        prev = getattr(job_state, "upscale_progress", None)
        if merge and isinstance(prev, dict):
            setattr(job_state, "upscale_progress", {**prev, **fields})
        else:
            setattr(job_state, "upscale_progress", dict(fields))
        if hasattr(job_state, "updated_at"):
            job_state.updated_at = time.time()


def ensure_upscale_dependencies(log_fn: Callable[[str], None] | None = None) -> None:
    """Ensure `spandrel`, `safetensors`, `einops` (and `torch_xla` on TPU) are installed safely."""
    tpu_hw = detect_tpu_hardware()
    need_tpu_xla = bool(tpu_hw["hasTpu"] and not shutil.which("nvidia-smi") and importlib.util.find_spec("torch_xla") is None)
    missing = [
        pkg for pkg in ("spandrel", "safetensors", "einops")
        if importlib.util.find_spec(pkg) is None
    ]
    if not missing and not need_tpu_xla:
        return
    with _PIP_INSTALL_LOCK:
        need_tpu_xla = bool(tpu_hw["hasTpu"] and not shutil.which("nvidia-smi") and importlib.util.find_spec("torch_xla") is None)
        if need_tpu_xla:
            if log_fn:
                log_fn(f"[SETUP] Cài đặt runtime PyTorch/XLA cho {tpu_hw['tpuName']}...")
            if importlib.util.find_spec("torch") is None:
                subprocess.check_call([
                    sys.executable, "-m", "pip", "install", "-q",
                    "torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/cpu",
                ])
            subprocess.check_call([
                sys.executable, "-m", "pip", "install", "-q",
                "torch_xla[tpu]",
                "-f", "https://storage.googleapis.com/libtpu-releases/index.html",
                "-f", "https://storage.googleapis.com/libtpu-wheels/index.html",
            ])
            importlib.invalidate_caches()

        still_missing = [
            pkg for pkg in ("spandrel", "safetensors", "einops")
            if importlib.util.find_spec(pkg) is None
        ]
        if not still_missing:
            return
        if log_fn:
            log_fn(f"[SETUP] Cài đặt thư viện AI Upscale còn thiếu ({', '.join(still_missing)})...")

        has_xla = importlib.util.find_spec("torch_xla") is not None
        if has_xla:
            torch_pin: list[str] = []
            try:
                import torch
                base_ver = str(torch.__version__).split("+")[0].strip()
                if base_ver:
                    torch_pin = [f"torch=={base_ver}"]
            except Exception:
                pass
            subprocess.check_call([
                sys.executable, "-m", "pip", "install", "-q",
                *torch_pin, "spandrel", "safetensors", "einops", "tqdm",
            ])
        else:
            subprocess.check_call([
                sys.executable, "-m", "pip", "install", "-q", "--upgrade",
                "spandrel", "safetensors", "einops", "tqdm",
            ])
        importlib.invalidate_caches()
        if log_fn:
            log_fn("[SETUP] Thư viện Spandrel + Safetensors đã sẵn sàng.")


def _init_tpu_runtime(log_fn: Callable[[str], None] | None = None) -> dict[str, Any]:
    """
    Lazy singleton initializer for Google Cloud TPU PJRT device + persistent XLA compilation cache.
    Must only be invoked inside the worker process right before inference so `/dev/vfio/0` is owned cleanly.
    """
    global _TPU_RUNTIME_CACHE
    if _TPU_RUNTIME_CACHE is not None:
        return _TPU_RUNTIME_CACHE

    with _TPU_INIT_LOCK:
        if _TPU_RUNTIME_CACHE is not None:
            return _TPU_RUNTIME_CACHE

        os.environ.setdefault("PJRT_DEVICE", "TPU")
        cache_dir = (os.environ.get("XLA_PERSISTENT_CACHE_PATH") or "").strip()
        if not cache_dir:
            drive_root = Path("/content/drive/MyDrive")
            cache_dir = str(drive_root / ".vaultbox_xla_cache") if drive_root.is_dir() else "/content/xla_cache"
            os.environ["XLA_PERSISTENT_CACHE_PATH"] = cache_dir
        try:
            Path(cache_dir).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        import torch_xla  # type: ignore[import-not-found]
        import torch_xla.core.xla_model as xm  # type: ignore[import-not-found]

        try:
            if hasattr(torch_xla, "runtime") and hasattr(torch_xla.runtime, "initialize_cache"):
                torch_xla.runtime.initialize_cache(cache_dir, readonly=False)
        except Exception:
            pass

        dev = xm.xla_device()

        def _sync() -> None:
            if hasattr(torch_xla, "sync"):
                torch_xla.sync()
            else:
                xm.mark_step()

        tpu_hw = detect_tpu_hardware()
        tpu_name = str(tpu_hw.get("tpuName") or "TPU v5e-1")
        hbm_bytes = int(float(tpu_hw.get("tpuHbmGb") or 16.0) * (1024 ** 3))
        try:
            if hasattr(xm, "get_memory_info"):
                mem_info = xm.get_memory_info(dev) or {}
                limit_b = int(mem_info.get("bytes_limit") or mem_info.get("total_memory") or 0)
                if limit_b > 0:
                    hbm_bytes = limit_b
        except Exception:
            pass

        if log_fn:
            log_fn(
                f"[TPU] Đã kết nối {tpu_name} ({hbm_bytes / (1024 ** 3):.1f} GB HBM BF16) | XLA Cache: {cache_dir}"
            )

        _TPU_RUNTIME_CACHE = {
            "device": dev,
            "xm": xm,
            "sync": _sync,
            "tpuName": tpu_name,
            "hbmBytes": hbm_bytes,
            "cacheDir": cache_dir,
        }
        return _TPU_RUNTIME_CACHE


def _get_tpu_hbm_used_gb(tpu_rt: dict[str, Any] | None) -> float:
    if not tpu_rt:
        return 0.0
    try:
        xm = tpu_rt.get("xm")
        dev = tpu_rt.get("device")
        if xm is not None and dev is not None and hasattr(xm, "get_memory_info"):
            info = xm.get_memory_info(dev) or {}
            used_b = int(info.get("bytes_used") or 0)
            if used_b > 0:
                return used_b / (1024 ** 3)
    except Exception:
        pass
    return 0.0


def target_short_pixels(res_choice: str) -> int | None:
    """Return short-side target pixel threshold for 720p/1080p, or None for Native Model Scale."""
    clean = str(res_choice or "1080p").strip()
    if clean.startswith("Native") or clean.lower().startswith("native"):
        return None
    if "720" in clean:
        return 720
    if "1080" in clean:
        return 1080
    return None


def is_already_at_or_above_target(in_w: int, in_h: int, res_choice: str) -> bool:
    """Return True if input short side already meets or exceeds `target_resolution` (720p/1080p)."""
    t_short = target_short_pixels(res_choice)
    if t_short is None or in_w <= 0 or in_h <= 0:
        return False
    return min(in_w, in_h) >= t_short


def get_linux_ram_bytes() -> tuple[int, int]:
    total_b = int(12.7 * (1024 ** 3))
    avail_b = int(9.0 * (1024 ** 3))
    try:
        meminfo = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    meminfo[parts[0].rstrip(":")] = int(parts[1]) * 1024
        total_b = meminfo.get("MemTotal", total_b)
        avail_b = meminfo.get("MemAvailable", avail_b)
    except Exception:
        pass
    return total_b, avail_b


def deep_cleanup_system(
    work_dir: Path | None = None,
    weights_dir: Path | None = None,
    remove_weights: bool = False,
    label: str = "",
    log_fn: Callable[[str], None] | None = None,
    verbose: bool = False,
) -> None:
    """Giải phóng toàn diện RAM hệ thống (kể cả glibc heap), GPU VRAM, cache Dynamo và file tạm SSD."""
    for temp_root in filter(None, (work_dir, pathlib.Path("/content") if os.name != "nt" else None)):
        if temp_root.exists():
            for tmp_p in temp_root.glob("_tmp_upscaled_*"):
                tmp_p.unlink(missing_ok=True)
            for stg_p in temp_root.glob("_staged_input*"):
                stg_p.unlink(missing_ok=True)

    w_dir = weights_dir or DEFAULT_WEIGHTS_DIR
    if remove_weights:
        _TPU_MODEL_CACHE.clear()
        if w_dir.exists():
            shutil.rmtree(w_dir, ignore_errors=True)

    try:
        import torch
        try:
            torch._dynamo.reset()
        except Exception:
            pass
    except ImportError:
        torch = None  # type: ignore[assignment]

    gc.collect()
    gc.collect()

    if torch is not None and torch.cuda.is_available():
        try:
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
        except Exception:
            pass
    elif _TPU_RUNTIME_CACHE is not None:
        try:
            _TPU_RUNTIME_CACHE["sync"]()
        except Exception:
            pass

    if os.name != "nt":
        try:
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass

    if verbose and label and log_fn:
        ram_tot, ram_av = get_linux_ram_bytes()
        ram_used_gb = max(0.0, (ram_tot - ram_av) / (1024 ** 3))
        if torch is not None and torch.cuda.is_available():
            mem_label = f"VRAM: {torch.cuda.memory_reserved(0) / (1024 ** 3):.2f} GB"
        elif _TPU_RUNTIME_CACHE is not None:
            mem_label = f"HBM: {_get_tpu_hbm_used_gb(_TPU_RUNTIME_CACHE):.2f} GB"
        else:
            mem_label = "VRAM: 0.00 GB"
        disk_probe = work_dir if (work_dir and work_dir.exists()) else BASE_DIR
        try:
            _, _, ssd_free = shutil.disk_usage(disk_probe)
        except Exception:
            ssd_free = 0
        log_fn(
            f"[CLEANUP] {label} | RAM: {ram_used_gb:.2f}/{ram_tot/(1024**3):.1f} GB | "
            f"{mem_label} | Free SSD: {ssd_free/(1024**3):.1f} GB"
        )


def download_if_needed(
    url: str,
    dest: Path,
    weights_dir: Path | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> None:
    w_dir = weights_dir or dest.parent
    w_dir.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 100_000:
        return
    if log_fn:
        log_fn(f"[MODEL] Tải trọng số AI: {dest.name}")
    tmp_dest = dest.with_suffix(dest.suffix + f".dl_{os.getpid()}_{threading.get_ident()}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp_dest, "wb") as f:
            shutil.copyfileobj(resp, f)
        os.replace(str(tmp_dest), str(dest))
    finally:
        tmp_dest.unlink(missing_ok=True)


def _is_reparam_or_span_model(net: Any, preset_name: str = "") -> bool:
    """Detect SPAN / Conv3XC re-parameterizable architectures whose dynamic weight fusion conflicts with NHWC strides."""
    if "span" in str(preset_name or "").lower():
        return True
    if "span" in type(net).__name__.lower():
        return True
    try:
        for mod in net.modules():
            cls_low = type(mod).__name__.lower()
            if "conv3xc" in cls_low or hasattr(mod, "eval_conv") or callable(getattr(mod, "update_params", None)):
                return True
    except Exception:
        pass
    return False


def _freeze_reparam_modules(net: Any) -> None:
    """
    Materialize `Conv3XC.update_params()` once in FP32 eval mode and replace `update_params` with a no-op
    so inference never mutates `eval_conv.weight.data` strides across batches or inside `torch.compile` / XLA.
    """
    try:
        for mod in net.modules():
            upd = getattr(mod, "update_params", None)
            if callable(upd) and hasattr(mod, "eval_conv"):
                upd()
                setattr(mod, "update_params", lambda: None)
    except Exception:
        pass


def load_upscale_model(
    preset_name: str,
    device: Any,
    fp16: bool,
    dn_strength: float,
    weights_dir: Path | None = None,
    log_fn: Callable[[str], None] | None = None,
    *,
    use_bf16: bool = False,
) -> Any:
    import torch
    from spandrel import ImageModelDescriptor, ModelLoader

    if preset_name not in MODEL_ZOO:
        preset_name = "2xLiveActionV1_SPAN"
    cfg = MODEL_ZOO[preset_name]
    w_dir = weights_dir or DEFAULT_WEIGHTS_DIR
    main_path = w_dir / cfg["filename"]
    download_if_needed(cfg["url"], main_path, weights_dir=w_dir, log_fn=log_fn)

    dev_type = getattr(device, "type", str(device))
    is_xla = str(dev_type).startswith("xla")
    loader_dev = torch.device("cpu") if is_xla else device
    loader = ModelLoader(device=loader_dev)
    if preset_name == "realesr-general-x4v3" and dn_strength < 1.0:
        wdn_path = w_dir / cfg["wdn_filename"]
        download_if_needed(cfg["wdn_url"], wdn_path, weights_dir=w_dir, log_fn=log_fn)
        sd_dn = loader.load_state_dict_from_file(main_path)
        sd_wdn = loader.load_state_dict_from_file(wdn_path)
        blended = {
            k: (
                sd_dn[k].float() * dn_strength + sd_wdn[k].float() * (1.0 - dn_strength)
                if torch.is_floating_point(sd_dn[k])
                else sd_dn[k]
            )
            for k in sd_dn.keys()
        }
        descriptor = loader.load_from_state_dict(blended)
    else:
        descriptor = loader.load_from_file(main_path)

    assert isinstance(descriptor, ImageModelDescriptor), "Model không phải dạng Image Super-Resolution!"
    descriptor.eval()
    _freeze_reparam_modules(descriptor.model)
    if is_xla:
        target_dtype = torch.bfloat16 if use_bf16 else torch.float32
        descriptor.to(dtype=target_dtype)
        descriptor.to(device=device)
        return descriptor

    is_cuda = dev_type == "cuda"
    if fp16 and is_cuda and descriptor.supports_half:
        descriptor.to(device=device, dtype=torch.float16)
    else:
        descriptor.to(device=device, dtype=torch.float32)
    return descriptor


def _get_cached_tpu_model(
    preset_name: str,
    device: Any,
    use_bf16: bool,
    dn_strength: float,
    weights_dir: Path | None = None,
    log_fn: Callable[[str], None] | None = None,
    sync_fn: Callable[[], None] | None = None,
) -> Any:
    cache_key = (preset_name, round(dn_strength, 3), "bf16" if use_bf16 else "fp32")
    cached = _TPU_MODEL_CACHE.get(cache_key)
    if cached is not None:
        return cached
    _TPU_MODEL_CACHE.clear()
    gc.collect()
    descriptor = load_upscale_model(
        preset_name,
        device=device,
        fp16=False,
        dn_strength=dn_strength,
        weights_dir=weights_dir,
        log_fn=log_fn,
        use_bf16=use_bf16,
    )
    if sync_fn is not None:
        sync_fn()
    _TPU_MODEL_CACHE[cache_key] = descriptor
    return descriptor


def get_video_metadata(video_path: Path) -> tuple[int, int, str, float, float, int, float]:
    probe_cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames,duration,bit_rate,tags:stream_side_data=rotation:format=duration,bit_rate",
        "-of", "json",
        str(video_path),
    ]
    info = json.loads(subprocess.check_output(probe_cmd).decode("utf-8", errors="replace"))
    streams = info.get("streams") or []
    if not streams:
        raise RuntimeError(f"Không tìm thấy luồng video hợp lệ trong file: {video_path.name}")
    st = streams[0]
    w, h = int(st["width"]), int(st["height"])

    rot = 0
    tags = st.get("tags", {}) or {}
    if "rotate" in tags:
        rot = int(float(tags["rotate"]))
    for sd in st.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    if abs(rot) % 180 == 90:
        w, h = h, w

    avg_fr = st.get("avg_frame_rate", "0/0")
    r_fr = st.get("r_frame_rate", "30/1")
    fps_str = avg_fr
    num, den = map(int, avg_fr.split("/")) if "/" in avg_fr else (0, 0)
    fps_val = (num / den) if den else 0.0
    if fps_val < 1.0 or fps_val > 125.0:
        fps_str = r_fr
        num, den = map(int, r_fr.split("/")) if "/" in r_fr else (30, 1)
        fps_val = (num / den) if den else 30.0
        if fps_val > 125.0 or fps_val <= 0:
            fps_str = "30/1"
            fps_val = 30.0

    fmt_dur = float(info.get("format", {}).get("duration", 0.0) or 0.0)
    st_dur = float(st.get("duration", 0.0) or 0.0)
    duration = st_dur if st_dur > 0 else fmt_dur
    nb_frames_raw = st.get("nb_frames")
    nb_frames = int(nb_frames_raw) if (nb_frames_raw and str(nb_frames_raw).isdigit()) else 0

    v_bitrate = int(st.get("bit_rate") or 0)
    if v_bitrate <= 0:
        fmt_br = int(info.get("format", {}).get("bit_rate") or 0)
        v_bitrate = max(0, fmt_br - 128_000) if fmt_br > 160_000 else fmt_br
    bpp = (v_bitrate / (w * h * fps_val)) if (v_bitrate > 0 and w * h * fps_val > 0) else 0.1

    return w, h, fps_str, fps_val, duration, nb_frames, bpp


def choose_auto_model(
    in_w: int,
    in_h: int,
    res_choice: str,
    est_frames: int,
    bpp: float,
    gpu_name: str,
    default_dn: float,
) -> tuple[str, float, str]:
    in_short = min(in_w, in_h)
    if res_choice == "720p":
        scale_needed = 720.0 / max(1, in_short)
    elif res_choice == "1080p":
        scale_needed = 1080.0 / max(1, in_short)
    else:
        scale_needed = 2.0

    upper_dev = gpu_name.upper()
    fast_gpu = any(tag in upper_dev for tag in ("A100", "L4", "V100", "H100", "4090", "3090"))
    is_tpu = "TPU" in upper_dev
    frames = est_frames if est_frames else 1800

    if scale_needed >= 2.6:
        if frames <= 1500 or fast_gpu or (is_tpu and frames <= 3000):
            return (
                "4xNomos8kSC",
                default_dn,
                f"Cần phóng lớn {scale_needed:.1f}x ({in_short}p → {res_choice}) & thời lượng vừa phải ({frames} frames) → ưu tiên mạng lớn 4x chất lượng cao",
            )
        auto_dn = 0.55 if (0 < bpp < 0.075) else 0.35
        return (
            "realesr-general-x4v3",
            auto_dn,
            f"Cần phóng lớn {scale_needed:.1f}x trên video dài ({frames} frames) → dùng Compact 4x tốc độ cao (denoise={auto_dn})",
        )

    if frames > 90000 and not fast_gpu:
        return (
            "2xNomosUni_compact_otf_medium",
            default_dn,
            f"Video rất dài ({frames} frames) phóng {scale_needed:.1f}x → dùng Compact 2x tốc độ cao để kịp thời gian Colab",
        )

    if 0 < bpp < 0.075:
        return (
            "2xNomosUni_span_multijpg_ldl",
            default_dn,
            f"Phát hiện bitrate thấp / nén mạnh (BPP={bpp:.3f}) phóng {scale_needed:.1f}x → dùng SPAN 2x chuyên khử hạt nén & giữ xóa phông",
        )

    return (
        "2xLiveActionV1_SPAN",
        default_dn,
        f"Phóng {scale_needed:.1f}x ({in_short}p → {res_choice}, BPP={bpp:.3f}) → dùng SPAN 2x chuyên người thật cân bằng nhất giữa tốc độ & vân da tự nhiên",
    )


def compute_target_dims(in_w: int, in_h: int, model_scale: int, res_choice: str) -> tuple[int, int]:
    if str(res_choice).startswith("Native") or str(res_choice).lower().startswith("native"):
        out_w = (in_w * model_scale // 2) * 2
        out_h = (in_h * model_scale // 2) * 2
        return max(2, out_w), max(2, out_h)

    target_short = 720 if "720" in str(res_choice) else 1080
    is_landscape = in_w >= in_h
    short_side = max(1, in_h if is_landscape else in_w)
    long_side = max(1, in_w if is_landscape else in_h)
    aspect = long_side / short_side

    if abs(aspect - (16.0 / 9.0)) < 0.025:
        target_long = 1280 if target_short == 720 else 1920
    else:
        target_long = int(round((target_short * aspect) / 2.0) * 2)

    target_short = (target_short // 2) * 2
    return (target_long, target_short) if is_landscape else (target_short, target_long)


def rgb_to_yuv420p_bt709(sr: Any) -> Any:
    import torch
    import torch.nn.functional as F

    b, _, h, w = sr.shape
    sr_base = sr.float() if sr.dtype == torch.bfloat16 else sr
    sr_c = sr_base.clamp(0.0, 1.0)
    r = sr_c[:, 0]
    g = sr_c[:, 1]
    b_ch = sr_c[:, 2]

    y = (16.0 + 46.5594 * r + 156.6288 * g + 15.8118 * b_ch).round_().clamp_(16.0, 235.0).to(torch.uint8)

    sr_sub = F.avg_pool2d(sr_c, kernel_size=2, stride=2)
    rs = sr_sub[:, 0]
    gs = sr_sub[:, 1]
    bs = sr_sub[:, 2]
    u = (128.0 - 25.6641 * rs - 86.3359 * gs + 112.0 * bs).round_().clamp_(16.0, 240.0).to(torch.uint8)
    v = (128.0 + 112.0 * rs - 101.7303 * gs - 10.2697 * bs).round_().clamp_(16.0, 240.0).to(torch.uint8)

    return torch.cat([y.reshape(b, -1), u.reshape(b, -1), v.reshape(b, -1)], dim=1)


def resize_sr_gpu(sr: Any, out_h: int, out_w: int) -> Any:
    import torch.nn.functional as F

    sr_h, sr_w = sr.shape[-2], sr.shape[-1]
    if sr_h == out_h and sr_w == out_w:
        return sr
    is_xla = str(getattr(getattr(sr, "device", None), "type", "")).startswith("xla")
    if is_xla:
        # Avoid `antialias=True` on XLA which triggers host CPU fallback (`aten::_upsample_bicubic2d_aa`).
        mode = "bilinear" if (out_h < sr_h or out_w < sr_w) else "bicubic"
        return F.interpolate(sr.float(), size=(out_h, out_w), mode=mode, align_corners=False).to(sr.dtype)
    if out_h < sr_h and (sr_h / out_h) > 1.25:
        return F.interpolate(sr.float(), size=(out_h, out_w), mode="bicubic", align_corners=False, antialias=True).to(sr.dtype)
    return F.interpolate(sr.float(), size=(out_h, out_w), mode="bicubic", align_corners=False).to(sr.dtype)


def _read_exact_into(stream: Any, mv: memoryview) -> int:
    total = 0
    target = len(mv)
    while total < target:
        n = stream.readinto(mv[total:])
        if not n:
            break
        total += n
    return total


def nvdec_available(video_path: Path) -> bool:
    ensure_nvidia_lib_paths()
    test_cmd = [
        "ffmpeg", "-v", "error",
        "-hwaccel", "cuda",
        "-i", str(video_path),
        "-frames:v", "1",
        "-f", "null", "-",
    ]
    res = subprocess.run(test_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=os.environ.copy())
    return res.returncode == 0


def nvenc_available(codec_name: str = "h264_nvenc") -> bool:
    ensure_nvidia_lib_paths()
    test_cmd = [
        "ffmpeg", "-v", "error",
        "-f", "lavfi", "-i", "nullsrc=s=256x256:d=0.1",
        "-pix_fmt", "yuv420p",
        "-c:v", codec_name, "-f", "null", "-",
    ]
    res = subprocess.run(test_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=os.environ.copy())
    return res.returncode == 0


def _calibrate_tpu_batch(
    in_w: int,
    in_h: int,
    model_scale: int,
    selected_model: str,
    target_pct: int,
    user_b: int,
    hbm_total_bytes: int,
) -> int:
    """
    Analytical TPU HBM batch sizer (zero dummy forward passes -> zero wasted XLA compilations).
    Returns deterministic static batch size independent of video length so consecutive videos of
    identical resolution reuse the exact same compiled XLA graph in 0.0s.
    """
    if user_b > 0:
        return max(1, min(32, user_b))

    in_px = max(1, in_w * in_h)
    sr_px = in_px * (model_scale * model_scale)
    if selected_model in HEAVY_MODELS:
        base_overhead = 2.5 * (1024 ** 3)
        per_frame = float(in_px * 190 + sr_px * 32)
        max_cap = 8
    elif model_scale >= 4:
        base_overhead = 1.5 * (1024 ** 3)
        per_frame = float(in_px * 52 + sr_px * 18)
        max_cap = 16
    else:
        base_overhead = 1.4 * (1024 ** 3)
        per_frame = float(in_px * 46 + sr_px * 16)
        max_cap = 16

    clamped_pct = max(35, min(85, int(target_pct)))
    budget = float(hbm_total_bytes) * (clamped_pct / 100.0)
    usable = max(0.0, budget - base_overhead)
    raw_b = max(1, int(usable // max(1.0, per_frame)))
    cand = max(1, min(raw_b, max_cap))
    if cand >= 8:
        cand = (cand // 4) * 4
    elif cand >= 2:
        cand = (cand // 2) * 2
    return max(1, cand)


def infer_tiled_single(
    model: Any,
    x: Any,
    tile: int = 384,
    pad: int = 16,
    *,
    fixed_tile_shape: bool = False,
    tpu_device: Any = None,
    sync_fn: Callable[[], None] | None = None,
) -> Any:
    import torch
    import torch.nn.functional as F

    b, c, h, w = x.shape
    s = model.scale
    use_fixed = bool(fixed_tile_shape or tpu_device is not None)
    if not use_fixed:
        out = torch.empty((b, c, h * s, w * s), dtype=x.dtype, device=x.device)
        for y0 in range(0, h, tile):
            for x0 in range(0, w, tile):
                y1 = min(y0 + tile, h)
                x1 = min(x0 + tile, w)
                py0, py1 = max(y0 - pad, 0), min(y1 + pad, h)
                px0, px1 = max(x0 - pad, 0), min(x1 + pad, w)
                patch = x[:, :, py0:py1, px0:px1]
                sr_patch = model(patch)
                oy0 = (y0 - py0) * s
                ox0 = (x0 - px0) * s
                oh = (y1 - y0) * s
                ow = (x1 - x0) * s
                out[:, :, y0 * s : y1 * s, x0 * s : x1 * s] = sr_patch[:, :, oy0 : oy0 + oh, ox0 : ox0 + ow]
        return out

    # Fixed-shape tile execution for XLA/TPU: every patch has constant static dimensions
    # `(b, c, full_h, full_w)`, guaranteeing a single XLA compilation across all image sizes.
    full_span = tile + 2 * pad
    req_pad_w, req_pad_h = (0, 0)
    size_req = getattr(model, "size_requirements", None)
    if size_req is not None and hasattr(size_req, "get_padding"):
        req_pad_w, req_pad_h = size_req.get_padding(full_span, full_span)
    full_h = full_span + int(req_pad_h)
    full_w = full_span + int(req_pad_w)

    out_dtype = torch.float32 if tpu_device is not None else x.dtype
    out = torch.empty((b, c, h * s, w * s), dtype=out_dtype, device=x.device)
    raw_net = getattr(model, "model", model)

    for y0 in range(0, h, tile):
        for x0 in range(0, w, tile):
            y1 = min(y0 + tile, h)
            x1 = min(x0 + tile, w)
            py0, py1 = max(y0 - pad, 0), min(y1 + pad, h)
            px0, px1 = max(x0 - pad, 0), min(x1 + pad, w)
            patch = x[:, :, py0:py1, px0:px1]
            need_h = full_h - (py1 - py0)
            need_w = full_w - (px1 - px0)
            if need_h > 0 or need_w > 0:
                patch = F.pad(patch, (0, max(0, need_w), 0, max(0, need_h)), mode="replicate")
            if tpu_device is not None:
                patch_dev = patch.to(device=tpu_device)
                sr_dev = raw_net(patch_dev)
                sr_patch = sr_dev.float().cpu()
                del patch_dev, sr_dev
                if sync_fn is not None:
                    sync_fn()
            else:
                sr_patch = raw_net(patch)
            oy0 = (y0 - py0) * s
            ox0 = (x0 - px0) * s
            oh = (y1 - y0) * s
            ow = (x1 - x0) * s
            out[:, :, y0 * s : y1 * s, x0 * s : x1 * s] = sr_patch[:, :, oy0 : oy0 + oh, ox0 : ox0 + ow]
    return out


def parse_upscale_options(options: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize incoming UI/payload options to exact Video2X notebook parameter defaults."""
    opts = options or {}

    def _bool(key: str, camel: str, default: bool) -> bool:
        val = opts.get(key)
        if val is None:
            val = opts.get(camel)
        if val is None:
            return default
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in {"1", "true", "yes", "on"}

    def _int(key: str, camel: str, default: int, lo: int, hi: int) -> int:
        val = opts.get(key)
        if val is None:
            val = opts.get(camel)
        try:
            parsed = int(float(val))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            parsed = default
        return max(lo, min(hi, parsed))

    def _float(key: str, camel: str, default: float, lo: float, hi: float) -> float:
        val = opts.get(key)
        if val is None:
            val = opts.get(camel)
        try:
            parsed = float(val)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            parsed = default
        return max(lo, min(hi, parsed))

    model_preset = str(
        opts.get("model_preset")
        or opts.get("upscale_model_preset")
        or opts.get("modelPreset")
        or "auto (Tự động chọn tối ưu)"
    ).strip()
    target_resolution = str(
        opts.get("target_resolution")
        or opts.get("upscale_target_resolution")
        or opts.get("targetResolution")
        or "1080p"
    ).strip()
    size_profile = str(
        opts.get("size_profile")
        or opts.get("upscale_size_profile")
        or opts.get("sizeProfile")
        or "Chuẩn HD cân bằng (Khuyên dùng)"
    ).strip()
    encoder_choice = str(
        opts.get("encoder_choice")
        or opts.get("upscale_encoder_choice")
        or opts.get("encoderChoice")
        or "auto (GPU h264_nvenc tương thích mọi máy)"
    ).strip()
    torch_compile_mode = str(
        opts.get("torch_compile_mode")
        or opts.get("upscale_torch_compile_mode")
        or opts.get("torchCompileMode")
        or "auto (Bật cho video >= 1500 frames)"
    ).strip()
    start_time = str(opts.get("start_time") or opts.get("startTime") or "00:00:00").strip() or "00:00:00"

    return {
        "model_preset": model_preset,
        "target_resolution": target_resolution,
        "resource_target_pct": _int("resource_target_pct", "resourceTargetPct", 80, 35, 85),
        "size_profile": size_profile,
        "encoder_choice": encoder_choice,
        "torch_compile_mode": torch_compile_mode,
        "clean_temp_and_cache": _bool("clean_temp_and_cache", "cleanTempAndCache", True),
        "delete_uploaded_source_in_colab": _bool("delete_uploaded_source_in_colab", "deleteUploadedSourceInColab", False),
        "preview_seconds": _float("preview_seconds", "previewSeconds", 0.0, 0.0, 86400.0),
        "start_time": start_time,
        "batch_size": _int("batch_size", "batchSize", 0, 0, 96),
        "denoise_strength": _float("denoise_strength", "denoiseStrength", 0.4, 0.0, 1.0),
        "use_fp16": _bool("use_fp16", "useFp16", True),
        "overwrite_output": _bool("overwrite_output", "overwriteOutput", True),
        "force_reoptimize": _bool("force_reoptimize", "forceReoptimize", False),
        "allow_cpu_upscale": _bool("allow_cpu_upscale", "allowCpuUpscale", False),
    }


def _short_res_tag(res_choice: str, out_w: int, out_h: int) -> str:
    if str(res_choice).startswith("Native") or str(res_choice).lower().startswith("native"):
        return f"{min(out_w, out_h)}p"
    return str(res_choice).split()[0]


def process_single_video(
    cur_input: Path,
    dest_dir: Path,
    cfg: dict[str, Any],
    idx: int,
    total_count: int,
    *,
    job_state: Any = None,
    work_dir: Path | None = None,
    weights_dir: Path | None = None,
    cancel_check: Callable[[], None] | None = None,
    on_frame_progress: Callable[[float], None] | None = None,
    file_entry: dict[str, Any] | None = None,
    batch_files: list[dict[str, Any]] | None = None,
) -> tuple[Path | None, str, str]:
    """
    Upscale a single video using the full Video2X pipeline:
    PyTorch CUDA FP16 + Spandrel + Zero-Copy Pinned Ring Pool + Dual CUDA Streams + GPU BT.709 YUV420p + NVENC.
    Outputs `<stem>-upscaled.mp4` with embedded `VaultBox_Optimized:true` metadata tag.
    Returns: (output_path_or_none_if_skipped, status_message, quality_or_model_badge)
    """
    ensure_nvidia_lib_paths()
    log_fn = getattr(job_state, "log", None) or (lambda msg: logger.info("%s", msg))

    if cancel_check:
        cancel_check()

    if not cfg["force_reoptimize"] and is_video_already_optimized(cur_input):
        log_fn(f"[{idx}/{total_count}] SKIP   {cur_input.name} | Đã được tối ưu/upscale trước đó")
        return None, "Skipped (Already optimized)", "-"

    in_w, in_h, fps_str, fps_val, total_dur, exact_nb_frames, bpp_val = get_video_metadata(cur_input)
    target_resolution = cfg["target_resolution"]

    if is_already_at_or_above_target(in_w, in_h, target_resolution):
        log_fn(
            f"[{idx}/{total_count}] SKIP   {cur_input.name} ({in_w}x{in_h}) | Đã đạt target {target_resolution}"
        )
        return None, f"Skipped (Already >= {target_resolution.split()[0]})", f"{in_w}x{in_h}"

    ensure_upscale_dependencies(log_fn=log_fn)
    import numpy as np  # noqa: F401
    import torch
    import torch.nn.functional as F

    has_cuda = torch.cuda.is_available()
    tpu_hw = {"hasTpu": False, "tpuName": "", "tpuHbmGb": 0.0} if has_cuda else detect_tpu_hardware()
    is_tpu = bool(not has_cuda and tpu_hw["hasTpu"])
    if not has_cuda and not is_tpu and not cfg["allow_cpu_upscale"]:
        raise RuntimeError(
            "Colab Server chưa bật GPU/TPU Runtime. Vui lòng vào Runtime > Change runtime type > chọn T4 GPU hoặc TPU v5e-1 để chạy AI Video Upscale."
        )

    tpu_rt: dict[str, Any] | None = None
    if has_cuda:
        device = torch.device("cuda")
        torch.backends.cudnn.benchmark = True
        gpu_name_str = torch.cuda.get_device_name(0)
        vram_total_bytes = torch.cuda.get_device_properties(0).total_memory
        accel_kind = "gpu"
    elif is_tpu:
        tpu_rt = _init_tpu_runtime(log_fn=log_fn)
        device = tpu_rt["device"]
        gpu_name_str = str(tpu_rt["tpuName"])
        vram_total_bytes = int(tpu_rt["hbmBytes"])
        accel_kind = "tpu"
    else:
        device = torch.device("cpu")
        gpu_name_str = "CPU"
        vram_total_bytes = int(4 * (1024 ** 3))
        accel_kind = "cpu"
    vram_total_gb = vram_total_bytes / (1024 ** 3)

    preview_seconds = cfg["preview_seconds"]
    start_time = cfg["start_time"]
    denoise_strength = cfg["denoise_strength"]
    model_preset = cfg["model_preset"]
    use_fp16 = bool(cfg["use_fp16"] and has_cuda)
    use_bf16 = bool(cfg["use_fp16"] and is_tpu)
    resource_target_pct = cfg["resource_target_pct"]
    batch_size = cfg["batch_size"]
    torch_compile_mode = cfg["torch_compile_mode"]
    size_profile = cfg["size_profile"]
    encoder_choice = cfg["encoder_choice"]
    overwrite_output = cfg["overwrite_output"]

    eff_dur = min(float(preview_seconds), total_dur) if (preview_seconds > 0 and total_dur > 0) else total_dur
    if preview_seconds <= 0 and start_time.strip() in ("", "00:00:00") and exact_nb_frames > 0:
        est_frames = exact_nb_frames
    else:
        est_frames = int(round(eff_dur * fps_val)) if eff_dur > 0 else None

    eff_denoise = denoise_strength
    auto_reason = None
    if model_preset.lower().startswith("auto"):
        selected_model, eff_denoise, auto_reason = choose_auto_model(
            in_w, in_h, target_resolution, est_frames or 0, bpp_val, gpu_name_str, denoise_strength
        )
    else:
        selected_model = model_preset

    dest_dir.mkdir(parents=True, exist_ok=True)
    out_filename = build_upscaled_video_name(cur_input.name)
    final_output = dest_dir / out_filename

    if final_output.exists() and not overwrite_output:
        log_fn(f"[{idx}/{total_count}] SKIP   {final_output.name} | File đã tồn tại (overwrite=False)")
        return final_output, "Skipped (Exists)", "-"

    if cancel_check:
        cancel_check()

    if file_entry is not None:
        file_entry.update({
            "status": "active",
            "inRes": f"{in_w}x{in_h}",
            "model": selected_model,
            "framesTotal": est_frames or 0,
            "framesDone": 0,
            "percent": 0,
        })
    _emit_upscale_progress(
        job_state,
        active=True,
        phase="loading_model",
        mediaType="video",
        accelerator=accel_kind,
        currentFile=cur_input.name,
        outputFile=out_filename,
        inRes=f"{in_w}x{in_h}",
        targetResolution=target_resolution,
        model=selected_model,
        gpuName=gpu_name_str,
        vramTotalGb=round(vram_total_gb, 1),
        framesDone=0,
        framesTotal=est_frames or 0,
        filePercent=0.0,
        fps=0.0,
        etaSeconds=None,
        **({"files": batch_files} if batch_files is not None else {}),
    )

    if is_tpu and tpu_rt is not None:
        model = _get_cached_tpu_model(
            selected_model,
            device=device,
            use_bf16=use_bf16,
            dn_strength=eff_denoise,
            weights_dir=weights_dir,
            log_fn=log_fn,
            sync_fn=tpu_rt["sync"],
        )
        model_dtype = torch.bfloat16 if use_bf16 else torch.float32
    else:
        model = load_upscale_model(
            selected_model,
            device=device,
            fp16=use_fp16,
            dn_strength=eff_denoise,
            weights_dir=weights_dir,
            log_fn=log_fn,
        )
        model_dtype = torch.float16 if (use_fp16 and model.supports_half) else torch.float32
    out_w, out_h = compute_target_dims(in_w, in_h, model.scale, target_resolution)

    use_channels_last = False
    allow_nhwc = has_cuda and model_dtype == torch.float16 and not _is_reparam_or_span_model(model.model, selected_model)
    if allow_nhwc:
        try:
            model.model.to(memory_format=torch.channels_last)
            with torch.inference_mode():
                _t_cl = torch.zeros(
                    (1, 3, min(64, in_h), min(64, in_w)),
                    device=device,
                    dtype=model_dtype,
                ).contiguous(memory_format=torch.channels_last)
                _ = model(_t_cl)
            use_channels_last = True
        except Exception:
            model.model.to(memory_format=torch.contiguous_format)
            use_channels_last = False

    def _prepare_gpu_batch(batch_u8_gpu: Any) -> Any:
        x = batch_u8_gpu.permute(0, 3, 1, 2)
        if use_channels_last:
            x = x.contiguous(memory_format=torch.channels_last)
        else:
            x = x.contiguous()
        return x.to(dtype=model_dtype).div_(255.0)

    def _calibrate_vram_batch(target_pct: int, user_b: int) -> int:
        if not has_cuda:
            return max(1, user_b) if user_b > 0 else 2
        if user_b > 0:
            cand = user_b
        else:
            prev_bench = torch.backends.cudnn.benchmark
            torch.backends.cudnn.benchmark = False
            try:
                with torch.inference_mode():
                    torch.cuda.empty_cache()
                    torch.cuda.reset_peak_memory_stats(device)
                    _d1 = torch.zeros((1, in_h, in_w, 3), dtype=torch.uint8, device=device)
                    _y1 = rgb_to_yuv420p_bt709(resize_sr_gpu(model(_prepare_gpu_batch(_d1)), out_h, out_w))
                    peak_1 = torch.cuda.max_memory_allocated(device)
                    del _d1, _y1

                    torch.cuda.empty_cache()
                    torch.cuda.reset_peak_memory_stats(device)
                    _d3 = torch.zeros((3, in_h, in_w, 3), dtype=torch.uint8, device=device)
                    _y3 = rgb_to_yuv420p_bt709(resize_sr_gpu(model(_prepare_gpu_batch(_d3)), out_h, out_w))
                    peak_3 = torch.cuda.max_memory_allocated(device)
                    del _d3, _y3
            finally:
                torch.backends.cudnn.benchmark = prev_bench

            per_frame = max((peak_3 - peak_1) / 2.0, float(in_w * in_h * 3 * 12))
            budget = vram_total_bytes * (target_pct / 100.0)
            usable_extra = max(0.0, (budget - peak_1) * 0.90)
            raw_b = 1 + int(usable_extra // per_frame)
            if selected_model in HEAVY_MODELS:
                max_cap = 8
            elif model.scale == 4:
                max_cap = 16
            else:
                max_cap = 20
            if est_frames and est_frames > 0:
                max_cap = min(max_cap, max(4, est_frames))
            cand = max(1, min(raw_b, max_cap))
            if cand >= 8:
                cand = (cand // 4) * 4
            elif cand >= 2:
                cand = (cand // 2) * 2

        while cand >= 1:
            try:
                with torch.inference_mode():
                    torch.cuda.empty_cache()
                    _dt = torch.zeros((cand, in_h, in_w, 3), dtype=torch.uint8, device=device)
                    _yt = rgb_to_yuv420p_bt709(resize_sr_gpu(model(_prepare_gpu_batch(_dt)), out_h, out_w))
                    del _dt, _yt
                return cand
            except torch.cuda.OutOfMemoryError:
                torch.cuda.empty_cache()
                if cand == 1:
                    return 1
                cand = max(1, (int(cand * 0.75) // 2) * 2)
        return 1

    if is_tpu:
        eff_batch = _calibrate_tpu_batch(
            in_w, in_h, model.scale, selected_model, resource_target_pct, batch_size, vram_total_bytes
        )
    else:
        eff_batch = _calibrate_vram_batch(resource_target_pct, batch_size)

    tpu_pad_w, tpu_pad_h = (0, 0)
    if is_tpu:
        size_req = getattr(model, "size_requirements", None)
        if size_req is not None and hasattr(size_req, "get_padding"):
            tpu_pad_w, tpu_pad_h = size_req.get_padding(in_w, in_h)

    def _run_tpu_static_sr(x: Any) -> Any:
        if tpu_pad_w or tpu_pad_h:
            x = F.pad(x, (0, int(tpu_pad_w), 0, int(tpu_pad_h)), mode="replicate")
        raw_net = getattr(model, "model", model)
        out = raw_net(x)
        if tpu_pad_w or tpu_pad_h:
            out = out[..., : in_h * model.scale, : in_w * model.scale]
        return out

    def _run_compiled_sr(comp_net: Any, x: Any) -> Any:
        _, _, h, w = x.shape
        pad_w, pad_h = model.size_requirements.get_padding(w, h)
        if pad_w or pad_h:
            rp_w, rp_h = min(pad_w, w - 1), min(pad_h, h - 1)
            x = F.pad(x, (0, rp_w, 0, rp_h), mode="reflect")
            if pad_w - rp_w or pad_h - rp_h:
                x = F.pad(x, (0, pad_w - rp_w, 0, pad_h - rp_h), mode="replicate")
        out = comp_net(x)
        if pad_w or pad_h:
            out = out[..., : h * model.scale, : w * model.scale]
        return out

    want_compile = has_cuda and (
        torch_compile_mode.lower().startswith("on")
        or (torch_compile_mode.lower().startswith("auto") and (est_frames or 0) >= 1500 and eff_batch >= 4)
    )
    compiled_callable = None
    if want_compile and hasattr(torch, "compile"):
        try:
            _emit_upscale_progress(job_state, phase="compiling", **({"files": batch_files} if batch_files is not None else {}))
            log_fn(f"[COMPILE] Tối ưu hóa nhân GPU bằng torch.compile cho {cur_input.name}...")
            _comp_net = torch.compile(model.model, mode="default", dynamic=False)
            with torch.inference_mode():
                _warm_in = torch.zeros((eff_batch, in_h, in_w, 3), dtype=torch.uint8, device=device)
                _warm_x = _prepare_gpu_batch(_warm_in)
                # Run 2 warmup passes to verify steady-state Inductor stride guards on Call #2 as well.
                _ = _run_compiled_sr(_comp_net, _warm_x)
                _ = _run_compiled_sr(_comp_net, _warm_x)
                torch.cuda.synchronize(device)
                compiled_callable = _comp_net
                del _warm_in, _warm_x
        except Exception as exc:
            try:
                torch._dynamo.reset()
            except Exception:
                pass
            log_fn(f"[COMPILE] Bỏ qua torch.compile, dùng chế độ Eager ({exc})")
            compiled_callable = None

    ram_total_b, ram_avail_b = get_linux_ram_bytes()
    ram_used_now = max(0, ram_total_b - ram_avail_b)
    ram_target_total = int(ram_total_b * (min(resource_target_pct, 80) / 100.0))
    ram_safe_ceiling = max(0, ram_avail_b - int(2.6 * (1024 ** 3)))
    ram_pool_budget = max(int(384 * (1024 ** 2)), min(max(0, ram_target_total - ram_used_now), ram_safe_ceiling))

    frame_in_bytes = in_w * in_h * 3
    frame_out_yuv_bytes = (out_w * out_h * 3) // 2
    batch_in_bytes = eff_batch * frame_in_bytes
    batch_out_bytes = eff_batch * frame_out_yuv_bytes

    num_out_slots = 4
    out_pool_bytes = num_out_slots * batch_out_bytes
    rem_in_budget = max(batch_in_bytes * 4, ram_pool_budget - out_pool_bytes)
    total_est_batches = ((est_frames + eff_batch - 1) // eff_batch) if (est_frames and est_frames > 0) else 64
    num_in_slots = max(4, min(total_est_batches + 2, int(rem_in_budget // max(1, batch_in_bytes)), 96))
    max_pinned_in = max(3, min(num_in_slots, int((1.5 * (1024 ** 3)) // max(1, batch_in_bytes)))) if has_cuda else 0

    in_free_q: queue.Queue[Any] = queue.Queue()
    in_ready_q: queue.Queue[Any] = queue.Queue()
    out_free_q: queue.Queue[Any] = queue.Queue()
    out_ready_q: queue.Queue[Any] = queue.Queue()

    for s_idx in range(num_in_slots):
        t_buf = (
            torch.zeros((eff_batch, in_h, in_w, 3), dtype=torch.uint8)
            if is_tpu
            else torch.empty((eff_batch, in_h, in_w, 3), dtype=torch.uint8, pin_memory=bool(has_cuda and s_idx < max_pinned_in))
        )
        in_free_q.put(t_buf)

    for _ in range(num_out_slots):
        t_out = torch.zeros((eff_batch, frame_out_yuv_bytes), dtype=torch.uint8, pin_memory=has_cuda)
        ev = torch.cuda.Event() if has_cuda else None
        out_free_q.put((t_out, ev))

    alloc_ram_gb = (num_in_slots * batch_in_bytes + out_pool_bytes) / (1024 ** 3)

    tmp_root = work_dir or dest_dir
    tmp_root.mkdir(parents=True, exist_ok=True)
    local_tmp_out = tmp_root / f"_tmp_upscaled_output_{idx}_{os.getpid()}.mp4"
    local_tmp_out.unlink(missing_ok=True)

    working_input = cur_input
    staged_local_input: Path | None = None
    if os.name != "nt" and str(cur_input).startswith("/content/drive"):
        try:
            in_sz = cur_input.stat().st_size
            _, _, disk_free = shutil.disk_usage(tmp_root)
            if in_sz < 4 * (1024 ** 3) and disk_free > in_sz + 6 * (1024 ** 3):
                staged_local_input = tmp_root / f"_staged_input_{idx}{cur_input.suffix}"
                shutil.copyfile(str(cur_input), str(staged_local_input))
                working_input = staged_local_input
        except Exception:
            working_input = cur_input

    use_nvdec = has_cuda and nvdec_available(working_input)
    want_hevc = "hevc_nvenc" in encoder_choice
    nvenc_codec = "hevc_nvenc" if (has_cuda and want_hevc and nvenc_available("hevc_nvenc")) else "h264_nvenc"
    use_nvenc = has_cuda and (not encoder_choice.startswith("libx264")) and nvenc_available(nvenc_codec)

    is_1080_plus = min(out_w, out_h) >= 1000
    if size_profile.startswith("Tiết kiệm"):
        nv_cq, cpu_crf = (28, 24)
        max_br, buf_br = ("5M", "10M") if is_1080_plus else ("2.8M", "5.6M")
    elif size_profile.startswith("Chất lượng lưu trữ"):
        nv_cq, cpu_crf = (21, 18)
        max_br, buf_br = ("16M", "32M") if is_1080_plus else ("9M", "18M")
    else:
        nv_cq, cpu_crf = (24, 20)
        max_br, buf_br = ("12M", "24M") if is_1080_plus else ("6M", "12M")

    cpu_cores = os.cpu_count() or 2
    dec_threads = min(8, max(2, cpu_cores // 3)) if (not use_nvdec and cpu_cores >= 12) else 0
    enc_threads = min(16, max(2, cpu_cores - 4)) if (not use_nvenc and cpu_cores >= 12) else 0
    cpu_preset = "faster" if cpu_cores >= 16 else "veryfast"

    dec_short = "NVDEC" if use_nvdec else (f"CPU({dec_threads}t)" if dec_threads > 0 else "CPU")
    enc_short = nvenc_codec if use_nvenc else (f"libx264({enc_threads}t)" if enc_threads > 0 else "libx264")
    if is_tpu:
        prec_str = "BF16+XLA" if use_bf16 else "FP32+XLA"
        pipe_str = f"{dec_short} -> TPU MXU -> {enc_short}"
        vram_reserved_gb = _get_tpu_hbm_used_gb(tpu_rt)
    else:
        prec_str = (
            f"{'FP16+NHWC' if use_channels_last else ('FP16' if model_dtype == torch.float16 else 'FP32')}"
            f"{'+Compile' if compiled_callable is not None else ''}"
        )
        pipe_str = f"{dec_short} -> Tensor Cores -> {enc_short}" if has_cuda else f"{dec_short} -> PyTorch -> {enc_short}"
        vram_reserved_gb = (torch.cuda.memory_reserved(device) / (1024 ** 3)) if has_cuda else 0.0

    if file_entry is not None:
        file_entry.update({
            "status": "active",
            "outputName": out_filename,
            "inRes": f"{in_w}x{in_h}",
            "outRes": f"{out_w}x{out_h}",
            "model": selected_model,
            "framesTotal": est_frames or 0,
            "framesDone": 0,
            "percent": 0,
        })
    _emit_upscale_progress(
        job_state,
        active=True,
        phase="upscaling_video",
        mediaType="video",
        accelerator=accel_kind,
        currentFile=cur_input.name,
        outputFile=out_filename,
        inRes=f"{in_w}x{in_h}",
        outRes=f"{out_w}x{out_h}",
        targetResolution=target_resolution,
        model=selected_model,
        precision=prec_str,
        batchSize=eff_batch,
        pipeline=pipe_str,
        vramUsedGb=round(vram_reserved_gb, 2),
        vramTotalGb=round(vram_total_gb, 1),
        gpuName=gpu_name_str,
        framesDone=0,
        framesTotal=est_frames or 0,
        filePercent=0.0,
        fps=0.0,
        etaSeconds=None,
        **({"files": batch_files} if batch_files is not None else {}),
    )
    log_fn(
        f"[{idx}/{total_count}] START  {cur_input.name} ({in_w}x{in_h} -> {out_w}x{out_h}) | "
        f"{selected_model} ({prec_str}, B={eff_batch}) | {dec_short} -> {enc_short}"
    )

    time_args_in: list[str] = []
    if start_time.strip() and start_time.strip() != "00:00:00":
        time_args_in.extend(["-ss", start_time.strip()])
    if preview_seconds > 0:
        time_args_in.extend(["-t", str(preview_seconds)])

    hwaccel_in = ["-hwaccel", "cuda"] if use_nvdec else ["-threads", str(dec_threads)]
    ffmpeg_in_cmd = [
        "ffmpeg", "-v", "error",
        *hwaccel_in,
        *time_args_in,
        "-i", str(working_input),
        "-vf", f"fps={fps_str}",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "pipe:1",
    ]

    if use_nvenc:
        video_enc_opts = [
            "-c:v", nvenc_codec,
            "-preset", "p4",
            "-rc", "vbr",
            "-cq", str(nv_cq),
            "-b:v", "0",
            "-maxrate", max_br,
            "-bufsize", buf_br,
            "-spatial-aq", "1",
            "-aq-strength", "8",
            "-pix_fmt", "yuv420p",
        ]
    else:
        video_enc_opts = [
            "-c:v", "libx264",
            "-preset", cpu_preset,
            *(["-threads", str(enc_threads)] if enc_threads > 0 else []),
            "-crf", str(cpu_crf),
            "-maxrate", max_br,
            "-bufsize", buf_br,
            "-pix_fmt", "yuv420p",
        ]

    ffmpeg_out_cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "yuv420p",
        "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-s", f"{out_w}x{out_h}",
        "-r", fps_str,
        "-i", "pipe:0",
        *time_args_in,
        "-i", str(working_input),
        "-map", "0:v:0", "-map", "1:a?",
        *video_enc_opts,
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
        "-c:a", "aac", "-b:a", "192k",
        *embed_video_optimization_metadata_args(),
        "-movflags", "+faststart+use_metadata_tags",
        str(local_tmp_out),
    ]

    in_errs: list[str] = []
    out_errs: list[str] = []
    proc_env = os.environ.copy()

    def _drain_stderr(pipe: Any, sink: list[str]) -> None:
        try:
            for line in iter(pipe.readline, b""):
                sink.append(line.decode("utf-8", errors="replace").strip())
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    p_in = subprocess.Popen(ffmpeg_in_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=32 * 1024 * 1024, env=proc_env)
    p_out = subprocess.Popen(ffmpeg_out_cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=32 * 1024 * 1024, env=proc_env)

    try:
        import fcntl  # type: ignore[import-not-found]
        F_SETPIPE_SZ = 1031
        if p_in.stdout:
            fcntl.fcntl(p_in.stdout.fileno(), F_SETPIPE_SZ, 1048576)
        if p_out.stdin:
            fcntl.fcntl(p_out.stdin.fileno(), F_SETPIPE_SZ, 1048576)
    except Exception:
        pass

    t_err_in = threading.Thread(target=_drain_stderr, args=(p_in.stderr, in_errs), daemon=True)
    t_err_out = threading.Thread(target=_drain_stderr, args=(p_out.stderr, out_errs), daemon=True)
    t_err_in.start()
    t_err_out.start()

    stop_event = threading.Event()
    worker_errors: list[BaseException] = []

    def _q_put(q_obj: queue.Queue[Any], item: Any) -> bool:
        while not stop_event.is_set():
            try:
                q_obj.put(item, timeout=0.5)
                return True
            except queue.Full:
                continue
        return False

    def _q_get(q_obj: queue.Queue[Any]) -> tuple[bool, Any]:
        while not stop_event.is_set():
            try:
                return True, q_obj.get(timeout=0.5)
            except queue.Empty:
                continue
        return False, None

    def _reader_worker() -> None:
        try:
            while not stop_event.is_set():
                ok, t_buf = _q_get(in_free_q)
                if not ok or t_buf is None:
                    break
                flat_mv = memoryview(t_buf.numpy()).cast("B")
                got_bytes = _read_exact_into(p_in.stdout, flat_mv)
                cur_b = got_bytes // frame_in_bytes
                if cur_b == 0:
                    in_free_q.put(t_buf)
                    break
                if not _q_put(in_ready_q, (cur_b, t_buf)):
                    break
                if cur_b < eff_batch:
                    break
        except Exception as e:
            worker_errors.append(e)
            stop_event.set()
        finally:
            _q_put(in_ready_q, None)

    def _writer_worker() -> None:
        try:
            while not stop_event.is_set():
                ok, item = _q_get(out_ready_q)
                if not ok or item is None:
                    break
                cur_b, t_out, ev = item
                if ev is not None:
                    ev.synchronize()
                mv_out = memoryview(t_out[:cur_b].numpy()).cast("B")
                if p_out.stdin:
                    p_out.stdin.write(mv_out)
                out_free_q.put((t_out, ev))
        except BrokenPipeError:
            stop_event.set()
        except Exception as e:
            worker_errors.append(e)
            stop_event.set()
        finally:
            if p_out.stdin:
                try:
                    p_out.stdin.close()
                except Exception:
                    pass

    processed_frames = 0
    t0 = time.time()
    fps_t0 = t0
    fps_base_frames = 0
    peak_tpu_hbm_gb = vram_reserved_gb if is_tpu else 0.0
    last_ui_tick = t0
    if has_cuda:
        torch.cuda.reset_peak_memory_stats(device)
        compute_stream = torch.cuda.current_stream(device)
        d2h_stream = torch.cuda.Stream(device=device)
        compute_done_ev = torch.cuda.Event()
    else:
        compute_stream = None
        d2h_stream = None
        compute_done_ev = None

    t_reader = threading.Thread(target=_reader_worker, daemon=True)
    t_writer = threading.Thread(target=_writer_worker, daemon=True)
    t_reader.start()
    t_writer.start()

    cancelled_exc: BaseException | None = None
    try:
        with torch.inference_mode():
            while not stop_event.is_set():
                if cancel_check:
                    cancel_check()

                ok, item = _q_get(in_ready_q)
                if not ok or item is None:
                    break
                cur_b, t_buf = item

                if is_tpu:
                    shape_key = (selected_model, str(model_dtype), eff_batch, in_h, in_w, out_h, out_w)
                    need_xla_compile_notice = shape_key not in _TPU_COMPILED_SHAPES
                    compile_t0 = 0.0
                    if need_xla_compile_notice:
                        compile_t0 = time.time()
                        _emit_upscale_progress(
                            job_state,
                            active=True,
                            phase="compiling_tpu",
                            accelerator="tpu",
                            **({"files": batch_files} if batch_files is not None else {}),
                        )
                        log_fn(
                            f"[XLA] Biên dịch đồ thị TPU ({prec_str}) cho khung {eff_batch}x{in_w}x{in_h} -> {out_w}x{out_h}..."
                        )

                    # Send full fixed `eff_batch` buffer so tail batches (`cur_b < eff_batch`) reuse the exact compiled XLA graph.
                    u8_dev = t_buf.to(device=device)
                    in_free_q.put(t_buf)
                    inp = _prepare_gpu_batch(u8_dev)
                    del u8_dev
                    sr = _run_tpu_static_sr(inp)
                    del inp
                    sr_resized = resize_sr_gpu(sr, out_h, out_w)
                    del sr
                    yuv_dev = rgb_to_yuv420p_bt709(sr_resized)
                    del sr_resized
                    yuv_cpu = yuv_dev.cpu()
                    del yuv_dev
                    if tpu_rt is not None:
                        tpu_rt["sync"]()

                    if need_xla_compile_notice:
                        _TPU_COMPILED_SHAPES.add(shape_key)
                        comp_sec = max(0.1, time.time() - compile_t0)
                        log_fn(f"[XLA] Biên dịch hoàn tất trong {comp_sec:.1f}s | Bắt đầu suy luận tốc độ cao")
                        fps_t0 = time.time()
                        fps_base_frames = processed_frames + cur_b

                    ok_out, out_slot = _q_get(out_free_q)
                    if not ok_out or out_slot is None:
                        break
                    t_out, ev = out_slot
                    t_out[:cur_b].copy_(yuv_cpu[:cur_b])
                    del yuv_cpu
                else:
                    u8_gpu = t_buf[:cur_b].to(device=device, non_blocking=bool(has_cuda and t_buf.is_pinned()))
                    in_free_q.put(t_buf)

                    inp = _prepare_gpu_batch(u8_gpu)
                    del u8_gpu

                    try:
                        if compiled_callable is not None and cur_b == eff_batch:
                            try:
                                sr = _run_compiled_sr(compiled_callable, inp)
                            except Exception as comp_exc:
                                if has_cuda and "out of memory" in str(comp_exc).lower():
                                    raise
                                compiled_callable = None
                                try:
                                    torch._dynamo.reset()
                                except Exception:
                                    pass
                                log_fn(f"[COMPILE] Tự động chuyển sang chế độ Eager ({comp_exc})")
                                sr = model(inp)
                        else:
                            sr = model(inp)
                    except RuntimeError as oom_exc:
                        if has_cuda and "out of memory" in str(oom_exc).lower():
                            torch.cuda.empty_cache()
                            sr_list = [infer_tiled_single(model, inp[i : i + 1]) for i in range(cur_b)]
                            sr = torch.cat(sr_list, dim=0)
                        else:
                            raise

                    del inp
                    sr_resized = resize_sr_gpu(sr, out_h, out_w)
                    del sr
                    yuv_gpu = rgb_to_yuv420p_bt709(sr_resized)
                    del sr_resized

                    ok_out, out_slot = _q_get(out_free_q)
                    if not ok_out or out_slot is None:
                        break
                    t_out, ev = out_slot

                    if has_cuda and compute_done_ev is not None and d2h_stream is not None and ev is not None:
                        compute_done_ev.record(compute_stream)
                        with torch.cuda.stream(d2h_stream):
                            d2h_stream.wait_event(compute_done_ev)
                            t_out[:cur_b].copy_(yuv_gpu, non_blocking=True)
                            ev.record(d2h_stream)
                        yuv_gpu.record_stream(d2h_stream)
                    else:
                        t_out[:cur_b].copy_(yuv_gpu)
                    del yuv_gpu

                if not _q_put(out_ready_q, (cur_b, t_out, ev)):
                    break

                processed_frames += cur_b
                now = time.time()
                if now - last_ui_tick >= 0.8 or (est_frames and processed_frames >= est_frames):
                    last_ui_tick = now
                    elapsed_so_far = max(now - t0, 1e-3)
                    if is_tpu and fps_base_frames > 0 and processed_frames > fps_base_frames:
                        cur_fps = (processed_frames - fps_base_frames) / max(now - fps_t0, 1e-3)
                    else:
                        cur_fps = processed_frames / elapsed_so_far
                    frame_ratio = min(0.99, processed_frames / est_frames) if (est_frames and est_frames > 0) else 0.5
                    if on_frame_progress:
                        on_frame_progress(frame_ratio)
                    pct_disp = int(round(frame_ratio * 100))
                    eta_sec = (
                        max(0, int(round((est_frames - processed_frames) / max(cur_fps, 1e-3))))
                        if (est_frames and est_frames > processed_frames and cur_fps > 0)
                        else 0
                    )
                    if has_cuda:
                        cur_vram_gb = (
                            max(torch.cuda.memory_allocated(device), torch.cuda.memory_reserved(device)) / (1024 ** 3)
                        )
                    elif is_tpu:
                        cur_vram_gb = _get_tpu_hbm_used_gb(tpu_rt)
                        peak_tpu_hbm_gb = max(peak_tpu_hbm_gb, cur_vram_gb)
                    else:
                        cur_vram_gb = 0.0
                    if file_entry is not None:
                        file_entry.update({
                            "status": "active",
                            "framesDone": processed_frames,
                            "framesTotal": est_frames or 0,
                            "percent": pct_disp,
                            "fps": round(cur_fps, 2),
                            "etaSeconds": eta_sec,
                        })
                    if job_state is not None:
                        job_state.set(current_file=f"{cur_input.name} ({pct_disp}% - {cur_fps:.1f} FPS)")
                        _emit_upscale_progress(
                            job_state,
                            active=True,
                            phase="upscaling_video",
                            accelerator=accel_kind,
                            framesDone=processed_frames,
                            framesTotal=est_frames or 0,
                            filePercent=round(frame_ratio * 100.0, 1),
                            fps=round(cur_fps, 2),
                            etaSeconds=eta_sec,
                            elapsedSeconds=round(elapsed_so_far, 1),
                            vramUsedGb=round(cur_vram_gb, 2),
                            **({"files": batch_files} if batch_files is not None else {}),
                        )

        _q_put(out_ready_q, None)
        t_writer.join()
    except BaseException as exc:
        cancelled_exc = exc
        stop_event.set()
        for proc in (p_in, p_out):
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
        raise
    finally:
        stop_event.set()
        t_reader.join(timeout=3)
        t_writer.join(timeout=5)
        if p_in.stdout:
            try:
                p_in.stdout.close()
            except Exception:
                pass
        if cancelled_exc is not None:
            for proc in (p_in, p_out):
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            local_tmp_out.unlink(missing_ok=True)
        rc_in = p_in.wait(timeout=10) if p_in.poll() is not None or cancelled_exc is None else -1
        rc_out = p_out.wait(timeout=15) if p_out.poll() is not None or cancelled_exc is None else -1
        t_err_in.join(timeout=2)
        t_err_out.join(timeout=2)
        if staged_local_input is not None and staged_local_input.exists():
            staged_local_input.unlink(missing_ok=True)

    if worker_errors:
        local_tmp_out.unlink(missing_ok=True)
        raise RuntimeError(f"Lỗi luồng xử lý AI Upscale: {worker_errors[0]}")

    elapsed = max(time.time() - t0, 1e-6)
    if has_cuda:
        peak_vram_gb = (
            max(torch.cuda.max_memory_allocated(device), torch.cuda.max_memory_reserved(device)) / (1024 ** 3)
        )
    elif is_tpu:
        peak_vram_gb = max(peak_tpu_hbm_gb, _get_tpu_hbm_used_gb(tpu_rt))
    else:
        peak_vram_gb = 0.0

    if rc_out != 0 or not local_tmp_out.exists() or local_tmp_out.stat().st_size < 1024:
        local_tmp_out.unlink(missing_ok=True)
        err_msg = "\n".join(in_errs + out_errs)
        raise RuntimeError(f"Quá trình FFmpeg gặp lỗi (rc_in={rc_in}, rc_out={rc_out}):\n{err_msg}")

    final_output.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(str(local_tmp_out), str(final_output))
    except OSError:
        shutil.copyfile(str(local_tmp_out), str(final_output))
        local_tmp_out.unlink(missing_ok=True)

    # Xóa ngay bản tải tạm trên SSD Colab (không bao giờ xóa file trên Google Drive mount!)
    if (
        cfg["delete_uploaded_source_in_colab"]
        and cur_input.exists()
        and not str(cur_input.resolve()).startswith("/content/drive/")
        and final_output.exists()
        and final_output.resolve() != cur_input.resolve()
    ):
        cur_input.unlink(missing_ok=True)

    out_bytes = final_output.stat().st_size
    size_mb = out_bytes / (1024 * 1024)
    avg_fps = processed_frames / elapsed
    res_tag = _short_res_tag(target_resolution, out_w, out_h)
    if file_entry is not None:
        file_entry.update({
            "status": "done",
            "outputName": final_output.name,
            "percent": 100,
            "framesDone": processed_frames,
            "framesTotal": max(processed_frames, est_frames or 0),
            "fps": round(avg_fps, 2),
            "durationSec": round(elapsed, 1),
            "outputSizeBytes": out_bytes,
            "etaSeconds": 0,
        })
    mem_tag = "HBM" if is_tpu else "VRAM"
    log_fn(
        f"[{idx}/{total_count}] DONE   {final_output.name} | {processed_frames} frames in {elapsed:.1f}s "
        f"({avg_fps:.2f} FPS) | {size_mb:.2f} MB | Peak {mem_tag} {peak_vram_gb:.2f}/{vram_total_gb:.1f} GB"
    )
    if on_frame_progress:
        on_frame_progress(1.0)
    return final_output, f"Upscaled ({res_tag})", f"{res_tag} · {selected_model}"


def process_single_image(
    cur_input: Path,
    dest_dir: Path,
    cfg: dict[str, Any],
    idx: int,
    total_count: int,
    *,
    model_cache: dict[tuple[str, float, bool], Any] | None = None,
    job_state: Any = None,
    weights_dir: Path | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> tuple[Path | None, str, str]:
    """
    Upscale a single image using the exact same Spandrel + MODEL_ZOO engine as Video2X.
    Supports RGB & RGBA transparency, EXIF orientation, tiled OOM protection,
    `>= target_resolution` auto-skip, and embeds `VaultBox_Optimized:true` metadata.
    """
    from PIL import Image as PILImage, ImageOps

    log_fn = getattr(job_state, "log", None) or (lambda msg: logger.info("%s", msg))
    if cancel_check:
        cancel_check()

    if not cfg["force_reoptimize"] and (is_upscaled_filename(cur_input.name) or is_image_already_optimized(cur_input)):
        log_fn(f"[{idx}/{total_count}] SKIP   {cur_input.name} | Đã được tối ưu/upscale trước đó")
        return None, "Skipped (Already optimized)", "-"

    with PILImage.open(cur_input) as raw_img:
        img = ImageOps.exif_transpose(raw_img)
        in_w, in_h = img.size
        has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
        work_img = img.convert("RGBA") if has_alpha else img.convert("RGB")

    target_resolution = cfg["target_resolution"]
    if is_already_at_or_above_target(in_w, in_h, target_resolution):
        log_fn(
            f"[{idx}/{total_count}] SKIP   {cur_input.name} ({in_w}x{in_h}) | Đã đạt target {target_resolution}"
        )
        return None, f"Skipped (Already >= {target_resolution.split()[0]})", f"{in_w}x{in_h}"

    ensure_upscale_dependencies(log_fn=log_fn)
    import numpy as np
    import torch
    import torch.nn.functional as F

    has_cuda = torch.cuda.is_available()
    tpu_hw = {"hasTpu": False, "tpuName": "", "tpuHbmGb": 0.0} if has_cuda else detect_tpu_hardware()
    is_tpu = bool(not has_cuda and tpu_hw["hasTpu"])
    tpu_rt = _init_tpu_runtime(log_fn=log_fn) if is_tpu else None
    device = torch.device("cuda") if has_cuda else (tpu_rt["device"] if tpu_rt is not None else torch.device("cpu"))
    gpu_name_str = torch.cuda.get_device_name(0) if has_cuda else (str(tpu_rt["tpuName"]) if tpu_rt is not None else "CPU")
    use_fp16 = bool(cfg["use_fp16"] and has_cuda)
    use_bf16 = bool(cfg["use_fp16"] and is_tpu)
    denoise_strength = cfg["denoise_strength"]
    model_preset = cfg["model_preset"]
    size_profile = cfg["size_profile"]

    file_size = cur_input.stat().st_size
    est_bpp = (file_size * 8.0) / max(1, in_w * in_h * 12)
    eff_denoise = denoise_strength
    if model_preset.lower().startswith("auto"):
        selected_model, eff_denoise, _ = choose_auto_model(
            in_w, in_h, target_resolution, 1, est_bpp, gpu_name_str, denoise_strength
        )
    else:
        selected_model = model_preset

    if is_tpu and tpu_rt is not None:
        model = _get_cached_tpu_model(
            selected_model,
            device=device,
            use_bf16=use_bf16,
            dn_strength=eff_denoise,
            weights_dir=weights_dir,
            log_fn=log_fn,
            sync_fn=tpu_rt["sync"],
        )
        model_dtype = torch.bfloat16 if use_bf16 else torch.float32
    else:
        cache_key = (selected_model, round(eff_denoise, 3), use_fp16)
        if model_cache is not None and cache_key in model_cache:
            model = model_cache[cache_key]
        else:
            model = load_upscale_model(
                selected_model,
                device=device,
                fp16=use_fp16,
                dn_strength=eff_denoise,
                weights_dir=weights_dir,
                log_fn=log_fn,
            )
            if model_cache is not None:
                model_cache[cache_key] = model
        model_dtype = torch.float16 if (use_fp16 and model.supports_half) else torch.float32

    out_w, out_h = compute_target_dims(in_w, in_h, model.scale, target_resolution)

    arr = np.array(work_img, dtype=np.uint8)
    rgb_arr = arr[:, :, :3]
    alpha_arr = arr[:, :, 3] if has_alpha else None

    with torch.inference_mode():
        if is_tpu and tpu_rt is not None:
            img_shape_key = ("img_tile_512", selected_model, str(model_dtype))
            need_img_compile = img_shape_key not in _TPU_COMPILED_SHAPES
            if need_img_compile:
                _emit_upscale_progress(
                    job_state,
                    active=True,
                    phase="compiling_tpu",
                    accelerator="tpu",
                    gpuName=gpu_name_str,
                    model=selected_model,
                    precision="BF16+XLA" if use_bf16 else "FP32+XLA",
                )
                log_fn(
                    f"[XLA] Biên dịch đồ thị TPU ({'BF16+XLA' if use_bf16 else 'FP32+XLA'}) cho xử lý ảnh theo ô tĩnh 512x512..."
                )
            inp_cpu = torch.from_numpy(rgb_arr).permute(2, 0, 1).unsqueeze(0).to(dtype=model_dtype).div_(255.0)
            sr = infer_tiled_single(
                model,
                inp_cpu,
                tile=512,
                pad=16,
                fixed_tile_shape=True,
                tpu_device=device,
                sync_fn=tpu_rt["sync"],
            )
            del inp_cpu
            if need_img_compile:
                _TPU_COMPILED_SHAPES.add(img_shape_key)
                _emit_upscale_progress(
                    job_state,
                    active=True,
                    phase="upscaling_image",
                    accelerator="tpu",
                    gpuName=gpu_name_str,
                    model=selected_model,
                )
        else:
            inp = torch.from_numpy(rgb_arr).to(device=device).permute(2, 0, 1).unsqueeze(0).to(dtype=model_dtype).div_(255.0)
            use_tiled = (in_w * in_h > 1280 * 1280) or not has_cuda
            try:
                if use_tiled:
                    sr = infer_tiled_single(model, inp, tile=512, pad=16)
                else:
                    sr = model(inp)
            except RuntimeError as oom_exc:
                if has_cuda and "out of memory" in str(oom_exc).lower():
                    torch.cuda.empty_cache()
                    sr = infer_tiled_single(model, inp, tile=384, pad=16)
                else:
                    raise
            del inp

        sr_resized = resize_sr_gpu(sr, out_h, out_w)
        del sr
        out_rgb_u8 = (
            sr_resized.clamp(0.0, 1.0)
            .mul(255.0)
            .round()
            .to(torch.uint8)
            .squeeze(0)
            .permute(1, 2, 0)
            .cpu()
            .numpy()
        )
        del sr_resized

        if alpha_arr is not None:
            alpha_dev = torch.device("cpu") if is_tpu else device
            alpha_t = torch.from_numpy(alpha_arr).to(device=alpha_dev, dtype=torch.float32).unsqueeze(0).unsqueeze(0).div_(255.0)
            alpha_up = F.interpolate(alpha_t, size=(out_h, out_w), mode="bicubic", align_corners=False)
            out_alpha_u8 = alpha_up.clamp(0.0, 1.0).mul(255.0).round().to(torch.uint8).squeeze(0).squeeze(0).cpu().numpy()
            del alpha_t, alpha_up
            out_arr = np.dstack([out_rgb_u8, out_alpha_u8])
            out_pil = PILImage.fromarray(out_arr, mode="RGBA")
        else:
            out_pil = PILImage.fromarray(out_rgb_u8, mode="RGB")

    src_ext = cur_input.suffix.lower()
    is_archive_profile = size_profile.startswith("Chất lượng lưu trữ")
    if has_alpha:
        out_ext = ".webp" if src_ext == ".webp" else ".png"
    elif src_ext in (".png", ".webp") and is_archive_profile:
        out_ext = src_ext
    elif src_ext == ".webp":
        out_ext = ".webp"
    else:
        out_ext = ".jpg"

    if size_profile.startswith("Tiết kiệm"):
        img_q = 82
    elif is_archive_profile:
        img_q = 98
    else:
        img_q = 92

    dest_dir.mkdir(parents=True, exist_ok=True)
    final_output = dest_dir / f"{cur_input.stem}{out_ext}"
    tmp_out = dest_dir / f"._tmp_img_{idx}_{os.getpid()}{out_ext}"
    tmp_out.unlink(missing_ok=True)

    try:
        if out_ext == ".png":
            out_pil.save(tmp_out, format="PNG", optimize=True)
        elif out_ext == ".webp":
            if is_archive_profile and src_ext == ".webp":
                out_pil.save(tmp_out, format="WEBP", lossless=True)
            else:
                out_pil.save(tmp_out, format="WEBP", quality=img_q)
        else:
            if out_pil.mode != "RGB":
                out_pil = out_pil.convert("RGB")
            out_pil.save(tmp_out, format="JPEG", quality=img_q, optimize=True)

        embed_optimization_metadata(tmp_out)
        os.replace(str(tmp_out), str(final_output))
    finally:
        tmp_out.unlink(missing_ok=True)

    if (
        cfg["delete_uploaded_source_in_colab"]
        and cur_input.exists()
        and not str(cur_input.resolve()).startswith("/content/drive/")
        and final_output.exists()
        and final_output.resolve() != cur_input.resolve()
    ):
        cur_input.unlink(missing_ok=True)

    res_tag = _short_res_tag(target_resolution, out_w, out_h)
    out_img_mb = final_output.stat().st_size / (1024 * 1024) if final_output.exists() else 0.0
    log_fn(
        f"[{idx}/{total_count}] DONE   {cur_input.name} ({in_w}x{in_h} -> {out_w}x{out_h}) | {selected_model} | {out_img_mb:.2f} MB"
    )
    return final_output, f"Upscaled ({res_tag})", f"{res_tag} · {selected_model}"


def _is_inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def upscale_directory(
    input_dir: Path,
    output_dir: Path,
    options: dict[str, Any],
    job_state: Any,
    cancel_check: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    """
    Scan `input_dir`, upscale all eligible Videos and Images into `output_dir` using Video2X's Spandrel engine:
    - Videos -> `<stem>-upscaled.mp4` + `VaultBox_Optimized:true` metadata tag + deep RAM/GPU/SSD cleanup per video.
    - Images -> `<stem>.<ext>` + `VaultBox_Optimized:true` EXIF/PNG tag + shared VRAM model cache across batch.
    - Files already `>= target_resolution` or already carrying `VaultBox_Optimized:true` / `-upscaled` are skipped cleanly.
    """
    cfg = parse_upscale_options(options)
    log_fn = getattr(job_state, "log", None) or (lambda msg: logger.info("%s", msg))

    all_files = sorted(
        (p for p in input_dir.rglob("*") if p.is_file() and not _is_inside(p, output_dir)),
        key=lambda p: p.as_posix().lower(),
    )

    from .media_converter import is_file_filter_enabled, should_download_file_item

    videos: list[Path] = []
    images: list[Path] = []
    other_files: list[Path] = []
    filter_on = is_file_filter_enabled(options)

    for p in all_files:
        if filter_on and not should_download_file_item(p.name, options):
            other_files.append(p)
            continue
        ext = p.suffix.lower()
        if ext in VIDEO_EXTENSIONS:
            videos.append(p)
        elif ext in IMAGE_EXTENSIONS:
            images.append(p)
        else:
            other_files.append(p)

    total_candidates = len(videos) + len(images)
    results: list[dict[str, Any]] = []
    batch_files: list[dict[str, Any]] = []
    for b_idx, img_p in enumerate(images, start=1):
        batch_files.append({
            "index": b_idx,
            "name": img_p.relative_to(input_dir).as_posix(),
            "mediaType": "image",
            "status": "pending",
            "percent": 0,
        })
    for v_offset, vid_p in enumerate(videos, start=len(images) + 1):
        batch_files.append({
            "index": v_offset,
            "name": vid_p.relative_to(input_dir).as_posix(),
            "mediaType": "video",
            "status": "pending",
            "percent": 0,
        })

    done_ok_count = 0
    skipped_media_count = 0

    def _update_overall_progress(completed_units: float) -> None:
        if total_candidates > 0 and job_state is not None:
            pct = min(100.0, max(0.0, (completed_units / total_candidates) * 100.0))
            if hasattr(job_state, "progress"):
                setattr(job_state.progress, "optimize", pct)
            _emit_upscale_progress(
                job_state,
                overallPercent=round(pct, 1),
                completedItems=done_ok_count,
                skippedItems=skipped_media_count,
                totalItems=total_candidates,
                files=batch_files,
            )

    work_dir = output_dir.parent / "upscale_work"
    deep_cleanup_system(work_dir=work_dir, remove_weights=False, label="Trước khi chạy", log_fn=log_fn, verbose=False)

    if total_candidates > 0:
        parts_desc = []
        if videos:
            parts_desc.append(f"{len(videos)} video")
        if images:
            parts_desc.append(f"{len(images)} ảnh")
        short_prof = cfg["size_profile"].split("(")[0].strip()
        log_fn(f"[UPSCALE] Hàng đợi: {', '.join(parts_desc)} | Target: {cfg['target_resolution']} | Profile: {short_prof}")
        _emit_upscale_progress(
            job_state,
            merge=False,
            active=True,
            phase="preparing",
            totalItems=total_candidates,
            completedItems=0,
            skippedItems=0,
            currentIndex=1,
            overallPercent=0.0,
            filePercent=0.0,
            targetResolution=cfg["target_resolution"],
            files=batch_files,
        )

    done_count = 0

    # 1. Process Images (reusing loaded Spandrel model in VRAM across image batch for maximum speed)
    if images:
        img_model_cache: dict[tuple[str, float, bool], Any] = {}
        try:
            for i_idx, img_path in enumerate(images, start=1):
                if cancel_check:
                    cancel_check()
                rel_path = img_path.relative_to(input_dir)
                rel_str = rel_path.as_posix()
                orig_size = img_path.stat().st_size if img_path.exists() else 0
                cur_entry = batch_files[done_count]
                cur_entry["status"] = "active"
                if job_state is not None:
                    job_state.set(current_file=rel_str)
                _emit_upscale_progress(
                    job_state,
                    active=True,
                    phase="upscaling_image",
                    mediaType="image",
                    currentIndex=done_count + 1,
                    currentFile=img_path.name,
                    filePercent=50.0,
                    fps=0.0,
                    etaSeconds=None,
                    files=batch_files,
                )

                t_img0 = time.time()
                out_p, status_msg, badge = process_single_image(
                    img_path,
                    output_dir / rel_path.parent,
                    cfg,
                    i_idx,
                    len(images),
                    model_cache=img_model_cache,
                    job_state=job_state,
                    cancel_check=cancel_check,
                )
                img_dur = round(max(0.1, time.time() - t_img0), 1)
                if out_p is None or status_msg.lower().startswith("skipped"):
                    skipped_media_count += 1
                    cur_entry.update({"status": "skipped", "percent": 100, "skipReason": status_msg, "durationSec": img_dur})
                    results.append({
                        "name": rel_str,
                        "source_name": rel_str,
                        "original_size": orig_size,
                        "optimized_size": orig_size,
                        "status": status_msg,
                        "quality": badge,
                        "mode": "upscale",
                    })
                else:
                    done_ok_count += 1
                    out_sz = out_p.stat().st_size
                    out_rel = out_p.relative_to(output_dir).as_posix()
                    cur_entry.update({
                        "status": "done",
                        "outputName": out_p.name,
                        "percent": 100,
                        "outputSizeBytes": out_sz,
                        "durationSec": img_dur,
                        "model": badge.split("·")[-1].strip() if "·" in badge else badge,
                    })
                    results.append({
                        "name": out_rel,
                        "source_name": rel_str,
                        "original_size": orig_size,
                        "optimized_size": out_sz,
                        "status": status_msg,
                        "quality": badge,
                        "mode": "upscale",
                    })
                done_count += 1
                _update_overall_progress(float(done_count))
        finally:
            img_model_cache.clear()
            if cfg["clean_temp_and_cache"]:
                deep_cleanup_system(
                    work_dir=work_dir,
                    remove_weights=False,
                    label=f"Sau lô {len(images)} ảnh",
                    log_fn=log_fn,
                    verbose=False,
                )

    # 2. Process Videos sequentially with full per-video deep cleanup (exact Video2X batch loop)
    if videos:
        for v_idx, v_path in enumerate(videos, start=1):
            if cancel_check:
                cancel_check()
            rel_path = v_path.relative_to(input_dir)
            rel_str = rel_path.as_posix()
            orig_size = v_path.stat().st_size if v_path.exists() else 0
            base_done = float(done_count)
            cur_entry = batch_files[done_count]
            cur_entry["status"] = "active"
            if job_state is not None:
                job_state.set(current_file=rel_str)
            _emit_upscale_progress(
                job_state,
                active=True,
                phase="preparing",
                mediaType="video",
                currentIndex=done_count + 1,
                currentFile=v_path.name,
                filePercent=0.0,
                fps=0.0,
                etaSeconds=None,
                files=batch_files,
            )

            try:
                out_p, status_msg, badge = process_single_video(
                    v_path,
                    output_dir / rel_path.parent,
                    cfg,
                    v_idx,
                    len(videos),
                    job_state=job_state,
                    work_dir=work_dir,
                    cancel_check=cancel_check,
                    on_frame_progress=lambda frac, b=base_done: _update_overall_progress(b + frac),
                    file_entry=cur_entry,
                    batch_files=batch_files,
                )
                if out_p is None or status_msg.lower().startswith("skipped"):
                    skipped_media_count += 1
                    cur_entry.update({"status": "skipped", "percent": 100, "skipReason": status_msg})
                    results.append({
                        "name": rel_str,
                        "source_name": rel_str,
                        "original_size": orig_size,
                        "optimized_size": orig_size,
                        "status": status_msg,
                        "quality": badge,
                        "mode": "upscale",
                    })
                else:
                    done_ok_count += 1
                    out_rel = out_p.relative_to(output_dir).as_posix()
                    results.append({
                        "name": out_rel,
                        "source_name": rel_str,
                        "original_size": orig_size,
                        "optimized_size": out_p.stat().st_size,
                        "status": status_msg,
                        "quality": badge,
                        "mode": "upscale",
                    })
            finally:
                done_count += 1
                _update_overall_progress(float(done_count))
                if cfg["clean_temp_and_cache"]:
                    deep_cleanup_system(
                        work_dir=work_dir,
                        remove_weights=False,
                        label=f"Sau video {v_idx}/{len(videos)}",
                        log_fn=log_fn,
                        verbose=False,
                    )

    if total_candidates > 0:
        _emit_upscale_progress(
            job_state,
            active=False,
            phase="completed",
            completedItems=done_ok_count,
            skippedItems=skipped_media_count,
            totalItems=total_candidates,
            overallPercent=100.0,
            filePercent=100.0,
            etaSeconds=0,
            files=batch_files,
        )

    # 3. Handle non-media passthrough files (when filter is OFF or extra categories are included)
    passthrough_copy = not bool(options.get("replace"))
    for p in other_files:
        if cancel_check:
            cancel_check()
        rel_path = p.relative_to(input_dir)
        rel_str = rel_path.as_posix()
        sz = p.stat().st_size if p.exists() else 0
        if passthrough_copy and (not filter_on or should_download_file_item(p.name, options)):
            target_p = output_dir / rel_path
            target_p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target_p)
        results.append({
            "name": rel_str,
            "source_name": rel_str,
            "original_size": sz,
            "optimized_size": sz,
            "status": "Skipped",
            "quality": "-",
            "mode": "upscale",
        })

    shutil.rmtree(work_dir, ignore_errors=True)
    return results

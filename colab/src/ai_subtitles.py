from __future__ import annotations

import array
import asyncio
from collections import deque
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import time
from typing import Any, AsyncGenerator
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .security import require_token

router = APIRouter(prefix="/ai", tags=["colab-ai"], dependencies=[Depends(require_token)])

SAMPLE_RATE = 16000
CHANNELS = 1
BYTES_PER_SAMPLE = 2
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE

THINK_TAG_RE = re.compile(r"<(think|reason|reasoning|thought)>.*?</\1>", re.IGNORECASE | re.DOTALL)

_whisper_models: dict[tuple[str, str], Any] = {}
_model_lock = asyncio.Lock()
_active_ai_sessions: dict[str, asyncio.Event] = {}
_llm_histories: dict[str, deque[dict[str, str]]] = {}
_edge_token: str = ""
_edge_token_exp: float = 0.0
_cuda_paths_configured: bool = False


def _detect_effective_cpus() -> int:
    """
    Detect true usable vCPU quota inside Colab / Kaggle / Docker cgroups.
    Avoids thread thrashing when host os.cpu_count() differs from container CPU quota.
    """
    # cgroups v2 (/sys/fs/cgroup/cpu.max -> "<quota> <period>")
    try:
        cpu_max = Path("/sys/fs/cgroup/cpu.max")
        if cpu_max.is_file():
            parts = cpu_max.read_text(encoding="utf-8").strip().split()
            if len(parts) == 2 and parts[0] != "max":
                quota = int(parts[0])
                period = int(parts[1])
                if quota > 0 and period > 0:
                    return max(1, math.ceil(quota / period))
    except Exception:
        pass

    # cgroups v1
    try:
        q_file = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
        p_file = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        if q_file.is_file() and p_file.is_file():
            quota = int(q_file.read_text(encoding="utf-8").strip())
            period = int(p_file.read_text(encoding="utf-8").strip())
            if quota > 0 and period > 0:
                return max(1, math.ceil(quota / period))
    except Exception:
        pass

    try:
        if hasattr(os, "sched_getaffinity"):
            aff = len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
            if aff > 0:
                return aff
    except Exception:
        pass

    return max(1, os.cpu_count() or 2)


def _detect_ram_mb() -> int:
    try:
        meminfo = Path("/proc/meminfo")
        if meminfo.is_file():
            for line in meminfo.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return kb // 1024
    except Exception:
        pass
    return 0


def _detect_cpu_simd() -> list[str]:
    flags: list[str] = []
    try:
        cpuinfo = Path("/proc/cpuinfo")
        if cpuinfo.is_file():
            raw = cpuinfo.read_text(encoding="utf-8", errors="ignore").lower()
            for feat in ("avx512_vnni", "avx512f", "avx2", "sse4_2"):
                if feat in raw:
                    flags.append(feat)
    except Exception:
        pass
    return flags


def _ensure_cuda_lib_paths() -> None:
    """Ensure pip-installed NVIDIA cuBLAS/cuDNN libraries are discoverable when user switches to GPU runtime."""
    global _cuda_paths_configured
    if _cuda_paths_configured or os.name == "nt":
        return
    _cuda_paths_configured = True
    extra_dirs: list[str] = []
    for mod_name in ("nvidia.cublas.lib", "nvidia.cudnn.lib"):
        try:
            spec = importlib.util.find_spec(mod_name)
            if spec and spec.submodule_search_locations:
                extra_dirs.extend(str(p) for p in spec.submodule_search_locations)
        except Exception:
            pass
    for sys_dir in ("/usr/local/cuda/lib64", "/usr/lib/x86_64-linux-gnu"):
        if os.path.isdir(sys_dir):
            extra_dirs.append(sys_dir)
    if extra_dirs:
        cur = os.environ.get("LD_LIBRARY_PATH", "")
        merged = ":".join(dict.fromkeys([*extra_dirs, *(p for p in cur.split(":") if p)]))
        os.environ["LD_LIBRARY_PATH"] = merged


def _detect_gpu_info() -> dict[str, Any]:
    if not shutil.which("nvidia-smi"):
        return {"hasGpu": False, "gpuName": "", "vramMb": 0}
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            text=True,
            timeout=4,
        ).strip()
        if out:
            line = out.splitlines()[0]
            parts = [p.strip() for p in line.split(",")]
            return {
                "hasGpu": True,
                "gpuName": parts[0],
                "vramMb": int(float(parts[1])) if len(parts) > 1 else 0,
            }
    except Exception:
        pass
    return {"hasGpu": False, "gpuName": "", "vramMb": 0}


def _detect_hardware_profile() -> dict[str, Any]:
    gpu = _detect_gpu_info()
    cpu_cores = _detect_effective_cpus()
    ram_mb = _detect_ram_mb()
    simd = _detect_cpu_simd()
    if gpu["hasGpu"]:
        rec_whisper = "large-v3-turbo"
        rec_compute = "float16"
        rec_llm = "qwen2.5:7b"
    else:
        # On Colab CPU: 'small' int8 + lightweight 'qwen2.5:1.5b' JSON batching by default!
        rec_whisper = "small" if cpu_cores >= 2 else "base"
        rec_compute = "int8"
        rec_llm = "qwen2.5:1.5b"
    return {
        "gpu": gpu,
        "cpu": {
            "cores": cpu_cores,
            "ramMb": ram_mb,
            "simd": simd,
        },
        "runtimeTier": "gpu" if gpu["hasGpu"] else "cpu",
        "recommendedWhisperModel": rec_whisper,
        "recommendedComputeType": rec_compute,
        "recommendedLlmModel": rec_llm,
        "recommendedTranslateMode": "ollama_json_batch",
    }


def _faster_whisper_installed() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


async def _probe_ollama(base_url: str = "http://127.0.0.1:11434") -> tuple[bool, list[str]]:
    try:
        async with httpx.AsyncClient(timeout=2.0) as http:
            resp = await http.get(f"{base_url.rstrip('/')}/api/tags")
            if resp.status_code == 200:
                data = resp.json()
                models = [str(m.get("name") or "") for m in (data.get("models") or []) if m.get("name")]
                return True, models
    except Exception:
        pass
    return False, []


def _build_wav(pcm: bytes) -> bytes:
    data_size = len(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        CHANNELS,
        SAMPLE_RATE,
        BYTES_PER_SECOND,
        CHANNELS * BYTES_PER_SAMPLE,
        16,
        b"data",
        data_size,
    )
    return header + pcm


def _patch_pyav_open_compat() -> None:
    """
    Compat shim for Google Colab's preinstalled PyAV (`av`):
    Older PyAV builds do not accept `metadata_errors="ignore"` passed by
    `faster_whisper.audio.decode_audio`. Strip `metadata_errors` if unsupported.
    """
    try:
        import inspect
        import av  # type: ignore[import-not-found]

        orig_open = getattr(av, "open", None)
        if callable(orig_open) and not getattr(orig_open, "_vb_patched", False):
            sig = inspect.signature(orig_open)
            has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
            if "metadata_errors" not in sig.parameters and not has_var_kw:
                def _compat_open(*args: Any, **kwargs: Any) -> Any:
                    kwargs.pop("metadata_errors", None)
                    return orig_open(*args, **kwargs)

                _compat_open._vb_patched = True  # type: ignore[attr-defined]
                av.open = _compat_open
    except Exception:
        pass


def _pcm_s16le_to_float32(raw_pcm: bytes) -> Any:
    """
    Convert 16kHz mono s16le PCM bytes directly into a 1-D numpy.float32 array in [-1.0, 1.0].
    Passing a numpy.ndarray to faster_whisper.WhisperModel.transcribe() completely bypasses
    `decode_audio` and `av.open(..., metadata_errors="ignore")`!
    """
    try:
        import numpy as np

        n_samples = len(raw_pcm) // 2
        pcm_view = memoryview(raw_pcm)[: n_samples * 2]
        return np.frombuffer(pcm_view, dtype="<i2").astype(np.float32) * (1.0 / 32768.0)
    except Exception:
        return io.BytesIO(_build_wav(raw_pcm))


def _has_speech_energy(pcm: bytes, rms_threshold: float = 42.0) -> bool:
    """
    Ultra-fast (<0.2ms) 16-bit PCM RMS energy check to skip pure digital silence
    without waking up Silero VAD / Whisper CPU threads.
    """
    n_samples = len(pcm) // 2
    if n_samples < 400:
        return False
    # Stride by 8 samples (2kHz effective envelope) for near-zero CPU cost
    samples = array.array("h")
    samples.frombytes(pcm[: n_samples * 2])
    if sys.byteorder != "little":
        samples.byteswap()
    step = 8
    total_sq = 0
    count = 0
    for idx in range(0, len(samples), step):
        s = samples[idx]
        total_sq += s * s
        count += 1
    if count == 0:
        return False
    rms = math.sqrt(total_sq / count)
    return rms >= rms_threshold


async def _get_whisper_model(model_name: str, has_gpu: bool, cpu_threads: int) -> tuple[Any, str]:
    """
    Load faster-whisper with hardware-optimal parameters:
    - GPU: CUDA float16 (with automatic fallback to CPU int8 if CUDA runtime libs fail)
    - CPU: CTranslate2 int8 quantization pinned to exact container vCPU count, inter_threads=1
    Returns (model_instance, actual_device).
    """
    desired_device = "cuda" if has_gpu else "cpu"
    cache_key = (model_name, desired_device)

    async with _model_lock:
        if cache_key in _whisper_models:
            return _whisper_models[cache_key], desired_device

        def _load() -> tuple[Any, str]:
            _patch_pyav_open_compat()
            from faster_whisper import WhisperModel

            if has_gpu:
                _ensure_cuda_lib_paths()
                try:
                    m = WhisperModel(model_name, device="cuda", compute_type="float16")
                    return m, "cuda"
                except Exception:
                    pass

            threads = max(1, int(cpu_threads))
            os.environ.setdefault("OMP_NUM_THREADS", str(threads))
            m = WhisperModel(
                model_name,
                device="cpu",
                compute_type="int8",
                cpu_threads=threads,
                num_workers=1,
            )
            return m, "cpu"

        model_obj, actual_dev = await asyncio.to_thread(_load)
        # On CPU runtime, keep at most 1 Whisper model in RAM to protect Colab's 12.7GB memory ceiling
        if actual_dev == "cpu":
            for old_key in [k for k in _whisper_models if k[1] == "cpu" and k != (model_name, "cpu")]:
                _whisper_models.pop(old_key, None)
        _whisper_models[(model_name, actual_dev)] = model_obj
        return model_obj, actual_dev


async def _translate_edge_fallback(texts: list[str], src_lang: str, target_lang: str) -> list[str]:
    """Zero-CPU neural batch fallback via Google Translate Clients5 / GTX (<80ms in Colab network). Never raises."""
    if not texts:
        return []
    sl = "auto" if not src_lang or src_lang.lower() == "auto" else src_lang
    tl = "zh-CN" if target_lang in {"zh", "zh-CN"} else ("zh-TW" if target_lang == "zh-TW" else (target_lang or "vi"))
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136.0.0.0 Safari/537.36"

    async def _one(http: httpx.AsyncClient, txt: str) -> str:
        clean = " ".join(line.strip() for line in str(txt or "").splitlines() if line.strip())
        if not clean:
            return ""
        try:
            c5_url = (
                f"https://clients5.google.com/translate_a/t?client=dict-chrome-ex"
                f"&sl={quote(sl)}&tl={quote(tl)}&q={quote(clean)}"
            )
            r5 = await http.get(c5_url)
            if r5.status_code == 200:
                d5 = r5.json()
                if isinstance(d5, list) and d5:
                    first = d5[0]
                    res = str(first[0] if isinstance(first, list) else first).strip()
                    if res:
                        return res
                elif isinstance(d5, dict) and d5.get("sentences"):
                    res = " ".join(str(s.get("trans") or "").strip() for s in d5["sentences"]).strip()
                    if res:
                        return res
        except Exception:
            pass
        try:
            g_url = (
                f"https://translate.googleapis.com/translate_a/single?client=gtx"
                f"&sl={quote(sl)}&tl={quote(tl)}&dt=t&q={quote(clean)}"
            )
            rg = await http.get(g_url)
            if rg.status_code == 200:
                dg = rg.json()
                if isinstance(dg, list) and dg and isinstance(dg[0], list):
                    parts = [str(seg[0]).strip() for seg in dg[0] if isinstance(seg, list) and seg and seg[0]]
                    res = " ".join(p for p in parts if p).strip()
                    if res:
                        return res
        except Exception:
            pass
        return clean

    try:
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": ua}) as http:
            return list(await asyncio.gather(*(_one(http, t) for t in texts)))
    except Exception:
        return list(texts)


async def _translate_ollama_keep_context(
    texts: list[str],
    src_lang: str,
    target_lang: str,
    model: str = "auto",
    session_id: str = "default",
    is_cpu: bool = False,
    cpu_cores: int = 2,
) -> list[str]:
    """
    Batched JSON context-aware Ollama subtitle translator.
    Packs all cues of a chunk into ONE numbered JSON map (4-5x less prompt prefill overhead than line-by-line).
    - CPU Default: qwen2.5:1.5b with pinned num_thread=cpu_cores, num_ctx=1536, keep_alive='30m', format='json'.
    - GPU Default: qwen2.5:7b with num_ctx=2048, keep_alive='30m', format='json'.
    Falls back to Google GTX / Clients5 only if Ollama is unavailable or times out.
    """
    if not texts:
        return []
    ollama_ok, models = await _probe_ollama()
    if not ollama_ok and shutil.which("ollama"):
        try:
            env = os.environ.copy()
            env.setdefault("OLLAMA_NUM_PARALLEL", "1")
            subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
            await asyncio.sleep(2.5)
            ollama_ok, models = await _probe_ollama()
        except Exception:
            pass
    if not ollama_ok or not models:
        return await _translate_edge_fallback(texts, src_lang, target_lang)

    req_m = (model or "auto").strip()
    if is_cpu:
        # On CPU, default to qwen2.5:1.5b even if client sent 'auto' or GPU default 'qwen2.5:7b'
        if req_m in {"", "auto", "qwen2.5:7b"}:
            chosen_model = next((m for m in ("qwen2.5:1.5b", "qwen2.5:3b", "gemma2:2b") if m in models), models[0])
        else:
            chosen_model = req_m if req_m in models else models[0]
    else:
        if req_m in {"", "auto"}:
            chosen_model = "qwen2.5:7b" if "qwen2.5:7b" in models else models[0]
        else:
            chosen_model = req_m if req_m in models else models[0]

    history_max = 6 if is_cpu else 10
    history = _llm_histories.setdefault(f"{session_id}:{chosen_model}:{target_lang}", deque(maxlen=history_max))

    cleaned_inputs = [" ".join(line.strip() for line in str(t).splitlines() if line.strip()) for t in texts]
    indexed_payload = {str(i): txt for i, txt in enumerate(cleaned_inputs) if txt}
    if not indexed_payload:
        return ["" for _ in texts]

    sys_prompt = (
        "You are a professional subtitle translator. Translate the JSON object of numbered subtitle lines "
        f"from {src_lang or 'auto'} into {target_lang} naturally, keeping consistent pronouns and dialogue context. "
        "Output ONLY a valid JSON object with the exact same string keys mapped to translated strings."
    )
    user_msg = json.dumps(indexed_payload, ensure_ascii=False)
    messages = [{"role": "system", "content": sys_prompt}, *list(history), {"role": "user", "content": user_msg}]

    options: dict[str, Any] = {
        "temperature": 0.2,
        "num_ctx": 1536 if is_cpu else 2048,
        "num_predict": 420,
    }
    if is_cpu:
        options["num_thread"] = max(1, int(cpu_cores))

    req_timeout = 45.0 if is_cpu else 35.0
    try:
        async with httpx.AsyncClient(timeout=req_timeout) as http:
            resp = await http.post(
                "http://127.0.0.1:11434/api/chat",
                json={
                    "model": chosen_model,
                    "messages": messages,
                    "stream": False,
                    "format": "json",
                    "keep_alive": "30m",
                    "options": options,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            raw = str((data.get("message") or {}).get("content") or "")
            reply = THINK_TAG_RE.sub("", raw).strip()
            reply_clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", reply, flags=re.IGNORECASE).strip()
            parsed = json.loads(reply_clean)
            if isinstance(parsed, dict):
                out = [
                    str(parsed.get(str(i)) or cleaned_inputs[i]).strip() if cleaned_inputs[i] else ""
                    for i in range(len(texts))
                ]
                history.append({"role": "user", "content": user_msg})
                history.append({
                    "role": "assistant",
                    "content": json.dumps({str(i): out[i] for i in range(len(out)) if out[i]}, ensure_ascii=False),
                })
                return out
    except Exception:
        pass

    return await _translate_edge_fallback(cleaned_inputs, src_lang, target_lang)


async def _translate_smart(
    texts: list[str],
    *,
    src_lang: str,
    target_lang: str,
    has_gpu: bool,
    cpu_cores: int = 2,
    translate_mode: str = "auto",
    llm_model: str = "auto",
    session_id: str = "default",
) -> list[str]:
    """
    Default ('auto' / 'llm'): ALWAYS use JSON-batched Ollama KeepContext:
    - CPU runtime -> qwen2.5:1.5b (pinned threads, 1536 ctx, grammar JSON)
    - GPU runtime -> qwen2.5:7b (CUDA, grammar JSON)
    Falls back to Google GTX / Clients5 only if Ollama is unavailable or explicitly 'edge_only'.
    """
    mode = (translate_mode or "auto").lower()
    if mode == "edge_only":
        return await _translate_edge_fallback(texts, src_lang, target_lang)
    return await _translate_ollama_keep_context(
        texts,
        src_lang=src_lang,
        target_lang=target_lang,
        model=llm_model,
        session_id=session_id,
        is_cpu=not has_gpu,
        cpu_cores=cpu_cores,
    )


class ColabAISetupRequest(BaseModel):
    install_whisper: bool = True
    preload_whisper: bool = True
    install_ollama: bool = False
    ollama_mode: str = "auto"  # "auto" (GPU only), "cpu_light" (qwen2.5:1.5b), "force", "off"
    ollama_model: str = "qwen2.5:7b"


class ColabAITranslateRequest(BaseModel):
    texts: list[str] = Field(default_factory=list)
    src_lang: str = "auto"
    target_lang: str = "vi"
    translate_mode: str = "auto"
    llm_model: str = "qwen2.5:7b"
    session_id: str = "default"


class ColabAISubtitleStreamRequest(BaseModel):
    session_id: str = "default"
    media_url: str
    headers: dict[str, str] = Field(default_factory=dict)
    start_time: float = 0.0
    language: str = "auto"
    translate_to: str = "vi"
    translate_mode: str = "auto"  # "auto" | "edge" | "llm"
    whisper_model: str = ""
    llm_model: str = "qwen2.5:7b"
    chunk_seconds: int = 0


@router.get("/health")
async def colab_ai_health() -> dict[str, Any]:
    hw = _detect_hardware_profile()
    fw_ok = _faster_whisper_installed()
    ollama_ok, ollama_models = await _probe_ollama()
    return {
        "ok": True,
        **hw,
        "fasterWhisperInstalled": fw_ok,
        "ollamaReady": ollama_ok,
        "ollamaModels": ollama_models,
    }


@router.post("/setup")
async def colab_ai_setup(payload: ColabAISetupRequest) -> dict[str, Any]:
    logs: list[str] = []
    hw = _detect_hardware_profile()
    has_gpu = bool(hw["gpu"]["hasGpu"])
    cpu_cores = int(hw["cpu"]["cores"])

    if payload.install_whisper and not _faster_whisper_installed():
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pip",
            "install",
            "-q",
            "faster-whisper",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            return {"ok": False, "message": err.decode("utf-8", errors="ignore")[:400], "logs": logs}
        logs.append("Installed faster-whisper")

    if payload.preload_whisper and _faster_whisper_installed():
        rec_model = hw["recommendedWhisperModel"]
        try:
            _, actual_dev = await _get_whisper_model(rec_model, has_gpu, cpu_cores)
            logs.append(f"Preloaded faster-whisper '{rec_model}' on {actual_dev}")
        except Exception as exc:
            logs.append(f"Whisper preload warning: {exc}")

    # Determine Ollama target model based on hardware:
    # Default ('auto') installs qwen2.5:1.5b on CPU (~980MB, fast JSON batching) and qwen2.5:7b on GPU!
    mode = (payload.ollama_mode or "auto").lower()
    should_setup_ollama = mode != "off"
    req_ollama = (payload.ollama_model or "").strip()

    if mode == "cpu_light":
        target_ollama_model = "qwen2.5:1.5b"
    elif mode == "force" and req_ollama and req_ollama != "auto":
        target_ollama_model = req_ollama
    elif has_gpu:
        target_ollama_model = req_ollama if (req_ollama and req_ollama != "auto") else "qwen2.5:7b"
    else:
        # CPU runtime default -> qwen2.5:1.5b
        target_ollama_model = (
            req_ollama if (req_ollama and req_ollama not in {"auto", "qwen2.5:7b"}) else "qwen2.5:1.5b"
        )

    if should_setup_ollama:
        ollama_ok, models = await _probe_ollama()
        if not ollama_ok:
            if not shutil.which("ollama") and os.name != "nt":
                if not shutil.which("zstd"):
                    prep_proc = await asyncio.create_subprocess_shell(
                        "apt-get update -qq && apt-get install -y -qq zstd pciutils",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await prep_proc.communicate()
                install_proc = await asyncio.create_subprocess_shell(
                    "curl -fsSL https://ollama.com/install.sh | sh",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await install_proc.communicate()
                if not shutil.which("ollama"):
                    fb_proc = await asyncio.create_subprocess_shell(
                        "curl -fsSL https://ollama.com/download/ollama-linux-amd64.tar.zst | zstd -d | tar -xf - -C /usr/local",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await fb_proc.communicate()
                logs.append("Installed Ollama binary")
            if shutil.which("ollama"):
                env = os.environ.copy()
                env.setdefault("OLLAMA_NUM_PARALLEL", "1")
                subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                    start_new_session=True,
                )
                await asyncio.sleep(3.0)
                logs.append("Started ollama serve")
        ollama_ok, models = await _probe_ollama()
        if ollama_ok and target_ollama_model and target_ollama_model not in models:
            pull_proc = await asyncio.create_subprocess_exec(
                "ollama",
                "pull",
                target_ollama_model,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await pull_proc.communicate()
            logs.append(f"Pulled {target_ollama_model}")

    status = await colab_ai_health()
    return {"ok": True, "logs": logs, **status}


@router.post("/translate")
async def colab_ai_translate(payload: ColabAITranslateRequest) -> dict[str, Any]:
    mode = (payload.translate_mode or "auto").lower()
    if mode != "edge":
        ollama_ok, ollama_models = await _probe_ollama()
        if not ollama_ok or not ollama_models:
            # Colab is in Transfer-only mode (AI models not installed); let VaultBox fallback cleanly without touching Colab
            return {"ok": False, "reason": "ai_not_installed", "translations": []}
    hw = _detect_hardware_profile()
    translations = await _translate_smart(
        payload.texts,
        src_lang=payload.src_lang,
        target_lang=payload.target_lang,
        has_gpu=bool(hw["gpu"]["hasGpu"]),
        cpu_cores=int(hw["cpu"]["cores"]),
        translate_mode=payload.translate_mode,
        llm_model=payload.llm_model,
        session_id=payload.session_id,
    )
    return {"ok": True, "translations": translations}


@router.post("/subtitles/cancel")
async def colab_ai_cancel(payload: dict[str, Any]) -> dict[str, Any]:
    sid = str(payload.get("session_id") or "")
    ev = _active_ai_sessions.pop(sid, None)
    if ev:
        ev.set()
    return {"ok": True, "cancelled": bool(ev)}


@router.post("/subtitles/stream")
async def colab_ai_subtitles_stream(payload: ColabAISubtitleStreamRequest) -> StreamingResponse:
    async def _gen() -> AsyncGenerator[str, None]:
        prev = _active_ai_sessions.pop(payload.session_id, None)
        if prev:
            prev.set()
        stop_ev = asyncio.Event()
        _active_ai_sessions[payload.session_id] = stop_ev

        hw = _detect_hardware_profile()
        has_gpu = bool(hw["gpu"]["hasGpu"])
        cpu_cores = int(hw["cpu"]["cores"])

        want_translation = bool(payload.translate_to and payload.translate_to.lower() != "none")
        want_ollama = want_translation and (payload.translate_mode or "auto").lower() != "edge"
        ollama_ok, ollama_models = await _probe_ollama() if want_ollama else (True, [])
        need_whisper = not _faster_whisper_installed()
        need_ollama = want_ollama and (not ollama_ok or not ollama_models)

        if need_whisper or need_ollama:
            rec_llm = hw["recommendedLlmModel"]
            yield _sse("status", {
                "state": "installing",
                "engine": "colab_gpu" if has_gpu else "colab_cpu",
                "gpuName": hw["gpu"]["gpuName"],
                "cpuCores": cpu_cores,
                "model": f"faster-whisper + {rec_llm}" if need_ollama else "faster-whisper",
                "startTime": payload.start_time,
            })
            setup_res = await colab_ai_setup(
                ColabAISetupRequest(
                    install_whisper=need_whisper,
                    preload_whisper=False,
                    ollama_mode="auto" if need_ollama else "off",
                )
            )
            if need_whisper and not setup_res.get("ok"):
                yield _sse("error", {"code": "colab_install_failed", "message": setup_res.get("message", "Install failed")})
                return

        req_model = (payload.whisper_model or "").strip()
        # Protect CPU runtime if client sent 'auto' or left 'large-v3-turbo' from earlier GPU settings
        if not has_gpu and req_model.lower() in {"", "auto", "large-v3", "large-v3-turbo", "large-v2", "large", "medium"}:
            req_model = ""
        is_auto_model = not req_model or req_model.lower() == "auto"
        w_model_name = hw["recommendedWhisperModel"] if is_auto_model else req_model

        yield _sse("status", {
            "state": "loading_model",
            "engine": "colab_gpu" if has_gpu else "colab_cpu",
            "gpuName": hw["gpu"]["gpuName"],
            "cpuCores": cpu_cores,
            "model": w_model_name,
            "startTime": payload.start_time,
        })

        try:
            model, actual_device = await _get_whisper_model(w_model_name, has_gpu, cpu_cores)
        except Exception as exc:
            yield _sse("error", {"code": "model_load_failed", "message": str(exc)})
            return

        is_cuda = actual_device == "cuda"
        yield _sse("status", {
            "state": "starting",
            "engine": "colab_gpu" if is_cuda else "colab_cpu",
            "device": actual_device,
            "gpuName": hw["gpu"]["gpuName"] if is_cuda else "",
            "cpuCores": cpu_cores,
            "model": w_model_name,
            "startTime": payload.start_time,
        })

        # FFmpeg pinned to 1 thread so it never competes with CTranslate2 OpenMP threads on Colab CPU!
        ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
        cmd: list[str] = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-threads", "1"]
        if payload.headers:
            hdr_lines = "".join(f"{k}: {v}\r\n" for k, v in payload.headers.items() if k and v)
            if hdr_lines:
                cmd.extend(["-headers", hdr_lines])
        if payload.start_time > 0.5:
            cmd.extend(["-ss", f"{payload.start_time:.3f}"])
        cmd.extend([
            "-i",
            payload.media_url,
            "-vn",
            "-threads",
            "1",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "s16le",
            "-",
        ])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Shorter chunks on CPU (4s first chunk -> <1s time-to-first-subtitle; 8s steady chunks)
        default_chunk_sec = 12 if is_cuda else 8
        chunk_sec = max(5, min(25, int(payload.chunk_seconds or default_chunk_sec)))
        first_sec = 4 if not is_cuda else min(6, chunk_sec)
        lang_arg = None if not payload.language or payload.language.lower() == "auto" else payload.language.lower()[:2]

        # Read-ahead audio prefetch queue so CDN download & FFmpeg decode overlap 100% with CPU Whisper inference!
        pcm_queue: asyncio.Queue[tuple[int, float, float, bytes] | None] = asyncio.Queue(maxsize=3)

        async def _pcm_producer() -> None:
            c_no = 0
            cur = float(payload.start_time)
            try:
                assert proc.stdout is not None
                while not stop_ev.is_set():
                    want_bytes = (first_sec if c_no == 0 else chunk_sec) * BYTES_PER_SECOND
                    buf = bytearray()
                    while len(buf) < want_bytes and not stop_ev.is_set():
                        try:
                            part = await asyncio.wait_for(
                                proc.stdout.read(min(65536, want_bytes - len(buf))),
                                timeout=25.0,
                            )
                        except asyncio.TimeoutError:
                            break
                        if not part:
                            break
                        buf.extend(part)
                    if stop_ev.is_set() or len(buf) < BYTES_PER_SAMPLE * 1600:
                        break
                    dur = len(buf) / float(BYTES_PER_SECOND)
                    c_start = cur
                    cur += dur
                    c_no += 1
                    await pcm_queue.put((c_no, c_start, dur, bytes(buf)))
            finally:
                await pcm_queue.put(None)

        producer_task = asyncio.create_task(_pcm_producer())
        slow_cpu_streak = 0
        warned_recommend_gpu = False
        last_end_time = float(payload.start_time)
        total_chunks = 0
        locked_lang: str | None = lang_arg

        try:
            while not stop_ev.is_set():
                item = await pcm_queue.get()
                if item is None:
                    break
                chunk_no, c_start, dur, raw_pcm = item
                total_chunks = chunk_no
                last_end_time = c_start + dur

                yield _sse("progress", {
                    "chunkNo": chunk_no,
                    "chunkStart": round(c_start, 2),
                    "chunkEnd": round(last_end_time, 2),
                    "device": actual_device,
                    "model": w_model_name,
                })

                # Fast RMS digital-silence skip (<0.2ms, 0% CPU)
                if not _has_speech_energy(raw_pcm):
                    continue

                t0 = time.perf_counter()

                def _transcribe_sync(
                    pcm_data: bytes = raw_pcm,
                    cs: float = c_start,
                    cur_model: Any = model,
                    use_cuda: bool = is_cuda,
                    cur_lang: str | None = locked_lang,
                ) -> tuple[list[dict[str, Any]], str, float]:
                    _patch_pyav_open_compat()
                    audio_input = _pcm_s16le_to_float32(pcm_data)
                    if use_cuda:
                        segs, info = cur_model.transcribe(
                            audio_input,
                            language=cur_lang,
                            beam_size=4,
                            vad_filter=True,
                            vad_parameters={"min_silence_duration_ms": 350},
                        )
                    else:
                        # CPU Fast-Path: greedy int8 + temperature=0.0 eliminates 6x fallback retry stalls on BGM/noise!
                        segs, info = cur_model.transcribe(
                            audio_input,
                            language=cur_lang,
                            beam_size=1,
                            best_of=1,
                            temperature=0.0,
                            condition_on_previous_text=False,
                            without_timestamps=False,
                            vad_filter=True,
                            vad_parameters={"min_silence_duration_ms": 300, "speech_pad_ms": 200},
                        )
                    out_cues = []
                    for s in segs:
                        txt = (s.text or "").strip()
                        if not txt:
                            continue
                        st = round(cs + float(s.start), 3)
                        ed = round(cs + max(float(s.end), float(s.start) + 0.35) - 0.02, 3)
                        out_cues.append({"startTime": st, "endTime": ed, "text": txt})
                    det = getattr(info, "language", None) or (cur_lang or "en")
                    prob = float(getattr(info, "language_probability", 0.0) or 0.0)
                    return out_cues, str(det), prob

                cues, det_lang, lang_prob = await asyncio.to_thread(_transcribe_sync)

                if stop_ev.is_set():
                    break

                # Lock detected language on CPU once high-confidence (>0.82) so subsequent chunks skip 30s mel language-ID overhead!
                if not is_cuda and locked_lang is None and det_lang and lang_prob >= 0.82:
                    locked_lang = det_lang

                if cues and payload.translate_to and payload.translate_to.lower() != "none":
                    try:
                        trans_list = await _translate_smart(
                            [c["text"] for c in cues],
                            src_lang=det_lang,
                            target_lang=payload.translate_to,
                            has_gpu=is_cuda,
                            cpu_cores=cpu_cores,
                            translate_mode=payload.translate_mode,
                            llm_model=payload.llm_model,
                            session_id=payload.session_id,
                        )
                        for c, tr in zip(cues, trans_list):
                            c["translatedText"] = tr
                    except Exception:
                        pass

                total_sec = time.perf_counter() - t0
                rtf = round(total_sec / max(0.5, dur), 2)

                # Adaptive CPU Real-Time Factor (RTF) Guard across combined (Whisper int8 + Qwen2.5-1.5B JSON Batch):
                # 1) Auto-downshift Whisper 'small'/'medium' -> 'base' if combined RTF > 0.88x for 2 chunks
                # 2) Recommend switching Colab Runtime to GPU if even 'base + 1.5b' exceeds RTF > 1.05x
                if not is_cuda:
                    if rtf > 0.88 and dur >= 3.5:
                        slow_cpu_streak += 1
                    else:
                        slow_cpu_streak = 0

                    if slow_cpu_streak >= 2:
                        slow_cpu_streak = 0
                        if is_auto_model and w_model_name not in {"base", "tiny"}:
                            prev_name = w_model_name
                            w_model_name = "base"
                            try:
                                model, actual_device = await _get_whisper_model("base", False, cpu_cores)
                                yield _sse("cpu_notice", {
                                    "level": "downshift",
                                    "fromModel": prev_name,
                                    "toModel": "base",
                                    "rtf": rtf,
                                    "message": (
                                        f"Colab CPU đang tải nặng (RTF {rtf}x khi chạy Whisper + Qwen2.5-1.5B) — "
                                        f"đã tự hạ Whisper từ '{prev_name}' xuống 'base (int8)' để giữ phụ đề kịp hình."
                                    ),
                                })
                            except Exception:
                                pass
                        elif not warned_recommend_gpu and rtf > 1.05:
                            warned_recommend_gpu = True
                            yield _sse("cpu_notice", {
                                "level": "recommend_gpu",
                                "model": w_model_name,
                                "rtf": rtf,
                                "message": (
                                    f"⚠ Colab CPU hiện xử lý chậm hơn tốc độ video (RTF {rtf}x trên '{w_model_name} + Qwen2.5-1.5B'). "
                                    "Khuyên dùng: Đổi Runtime trên Colab sang GPU để chạy mượt với large-v3-turbo + Qwen2.5-7B!"
                                ),
                            })

                if cues:
                    yield _sse("cues", {
                        "chunkNo": chunk_no,
                        "language": det_lang,
                        "device": actual_device,
                        "model": w_model_name,
                        "rtf": rtf,
                        "cues": cues,
                    })

            if not stop_ev.is_set():
                yield _sse("completed", {
                    "chunks": total_chunks,
                    "endTime": round(last_end_time, 2),
                    "device": actual_device,
                    "model": w_model_name,
                })
        finally:
            stop_ev.set()
            if not producer_task.done():
                producer_task.cancel()
            if _active_ai_sessions.get(payload.session_id) is stop_ev:
                _active_ai_sessions.pop(payload.session_id, None)
            if proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

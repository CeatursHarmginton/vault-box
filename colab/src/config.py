from __future__ import annotations

import os
import secrets
from pathlib import Path

NO_AUTH = os.environ.get("COLAB_NO_AUTH", "").lower() in {"1", "true", "yes"}

# Environment auto-detection
IS_KAGGLE = bool(
    os.environ.get("KAGGLE_KERNEL_RUN_TYPE")
    or os.environ.get("KAGGLE_CONTAINER_NAME")
    or os.environ.get("KAGGLE_URL_BASE")
    or Path("/kaggle").exists()
)
IS_COLAB = bool(
    not IS_KAGGLE
    and (
        os.environ.get("COLAB_GPU") is not None
        or os.environ.get("GCS_READ_CACHE") is not None
        or Path("/content").exists()
    )
)
IS_LOCAL = os.environ.get("COLAB_LOCAL") == "1" or NO_AUTH or (not IS_KAGGLE and not IS_COLAB)
ENV_NAME = "Kaggle" if IS_KAGGLE else ("Colab" if IS_COLAB else "Local")

if os.name == "nt":
    default_base_dir = str(Path(__file__).resolve().parents[2] / "data" / "colab_tmp")
elif IS_KAGGLE:
    default_base_dir = "/kaggle/working/vaultbox_tmp"
else:
    default_base_dir = "/content/vaultbox_tmp"

BASE_DIR = Path(os.environ.get("VAULTBOX_COLAB_TMP", default_base_dir))
JOBS_DIR = BASE_DIR / "jobs"
SERVER_TOKEN = "" if NO_AUTH else (os.environ.get("COLAB_SERVER_TOKEN") or secrets.token_urlsafe(32))
PUBLIC_URL = os.environ.get("COLAB_PUBLIC_URL", "")
HOST = os.environ.get("COLAB_HOST", "127.0.0.1" if (NO_AUTH or os.environ.get("COLAB_LOCAL") == "1") else "0.0.0.0")
PORT = int(os.environ.get("COLAB_PORT", "8000"))
CHUNK_SIZE = int(os.environ.get("COLAB_CHUNK_SIZE", str(4 * 1024 * 1024)))
MAX_JOBS = int(os.environ.get("COLAB_MAX_JOBS", "8"))
FOLDER_DOWNLOAD_CONCURRENCY = int(os.environ.get("COLAB_FOLDER_DOWNLOAD_CONCURRENCY", "12"))
FOLDER_UPLOAD_CONCURRENCY = int(os.environ.get("COLAB_FOLDER_UPLOAD_CONCURRENCY", "12"))
# Files uploaded in parallel out of one optimize batch (each file may itself split into parallel parts).
UPLOAD_FILE_CONCURRENCY = int(os.environ.get("COLAB_UPLOAD_FILE_CONCURRENCY", "8"))
PIKPAK_UPLOAD_CONCURRENCY = int(os.environ.get("COLAB_PIKPAK_UPLOAD_CONCURRENCY", "16"))
TERABOX_UPLOAD_CONCURRENCY = int(os.environ.get("COLAB_TERABOX_UPLOAD_CONCURRENCY", "32"))
DOWNLOAD_THREADS = int(os.environ.get("COLAB_DOWNLOAD_THREADS", "32"))
COLAB_RELAY_URL = os.environ.get("COLAB_RELAY_URL", "")
COLAB_RELAY_ROOM_ID = os.environ.get("COLAB_RELAY_ROOM_ID", "")
COLAB_RELAY_TOKEN = os.environ.get("COLAB_RELAY_TOKEN", "")

# Auto-stop configuration (Only enabled by default on Kaggle to save compute quota; Colab keeps running)
KAGGLE_AUTO_STOP_MINUTES = int(os.environ.get("KAGGLE_AUTO_STOP_MINUTES", "10"))
AUTO_STOP_ENABLED = (IS_KAGGLE and KAGGLE_AUTO_STOP_MINUTES > 0) or (os.environ.get("COLAB_AUTO_STOP", "").lower() in {"1", "true", "yes"})
AUTO_STOP_IDLE_MINUTES = KAGGLE_AUTO_STOP_MINUTES if IS_KAGGLE else int(os.environ.get("AUTO_STOP_IDLE_MINUTES", "10"))

JOBS_DIR.mkdir(parents=True, exist_ok=True)

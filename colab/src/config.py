from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

NO_AUTH = os.environ.get("COLAB_NO_AUTH", "").lower() in {"1", "true", "yes"}

# Environment auto-detection
IS_GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"

_explicit_env = os.environ.get("VAULTBOX_ENV", "").strip().lower()
if _explicit_env == "kaggle":
    IS_KAGGLE = True
    IS_COLAB = False
elif _explicit_env in {"colab", "google_colab"}:
    IS_KAGGLE = False
    IS_COLAB = True
else:
    # 1. Colab markers: presence of google.colab module, Colab release/runtime tags, or Linux /content directory
    _colab_markers = bool(
        "google.colab" in sys.modules
        or os.environ.get("COLAB_RELEASE_TAG") is not None
        or os.environ.get("COLAB_BACKEND_VERSION") is not None
        or os.environ.get("COLAB_GPU") is not None
        or os.environ.get("COLAB_JUPYTER_IP") is not None
        or os.environ.get("GCS_READ_CACHE") is not None
        or (os.name != "nt" and Path("/content/sample_data").is_dir())
        or (os.name != "nt" and Path("/content").is_dir() and not Path("/kaggle/working").is_dir())
    )

    # 2. Kaggle markers: Kaggle environment variables or /kaggle/working directory (without /content)
    _kaggle_markers = bool(
        os.environ.get("KAGGLE_KERNEL_RUN_TYPE") is not None
        or os.environ.get("KAGGLE_CONTAINER_NAME") is not None
        or os.environ.get("KAGGLE_URL_BASE") is not None
        or os.environ.get("KAGGLE_DOCKER_IMAGE") is not None
        or os.environ.get("KAGGLE_USER_SECRETS_TOKEN") is not None
        or (os.name != "nt" and Path("/kaggle/working").is_dir() and not Path("/content").is_dir())
    )

    if not IS_GITHUB_ACTIONS and _colab_markers and not _kaggle_markers:
        IS_COLAB = True
        IS_KAGGLE = False
    elif not IS_GITHUB_ACTIONS and _kaggle_markers and not _colab_markers:
        IS_KAGGLE = True
        IS_COLAB = False
    elif not IS_GITHUB_ACTIONS and _colab_markers and _kaggle_markers:
        # Conflict resolution between competing signals
        if os.environ.get("KAGGLE_KERNEL_RUN_TYPE") or os.environ.get("KAGGLE_URL_BASE"):
            IS_KAGGLE = True
            IS_COLAB = False
        elif "google.colab" in sys.modules or os.environ.get("COLAB_RELEASE_TAG") or os.environ.get("COLAB_BACKEND_VERSION"):
            IS_COLAB = True
            IS_KAGGLE = False
        else:
            IS_COLAB = False
            IS_KAGGLE = False
    else:
        IS_COLAB = False
        IS_KAGGLE = False

IS_LOCAL = os.environ.get("COLAB_LOCAL") == "1" or NO_AUTH or (not IS_KAGGLE and not IS_COLAB and not IS_GITHUB_ACTIONS)
ENV_NAME = "GitHub Actions" if IS_GITHUB_ACTIONS else ("Kaggle" if IS_KAGGLE else ("Colab" if IS_COLAB else "Local"))

if os.name == "nt":
    default_base_dir = str(Path(__file__).resolve().parents[2] / "data" / "colab_tmp")
elif IS_KAGGLE:
    default_base_dir = "/kaggle/working/vaultbox_tmp"
elif IS_COLAB:
    default_base_dir = "/content/vaultbox_tmp"
else:
    default_base_dir = "/tmp/vaultbox_tmp"

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

# Auto-stop configuration (Enabled by default on Kaggle & GitHub Actions to save compute quota; Colab keeps running)
KAGGLE_AUTO_STOP_MINUTES = int(os.environ.get("KAGGLE_AUTO_STOP_MINUTES", "10"))
AUTO_STOP_ENABLED = (
    (IS_KAGGLE and KAGGLE_AUTO_STOP_MINUTES > 0)
    or IS_GITHUB_ACTIONS
    or (os.environ.get("COLAB_AUTO_STOP", "").lower() in {"1", "true", "yes"})
)
AUTO_STOP_IDLE_MINUTES = KAGGLE_AUTO_STOP_MINUTES if IS_KAGGLE else int(os.environ.get("AUTO_STOP_IDLE_MINUTES", "5" if IS_GITHUB_ACTIONS else "10"))

JOBS_DIR.mkdir(parents=True, exist_ok=True)

from __future__ import annotations

import os
import secrets
from pathlib import Path

BASE_DIR = Path(os.environ.get("VAULTBOX_COLAB_TMP", "/content/vaultbox_tmp"))
JOBS_DIR = BASE_DIR / "jobs"
SERVER_TOKEN = os.environ.get("COLAB_SERVER_TOKEN") or secrets.token_urlsafe(32)
PUBLIC_URL = os.environ.get("COLAB_PUBLIC_URL", "")
HOST = os.environ.get("COLAB_HOST", "0.0.0.0")
PORT = int(os.environ.get("COLAB_PORT", "8000"))
CHUNK_SIZE = int(os.environ.get("COLAB_CHUNK_SIZE", str(4 * 1024 * 1024)))
MAX_JOBS = int(os.environ.get("COLAB_MAX_JOBS", "8"))
FOLDER_DOWNLOAD_CONCURRENCY = int(os.environ.get("COLAB_FOLDER_DOWNLOAD_CONCURRENCY", "12"))
FOLDER_UPLOAD_CONCURRENCY = int(os.environ.get("COLAB_FOLDER_UPLOAD_CONCURRENCY", "12"))
PIKPAK_DOWNLOAD_CONCURRENCY = int(os.environ.get("COLAB_PIKPAK_DOWNLOAD_CONCURRENCY", "8"))
PIKPAK_DOWNLOAD_PART_SIZE = int(os.environ.get("COLAB_PIKPAK_DOWNLOAD_PART_MB", "16")) * 1024 * 1024
# Files uploaded in parallel out of one optimize batch (each file may itself split into parallel parts).
UPLOAD_FILE_CONCURRENCY = int(os.environ.get("COLAB_UPLOAD_FILE_CONCURRENCY", "8"))
PIKPAK_UPLOAD_CONCURRENCY = int(os.environ.get("COLAB_PIKPAK_UPLOAD_CONCURRENCY", "16"))
TERABOX_UPLOAD_CONCURRENCY = int(os.environ.get("COLAB_TERABOX_UPLOAD_CONCURRENCY", "32"))
COLAB_RELAY_URL = os.environ.get("COLAB_RELAY_URL", "")
COLAB_RELAY_ROOM_ID = os.environ.get("COLAB_RELAY_ROOM_ID", "")
COLAB_RELAY_TOKEN = os.environ.get("COLAB_RELAY_TOKEN", "")

JOBS_DIR.mkdir(parents=True, exist_ok=True)

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
MAX_JOBS = int(os.environ.get("COLAB_MAX_JOBS", "2"))

JOBS_DIR.mkdir(parents=True, exist_ok=True)

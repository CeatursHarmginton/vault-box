from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import HOST, PORT, SERVER_TOKEN  # noqa: E402

try:
    import subprocess
    repo_root = ROOT.parent
    if (repo_root / ".git").exists():
        subprocess.run(["git", "checkout", "."], cwd=str(repo_root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        res = subprocess.run(["git", "pull", "--rebase"], cwd=str(repo_root), capture_output=True, text=True, timeout=15)
        if res.returncode == 0:
            print("[*] Code updated from GitHub (git pull successful).", flush=True)
except Exception:
    pass

if __name__ == "__main__":
    print(f"Connection Token: {SERVER_TOKEN}", flush=True)
    uvicorn.run("src.server:app", host=HOST, port=PORT, reload=False)

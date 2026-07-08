from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import HOST, PORT, SERVER_TOKEN  # noqa: E402

if __name__ == "__main__":
    print(f"Connection Token: {SERVER_TOKEN}", flush=True)
    uvicorn.run("src.server:app", host=HOST, port=PORT, reload=False)

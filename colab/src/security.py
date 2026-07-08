from __future__ import annotations

import hmac
from pathlib import Path
from typing import Any

from fastapi import Header, HTTPException

from .config import SERVER_TOKEN

SECRET_KEYS = ("token", "cookie", "authorization", "access", "refresh", "ndus", "bdstoken", "jstoken")

def require_token(authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"code": "INVALID_COLAB_TOKEN", "message": "Missing Colab token"})
    token = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, SERVER_TOKEN):
        raise HTTPException(status_code=401, detail={"code": "INVALID_COLAB_TOKEN", "message": "Invalid Colab token"})

def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***" if any(s in k.lower() for s in SECRET_KEYS) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value

def safe_join(root: Path, *parts: str) -> Path:
    root = root.resolve()
    path = root.joinpath(*[p for p in parts if p]).resolve()
    if root != path and root not in path.parents:
        raise ValueError("Path escapes temp workspace")
    return path

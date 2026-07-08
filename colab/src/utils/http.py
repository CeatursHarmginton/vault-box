from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

import httpx

T = TypeVar("T")

async def retry(fn: Callable[[], Awaitable[T]], *, attempts: int = 3) -> T:
    last: Exception | None = None
    for i in range(attempts):
        try:
            return await fn()
        except (httpx.HTTPError, OSError) as exc:
            last = exc
            if i == attempts - 1:
                break
            await asyncio.sleep(min(2**i, 8))
    raise last or RuntimeError("retry failed")

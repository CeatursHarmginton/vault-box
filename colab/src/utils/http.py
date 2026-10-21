from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

import httpx

T = TypeVar("T")

def ensure_socks_support() -> bool:
    """Ensure socksio is installed and httpcore SOCKS proxy support is active.

    If httpcore was imported before socksio was installed, httpcore defines
    dummy stub classes (AsyncSOCKSProxy, SOCKSProxy) that raise RuntimeError.
    This function detects that state, installs socksio/httpcore[socks] if missing,
    and reloads httpcore so SOCKS proxies function immediately without restarting the server.
    """
    def _is_ready() -> bool:
        try:
            import socksio  # noqa: F401
            import httpcore
            if hasattr(httpcore, "AsyncSOCKSProxy"):
                httpcore.AsyncSOCKSProxy(proxy_url=b"socks5://127.0.0.1:9050")
            if hasattr(httpcore, "SOCKSProxy"):
                httpcore.SOCKSProxy(proxy_url=b"socks5://127.0.0.1:9050")
            return True
        except (ImportError, RuntimeError):
            return False
        except Exception:
            return True

    if _is_ready():
        return True

    import subprocess
    import sys

    for cmd in (
        [sys.executable, "-m", "pip", "install", "-q", "socksio", "httpcore[socks]", "httpx[socks]"],
        [sys.executable, "-m", "pip", "install", "-q", "--break-system-packages", "socksio", "httpcore[socks]", "httpx[socks]"],
    ):
        try:
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            if res.returncode == 0:
                break
        except Exception:
            pass

    try:
        import sys
        for mod_key in ("httpcore._async.socks_proxy", "httpcore._sync.socks_proxy"):
            sys.modules.pop(mod_key, None)
        import importlib
        import httpcore
        if "httpcore._async" in sys.modules:
            importlib.reload(sys.modules["httpcore._async"])
        if "httpcore._sync" in sys.modules:
            importlib.reload(sys.modules["httpcore._sync"])
        importlib.reload(httpcore)
    except Exception:
        pass

    return _is_ready()

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


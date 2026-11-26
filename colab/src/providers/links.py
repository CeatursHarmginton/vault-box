from __future__ import annotations
import asyncio
import base64
import hashlib
import html
import json
import os
import random
import re
import shutil
import string
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse, urljoin, urlunparse

TOR_PROXY = "socks5://127.0.0.1:9050"
PROXY_SETUP_LOCK: asyncio.Lock | None = None
_proxy_ready = False
_PROXY_REQUIRED_DOMAINS: set[str] = set()
os.environ.pop("DOTNET_SYSTEM_GLOBALIZATION_INVARIANT", None)

import httpx

from ..providers.base import BaseProvider, ProviderFailure, _download_error_payload, safe_name, stream_download
_default_stream_download = stream_download
from ..jobs.progress import JobState
from ..config import DOWNLOAD_THREADS

BT_TRACKERS = "udp://tracker.opentrackr.org:1337/announce,udp://open.stealth.si:80/announce,udp://tracker.openbittorrent.com:6969/announce,udp://exodus.desync.com:6969/announce"

_GDRIVE_FRONTEND_API_KEY = "AIzaSyD_InbmSFufIEps5UAt2NmB_3LvBH3Sz_8"
_GDRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
_GDRIVE_SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
_GDRIVE_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_PLACEHOLDER_LINK_NAME_RE = re.compile(
    r"^(?:"
    r"GDrive_Folder_[A-Za-z0-9_-]+|"
    r"GDrive_[A-Za-z0-9_-]+|"
    r"GDrive_File|"
    r"MEGA_Folder_[A-Za-z0-9_-]+|"
    r"MEGA_File_[A-Za-z0-9_-]+|"
    r"MEGA_Download|"
    r"MediaFire_Folder_[A-Za-z0-9,_-]+|"
    r"MediaFire_[A-Za-z0-9_-]+|"
    r"Pixeldrain_List_[A-Za-z0-9_-]+|"
    r"Pixeldrain_[A-Za-z0-9_-]+|"
    r"Gofile_[A-Za-z0-9_-]+|"
    r"Dropbox_Folder_[A-Za-z0-9_-]+|"
    r"Yandex_Folder_[A-Za-z0-9_-]+|"
    r"Yandex_[A-Za-z0-9_-]+|"
    r"Yandex_Public|Yandex_Disk|Yandex_Download|"
    r"tg_[A-Za-z0-9_]+_\d+|"
    r"Telegram_Media|"
    r"cloud_folder|folder|"
    r"(?:video_)?download_\d+(?:\.[A-Za-z0-9]{2,5})?"
    r")$",
    re.IGNORECASE,
)


def is_placeholder_link_name(name: str | None, url: str = "") -> bool:
    """Return True if ``name`` is empty or an auto-generated link ID placeholder rather than a real title."""
    s = str(name or "").strip()
    if not s:
        return True
    if s.startswith(("http://", "https://")) or "#" in s:
        return True
    if _PLACEHOLDER_LINK_NAME_RE.match(s):
        return True
    stem = s[:-4].strip() if s.lower().endswith(".mp4") else s
    if _PLACEHOLDER_LINK_NAME_RE.match(stem):
        return True
    if url:
        clean_tail = str(url).split("#", 1)[0].split("?", 1)[0].rstrip("/").split("/")[-1].strip()
        if clean_tail and stem in (clean_tail, safe_name(clean_tail)):
            # For cloud share links where URL tail is an opaque ID (Drive/MEGA/Gofile/Pixeldrain/MediaFire/Yandex), treat as placeholder
            url_low = url.lower()
            if any(h in url_low for h in ("drive.google.com", "docs.google.com", "drive.usercontent.google.com", "mega.nz", "mega.co.nz", "gofile.io", "pixeldrain.com", "mediafire.com")):
                return True
            url_no_q = str(url).split("#", 1)[0].split("?", 1)[0].rstrip("/")
            if re.search(r"(?:disk\.(?:360\.)?yandex\.[a-z.]+|yadi\.sk)/(?:d|i)/[^/]+$", url_no_q, re.IGNORECASE):
                return True
    return False


class LinksProvider(BaseProvider):
    """Provider for downloading files from user-provided URLs."""
    
    name = "links"
    _deps_checked = False
    _telethon_checked = False

    @classmethod
    async def _ensure_telethon(cls, progress: JobState | None = None) -> None:
        """Ensure telethon (and optional cryptg C-accelerator) are importable."""
        if cls._telethon_checked:
            return
        from ..providers.base import _loop_store, dict_lock

        lock = dict_lock(_loop_store("telethon_install_locks"), "telethon")
        async with lock:
            if cls._telethon_checked:
                return
            try:
                import telethon  # noqa: F401
                cls._telethon_checked = True
                return
            except ImportError:
                pass

            if progress:
                progress.log("[Telegram] Installing Telethon fallback engine...")
            try:
                await asyncio.to_thread(
                    subprocess.check_call,
                    [sys.executable, "-m", "pip", "install", "-q", "telethon>=1.36", "cryptg>=0.4"],
                )
            except Exception:
                try:
                    await asyncio.to_thread(
                        subprocess.check_call,
                        [sys.executable, "-m", "pip", "install", "-q", "telethon>=1.36"],
                    )
                except Exception as exc:
                    raise ProviderFailure(
                        "MISSING_DEPENDENCY",
                        f"Failed to install Telethon fallback package: {exc}",
                    ) from exc

            try:
                import telethon  # noqa: F401
            except ImportError as exc:
                raise ProviderFailure(
                    "MISSING_DEPENDENCY",
                    f"Telethon package unavailable after installation: {exc}",
                ) from exc
            cls._telethon_checked = True

    @classmethod
    async def _ensure_deps(cls) -> None:
        if cls._deps_checked:
            return
            
        if not shutil.which("aria2c"):
            try:
                subprocess.check_call(['apt-get', 'install', '-y', '-qq', 'aria2'])
            except Exception:
                pass


            
        missing = []
        if not shutil.which("yt-dlp"):
            missing.append("yt-dlp")
        else:
            try:
                subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp", "curl_cffi"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            except Exception:
                pass

        try:
            import curl_cffi
        except ImportError:
            missing.append("curl_cffi")
            
        try:
            import gdown
        except ImportError:
            missing.append("gdown")
            
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher
        except ImportError:
            try:
                import Crypto.Cipher.AES
            except ImportError:
                missing.append("cryptography")

        if missing:
            try:
                subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q'] + missing)
            except Exception:
                pass

        from ..utils.http import ensure_socks_support
        ensure_socks_support()

        cls._deps_checked = True

    @classmethod
    async def _ensure_tor_proxy(cls, progress: JobState | None = None) -> str | None:
        """Set up Tor as SOCKS5 proxy on 127.0.0.1:9050 (pure TCP, works 100% in Colab)."""
        from ..providers.base import _loop_store, dict_lock
        from ..utils.http import ensure_socks_support
        ensure_socks_support()
        global _proxy_ready
        import socket

        def _is_port_open(port: int = 9050) -> bool:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                    return True
            except Exception:
                return False

        if _proxy_ready and _is_port_open(9050):
            return TOR_PROXY
        lock = dict_lock(_loop_store("proxy_setup_locks"), "tor")
        async with lock:
            if _proxy_ready and _is_port_open(9050):
                return TOR_PROXY
            if not sys.platform.startswith("linux"):
                return None

            async def _probe_socks() -> str:
                for probe_url in ("https://checkip.amazonaws.com", "https://api.ipify.org", "https://icanhazip.com"):
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            "curl", "-s", "--socks5-hostname", "127.0.0.1:9050",
                            "--connect-timeout", "4", "--max-time", "6", probe_url,
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                        )
                        out, _ = await proc.communicate()
                        txt = out.decode(errors="ignore").strip()
                        if proc.returncode == 0 and txt and len(txt) < 64 and ("." in txt or ":" in txt):
                            return txt
                    except Exception:
                        pass
                return ""

            if _is_port_open(9050):
                ip_txt = await _probe_socks()
                if ip_txt:
                    _proxy_ready = True
                    return TOR_PROXY

            try:
                if progress:
                    progress.log("[Proxy] Starting Tor SOCKS5 proxy for IP bypass (TCP-based)...")

                def _setup() -> None:
                    if not shutil.which("tor"):
                        subprocess.run(["apt-get", "update", "-qq"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        subprocess.run(["apt-get", "install", "-y", "-qq", "tor"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    torrc = Path("/etc/tor/torrc")
                    high_speed_marker = "# vaultbox-high-speed-tor"
                    if torrc.exists():
                        try:
                            txt = torrc.read_text(encoding="utf-8", errors="ignore")
                            needs_restart = False
                            if "HTTPTunnelPort" in txt:
                                txt = "\n".join(line for line in txt.splitlines() if "HTTPTunnelPort" not in line) + "\n"
                                needs_restart = True
                            if high_speed_marker not in txt:
                                txt = (
                                    txt.rstrip()
                                    + f"\n{high_speed_marker}\n"
                                    + "ConnLimit 4096\n"
                                    + "SocksPort 127.0.0.1:9050 IsolateSOCKSAuth KeepAliveIsolateSOCKSAuth\n"
                                    + "ControlPort 127.0.0.1:9060\n"
                                    + "CookieAuthentication 0\n"
                                    + "NumEntryGuards 8\n"
                                    + "CircuitBuildTimeout 15\n"
                                    + "LearnCircuitBuildTimeout 0\n"
                                    + "MaxCircuitDirtiness 7200\n"
                                    + "CircuitStreamTimeout 60\n"
                                )
                                needs_restart = True
                            elif "ControlPort 127.0.0.1:9060" not in txt:
                                txt = (
                                    txt.rstrip()
                                    + "\nControlPort 127.0.0.1:9060\n"
                                    + "CookieAuthentication 0\n"
                                )
                                needs_restart = True
                            if needs_restart:
                                torrc.write_text(txt, encoding="utf-8")
                                subprocess.run(["pkill", "-9", "tor"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        except Exception:
                            pass
                    if not _is_port_open(9050):
                        subprocess.run(["service", "tor", "start"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        time.sleep(1.5)
                    if not _is_port_open(9050):
                        subprocess.run(
                            [
                                "tor",
                                "--ConnLimit", "4096",
                                "--SocksPort", "127.0.0.1:9050 IsolateSOCKSAuth KeepAliveIsolateSOCKSAuth",
                                "--ControlPort", "127.0.0.1:9060",
                                "--CookieAuthentication", "0",
                                "--NumEntryGuards", "8",
                                "--MaxCircuitDirtiness", "7200",
                                "--RunAsDaemon", "1",
                            ],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        )

                await asyncio.to_thread(_setup)

                for _ in range(20):
                    await asyncio.sleep(1)
                    if not _is_port_open(9050):
                        continue
                    ip_txt = await _probe_socks()
                    if ip_txt:
                        _proxy_ready = True
                        if progress:
                            progress.log(f"[Proxy] Tor proxy active (Exit IP: {ip_txt[:60]})")
                        return TOR_PROXY
                    for log_path in (Path("/var/log/tor/notices.log"), Path("/var/log/tor/log")):
                        if log_path.exists():
                            try:
                                if "Bootstrapped 100%" in log_path.read_text(encoding="utf-8", errors="ignore"):
                                    _proxy_ready = True
                                    if progress:
                                        progress.log("[Proxy] Tor proxy active (Bootstrapped 100%)")
                                    return TOR_PROXY
                            except Exception:
                                pass
            except Exception as exc:
                if progress:
                    progress.log(f"[Proxy] Tor setup notice: {exc}")
            return None

    @staticmethod
    def _isolated_proxy(proxy: str | None, circuit_tag: str | None = None) -> str | None:
        """Return a SOCKS5 URL with IsolateSOCKSAuth credentials so a download and its token resolution share one dedicated exit IP."""
        if not proxy:
            return None
        if circuit_tag:
            m_tor = re.search(r"127\.0\.0\.1:(905\d)\b", str(proxy))
            if m_tor:
                return f"socks5://vb_{circuit_tag}:pass@127.0.0.1:{m_tor.group(1)}"
        return proxy

    _TOR_TURBO_EXTRA_PORTS: tuple[int, ...] = (9051, 9052, 9053)
    _tor_turbo_daemons: list[str] = []
    _tor_newnym_ts: dict[int, float] = {}
    _tor_newnym_notice_ts: float = 0.0
    _mega_direct_cooldown_until: float = 0.0
    _mega_direct_notice_ts: float = 0.0
    _mega_direct_was_throttled: bool = False
    _mega_direct_probe_ts: float = 0.0

    async def _ensure_mega_tor_pool(self, progress: JobState | None = None) -> list[str]:
        """Ensure primary Tor proxy plus up to 3 extra independent Tor daemons (ports 9050-9053) for multi-core parallel throughput."""
        base_px = await self._ensure_tor_proxy(progress)
        if not base_px:
            return []
        if not sys.platform.startswith("linux") or "127.0.0.1:9050" not in str(base_px) or not shutil.which("tor"):
            return [base_px]

        import socket
        from ..providers.base import _loop_store

        def _port_open(port: int) -> bool:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.6):
                    return True
            except Exception:
                return False

        if len(type(self)._tor_turbo_daemons) >= 1 + len(self._TOR_TURBO_EXTRA_PORTS) and all(
            _port_open(p) for p in (9050, *self._TOR_TURBO_EXTRA_PORTS)
        ):
            return list(type(self)._tor_turbo_daemons)

        locks = _loop_store("mega_tor_locks")
        lock = locks.get("pool_init")
        if lock is None:
            lock = locks["pool_init"] = asyncio.Lock()

        async with lock:
            if len(type(self)._tor_turbo_daemons) >= 1 + len(self._TOR_TURBO_EXTRA_PORTS) and all(
                _port_open(p) for p in (9050, *self._TOR_TURBO_EXTRA_PORTS)
            ):
                return list(type(self)._tor_turbo_daemons)

            def _spawn_extra_daemons() -> None:
                cache_src = Path("/var/lib/tor")
                cache_names = (
                    "cached-certs",
                    "cached-microdesc-consensus",
                    "cached-microdescs",
                    "cached-microdescs.new",
                )
                for port in self._TOR_TURBO_EXTRA_PORTS:
                    ctrl_port = port + 10
                    data_dir = Path(f"/tmp/vb_tor_{port}")
                    torrc_f = data_dir / "torrc"
                    if _port_open(port):
                        try:
                            if torrc_f.is_file() and f"ControlPort 127.0.0.1:{ctrl_port}" in torrc_f.read_text(encoding="utf-8", errors="ignore"):
                                continue
                            pid_f = data_dir / "tor.pid"
                            if pid_f.is_file():
                                pid_txt = pid_f.read_text(encoding="utf-8", errors="ignore").strip()
                                if pid_txt.isdigit():
                                    subprocess.run(["kill", "-9", pid_txt], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                    time.sleep(0.2)
                        except Exception:
                            continue
                        if _port_open(port):
                            continue
                    try:
                        data_dir.mkdir(parents=True, exist_ok=True)
                        os.chmod(data_dir, 0o700)
                        if cache_src.is_dir():
                            for cname in cache_names:
                                src_f = cache_src / cname
                                dst_f = data_dir / cname
                                if src_f.is_file() and not dst_f.exists():
                                    try:
                                        shutil.copyfile(src_f, dst_f)
                                        os.chmod(dst_f, 0o600)
                                    except Exception:
                                        pass
                        torrc_f.write_text(
                            f"DataDirectory {data_dir}\n"
                            f"PidFile {data_dir}/tor.pid\n"
                            f"SocksPort 127.0.0.1:{port} IsolateSOCKSAuth KeepAliveIsolateSOCKSAuth\n"
                            f"ControlPort 127.0.0.1:{ctrl_port}\n"
                            "CookieAuthentication 0\n"
                            "ConnLimit 4096\n"
                            "NumEntryGuards 8\n"
                            "CircuitBuildTimeout 10\n"
                            "LearnCircuitBuildTimeout 0\n"
                            "MaxCircuitDirtiness 7200\n"
                            "CircuitStreamTimeout 30\n"
                            "RunAsDaemon 1\n",
                            encoding="utf-8",
                        )
                        subprocess.run(
                            ["tor", "-f", str(torrc_f)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=10,
                        )
                    except Exception:
                        pass

            await asyncio.to_thread(_spawn_extra_daemons)

            for _ in range(12):
                if all(_port_open(p) for p in self._TOR_TURBO_EXTRA_PORTS):
                    break
                await asyncio.sleep(0.4)

            active_ports = [9050] + [p for p in self._TOR_TURBO_EXTRA_PORTS if _port_open(p)]
            daemons = [f"socks5://127.0.0.1:{p}" for p in active_ports]
            prev_count = len(type(self)._tor_turbo_daemons)
            type(self)._tor_turbo_daemons = daemons
            if progress and len(daemons) > 1 and prev_count <= 1:
                progress.log(f"[Proxy] Tor Turbo pool active ({len(daemons)} parallel SOCKS5 daemons on ports {', '.join(str(p) for p in active_ports)})")
            return daemons

    def _get_mega_circuit_pool(self) -> dict[str, Any]:
        from ..providers.base import _loop_store
        pool = _loop_store("mega_circuit_pool")
        if "idle" not in pool:
            pool["idle"] = []  # LIFO stack of verified fast (proxy_url, httpx.AsyncClient)
            pool["rr"] = 0
            pool["sem"] = asyncio.Semaphore(64)
            pool["tor_sem"] = asyncio.Semaphore(16)
        elif "tor_sem" not in pool:
            pool["tor_sem"] = asyncio.Semaphore(16)
        return pool

    async def _rotate_tor_newnym(self, progress: JobState | None = None, proxy_hint: str | None = None) -> bool:
        """Send SIGNAL NEWNYM to Tor ControlPort(s) (with 10.5s per-daemon cooldown) to obtain fresh Exit IPs."""
        from ..providers.base import _loop_store

        locks = _loop_store("mega_tor_locks")
        lock = locks.get("newnym")
        if lock is None:
            lock = locks["newnym"] = asyncio.Lock()

        target_ports: list[int] = []
        if proxy_hint:
            m_port = re.search(r"127\.0\.0\.1:(905\d)\b", str(proxy_hint))
            if m_port:
                target_ports.append(int(m_port.group(1)))
        if not target_ports:
            target_ports = [9050, *self._TOR_TURBO_EXTRA_PORTS]

        rotated_ports: set[int] = set()
        async with lock:
            now = time.monotonic()
            for socks_port in target_ports:
                last_ts = type(self)._tor_newnym_ts.get(socks_port, 0.0)
                if (now - last_ts) < 10.5:
                    continue
                ctrl_port = socks_port + 10
                writer: asyncio.StreamWriter | None = None
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection("127.0.0.1", ctrl_port),
                        timeout=1.2,
                    )
                    writer.write(b'AUTHENTICATE ""\r\nSIGNAL NEWNYM\r\nQUIT\r\n')
                    await asyncio.wait_for(writer.drain(), timeout=1.0)
                    reply = await asyncio.wait_for(reader.read(256), timeout=1.2)
                    if b"250" in reply:
                        type(self)._tor_newnym_ts[socks_port] = time.monotonic()
                        rotated_ports.add(socks_port)
                except Exception:
                    pass
                finally:
                    if writer is not None:
                        try:
                            writer.close()
                            await writer.wait_closed()
                        except Exception:
                            pass

            if rotated_ports:
                pool = self._get_mega_circuit_pool()
                idle: list[tuple[str, httpx.AsyncClient]] = pool.get("idle") or []
                kept_idle: list[tuple[str, httpx.AsyncClient]] = []
                stale_clients: list[httpx.AsyncClient] = []
                for px_u, cli_obj in idle:
                    if any(f"127.0.0.1:{p}" in str(px_u) for p in rotated_ports):
                        stale_clients.append(cli_obj)
                    else:
                        kept_idle.append((px_u, cli_obj))
                pool["idle"] = kept_idle
                for stale_cli in stale_clients:
                    await self._discard_mega_circuit(stale_cli)

                now_wall = time.time()
                if progress and (now_wall - type(self)._tor_newnym_notice_ts) >= 20.0:
                    type(self)._tor_newnym_notice_ts = now_wall
                    progress.log("[Proxy] Đã xoay mạch Tor (SIGNAL NEWNYM) sang Exit IP mới để tiếp tục tải phần còn thiếu...")
        return bool(rotated_ports)

    def _acquire_mega_circuit(self, daemons: list[str]) -> tuple[str, httpx.AsyncClient]:
        from ..providers.base import track_client
        from ..utils.http import ensure_socks_support
        ensure_socks_support()
        pool = self._get_mega_circuit_pool()
        idle: list[tuple[str, httpx.AsyncClient]] = pool["idle"]
        while idle:
            px, cli = idle.pop()
            if not getattr(cli, "is_closed", False):
                return px, cli
        idx = int(pool.get("rr", 0))
        pool["rr"] = idx + 1
        base_daemon = daemons[idx % len(daemons)]
        px = self._isolated_proxy(base_daemon, uuid.uuid4().hex[:10]) or base_daemon
        cli = track_client(
            httpx.AsyncClient(
                proxy=px,
                follow_redirects=True,
                timeout=httpx.Timeout(connect=12.0, read=18.0, write=15.0, pool=20.0),
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            )
        )
        return px, cli

    def _release_mega_circuit(self, px: str | None, cli: httpx.AsyncClient | None) -> None:
        if not px or cli is None or getattr(cli, "is_closed", False):
            return
        pool = self._get_mega_circuit_pool()
        idle: list[tuple[str, httpx.AsyncClient]] = pool["idle"]
        if len(idle) < 96:
            idle.append((px, cli))

    async def _discard_mega_circuit(self, cli: httpx.AsyncClient | None) -> None:
        if cli is None:
            return
        try:
            await cli.aclose()
        except Exception:
            pass

    @staticmethod
    def _is_ip_bound_token_error(msg: str) -> bool:
        """Detect errors where a URL token is strictly bound to another IP or expired, making direct retries useless."""
        low = str(msg or "").lower()
        return any(k in low for k in (
            "error_wrong_ip", "wrong_ip", "wrong ip", "ip not allowed", "ip mismatch",
            "invalid token", "token expired", "link expired", "url expired",
            "binds the url to the original browser/ip",
        ))

    @staticmethod
    def _is_ip_or_captcha_blocked(text_or_msg: str, status_code: int = 200) -> bool:
        """Universal check for datacenter IP blocks, Cloudflare/Turnstile challenges, or CDN token/IP refusals."""
        if status_code in (401, 403, 429, 451, 503):
            return True
        low = str(text_or_msg or "").lower()
        if len(low) < 25000 and any(k in low for k in (
            "captcha-player", "cf-challenge", "cf-turnstile", "just a moment...",
            "attention required! | cloudflare", "ddos-guard", "verify you are human",
            "challenge.html", "shield-challenge", "cf-mitigated",
        )):
            return True
        if any(k in low for k in (
            "error_wrong_ip", "wrong_ip", "ip not allowed", "ip blocked", "403 forbidden",
            "http error 403", "429 too many", "http error 429", "too many requests",
        )):
            return True
        return False

    async def _fetch_webpage(
        self,
        target_url: str,
        req_headers: dict[str, str],
        progress: JobState,
        proxy: str | None = None,
        auto_proxy: bool = True,
        session_cookies: dict[str, str] | None = None,
        post_data: bytes | str | None = None,
    ) -> tuple[int, str, str, str | None]:
        """Universal webpage fetcher with browser TLS impersonation, domain proxy memory, and parallel 3-circuit racing."""
        def _do_fetch(active_px: str | None, use_cookies: bool = True) -> tuple[int, str, str]:
            hdrs = dict(req_headers)
            if not use_cookies:
                hdrs.pop("Cookie", None)
                hdrs.pop("cookie", None)
            try:
                from curl_cffi import requests as cffi_requests
                s = None
                for target in ("chrome120", "chrome110", "chrome"):
                    try:
                        s = cffi_requests.Session(impersonate=target)
                        break
                    except Exception:
                        pass
                if s is None:
                    s = cffi_requests.Session()
                if use_cookies and session_cookies:
                    s.cookies.update(session_cookies)
                if post_data is not None:
                    r = s.post(target_url, data=post_data, headers=hdrs, proxy=active_px, timeout=25)
                else:
                    r = s.get(target_url, headers=hdrs, proxy=active_px, timeout=25)
                if session_cookies is not None:
                    try:
                        for k, v in s.cookies.items():
                            session_cookies[str(k)] = str(v)
                    except Exception:
                        pass
                return r.status_code, r.text, str(r.url)
            except Exception as cffi_exc:
                try:
                    cks = session_cookies if use_cookies else None
                    if active_px and "socks" in str(active_px).lower():
                        from ..utils.http import ensure_socks_support
                        ensure_socks_support()
                    with httpx.Client(follow_redirects=True, timeout=25.0, proxy=active_px, cookies=cks) as client:
                        if post_data is not None:
                            r = client.post(target_url, content=post_data if isinstance(post_data, (bytes, str)) else json.dumps(post_data), headers=hdrs)
                        else:
                            r = client.get(target_url, headers=hdrs)
                        if session_cookies is not None:
                            try:
                                for k, v in client.cookies.items():
                                    session_cookies[str(k)] = str(v)
                            except Exception:
                                pass
                        return r.status_code, r.text, str(r.url)
                except Exception:
                    raise cffi_exc

        target_host = urlparse(target_url).netloc.lower()
        code, body, final_url = 0, "", target_url

        # 1. Try direct fetch with browser TLS first (fastest and often bypasses WAF if datacenter IP is not hard-banned)
        try:
            code, body, final_url = await asyncio.to_thread(_do_fetch, proxy, True)
            if not self._is_ip_or_captcha_blocked(body, code) and body.strip():
                return code, body, final_url, proxy
            # If fetch with stale cookies was rejected (401/403), retry direct with clean session
            if session_cookies and code in (401, 403):
                c_code, c_body, c_url = await asyncio.to_thread(_do_fetch, proxy, False)
                if not self._is_ip_or_captcha_blocked(c_body, c_code) and c_body.strip():
                    return c_code, c_body, c_url, proxy
        except Exception as exc:
            if not auto_proxy:
                raise
            progress.log(f"[IP-Guard] Direct fetch failed on {target_host} ({exc}); activating proxy...")

        if not auto_proxy:
            return code, body, final_url, proxy

        base_proxy = await self._ensure_tor_proxy(progress)
        if not base_proxy:
            return code, body, final_url, proxy

        # If caller already pinned a specific circuit (e.g. /pass_md5/ after /e/), try that circuit first
        if proxy and "vb_" in proxy:
            try:
                c_code, c_body, c_url = await asyncio.to_thread(_do_fetch, proxy)
                if not self._is_ip_or_captcha_blocked(c_body, c_code) and c_body.strip():
                    return c_code, c_body, c_url, proxy
            except Exception:
                pass

        # Race 3 isolated Tor circuits in parallel so the lowest-latency, highest-bandwidth exit node wins
        progress.log(f"[IP-Guard] Racing 3 parallel proxy circuits on {target_host} to select fastest exit IP...")

        async def _race_circuit(circuit_px: str) -> tuple[int, str, str, str]:
            r_code, r_body, r_url = await asyncio.to_thread(_do_fetch, circuit_px)
            if self._is_ip_or_captcha_blocked(r_body, r_code) or not r_body.strip():
                raise RuntimeError(f"circuit challenged (HTTP {r_code})")
            return r_code, r_body, r_url, circuit_px

        candidates = [self._isolated_proxy(base_proxy, uuid.uuid4().hex[:8]) for _ in range(3)]
        tasks = [asyncio.create_task(_race_circuit(px)) for px in candidates if px]
        try:
            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for d in done:
                    try:
                        w_code, w_body, w_url, w_px = d.result()
                        for p_task in pending:
                            p_task.cancel()
                        if not self._is_ip_or_captcha_blocked(w_body, w_code) and w_body.strip():
                            return w_code, w_body, w_url, w_px
                    except Exception:
                        pass
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()

        # Fallback to direct fetch attempt if proxy circuits were all challenged
        try:
            fb_code, fb_body, fb_url = await asyncio.to_thread(_do_fetch, None)
            if not self._is_ip_or_captcha_blocked(fb_body, fb_code) and fb_body.strip():
                return fb_code, fb_body, fb_url, None
        except Exception:
            pass

        return code, body, final_url, proxy or base_proxy

    def _classify_link(self, url: str) -> str:
        url_lower = url.lower()
        if url_lower.startswith("magnet:?xt=") or url_lower.endswith(".torrent"):
            return "torrent"

        parsed = urlparse(url_lower)
        if parsed.netloc in ("t.me", "telegram.me", "www.t.me") or parsed.netloc.endswith((".t.me", ".telegram.me")):
            return "telegram"
        if parsed.netloc.endswith("gofile.io"):
            return "direct" if parsed.path.startswith("/download/web/") else "gofile_page"

        if any(h in url_lower for h in ("mega.nz", "mega.co.nz")):
            if "#" in url_lower and "/file/" in url_lower.split("#", 1)[1]:
                return "mega_file"
            if "/folder/" in url_lower or "#f!" in url_lower:
                return "mega_folder"
            return "mega_file"

        if "pixeldrain.com" in url_lower:
            if "/l/" in url_lower or "/api/list/" in url_lower:
                return "pixeldrain_list"
            return "pixeldrain"

        if "mediafire.com" in url_lower:
            if "/folder/" in url_lower:
                return "mediafire_folder"
            return "mediafire"

        if any(h in url_lower for h in ("drive.google.com", "docs.google.com", "drive.usercontent.google.com")):
            if "/drive/folders/" in url_lower or ("/drive/u/" in url_lower and "/folders/" in url_lower) or "folders/" in parsed.path or "embeddedfolderview" in parsed.path:
                return "gdrive_folder"
            return "gdrive"

        if "dropbox.com" in url_lower or "dropboxusercontent.com" in url_lower:
            if "/scl/fo/" in url_lower or "/sh/" in url_lower:
                return "dropbox_folder"
            return "dropbox"

        if any(h in url_lower for h in ("1drv.ms", "onedrive.live.com", "sharepoint.com")):
            return "onedrive"

        if re.search(r"(?:^|\.)downloader\.disk\.yandex\.[a-z.]+$", parsed.netloc):
            return "direct"
        if self._is_yandex_disk_host(parsed.netloc):
            return "yandex_disk"

        if "sorafolder.com" in url_lower:
            return "sorafolder"

        # Direct & Embed resolvers for Mixdrop / Mxdrop / Mxcontent
        if any(h in url_lower for h in ["mxcontent.net", "mxdrop.to", "mxdrop.top", "mixdrop.co", "mixdrop.to", "mixdrop.bz", "mixdrop.ch"]):
            return "mxdrop"

        # DoodStream / Playmogo / CloudataCDN / Archivebate / MyVidPlay / Livecamsrip
        dood_domains = [
            "cloudatacdn.com", "playmogo.com", "archivebate.com", "doods.pro",
            "dooood.com", "dooood", "doodstream", "ds2play", "do0od", "d000d", "d0000d",
            "dood.to", "dood.watch", "dood.so", "dood.ws", "dood.pm",
            "dood.re", "dood.li", "dood.cx", "dood.wf", "dood.la",
            "dood.sh", "dood.video", "dood.stream", "myvidplay.com", "myvidplay",
            "livecamsrip.com", "dsvplay.com", "dsvplay", "dood.yt", "dood.club",
        ]
        if any(d in url_lower for d in dood_domains):
            return "doodstream"

        ytdlp_domains = [
            "youtube", "youtu.be", "tiktok", "bilibili", "vimeo", 
            "dailymotion", "twitch", 
            "1fichier.com", "pornhub.com",
            "dzen.ru", "zen.yandex.", "vh.yandex.",
        ]
        
        if url_lower.endswith(".m3u8") or url_lower.endswith(".mpd") or ".m3u8?" in url_lower or ".mpd?" in url_lower or "/hls/" in url_lower:
            return "stream"

        if re.search(r"(?:^|\.)(?:yandex\.[a-z.]+|ya\.ru)$", parsed.netloc) and "/video/" in parsed.path:
            return "ytdlp"
            
        for domain in ytdlp_domains:
            if domain in url_lower:
                return "ytdlp"
                
        return "direct"

    def _parse_aria2_progress(self, line: str) -> tuple[int, int] | None:
        def to_bytes(s: str) -> int:
            s = s.upper().replace('B', '')
            mult = 1
            if s.endswith('K') or s.endswith('KI'): mult = 1024
            elif s.endswith('M') or s.endswith('MI'): mult = 1024**2
            elif s.endswith('G') or s.endswith('GI'): mult = 1024**3
            elif s.endswith('T') or s.endswith('TI'): mult = 1024**4
            num_str = re.sub(r'[A-Z]', '', s)
            try:
                return int(float(num_str) * mult)
            except ValueError:
                return 0

        match = re.search(r'([0-9.]+[A-Za-z]+)/([0-9.]+[A-Za-z]+)\(', line)
        if match:
            done_bytes = to_bytes(match.group(1))
            total_bytes = to_bytes(match.group(2))
            return done_bytes, total_bytes
        return None

    def _update_stream_progress(self, line: str, progress: JobState, default_size: int = 0) -> None:
        """Parse real speed, percentage, size, and fragments from N_m3u8DL-RE / yt-dlp stdout."""
        try:
            total_sz = 0
            done_sz = 0

            # Match N_m3u8DL-RE format: 14.35MB/2.20GB
            m_nm_sz = re.search(r'(\d+(?:\.\d+)?)\s*([KMGT]?i?B)\s*/\s*(\d+(?:\.\d+)?)\s*([KMGT]?i?B)', line, re.IGNORECASE)
            if m_nm_sz:
                try:
                    u1 = m_nm_sz.group(2).upper().replace('I', '')
                    u2 = m_nm_sz.group(4).upper().replace('I', '')
                    m1 = {'B': 1, 'KB': 1024, 'MB': 1024**2, 'GB': 1024**3}.get(u1, 1024**2)
                    m2 = {'B': 1, 'KB': 1024, 'MB': 1024**2, 'GB': 1024**3}.get(u2, 1024**2)
                    done_sz = int(float(m_nm_sz.group(1)) * m1)
                    total_sz = int(float(m_nm_sz.group(3)) * m2)
                except (ValueError, TypeError):
                    pass
            else:
                m_sz = re.search(r'of\s*~?\s*(\d+(?:\.\d+)?)\s*([KMGT]?i?B)', line, re.IGNORECASE)
                if m_sz:
                    try:
                        val = float(m_sz.group(1))
                        unit = m_sz.group(2).upper().replace('I', '')
                        mult = {'B': 1, 'KB': 1024, 'MB': 1024**2, 'GB': 1024**3}.get(unit, 1024**2)
                        total_sz = int(val * mult)
                    except (ValueError, TypeError):
                        pass
                elif not progress.bytes_total and default_size:
                    total_sz = default_size

                m_done = re.search(r'\[download\]\s+(\d+(?:\.\d+)?)\s*([KMGT]?i?B)', line, re.IGNORECASE)
                if m_done:
                    try:
                        val = float(m_done.group(1))
                        unit = m_done.group(2).upper().replace('I', '')
                        mult = {'B': 1, 'KB': 1024, 'MB': 1024**2, 'GB': 1024**3}.get(unit, 1024**2)
                        done_sz = int(val * mult)
                    except (ValueError, TypeError):
                        pass

            pct_val = 0.0
            m_pct = re.search(r'(\d+(?:\.\d+)?)\s*%', line)
            if m_pct:
                try:
                    pct_val = float(m_pct.group(1))
                except (ValueError, TypeError):
                    pass
            else:
                m_frag = re.search(r'(?:frag\s+|^\s*|\s+)(\d+)/(\d+)', line, re.IGNORECASE)
                if m_frag:
                    try:
                        cur_frag = int(m_frag.group(1))
                        tot_frag = int(m_frag.group(2))
                        if tot_frag > 0:
                            pct_val = min(100.0, (cur_frag / tot_frag) * 100.0)
                    except (ValueError, TypeError):
                        pass

            speed_val = 0.0
            # Matches both yt-dlp "14.35MiB/s" and N_m3u8DL-RE "14.35MBps"
            m_spd = re.search(r'(\d+(?:\.\d+)?)\s*([KMGT]?i?B)(?:ps|/s)', line, re.IGNORECASE)
            if m_spd:
                try:
                    val = float(m_spd.group(1))
                    unit = m_spd.group(2).upper().replace('I', '')
                    mult = {'B': 1, 'KB': 1024, 'MB': 1024**2, 'GB': 1024**3}.get(unit, 1024**2)
                    speed_val = val * mult
                except (ValueError, TypeError):
                    pass

            if hasattr(progress, "set_phase_progress"):
                progress.set_phase_progress(
                    done_bytes=done_sz,
                    total_bytes=total_sz or progress.bytes_total,
                    pct=pct_val,
                    speed=speed_val,
                    phase="download",
                )
            else:
                if total_sz:
                    progress.bytes_total = total_sz
                if pct_val > 0:
                    progress.progress.download = min(100.0, pct_val)
                    if progress.bytes_total:
                        progress.bytes_done = int(progress.bytes_total * (pct_val / 100.0))
                if speed_val > 0:
                    progress.speed = speed_val
                progress.updated_at = time.time()
        except Exception:
            pass

    def _http_headers(self, headers: dict[str, str] | None, *, range_header: str | None = None, cookies: str | None = None) -> dict[str, str]:
        out = {str(k): str(v) for k, v in (headers or {}).items() if v and str(k).lower() not in ("host", "content-length", "accept-encoding", "connection", "transfer-encoding")}
        out["Accept-Encoding"] = "identity"
        out.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
        if cookies and "cookie" not in {k.lower() for k in out}:
            out["Cookie"] = cookies
        if range_header:
            out["Range"] = range_header
        return out

    def _needs_browser_stream(self, headers: dict[str, str], file_ref: dict[str, Any]) -> bool:
        keys = {str(k).lower() for k in headers}
        return bool(keys & {"origin", "referer"} or any(k.startswith(("sec-fetch-", "sec-ch-")) for k in keys) or str(file_ref.get("type") or "").lower() in {"mp4", "video"})

    async def _remote_size(self, url: str, headers: dict[str, str] | None = None) -> int:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=8.0) as client:
                resp = await client.head(url, headers=self._http_headers(headers))
                if resp.status_code >= 400:
                    async with client.stream("GET", url, headers=self._http_headers(headers, range_header="bytes=0-0")) as streamed:
                        resp = streamed
                        content_range = resp.headers.get("Content-Range") or resp.headers.get("content-range") or ""
                        match = re.search(r"/(\d+)$", content_range)
                        if match:
                            return int(match.group(1))
                        return int(resp.headers.get("Content-Length") or resp.headers.get("content-length") or 0) if resp.status_code == 200 else 0
                content_range = resp.headers.get("Content-Range") or resp.headers.get("content-range") or ""
                match = re.search(r"/(\d+)$", content_range)
                if match:
                    return int(match.group(1))
                return int(resp.headers.get("Content-Length") or resp.headers.get("content-length") or 0)
        except Exception:
            return 0

    async def _filter_urls_by_size(self, urls: list[str], expected_size: int, headers: dict[str, str] | None = None) -> list[str]:
        probes = await asyncio.gather(*(self._probe_url(url, headers) for url in urls))
        filtered = [probe for probe in probes if not probe[1] or probe[1] >= expected_size]
        filtered.sort(key=lambda probe: probe[2], reverse=True)
        return [probe[0] for probe in filtered] or urls

    async def _probe_url(self, url: str, headers: dict[str, str] | None = None) -> tuple[str, int, float]:
        size = await self._remote_size(url, headers)
        started = time.monotonic()
        read = 0
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
                async with client.stream("GET", url, headers=self._http_headers(headers, range_header="bytes=0-2097151")) as resp:
                    async for chunk in resp.aiter_bytes():
                        read += len(chunk)
                        if read >= 2 * 1024 * 1024:
                            break
                    content_range = resp.headers.get("Content-Range") or resp.headers.get("content-range") or ""
                    match = re.search(r"/(\d+)$", content_range)
                    if match:
                        size = int(match.group(1))
                    elif not size and resp.status_code == 200:
                        size = int(resp.headers.get("Content-Length") or resp.headers.get("content-length") or 0)
        except Exception:
            pass
        elapsed = max(time.monotonic() - started, 0.001)
        return url, size, read / elapsed

    async def _download_parallel_ranges(
        self,
        url: str,
        dest: Path,
        progress: JobState,
        *,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        num_connections: int | None = None,
    ) -> Path:
        """High-speed multi-connection HTTP Range downloader that multiplexes parallel streams over the same SOCKS5 circuit or direct connection."""
        import threading
        from ..providers import base as _base_mod

        dest.parent.mkdir(parents=True, exist_ok=True)
        part_path = dest.with_suffix(dest.suffix + ".part")
        base_headers = {k: v for k, v in (headers or {}).items() if str(k).lower() != "range"}

        options = (progress.payload or {}).get("options", {}) if hasattr(progress, "payload") else {}
        if num_connections is None:
            num_connections = int(options.get("download_threads") or options.get("threads_per_item") or options.get("downloadThreads") or DOWNLOAD_THREADS)
        num_connections = max(1, min(int(num_connections), 64))
        if proxy:
            # Over SOCKS5/Tor, multiplexing 32 streams per file causes SOCKS server saturation and connection drops
            num_connections = min(num_connections, 4)

        limits = httpx.Limits(
            max_connections=max(12, num_connections * 2),
            max_keepalive_connections=max(8, num_connections),
        )
        timeout = httpx.Timeout(connect=30.0, read=45.0, write=30.0, pool=60.0)
        client_kwargs: dict[str, Any] = {"follow_redirects": True, "timeout": timeout, "limits": limits}
        if proxy:
            if "socks" in str(proxy).lower():
                from ..utils.http import ensure_socks_support
                ensure_socks_support()
            client_kwargs["proxy"] = proxy

        async with _base_mod.httpx.AsyncClient(**client_kwargs) as client:
            probe_headers = {**base_headers, "Range": "bytes=0-65535"}
            async with client.stream("GET", url, headers=probe_headers) as resp:
                if resp.status_code in (401, 403):
                    raise ProviderFailure(
                        "DOWNLOAD_FAILED",
                        f"Source rejected direct download (HTTP {resp.status_code}): the site likely binds the URL to the original browser/IP or expired the token.",
                        status=resp.status_code,
                    )
                resp.raise_for_status()
                content_type = resp.headers.get("content-type") or ""

                # If server does not support Range (200 OK), stream the response directly without re-requesting
                if resp.status_code != 206:
                    total = int(resp.headers.get("content-length") or 0)
                    done = 0
                    with part_path.open("wb") as f:
                        async for chunk in resp.aiter_bytes(262144):
                            progress.check_cancelled()
                            if not chunk:
                                continue
                            f.write(chunk)
                            done += len(chunk)
                            if done <= 4096 and (total == 0 or total <= 4096):
                                sample_low = chunk.lower()
                                if any(tok in sample_low for tok in (
                                    b"error_wrong_ip", b"file not found", b"403 forbidden",
                                    b"access denied", b"ip not allowed", b"invalid token",
                                    b"link expired", b'"errmsg"', b"<html",
                                )):
                                    continue
                            progress.add_bytes(len(chunk), total, "download", dest.name)
                    if error := _download_error_payload(part_path, content_type, False):
                        part_path.unlink(missing_ok=True)
                        err_code = str(error.get("code") or error.get("errno") or "DOWNLOAD_FAILED")
                        err_msg = str(error.get("message") or "Download returned error payload instead of file")
                        raise ProviderFailure(err_code, err_msg, details=error)
                    part_path.replace(dest)
                    return dest

                # 206 Partial Content: read initial 64 KB probe
                first_chunks: list[bytes] = []
                async for chunk in resp.aiter_bytes(65536):
                    if chunk:
                        first_chunks.append(chunk)
                first_bytes = b"".join(first_chunks)
                content_range = resp.headers.get("Content-Range") or resp.headers.get("content-range") or ""
                m_total = re.search(r"/(\d+)$", content_range)
                total_size = int(m_total.group(1)) if m_total else 0

            # Validate initial probe before allocating or updating progress
            if len(first_bytes) <= 4096:
                part_path.write_bytes(first_bytes)
                if error := _download_error_payload(part_path, content_type, False):
                    part_path.unlink(missing_ok=True)
                    err_code = str(error.get("code") or error.get("errno") or "DOWNLOAD_FAILED")
                    err_msg = str(error.get("message") or "Download returned error payload instead of file")
                    raise ProviderFailure(err_code, err_msg, details=error)

            first_len = len(first_bytes)
            if total_size <= first_len:
                part_path.write_bytes(first_bytes)
                if error := _download_error_payload(part_path, content_type, False):
                    part_path.unlink(missing_ok=True)
                    err_code = str(error.get("code") or error.get("errno") or "DOWNLOAD_FAILED")
                    err_msg = str(error.get("message") or "Download returned error payload instead of file")
                    raise ProviderFailure(err_code, err_msg, details=error)
                progress.add_bytes(first_len, total_size or first_len, "download", dest.name)
                part_path.replace(dest)
                return dest

            # Prepare output file and write first 64 KB
            with part_path.open("wb") as f:
                f.truncate(total_size)
                f.seek(0)
                f.write(first_bytes)
            progress.add_bytes(first_len, total_size, "download", dest.name)

            remaining = total_size - first_len
            seg_size = max(512 * 1024, min(4 * 1024 * 1024, max(1, remaining // num_connections)))
            seg_queue: asyncio.Queue[tuple[int, int]] = asyncio.Queue()
            pos = first_len
            while pos < total_size:
                seg_end = min(total_size - 1, pos + seg_size - 1)
                seg_queue.put_nowait((pos, seg_end))
                pos = seg_end + 1

            worker_count = min(num_connections, seg_queue.qsize())
            progress.log(f"[Turbo] Downloading {dest.name} via {worker_count} parallel Range streams...")

            write_lock = threading.Lock()
            has_pwrite = hasattr(os, "pwrite")

            with part_path.open("r+b") as f_out:
                fd = f_out.fileno()

                def _write_at(data: bytes, offset: int) -> None:
                    if has_pwrite:
                        os.pwrite(fd, data, offset)
                    else:
                        with write_lock:
                            f_out.seek(offset)
                            f_out.write(data)

                async def _range_worker() -> None:
                    while not seg_queue.empty():
                        try:
                            seg_start, seg_end = seg_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        cur = seg_start
                        for attempt in range(5):
                            progress.check_cancelled()
                            if cur > seg_end:
                                break
                            req_hdr = {**base_headers, "Range": f"bytes={cur}-{seg_end}"}
                            try:
                                async with client.stream("GET", url, headers=req_hdr) as r_seg:
                                    if r_seg.status_code in (401, 403):
                                        raise ProviderFailure(
                                            "DOWNLOAD_FAILED",
                                            f"Range request rejected with HTTP {r_seg.status_code}",
                                            status=r_seg.status_code,
                                        )
                                    r_seg.raise_for_status()
                                    async for chunk in r_seg.aiter_bytes(262144):
                                        progress.check_cancelled()
                                        if not chunk:
                                            continue
                                        # Trim if server returned more than requested end
                                        max_need = (seg_end - cur) + 1
                                        if len(chunk) > max_need:
                                            chunk = chunk[:max_need]
                                        _write_at(chunk, cur)
                                        cur += len(chunk)
                                        progress.add_bytes(len(chunk), total_size, "download", dest.name)
                                        if cur > seg_end:
                                            break
                                if cur > seg_end:
                                    break
                            except ProviderFailure:
                                raise
                            except Exception as exc:
                                if attempt == 4:
                                    raise ProviderFailure("DOWNLOAD_FAILED", f"Parallel segment {cur}-{seg_end} failed: {exc}") from exc
                                await asyncio.sleep(min(2.0, 0.4 * (attempt + 1)))

                await asyncio.gather(*(_range_worker() for _ in range(worker_count)))

        if error := _download_error_payload(part_path, content_type, False):
            part_path.unlink(missing_ok=True)
            err_code = str(error.get("code") or error.get("errno") or "DOWNLOAD_FAILED")
            err_msg = str(error.get("message") or "Download returned error payload instead of file")
            raise ProviderFailure(err_code, err_msg, details=error)
        part_path.replace(dest)
        return dest

    async def _download_http_stream(self, url: str | list[str], dest_dir: Path, name: str | None, progress: JobState, headers: dict[str, str] | None = None, proxy: str | None = None, cookies: str | None = None) -> list[Path]:
        from ..providers import base as _base_mod
        urls = [str(item) for item in (url if isinstance(url, list) else [url]) if str(item or "")]
        last: ProviderFailure | None = None
        use_parallel = (
            stream_download is _default_stream_download
            and getattr(_base_mod.httpx.AsyncClient, "__module__", "").startswith("httpx")
        )
        options = (progress.payload or {}).get("options", {}) if hasattr(progress, "payload") else {}
        thread_count = int(options.get("download_threads") or options.get("threads_per_item") or options.get("downloadThreads") or DOWNLOAD_THREADS)
        thread_count = max(1, min(thread_count, 64))
        for one in urls:
            out_name = name or safe_name(unquote(Path(urlparse(one).path).name) or "download")
            try:
                progress.log(f"Starting browser-compatible download ({thread_count} threads): {out_name}")
                if use_parallel:
                    try:
                        downloaded = [await self._download_parallel_ranges(
                            one,
                            dest_dir / out_name,
                            progress,
                            headers=self._http_headers(headers, cookies=cookies),
                            proxy=proxy,
                            num_connections=thread_count,
                        )]
                    except Exception as range_exc:
                        if isinstance(range_exc, ProviderFailure) and self._is_ip_bound_token_error(range_exc.message):
                            raise
                        progress.log(f"Parallel Range download notice ({range_exc}); falling back to single-stream downloader...")
                        downloaded = [await stream_download(
                            one,
                            dest_dir / out_name,
                            progress,
                            headers=self._http_headers(headers, cookies=cookies),
                            auth_fail_code="DOWNLOAD_FAILED",
                            auth_fail_message="Direct link rejected Colab; this host likely binds the URL to the original browser/IP",
                            proxy=proxy,
                        )]
                else:
                    downloaded = [await stream_download(
                        one,
                        dest_dir / out_name,
                        progress,
                        headers=self._http_headers(headers, cookies=cookies),
                        auth_fail_code="DOWNLOAD_FAILED",
                        auth_fail_message="Direct link rejected Colab; this host likely binds the URL to the original browser/IP",
                        proxy=proxy,
                    )]
                self._validate_downloaded_files(downloaded)
                return downloaded
            except ProviderFailure as exc:
                last = exc
        if last:
            raise last
        raise ProviderFailure("DOWNLOAD_FAILED", "No URL provided")

    async def _download_aria2(self, url: str | list[str], dest_dir: Path, name: str | None, progress: JobState, headers: dict[str, str] | None = None, proxy: str | None = None, cookies: str | None = None) -> list[Path]:
        if proxy and str(proxy).startswith("socks"):
            return await self._download_http_stream(url, dest_dir, name, progress, headers=headers, proxy=proxy, cookies=cookies)
        urls = [str(item) for item in (url if isinstance(url, list) else [url]) if str(item or "")]
        uri_file: Path | None = None

        options = (progress.payload or {}).get("options", {}) if hasattr(progress, "payload") else {}
        thread_count = int(options.get("download_threads") or options.get("threads_per_item") or options.get("downloadThreads") or DOWNLOAD_THREADS)
        thread_count = max(1, min(thread_count, 64))
        max_conn = min(thread_count, 16)
        split_conn = thread_count

        cmd = [
            "aria2c", 
            f"--dir={dest_dir}",
            f"--max-connection-per-server={max_conn}",
            f"--split={split_conn}",
            "--min-split-size=1M",
            "--summary-interval=1",
            "--console-log-level=warn",
            "--auto-file-renaming=true",
            "--allow-overwrite=true",
            "--max-tries=5",
            "--retry-wait=2",
            "--uri-selector=adaptive",
        ]
        if proxy:
            cmd.append(f"--all-proxy={proxy}")

        all_headers = dict(headers or {})
        if cookies and "cookie" not in {str(k).lower() for k in all_headers}:
            all_headers["Cookie"] = cookies

        skip_hdrs = ("host", "content-length", "accept-encoding", "connection", "transfer-encoding", "range")
        if all_headers:
            for k, v in all_headers.items():
                low = str(k).lower()
                if v and low not in skip_hdrs:
                    cmd.append(f"--header={k}: {v}")
        if len(urls) > 1:
            uri_file = dest_dir / f".vaultbox-aria2-{uuid.uuid4().hex}.txt"
            header_lines = "".join([f"\n  header={k}: {v}" for k, v in all_headers.items() if v and str(k).lower() not in skip_hdrs])
            uri_file.write_text("\t".join(urls) + ("\n  out=" + name if name else "") + header_lines + "\n", encoding="utf-8")
            cmd.append(f"--input-file={uri_file}")
        elif name:
            cmd.append(f"--out={name}")
        if len(urls) <= 1:
            cmd.extend(urls)
        
        try:
            downloaded = await self._run_aria2_cmd(cmd, dest_dir, progress)
            expected = dest_dir / name if name else None
            res = [expected] if expected and expected.exists() else downloaded
            self._validate_downloaded_files(res)
            return res
        finally:
            if uri_file:
                uri_file.unlink(missing_ok=True)

    async def _download_torrent(self, url: str, dest_dir: Path, progress: JobState) -> list[Path]:
        progress.log("Starting aria2c torrent download")
        cmd = [
            "aria2c", 
            f"--dir={dest_dir}",
            "--seed-time=0",
            "--bt-stop-timeout=300",
            "--bt-max-peers=80",
            "--enable-dht=true",
            "--enable-peer-exchange=true",
            f"--bt-tracker={BT_TRACKERS}",
            "--summary-interval=1",
            "--console-log-level=warn",
            "--auto-file-renaming=true",
            "--allow-overwrite=true",
            "--max-tries=5",
            "--retry-wait=5",
            "--timeout=60"
        ]
        cmd.append(url)
        
        return await self._run_aria2_cmd(cmd, dest_dir, progress)
        
    async def _run_aria2_cmd(self, cmd: list[str], dest_dir: Path, progress: JobState) -> list[Path]:
        before = set(dest_dir.iterdir()) if dest_dir.exists() else set()
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        last_done = 0
        output_tail: list[str] = []
        if process.stdout:
            while True:
                chunk = await process.stdout.read(1024)
                if not chunk:
                    break
                for line in chunk.decode('utf-8', errors='ignore').replace("\r", "\n").splitlines():
                    line = line.strip()
                    if line:
                        output_tail = (output_tail + [line])[-3:]
                    progress.check_cancelled()
                    parsed = self._parse_aria2_progress(line)
                    if parsed:
                        done, total = parsed
                        diff = done - last_done
                        if diff > 0 and not (total > 0 and total <= 4096):
                            progress.add_bytes(diff, total)
                        last_done = done

        await process.wait()
        if process.returncode != 0:
            detail = f": {' | '.join(output_tail)}" if output_tail else ""
            raise ProviderFailure("DOWNLOAD_FAILED", f"aria2c exited with code {process.returncode}{detail}")

        after = set(dest_dir.iterdir()) if dest_dir.exists() else set()
        new_files = [p for p in (after - before) if p.is_file() and not p.name.endswith(".aria2") and not p.name.startswith(".vaultbox-aria2-")]
        res = new_files or [p for p in dest_dir.iterdir() if p.is_file() and not p.name.endswith(".aria2") and not p.name.startswith(".vaultbox-aria2-")]
        self._validate_downloaded_files(res)
        return res

    @staticmethod
    def _validate_downloaded_files(files: list[Path]) -> None:
        video_exts = (".mp4", ".mkv", ".ts", ".webm", ".avi", ".mov", ".flv", ".mp3", ".m4a")
        text_errors = (
            b"file not found", b"404 not found", b"403 forbidden", b"forbidden",
            b"access denied", b"link expired", b"url expired", b"invalid token", b"video deleted",
            b"ip not allowed", b"bad request", b"not authorized", b"unauthorized",
            b"<html", b"<!doctype html"
        )
        for f in files:
            if not f.is_file():
                continue
            sz = f.stat().st_size
            if sz == 0:
                f.unlink(missing_ok=True)
                raise ProviderFailure("DOWNLOAD_FAILED", f"Downloaded file {f.name} is empty (0 bytes)")
            clean_name = f.name.lower()
            is_media = any(clean_name.endswith(ext) for ext in video_exts)
            if sz <= 4096 and is_media:
                content = f.read_bytes()
                raw_low = content.lower()
                if any(err in raw_low for err in text_errors):
                    f.unlink(missing_ok=True)
                    sample = content.decode("utf-8", errors="ignore").strip()
                    raise ProviderFailure("DOWNLOAD_FAILED", f"Server returned error payload ({sample[:100]}) instead of media content for {f.name}")

    @staticmethod
    def _probe_media_durations(file_path: Path) -> dict[str, Any]:
        """Run ffprobe to measure video and audio stream durations with sub-second accuracy."""
        result: dict[str, Any] = {
            "has_video": False,
            "has_audio": False,
            "video_duration": 0.0,
            "audio_duration": 0.0,
            "container_duration": 0.0,
        }
        if not shutil.which("ffprobe") or not file_path.exists() or file_path.stat().st_size == 0:
            return result
        try:
            cmd = [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration:stream=codec_type,duration",
                "-of", "json",
                str(file_path),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if res.returncode != 0:
                return result
            data = json.loads(res.stdout)
            fmt = data.get("format") or {}
            if fmt.get("duration"):
                try:
                    result["container_duration"] = float(fmt["duration"])
                except (ValueError, TypeError):
                    pass
            streams = data.get("streams") or []
            for s in streams:
                ctype = str(s.get("codec_type") or "").lower()
                dur_str = s.get("duration")
                dur_val = 0.0
                if dur_str:
                    try:
                        dur_val = float(dur_str)
                    except (ValueError, TypeError):
                        dur_val = 0.0
                if ctype == "video":
                    result["has_video"] = True
                    if dur_val > result["video_duration"]:
                        result["video_duration"] = dur_val
                elif ctype == "audio":
                    result["has_audio"] = True
                    if dur_val > result["audio_duration"]:
                        result["audio_duration"] = dur_val
            # Fallback to container duration if stream duration is missing
            if result["has_video"] and result["video_duration"] == 0.0 and result["container_duration"] > 0:
                result["video_duration"] = result["container_duration"]
            if result["has_audio"] and result["audio_duration"] == 0.0 and result["container_duration"] > 0:
                result["audio_duration"] = result["container_duration"]
        except Exception:
            pass
        return result

    @staticmethod
    def _heal_audio_clock(video_path: Path, target_duration: float, progress: JobState | None = None) -> bool:
        """
        Pad silence to audio track if audio ends prematurely (e.g. at 28m while video is 44m),
        preventing media players (VLC, KMPlayer) from stalling on seek waiting for Audio Clock packets.
        Uses -c:v copy to run in ~1-2 seconds with zero video re-encoding.
        """
        if not shutil.which("ffmpeg") or not video_path.exists() or video_path.stat().st_size == 0 or target_duration <= 0:
            return False
        tmp_fixed = video_path.with_name(f"{video_path.stem}_audiofix{video_path.suffix}")
        try:
            if progress:
                progress.log(f"[Audio-Guard] Healing audio clock: padding silence to {target_duration:.1f}s to eliminate seek freeze...")
            cmd = [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-filter_complex", f"[0:a]apad=whole_dur={target_duration}[a]",
                "-map", "0:v:0",
                "-map", "[a]",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-movflags", "+faststart",
                str(tmp_fixed),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if res.returncode == 0 and tmp_fixed.exists() and tmp_fixed.stat().st_size > 0:
                shutil.move(str(tmp_fixed), str(video_path))
                if progress:
                    progress.log(f"[Audio-Guard] Audio clock successfully healed to {target_duration:.1f}s! Player seek freeze eliminated.")
                return True
            else:
                tmp_fixed.unlink(missing_ok=True)
                return False
        except Exception as exc:
            tmp_fixed.unlink(missing_ok=True)
            if progress:
                progress.log(f"[Audio-Guard] Audio clock healing notice: {exc}")
            return False

    async def _download_stream_nm3u8dl(
        self,
        url: str,
        dest_dir: Path,
        name: str | None,
        progress: JobState,
        headers: dict[str, str] | None = None,
        cookies: str | None = None,
        dec_key: dict[str, Any] | None = None,
        proxy: str | None = None,
        page_url: str | None = None,
        file_ref: dict[str, Any] | None = None,
    ) -> list[Path]:
        # Universal High-Speed Strategy:
        # If a canonical webpage URL is present (from Sniffer JSON), attempt downloading via yt-dlp first
        # ONLY IF the webpage is from a platform where yt-dlp has a dedicated high-speed extractor (e.g. Pornhub, YouTube)
        if page_url and page_url.startswith(("http://", "https://")) and page_url != url:
            clean_page = page_url
            if "pornhub.com" in clean_page:
                clean_page = re.sub(r'https?://[a-zA-Z0-9_-]+\.pornhub\.com', 'https://www.pornhub.com', clean_page)
            if any(dom in clean_page.lower() for dom in ("pornhub.com", "phncdn.com", "youtube.com", "youtu.be", "vimeo.com")):
                progress.log(f"[links] Supported canonical page detected. Attempting high-speed extraction with yt-dlp: {clean_page[:80]}...")
                try:
                    return await self._download_ytdlp(clean_page, dest_dir, name, progress, headers=None, cookies=cookies, proxy=proxy)
                except Exception as page_err:
                    progress.log(f"[links] Canonical page extraction bypassed ({str(page_err)[:100]}). Falling back to multi-threaded stream download...")

        dest_dir.mkdir(parents=True, exist_ok=True)
        current_url = url
        current_headers = dict(headers or {})

        raw_clean = name or safe_name(unquote(Path(urlparse(url).path).name) or "stream.mp4")
        if not raw_clean.lower().endswith((".mp4", ".mkv", ".webm", ".ts")):
            raw_clean += ".mp4"
        out_name = re.sub(r'[,;:*?"<>|/\\`\s]+', '_', raw_clean).strip('_')
        if not out_name.lower().endswith((".mp4", ".mkv", ".webm", ".ts")):
            out_name += ".mp4"
        name_hash = hashlib.sha256(out_name.encode("utf-8", errors="ignore")).hexdigest()[:12]
        stg_dir = dest_dir / f".tmp_ytdlp_{name_hash}"

        final_dest_file = dest_dir / out_name
        if final_dest_file.is_file() and final_dest_file.stat().st_size > 0:
            has_incomplete = stg_dir.exists() and bool(list(stg_dir.glob("*.part*")) or list(stg_dir.glob("*.ytdl*")))
            if not has_incomplete:
                progress.log(f"[links] Target file already exists and is complete: {final_dest_file.name} ({final_dest_file.stat().st_size} bytes). Skipping download.")
                return [final_dest_file]

        def _measure_staged_bytes() -> int:
            if not stg_dir.exists():
                return 0
            total = 0
            for p in stg_dir.glob("*"):
                try:
                    if p.is_file():
                        total += p.stat().st_size
                except Exception:
                    pass
            return total

        attempt = 0
        no_progress_consecutive = 0
        max_consecutive_no_progress = 6
        max_total_attempts = 50

        while attempt < max_total_attempts:
            attempt += 1
            progress.check_cancelled()
            stall_info = f"stall {no_progress_consecutive}/{max_consecutive_no_progress}" if no_progress_consecutive > 0 else "active"
            log_label = (
                f"Starting yt-dlp multi-fragment stream download: {name or current_url[:60]}"
                if attempt == 1 else
                f"[Resume] Resuming stream download (run {attempt}/{max_total_attempts}, {stall_info}): {name or current_url[:60]}"
            )
            progress.log(log_label)

            before_bytes = _measure_staged_bytes()
            before_job_bytes = getattr(progress, "bytes_done", 0) or 0

            is_final_attempt = (no_progress_consecutive >= max_consecutive_no_progress - 1) or (attempt >= max_total_attempts)
            try:
                try:
                    return await self._download_ytdlp(
                        current_url, dest_dir, name, progress,
                        headers=current_headers, cookies=cookies, proxy=proxy,
                        can_retry=not is_final_attempt,
                    )
                except TypeError:
                    return await self._download_ytdlp(
                        current_url, dest_dir, name, progress,
                        headers=current_headers,
                    )
            except Exception as e:
                err_str = str(e)
                after_bytes = _measure_staged_bytes()
                after_job_bytes = getattr(progress, "bytes_done", 0) or 0
                gained_bytes = max(0, after_bytes - before_bytes, after_job_bytes - before_job_bytes)

                if gained_bytes > 32 * 1024:
                    no_progress_consecutive = 0
                    mb_gained = gained_bytes / (1024 * 1024)
                    progress.log(f"[Stream-Resume] Run {attempt} downloaded +{mb_gained:.2f} MB. Progress made! Resetting stall counter to 0.")
                else:
                    no_progress_consecutive += 1
                    stall_reason = "truncated audio track" if "AUDIO_STREAM_TRUNCATED" in err_str else "no byte progress"
                    progress.log(f"[Stream-Resume] Run {attempt} made no progress ({stall_reason}, stalled {no_progress_consecutive}/{max_consecutive_no_progress}).")

                # If error was fragment file missing / hole, clean leftover incomplete part-Frag files in stage dir
                if any(k in err_str.lower() for k in ("no such file", "part-frag")):
                    for frag_f in stg_dir.glob("*.part-Frag*"):
                        try:
                            frag_f.unlink(missing_ok=True)
                        except Exception:
                            pass

                # Stop if 6 consecutive runs made NO progress at all or max total attempts reached
                if no_progress_consecutive >= max_consecutive_no_progress or attempt >= max_total_attempts:
                    progress.log(f"[Stream-Resume] Stopped after {no_progress_consecutive} consecutive stalls (total runs: {attempt}).")
                    if page_url and page_url != url and page_url.startswith(("http://", "https://")):
                        clean_page_url = page_url
                        if "pornhub.com" in clean_page_url:
                            clean_page_url = re.sub(r'https?://[a-zA-Z0-9_-]+\.pornhub\.com', 'https://www.pornhub.com', clean_page_url)
                        is_ytdlp_site = any(d in clean_page_url.lower() for d in ("pornhub.com", "phncdn.com", "youtube.com", "youtu.be", "vimeo.com"))
                        if is_ytdlp_site:
                            progress.log(f"[Fallback] Direct stream failed ({err_str[:80]}). Retrying from canonical page URL with yt-dlp: {clean_page_url[:80]}...")
                            try:
                                return await self._download_ytdlp(clean_page_url, dest_dir, name, progress, headers=None, cookies=cookies, proxy=proxy, can_retry=False)
                            except Exception as page_err:
                                progress.log(f"[Fallback] Canonical page URL download failed: {page_err}")
                    raise

                can_refresh = bool(page_url or (file_ref and file_ref.get("page_url")))
                if can_refresh:
                    ref_payload = {**(file_ref or {}), "page_url": page_url or (file_ref or {}).get("page_url")}
                    progress.log(f"[Stream-Resume] Stream token expired or interrupted ({err_str[:80]}). Auto-refreshing fresh token and cookies from page...")
                    try:
                        res = await self._resolve_universal_page(current_url, ref_payload, progress, proxy=proxy)
                        fresh_stream_url = res[0]
                        if fresh_stream_url:
                            current_url = fresh_stream_url
                            if res[2]:
                                current_headers.update(res[2])
                                if "Cookie" in res[2]:
                                    cookies = res[2]["Cookie"]
                            progress.log(f"[Stream-Resume] Fresh stream URL and cookies acquired! Continuing download with yt-dlp...")
                            await asyncio.sleep(1.0)
                            continue
                    except Exception as ref_err:
                        progress.log(f"[Stream-Resume] Auto-refresh notice: {ref_err}")
                else:
                    await asyncio.sleep(2.0)

    @staticmethod
    def _write_netscape_cookies(cookies_str: str, domain: str, dest_file: Path) -> Path | None:
        """Write raw Cookie string (key=value; ...) into Netscape format cookies file."""
        if not cookies_str:
            return None
        domain_clean = str(domain).strip()
        if "://" in domain_clean:
            domain_clean = urlparse(domain_clean).netloc
        domain_clean = domain_clean.split(":")[0]
        parts = [p for p in domain_clean.split(".") if p]
        if len(parts) >= 2:
            base_domain = f".{parts[-2]}.{parts[-1]}"
        else:
            base_domain = f".{domain_clean}" if not domain_clean.startswith(".") else domain_clean

        domains_to_write = {base_domain}
        if domain_clean:
            domains_to_write.add(f".{domain_clean.lstrip('.')}")
            domains_to_write.add(domain_clean)

        lines = ["# Netscape HTTP Cookie File\n", "# https://curl.se/docs/http-cookies.html\n\n"]
        for item in str(cookies_str).split(";"):
            item = item.strip()
            if not item or "=" not in item:
                continue
            k, v = item.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k:
                for d in domains_to_write:
                    is_sub = "TRUE" if d.startswith(".") else "FALSE"
                    lines.append(f"{d}\t{is_sub}\t/\tTRUE\t2147483647\t{k}\t{v}\n")

        if len(lines) > 2:
            dest_file.write_text("".join(lines), encoding="utf-8")
            return dest_file
        return None

    async def _download_ytdlp(
        self,
        url: str,
        dest_dir: Path,
        name: str | None,
        progress: JobState,
        headers: dict[str, str] | None = None,
        cookies: str | None = None,
        proxy: str | None = None,
        can_retry: bool = False,
    ) -> list[Path]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        raw_clean = name or safe_name(unquote(Path(urlparse(url).path).name) or "stream.mp4")
        if not raw_clean.lower().endswith((".mp4", ".mkv", ".webm", ".ts")):
            raw_clean += ".mp4"
        out_name = re.sub(r'[,;:*?"<>|/\\`\s]+', '_', raw_clean).strip('_')
        if not out_name.lower().endswith((".mp4", ".mkv", ".webm", ".ts")):
            out_name += ".mp4"
        progress.current_file = out_name

        name_hash = hashlib.sha256(out_name.encode("utf-8", errors="ignore")).hexdigest()[:12]
        stage_dir = dest_dir / f".tmp_ytdlp_{name_hash}"
        stage_dir.mkdir(parents=True, exist_ok=True)
        completed_files: list[Path] = []

        if "pornhub.com" in url:
            url = re.sub(r'https?://[a-zA-Z0-9_-]+\.pornhub\.com', 'https://www.pornhub.com', url)

        parsed_url = urlparse(url)
        default_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        default_ref = f"{parsed_url.scheme}://{parsed_url.netloc}/" if parsed_url.netloc else ""
        if "pornhub.com" in url:
            default_ref = "https://www.pornhub.com/"

        req_headers = dict(headers or {})
        if "pornhub.com" in url:
            for k in list(req_headers.keys()):
                low = k.lower()
                if low in ("referer", "origin"):
                    req_headers[k] = re.sub(r'https?://[a-zA-Z0-9_-]+\.pornhub\.com/?', 'https://www.pornhub.com/', req_headers[k])
                elif low == "accept" and "vnd.t1c" in str(req_headers[k]):
                    del req_headers[k]

        # Extract any cookies passed in headers so they are never passed via --add-header
        header_cookies = ""
        for k in list(req_headers.keys()):
            if k.lower() == "cookie":
                header_cookies = str(req_headers.pop(k)).strip()

        has_ua = any(k.lower() == "user-agent" for k in req_headers)
        has_ref = any(k.lower() == "referer" for k in req_headers)

        ytdlp_bin = shutil.which("yt-dlp") or str(Path(sys.executable).parent / "yt-dlp.exe")
        if not (shutil.which(ytdlp_bin) or os.path.exists(ytdlp_bin)):
            ytdlp_bin = "yt-dlp"

        options = (progress.payload or {}).get("options", {}) if hasattr(progress, "payload") else {}
        thread_count = int(options.get("download_threads") or options.get("threads_per_item") or options.get("downloadThreads") or (32 if proxy else DOWNLOAD_THREADS))
        thread_count = max(1, min(thread_count, 64))

        cmd = [
            ytdlp_bin,
            "--no-warnings",
            "--no-playlist",
            "--no-check-certificates",
            "--newline",
            "--progress",
            "--continue",
            "-f", "bestvideo*+bestaudio/best",
            "-N", str(thread_count),
            "--concurrent-fragments", str(thread_count),
            "--socket-timeout", "30",
            "--fragment-retries", "15",
            "--retries", "15",
            "--retry-sleep", "fragment:exp=2:4:30",
            "--buffer-size", "16M",
            "--paths", f"home:{str(stage_dir)}",
            "--paths", f"temp:{str(stage_dir)}",
            "-o", out_name,
        ]

        is_direct_stream = any(k in url.lower() for k in (".m3u8", ".mpd", "m3u8"))
        if is_direct_stream:
            cmd.extend(["--downloader", "dash,m3u8:native"])
            if options.get("skip_broken_segments") or options.get("skipBrokenSegments") or options.get("skip_failed_segments"):
                cmd.append("--skip-unavailable-fragments")
            else:
                cmd.append("--abort-on-unavailable-fragment")

        # Clean any 0-byte or corrupted fragment files left from interrupted downloads
        for stray_frag in stage_dir.glob("*.part-Frag*"):
            try:
                if stray_frag.is_file() and stray_frag.stat().st_size == 0:
                    stray_frag.unlink(missing_ok=True)
            except Exception:
                pass

        # Enable browser impersonation via curl_cffi to bypass Cloudflare/TLS fingerprint detection
        has_curl_cffi = False
        try:
            import curl_cffi
            has_curl_cffi = True
        except ImportError:
            pass
        if has_curl_cffi and not is_direct_stream:
            cmd.extend(["--impersonate", "Chrome"])

        if not has_ua:
            cmd.extend(["--user-agent", default_ua])
        if not has_ref and default_ref:
            cmd.extend(["--referer", default_ref])

        if shutil.which("ffmpeg"):
            cmd.extend(["--remux-video", "mp4"])

        skip_hdrs = (
            "host", "content-length", "accept-encoding", "connection", "range", "transfer-encoding",
            "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
            "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "sec-fetch-user",
            "cookie",
        )
        if req_headers:
            for k, v in req_headers.items():
                if not v:
                    continue
                k_low = str(k).lower()
                if k_low == "referer":
                    cmd.extend(["--referer", str(v)])
                elif k_low == "user-agent":
                    cmd.extend(["--user-agent", str(v)])
                elif k_low not in skip_hdrs:
                    cmd.extend(["--add-header", f"{k}: {v}"])

        all_cookie_parts = []
        if cookies:
            all_cookie_parts.append(str(cookies).strip())
        if header_cookies:
            all_cookie_parts.append(str(header_cookies).strip())
        combined_cookies = "; ".join(p for p in all_cookie_parts if p)

        cookie_dir: Path | None = None
        if combined_cookies:
            ref_domain = req_headers.get("referer") or req_headers.get("Referer") or default_ref or url
            cookie_dir = Path(tempfile.gettempdir()) / f"vb_cookies_{uuid.uuid4().hex[:8]}"
            cookie_dir.mkdir(parents=True, exist_ok=True)
            c_file = cookie_dir / "cookies.txt"
            if self._write_netscape_cookies(combined_cookies, ref_domain, c_file):
                cmd.extend(["--cookies", str(c_file)])

        if proxy:
            cmd.extend(["--proxy", proxy])

        cmd.append(url)
        progress.log(f"Starting yt-dlp multi-fragment download for: {url}")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            if process.stdout:
                while True:
                    chunk = await process.stdout.read(2048)
                    if not chunk:
                        break
                    text = chunk.decode('utf-8', errors='ignore').replace('\r', '\n')
                    for line in text.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        progress.check_cancelled()
                        self._update_stream_progress(line, progress)

            await process.wait()
            if process.returncode != 0:
                stderr_out = await process.stderr.read() if process.stderr else b""
                err_msg = stderr_out.decode('utf-8', errors='ignore').strip()
                raise ProviderFailure("DOWNLOAD_FAILED", f"yt-dlp failed (exit code {process.returncode}): {err_msg[:300]}")

            # Check if incomplete stream parts remain (e.g. video finished but audio still downloading)
            stray_parts = [p for p in stage_dir.glob("*.part") if p.is_file() and p.stat().st_size > 0]
            if stray_parts:
                part_names = ", ".join(p.name for p in stray_parts[:3])
                raise ProviderFailure("DOWNLOAD_FAILED", f"Stream download incomplete (parts remaining: {part_names})")

            # Look for the final merged video file, avoiding unmerged intermediate track files
            final_target = stage_dir / out_name
            if final_target.exists() and final_target.stat().st_size > 0:
                completed_files = [final_target]
            else:
                completed_files = [
                    p for p in stage_dir.iterdir()
                    if p.is_file()
                    and not p.name.startswith(".")
                    and not p.name.endswith((".part", ".ytdl", ".aria2", ".tmp", ".txt", ".json", ".log"))
                    and not (p.name.lower() in ("cookies.txt", "cookie.txt") or p.name.lower().startswith("cookie"))
                    and ".part-Frag" not in p.name
                    and not re.search(r'\.f[0-9a-zA-Z_-]+\.(mp4|m4a|webm|mkv|ts)$', p.name)
                    and p.stat().st_size > 0
                ]

            if not completed_files:
                raise ProviderFailure("DOWNLOAD_FAILED", "yt-dlp finished but no valid merged video file was produced (incomplete streams)")

            final_files = []
            for src_file in completed_files:
                target_file = dest_dir / (out_name if len(completed_files) == 1 and name else src_file.name)
                shutil.move(str(src_file), str(target_file))

                # Verify audio & video stream integrity
                probe_res = self._probe_media_durations(target_file)
                v_dur = probe_res.get("video_duration", 0.0)
                a_dur = probe_res.get("audio_duration", 0.0)
                has_a = probe_res.get("has_audio", False)
                has_v = probe_res.get("has_video", False)

                if has_v and has_a and v_dur > 30.0 and a_dur > 0.0:
                    dur_gap = v_dur - a_dur
                    if dur_gap > 4.0:
                        gap_m = int(dur_gap // 60)
                        gap_s = int(dur_gap % 60)
                        gap_desc = f"{gap_m}m{gap_s}s" if gap_m else f"{dur_gap:.1f}s"
                        progress.log(f"[Audio-Guard] Detected audio truncation: video is {v_dur:.1f}s, audio is only {a_dur:.1f}s (missing {gap_desc})!")
                        if can_retry:
                            # Move back to stage_dir so it can be resumed or re-downloaded
                            shutil.move(str(target_file), str(src_file))
                            raise ProviderFailure("AUDIO_STREAM_TRUNCATED", f"Audio stream truncated at {a_dur:.1f}s while video is {v_dur:.1f}s (gap {gap_desc}). Retrying resume...")
                        else:
                            # Max retries reached; heal audio clock with silence padding to eliminate VLC/KMPlayer seek freeze
                            self._heal_audio_clock(target_file, v_dur, progress)

                final_files.append(target_file)

            # Clean any stray cookie files in dest_dir
            for extra in dest_dir.glob("*cookie*"):
                if extra.is_file():
                    extra.unlink(missing_ok=True)

            return final_files
        finally:
            if completed_files:
                shutil.rmtree(stage_dir, ignore_errors=True)
            if cookie_dir:
                shutil.rmtree(cookie_dir, ignore_errors=True)

    @staticmethod
    def _extract_gdrive_id_and_resourcekey(url: str) -> tuple[str, str]:
        u = str(url or "").strip()
        if not u:
            return "", ""
        parsed = urlparse(u)
        qs = parse_qs(parsed.query)
        rk = str((qs.get("resourcekey") or qs.get("resourceKey") or [""])[0] or "").strip()

        for pat in (
            r"/folders/([A-Za-z0-9_-]{10,})",
            r"/file/d/([A-Za-z0-9_-]{10,})",
            r"/(?:document|spreadsheets|presentation|drawings)/d/([A-Za-z0-9_-]{10,})",
            r"/d/([A-Za-z0-9_-]{10,})",
        ):
            m = re.search(pat, parsed.path)
            if m:
                return m.group(1), rk

        q_id = str((qs.get("id") or [""])[0] or "").strip()
        if q_id:
            return q_id, rk

        tail = parsed.path.rstrip("/").split("/")[-1].strip()
        if re.match(r"^[A-Za-z0-9_-]{15,}$", tail):
            return tail, rk
        return "", rk

    @staticmethod
    def _clean_gdrive_html_title(raw_title: str) -> str:
        t = html.unescape(str(raw_title or "")).strip()
        t = re.sub(r"\s+", " ", t)
        for suffix in (
            " - Google Drive",
            " – Google Drive",
            " - Google Tài liệu",
            " - Google Trang tính",
            " - Google Trang trình bày",
            " - Google Docs",
            " - Google Sheets",
            " - Google Slides",
        ):
            if t.lower().endswith(suffix.lower()):
                t = t[: -len(suffix)].strip()
                break
        bad_lower = t.lower()
        if not t or bad_lower in {
            "google drive",
            "meet google drive – one place for all your files",
            "sign in - google accounts",
            "google accounts",
            "error 404 (not found)!!1",
            "page not found",
        } or "sign in" in bad_lower and "google" in bad_lower:
            return ""
        return t

    @staticmethod
    def _parse_content_disposition_filename(cd_header: str) -> str:
        if not cd_header:
            return ""
        m_utf8 = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;\r\n]+)", cd_header)
        if m_utf8:
            val = unquote(m_utf8.group(1).strip().strip('"'))
            if val:
                return val
        m_quoted = re.search(r'filename\s*=\s*"([^"\r\n]+)"', cd_header)
        if m_quoted:
            val = unquote(m_quoted.group(1).strip())
            if val:
                return val
        m_plain = re.search(r"filename\s*=\s*([^;\r\n]+)", cd_header)
        if m_plain:
            val = unquote(m_plain.group(1).strip().strip('"'))
            if val:
                return val
        return ""

    @staticmethod
    def _extract_gdrive_confirm_download_url(html_text: str, fallback_fid: str = "", rk: str = "") -> tuple[str, str]:
        """Parse Google Drive virus-scan confirmation HTML and return (direct_download_url, discovered_filename)."""
        discovered_name = ""
        m_name = re.search(r'class="uc-name-size"[^>]*>\s*<a[^>]*>(.*?)</a>', html_text, re.S | re.I)
        if m_name:
            discovered_name = html.unescape(re.sub(r"<[^>]+>", "", m_name.group(1))).strip()

        if "uc-error-subcaption" in html_text:
            m_err = re.search(r'<p[^>]*class="uc-error-subcaption"[^>]*>(.*?)</p>', html_text, re.S | re.I)
            err_txt = html.unescape(re.sub(r"<[^>]+>", "", m_err.group(1))).strip() if m_err else "Google Drive denied public download"
            raise ProviderFailure("PROVIDER_RATE_LIMITED" if "quota" in err_txt.lower() or "too many" in err_txt.lower() else "DOWNLOAD_FAILED", err_txt)

        m_form = re.search(r'<form[^>]*id="download-form"[^>]*action="([^"]+)"[^>]*>(.*?)</form>', html_text, re.S | re.I)
        if m_form:
            action = html.unescape(m_form.group(1))
            form_inner = m_form.group(2)
            parsed_action = urlparse(action)
            q_params = {k: v[0] for k, v in parse_qs(parsed_action.query).items() if v}
            for inp in re.finditer(r'<input[^>]+type="hidden"[^>]*>', form_inner, re.I):
                tag = inp.group(0)
                mn = re.search(r'name="([^"]+)"', tag, re.I)
                mv = re.search(r'value="([^"]*)"', tag, re.I)
                if mn and mv is not None:
                    q_params[html.unescape(mn.group(1))] = html.unescape(mv.group(1))
            if rk and "resourcekey" not in q_params:
                q_params["resourcekey"] = rk
            new_q = urlencode(q_params)
            return urlunparse((parsed_action.scheme or "https", parsed_action.netloc or "drive.usercontent.google.com", parsed_action.path, "", new_q, "")), discovered_name

        m_dl = re.search(r'"downloadUrl"\s*:\s*"([^"]+)"', html_text)
        if m_dl:
            dl_u = m_dl.group(1).replace("\\u003d", "=").replace("\\u0026", "&").replace("\\/", "/")
            return dl_u, discovered_name

        m_href = re.search(r'href="(/uc\?export=download[^"]+)"', html_text)
        if m_href:
            return "https://drive.google.com" + html.unescape(m_href.group(1)), discovered_name

        if fallback_fid:
            params = {"id": fallback_fid, "export": "download", "confirm": "t"}
            if rk:
                params["resourcekey"] = rk
            return f"https://drive.usercontent.google.com/download?{urlencode(params)}", discovered_name
        return "", discovered_name

    @classmethod
    def _normalize_gdrive_workspace_item(cls, fid: str, raw_title: str, mime: str, rk: str = "") -> tuple[str, str] | None:
        """If ``mime`` is a Google Docs/Sheets/Slides/Drawings native document, return (export_url, filename_with_ext)."""
        if not mime.startswith("application/vnd.google-apps.") or mime in (_GDRIVE_FOLDER_MIME, _GDRIVE_SHORTCUT_MIME):
            return None
        kind_map = {
            "application/vnd.google-apps.document": ("document", "docx", ".docx"),
            "application/vnd.google-apps.spreadsheet": ("spreadsheets", "xlsx", ".xlsx"),
            "application/vnd.google-apps.presentation": ("presentation", "pptx", ".pptx"),
            "application/vnd.google-apps.drawing": ("drawings", "pdf", ".pdf"),
        }
        if mime in kind_map:
            doc_path, fmt, ext = kind_map[mime]
            q = {"format": fmt}
            if rk:
                q["resourcekey"] = rk
            export_url = f"https://docs.google.com/{doc_path}/d/{fid}/export?{urlencode(q)}"
            clean_t = safe_name(raw_title or fid)
            if not clean_t.lower().endswith(ext):
                clean_t = f"{clean_t}{ext}"
            return export_url, clean_t
        return None

    async def _resolve_gdrive_folder_title(self, folder_id: str, resourcekey: str = "") -> str:
        if not folder_id:
            return ""
        cache = getattr(self, "_folder_title_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._folder_title_cache = cache
        if folder_id in cache and cache[folder_id]:
            return cache[folder_id]

        headers = {"User-Agent": _GDRIVE_BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=headers) as client:
            params: dict[str, str] = {"id": folder_id}
            if resourcekey:
                params["resourcekey"] = resourcekey
            try:
                r_emb = await client.get("https://drive.google.com/embeddedfolderview", params=params)
                if r_emb.status_code == 200 and r_emb.text:
                    m_t = re.search(r"<title>(.*?)</title>", r_emb.text, re.S | re.I)
                    if m_t:
                        title = self._clean_gdrive_html_title(m_t.group(1))
                        if title:
                            cache[folder_id] = title
                            return title
            except Exception:
                pass

            try:
                folder_url = f"https://drive.google.com/drive/folders/{folder_id}"
                r_page = await client.get(folder_url, params={"resourcekey": resourcekey} if resourcekey else None)
                if r_page.status_code == 200 and r_page.text:
                    m_t = re.search(r"<title>(.*?)</title>", r_page.text, re.S | re.I)
                    if m_t:
                        title = self._clean_gdrive_html_title(m_t.group(1))
                        if title:
                            cache[folder_id] = title
                            return title
            except Exception:
                pass
        return ""

    async def _list_gdrive_folder_items(self, url: str, credentials: dict[str, Any] | None = None) -> tuple[str, list[dict[str, Any]]]:
        folder_id, rk = self._extract_gdrive_id_and_resourcekey(url)
        if not folder_id:
            return "", []

        title_task = asyncio.create_task(self._resolve_gdrive_folder_title(folder_id, rk))
        items: list[dict[str, Any]] = []

        def _make_item(cid: str, raw_name: str, mime: str, size: int = 0) -> dict[str, Any] | None:
            if not cid or mime == _GDRIVE_SHORTCUT_MIME:
                return None
            if mime == _GDRIVE_FOLDER_MIME:
                sub_u = f"https://drive.google.com/drive/folders/{cid}"
                d_name = safe_name(raw_name or cid, is_dir=True)
                return {
                    "id": sub_u,
                    "path": sub_u,
                    "url": sub_u,
                    "name": d_name,
                    "raw_name": raw_name or d_name,
                    "type": "folder",
                    "is_folder": True,
                    "mimeType": mime,
                    "size": 0,
                    "provider": "links",
                    "meta": {"provider": "links", "gdrive_id": cid, "mimeType": mime, "type": "folder"},
                }
            ws_doc = self._normalize_gdrive_workspace_item(cid, raw_name, mime, rk=rk)
            if ws_doc is not None:
                exp_url, exp_name = ws_doc
                return {
                    "id": exp_url,
                    "path": exp_url,
                    "url": exp_url,
                    "name": exp_name,
                    "raw_name": raw_name or exp_name,
                    "type": "file",
                    "is_folder": False,
                    "mimeType": mime,
                    "size": 0,
                    "provider": "links",
                    "meta": {"provider": "links", "gdrive_id": cid, "mimeType": mime, "size": 0},
                }
            q_dl = {"id": cid, "export": "download"}
            if rk:
                q_dl["resourcekey"] = rk
            dl_u = f"https://drive.google.com/uc?{urlencode(q_dl)}"
            f_name = safe_name(raw_name or cid)
            return {
                "id": dl_u,
                "path": dl_u,
                "url": dl_u,
                "name": f_name,
                "raw_name": raw_name or f_name,
                "type": "file",
                "is_folder": False,
                "mimeType": mime,
                "size": max(0, int(size or 0)),
                "provider": "links",
                "meta": {"provider": "links", "gdrive_id": cid, "mimeType": mime, "size": max(0, int(size or 0))},
            }

        # 1. Primary: Paginated drivefrontend-pa v1/items:list (supports 1000+ items & exact byte sizes)
        try:
            async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
                page_token = ""
                parent_entry = [folder_id, 0, rk] if rk else [folder_id, 0]
                while True:
                    body: list[Any] = [[None] * 44, [1000, page_token, [2, 5]]]
                    body[0][4] = 0
                    body[0][21] = 0
                    body[0][24] = [4, 1, 1]
                    body[0][35] = [[1]]
                    body[0][43] = [parent_entry]
                    resp = await client.post(
                        "https://drivefrontend-pa.clients6.google.com/v1/items:list",
                        params={"key": _GDRIVE_FRONTEND_API_KEY},
                        json=body,
                        headers={
                            "User-Agent": _GDRIVE_BROWSER_UA,
                            "Origin": "https://drive.google.com",
                            "Referer": f"https://drive.google.com/drive/folders/{folder_id}",
                            "content-type": "application/json+protobuf",
                            "x-goog-fieldmask": "items(id,title,mime_type,file_size,explicitly_trashed,trashed),continuation_token",
                        },
                    )
                    if resp.status_code != 200:
                        items.clear()
                        break
                    txt = resp.text.lstrip()
                    if txt.startswith(")]}'"):
                        txt = txt[4:].lstrip()
                    data = json.loads(txt)
                    raw_rows = data[0] if isinstance(data, list) and data and isinstance(data[0], list) else []
                    for row in raw_rows:
                        if not isinstance(row, list) or not row:
                            continue
                        cid = str(row[0] if len(row) > 0 and row[0] else "")
                        cname = str(row[2] if len(row) > 2 and row[2] else "")
                        cmime = str(row[3] if len(row) > 3 and row[3] else "")
                        csize = int(row[13] if len(row) > 13 and row[13] else 0)
                        trashed = bool((len(row) > 25 and row[25]) or (len(row) > 87 and row[87]))
                        if trashed:
                            continue
                        built = _make_item(cid, cname, cmime, csize)
                        if built:
                            items.append(built)
                    next_tok = str(data[1] if isinstance(data, list) and len(data) > 1 and data[1] else "")
                    if not next_tok or next_tok == page_token:
                        break
                    page_token = next_tok
        except Exception:
            items.clear()

        # 2. Fallback: _DRIVE_ivd embedded JSON or embeddedfolderview HTML links
        if not items:
            try:
                async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers={"User-Agent": _GDRIVE_BROWSER_UA}) as client:
                    r_page = await client.get(
                        f"https://drive.google.com/drive/folders/{folder_id}",
                        params={"resourcekey": rk} if rk else None,
                    )
                    if r_page.status_code == 200 and r_page.text:
                        m_ivd = re.search(r"_DRIVE_ivd'\s*\]\s*=\s*'([^']+)'", r_page.text)
                        if m_ivd:
                            dec = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), m_ivd.group(1))
                            dec = dec.replace("\\'", "'").replace("\\\\", "\\")
                            ivd_data = json.loads(dec)
                            raw_rows = ivd_data[0] if isinstance(ivd_data, list) and ivd_data and isinstance(ivd_data[0], list) else []
                            for row in raw_rows:
                                if not isinstance(row, list) or not row:
                                    continue
                                cid = str(row[0] if len(row) > 0 and row[0] else "")
                                cname = str(row[2] if len(row) > 2 and row[2] else "")
                                cmime = str(row[3] if len(row) > 3 and row[3] else "")
                                csize = int(row[13] if len(row) > 13 and row[13] else 0)
                                built = _make_item(cid, cname, cmime, csize)
                                if built:
                                    items.append(built)

                    if not items:
                        r_emb = await client.get(
                            "https://drive.google.com/embeddedfolderview",
                            params={"id": folder_id, **({"resourcekey": rk} if rk else {})},
                        )
                        if r_emb.status_code == 200 and r_emb.text:
                            for m_a in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r_emb.text, re.S | re.I):
                                href = html.unescape(m_a.group(1))
                                label = html.unescape(re.sub(r"<[^>]+>", "", m_a.group(2))).strip()
                                m_f = re.search(r"drive\.google\.com/file/d/([A-Za-z0-9_-]{10,})", href)
                                if m_f:
                                    built = _make_item(m_f.group(1), label, "application/octet-stream", 0)
                                    if built:
                                        items.append(built)
                                    continue
                                m_doc = re.search(r"docs\.google\.com/(\w+)/d/([A-Za-z0-9_-]{10,})", href)
                                if m_doc:
                                    kind, doc_id = m_doc.group(1), m_doc.group(2)
                                    mime = f"application/vnd.google-apps.{kind.removesuffix('s')}"
                                    built = _make_item(doc_id, label, mime, 0)
                                    if built:
                                        items.append(built)
                                    continue
                                m_dir = re.search(r"/folders/([A-Za-z0-9_-]{10,})", href)
                                if m_dir:
                                    built = _make_item(m_dir.group(1), label, _GDRIVE_FOLDER_MIME, 0)
                                    if built:
                                        items.append(built)
            except Exception:
                pass

        # 3. Authenticated fallback if Drive credentials were supplied
        if not items and credentials and (credentials.get("access_token") or credentials.get("token") or credentials.get("cookies")):
            try:
                from .drive import DriveProvider
                drv_res = await DriveProvider().list_files(credentials, folder_id)
                for d_it in drv_res.get("items") or []:
                    built = _make_item(
                        str(d_it.get("id") or ""),
                        str(d_it.get("name") or ""),
                        str(d_it.get("mimeType") or (_GDRIVE_FOLDER_MIME if d_it.get("is_folder") else "application/octet-stream")),
                        int(d_it.get("size") or 0),
                    )
                    if built:
                        items.append(built)
            except Exception:
                pass

        folder_title = ""
        try:
            folder_title = await title_task
        except Exception:
            folder_title = ""

        if folder_title:
            titles = getattr(self, "_last_folder_titles", None)
            if not isinstance(titles, dict):
                titles = {}
                self._last_folder_titles = titles
            titles[url] = folder_title

        return folder_title, items

    async def _resolve_gdrive_file_meta(self, url: str) -> dict[str, Any]:
        fid, rk = self._extract_gdrive_id_and_resourcekey(url)
        if not fid:
            return {}
        params = {"id": fid, "export": "download", "confirm": "t"}
        if rk:
            params["resourcekey"] = rk
        probe_url = f"https://drive.usercontent.google.com/download?{urlencode(params)}"
        try:
            async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers={"User-Agent": _GDRIVE_BROWSER_UA}) as client:
                async with client.stream("GET", probe_url) as resp:
                    if resp.status_code >= 400:
                        return {}
                    ct = (resp.headers.get("content-type") or "").lower()
                    cd = resp.headers.get("content-disposition") or ""
                    fname = self._parse_content_disposition_filename(cd)
                    cl = resp.headers.get("content-length") or ""
                    size = int(cl) if cl.isdigit() else 0
                    if fname:
                        return {"name": safe_name(fname), "size": size}
                    if "text/html" in ct:
                        raw_bytes = await resp.aread()
                        html_txt = raw_bytes.decode("utf-8", errors="replace")
                        _, html_name = self._extract_gdrive_confirm_download_url(html_txt, fallback_fid=fid, rk=rk)
                        if html_name:
                            return {"name": safe_name(html_name), "size": 0}
        except Exception:
            pass
        return {}

    async def resolve_item_metadata(self, credentials: dict[str, Any], item: dict[str, Any], *, resolve_file_names: bool = False) -> dict[str, Any]:
        """Resolve real folder/file title for cloud links that currently only have an ID placeholder."""
        url = str(item.get("id") or item.get("path") or item.get("url") or "").strip()
        if not url:
            return item
        cur_name = str(item.get("name") or "").strip()
        ltype = self._classify_link(url)
        is_placeholder = is_placeholder_link_name(cur_name, url)

        if ltype == "yandex_disk":
            yd_parsed = self._parse_yandex_disk_url(url)
            need_type_probe = yd_parsed.get("kind") in ("d", "hash") and not isinstance(item.get("_prefetched_listing"), dict)
            if need_type_probe or is_placeholder:
                try:
                    folder_title, children, raw_meta = await self._list_yandex_folder_items(url)
                    if raw_meta and raw_meta.get("type") == "dir":
                        item["type"] = "folder"
                        item["is_folder"] = True
                        if isinstance(item.get("meta"), dict):
                            item["meta"]["type"] = "folder"
                            item["meta"]["cloud_host"] = "Yandex Disk"
                        if folder_title and is_placeholder:
                            item["name"] = safe_name(folder_title, is_dir=True)
                        elif cur_name.lower().endswith(".mp4") and is_placeholder:
                            item["name"] = safe_name(cur_name[:-4], is_dir=True)
                        item["_prefetched_listing"] = {"name": folder_title, "items": children}
                    elif raw_meta and raw_meta.get("type") == "file":
                        item["type"] = "file"
                        item["is_folder"] = False
                        real_fname = str(raw_meta.get("name") or "").strip()
                        real_size = int(raw_meta.get("size") or 0)
                        if real_fname and is_placeholder:
                            item["name"] = safe_name(real_fname)
                        if real_size and not item.get("size"):
                            item["size"] = real_size
                        if not isinstance(item.get("meta"), dict):
                            item["meta"] = {}
                        meta_d = item["meta"]
                        meta_d["provider"] = "links"
                        meta_d["cloud_host"] = "Yandex Disk"
                        meta_d["type"] = "file"
                        if real_size:
                            meta_d["size"] = real_size
                        if raw_meta.get("mime_type"):
                            meta_d["mimeType"] = str(raw_meta["mime_type"])
                        if raw_meta.get("file"):
                            meta_d["yandex_direct_url"] = str(raw_meta["file"])
                            meta_d["yandex_direct_ts"] = time.time()
                        meta_d["yandex_public_key"] = yd_parsed.get("public_key", "")
                        meta_d["yandex_path"] = yd_parsed.get("path", "")
                except Exception:
                    pass
            return item

        if not is_placeholder:
            return item

        if ltype == "gdrive_folder":
            fid, rk = self._extract_gdrive_id_and_resourcekey(url)
            title = await self._resolve_gdrive_folder_title(fid, rk)
            if title:
                item["name"] = safe_name(title, is_dir=True)
            return item

        if ltype in ("mega_folder", "pixeldrain_list", "gofile_page", "mediafire_folder"):
            try:
                listing = await self.list_files(credentials, url)
                item["_prefetched_listing"] = listing
                title = str(listing.get("name") or "").strip()
                if title:
                    item["name"] = safe_name(title, is_dir=True)
            except Exception:
                pass
            return item

        if resolve_file_names and ltype == "gdrive":
            meta = await self._resolve_gdrive_file_meta(url)
            if meta.get("name"):
                item["name"] = meta["name"]
            if meta.get("size") and not item.get("size"):
                item["size"] = meta["size"]
        return item

    async def _download_gdrive(self, url: str, dest_dir: Path, name: str | None, progress: JobState) -> list[Path]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        clean_name = None if is_placeholder_link_name(name, url) else safe_name(name)
        fid, rk = self._extract_gdrive_id_and_resourcekey(url)
        is_docs_export = "docs.google.com/" in url.lower() and "/export" in url.lower()

        # 1. Fast async streaming via drive.usercontent.google.com / docs export (live progress + MB/s speed!)
        if fid or is_docs_export:
            try:
                if is_docs_export:
                    dl_url = url
                else:
                    q_params = {"id": fid, "export": "download", "confirm": "t"}
                    if rk:
                        q_params["resourcekey"] = rk
                    dl_url = f"https://drive.usercontent.google.com/download?{urlencode(q_params)}"

                cookies_jar = httpx.Cookies()
                resolved_name = clean_name or ""
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(None, connect=25.0, read=60.0, write=30.0, pool=30.0),
                    follow_redirects=True,
                    headers={"User-Agent": _GDRIVE_BROWSER_UA},
                    cookies=cookies_jar,
                ) as probe_client:
                    for _hop in range(2):
                        async with probe_client.stream("GET", dl_url) as resp:
                            if resp.status_code in (401, 403, 429):
                                raise ProviderFailure("PROVIDER_RATE_LIMITED", f"Google Drive rejected public download (HTTP {resp.status_code})")
                            resp.raise_for_status()
                            ct = (resp.headers.get("content-type") or "").lower()
                            cd = resp.headers.get("content-disposition") or ""
                            cd_name = self._parse_content_disposition_filename(cd)
                            if cd_name and not resolved_name:
                                resolved_name = safe_name(cd_name)

                            if "text/html" in ct and not cd:
                                html_body = (await resp.aread()).decode("utf-8", errors="replace")
                                next_url, html_name = self._extract_gdrive_confirm_download_url(html_body, fallback_fid=fid, rk=rk)
                                if html_name and not resolved_name:
                                    resolved_name = safe_name(html_name)
                                if next_url and next_url != dl_url:
                                    dl_url = next_url
                                    continue
                                raise ProviderFailure("DOWNLOAD_FAILED", "Google Drive returned HTML confirmation page instead of binary stream")

                            final_filename = resolved_name or safe_name(f"{fid or 'gdrive_file'}.bin")
                            target_path = dest_dir / final_filename
                            part_path = target_path.with_suffix(target_path.suffix + ".part")
                            total_bytes = int(resp.headers.get("content-length") or 0)
                            progress.current_file = target_path.name
                            done = 0
                            with part_path.open("wb") as fh:
                                async for chunk in resp.aiter_bytes(262144):
                                    progress.check_cancelled()
                                    if not chunk:
                                        continue
                                    fh.write(chunk)
                                    done += len(chunk)
                                    progress.add_bytes(len(chunk), total_bytes, "download", target_path.name)
                            if total_bytes and done < total_bytes:
                                # Resume remaining bytes via stream_download
                                downloaded_file = await _default_stream_download(
                                    dl_url,
                                    target_path,
                                    progress,
                                    headers={"User-Agent": _GDRIVE_BROWSER_UA},
                                    cookies=cookies_jar,
                                )
                                return [downloaded_file]
                            part_path.replace(target_path)
                            self._validate_downloaded_files([target_path])
                            return [target_path]
            except asyncio.CancelledError:
                raise
            except Exception as stream_exc:
                if progress.__class__.__name__ == "JobCancelled" or stream_exc.__class__.__name__ == "JobCancelled":
                    raise
                progress.log(f"[GDrive] Direct stream notice ({stream_exc}); trying fallback downloader...")

        # 2. Authenticated Drive fallback if job carries Google Drive credentials (bypasses anonymous public quota!)
        if fid and not is_docs_export:
            payload = getattr(progress, "payload", None) or {}
            for side in ("source", "target"):
                side_obj = payload.get(side) or {}
                if str(side_obj.get("provider") or "").lower() == "drive":
                    drv_creds = side_obj.get("credentials") or {}
                    if drv_creds and (drv_creds.get("access_token") or drv_creds.get("token") or drv_creds.get("cookies")):
                        try:
                            from .drive import DriveProvider
                            progress.log("[GDrive] Using connected Google Drive session to fetch quota-limited file...")
                            drv_path = await DriveProvider().download_file(
                                drv_creds,
                                {"id": fid, **({"name": clean_name} if clean_name else {})},
                                dest_dir,
                                progress,
                            )
                            if drv_path and drv_path.is_file():
                                return [drv_path]
                        except Exception as drv_exc:
                            progress.log(f"[GDrive] Connected Drive session fallback notice: {drv_exc}")
                        break

        # 3. Fallback to gdown / yt-dlp
        progress.log(f"Starting gdown download for: {url}")
        try:
            import gdown
            before = {p for p in dest_dir.rglob("*") if p.is_file()} if dest_dir.exists() else set()
            def run_gdown() -> None:
                output = str(dest_dir / clean_name) if (clean_name and "." in clean_name) else str(dest_dir) + "/"
                gdown.download(url, output, quiet=True, fuzzy=True)
            await asyncio.to_thread(run_gdown)
            after = {p for p in dest_dir.rglob("*") if p.is_file()} if dest_dir.exists() else set()
            new_files = [p for p in sorted(after - before) if p.is_file() and not p.name.endswith(".part")]
            return new_files or [p for p in sorted(dest_dir.rglob("*")) if p.is_file() and not p.name.endswith(".part")]
        except Exception as e:
            progress.log(f"gdown failed, falling back to yt-dlp: {e}")
            return await self._download_ytdlp(url, dest_dir, clean_name, progress)

    async def _download_gdrive_folder(self, url: str, dest_dir: Path, progress: JobState) -> list[Path]:
        progress.log(f"Starting Google Drive folder download via gdown: {url}")
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            import inspect
            import gdown
            before = {p for p in dest_dir.rglob("*") if p.is_file()} if dest_dir.exists() else set()
            sig_params = inspect.signature(gdown.download_folder).parameters
            def run_gdown_folder() -> None:
                kw: dict[str, Any] = {"url": url, "output": str(dest_dir), "quiet": True, "use_cookies": False}
                if "remaining_ok" in sig_params:
                    kw["remaining_ok"] = True
                gdown.download_folder(**kw)
            await asyncio.to_thread(run_gdown_folder)
            after = {p for p in dest_dir.rglob("*") if p.is_file()} if dest_dir.exists() else set()
            new_files = [p for p in sorted(after - before) if p.is_file() and not p.name.endswith(".part")]
            res = new_files or [p for p in sorted(dest_dir.rglob("*")) if p.is_file() and not p.name.endswith(".part")]
            if not res:
                raise ProviderFailure("SOURCE_FILE_NOT_FOUND", f"No files downloaded from Google Drive folder: {url}")
            self._validate_downloaded_files(res)
            return res
        except ProviderFailure:
            raise
        except Exception as exc:
            raise ProviderFailure("DOWNLOAD_FAILED", f"Google Drive folder download failed: {exc}") from exc

    # -------------------------------------------------------------------------
    # MEGA.nz Cloud Support (File + Folder with AES-128-ECB/CBC/CTR Decryption)
    # -------------------------------------------------------------------------

    @staticmethod
    def _mega_b64_decode(s: str) -> bytes:
        s = str(s or "").strip().replace("-", "+").replace("_", "/").replace(",", "")
        pad = len(s) % 4
        if pad:
            s += "=" * (4 - pad)
        return base64.b64decode(s)

    @staticmethod
    def _mega_bytes_to_a32(b: bytes) -> list[int]:
        if len(b) % 4:
            b += b"\x00" * (4 - (len(b) % 4))
        return list(struct.unpack(f">{len(b) // 4}I", b))

    @staticmethod
    def _mega_a32_to_bytes(a: list[int]) -> bytes:
        return struct.pack(f">{len(a)}I", *(x & 0xFFFFFFFF for x in a))

    @classmethod
    def _mega_aes_ecb_decrypt(cls, data: bytes, key: bytes) -> bytes:
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.backends import default_backend
            dec = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend()).decryptor()
            return dec.update(data) + dec.finalize()
        except ImportError:
            from Crypto.Cipher import AES
            return AES.new(key, AES.MODE_ECB).decrypt(data)

    @classmethod
    def _mega_aes_cbc_decrypt(cls, data: bytes, key: bytes) -> bytes:
        iv = b"\x00" * 16
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.backends import default_backend
            dec = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend()).decryptor()
            return dec.update(data) + dec.finalize()
        except ImportError:
            from Crypto.Cipher import AES
            return AES.new(key, AES.MODE_CBC, iv).decrypt(data)

    @classmethod
    def _mega_decrypt_attr(cls, attr_b64: str, key_a32: list[int]) -> dict[str, Any]:
        try:
            raw = cls._mega_b64_decode(attr_b64)
            if not raw:
                return {}
            if len(key_a32) >= 8:
                k = [key_a32[0] ^ key_a32[4], key_a32[1] ^ key_a32[5], key_a32[2] ^ key_a32[6], key_a32[3] ^ key_a32[7]]
            else:
                k = key_a32[:4]
            dec = cls._mega_aes_cbc_decrypt(raw, cls._mega_a32_to_bytes(k)).rstrip(b"\x00")
            if dec.startswith(b"MEGA{"):
                end = dec.rfind(b"}") + 1
                if end > 4:
                    return json.loads(dec[4:end].decode("utf-8", errors="replace"))
        except Exception:
            pass
        return {}

    @staticmethod
    def _parse_mega_url(url: str) -> dict[str, str]:
        """Parse MEGA URL formats:
        - https://mega.nz/folder/FOLDER_ID#KEY or .../folder/FOLDER_ID#KEY/file/SUB_ID
        - https://mega.nz/file/FILE_ID#KEY
        - https://mega.nz/#F!FOLDER_ID!KEY
        - https://mega.nz/#!FILE_ID!KEY
        """
        u = str(url or "").strip()
        m_folder = re.search(r"mega\.(?:co\.)?nz/folder/([a-zA-Z0-9_-]+)#([a-zA-Z0-9_-]+)(?:/(?:file|folder)/([a-zA-Z0-9_-]+))?", u)
        if m_folder:
            return {"kind": "folder", "id": m_folder.group(1), "key": m_folder.group(2), "sub_id": m_folder.group(3) or ""}
        m_old_folder = re.search(r"mega\.(?:co\.)?nz/#F!([a-zA-Z0-9_-]+)!([a-zA-Z0-9_-]+)", u)
        if m_old_folder:
            return {"kind": "folder", "id": m_old_folder.group(1), "key": m_old_folder.group(2), "sub_id": ""}
        m_file = re.search(r"mega\.(?:co\.)?nz/file/([a-zA-Z0-9_-]+)#([a-zA-Z0-9_-]+)", u)
        if m_file:
            return {"kind": "file", "id": m_file.group(1), "key": m_file.group(2), "sub_id": ""}
        m_old_file = re.search(r"mega\.(?:co\.)?nz/#!([a-zA-Z0-9_-]+)!([a-zA-Z0-9_-]+)", u)
        if m_old_file:
            return {"kind": "file", "id": m_old_file.group(1), "key": m_old_file.group(2), "sub_id": ""}
        raise ProviderFailure("SOURCE_FILE_NOT_FOUND", f"Invalid or unsupported MEGA URL format (missing decryption key after #): {u}")

    async def _mega_api_req(self, payload: list[dict[str, Any]], params: dict[str, Any] | None = None, proxy: str | None = None) -> Any:
        from ..providers.base import shared_client
        from ..utils.http import ensure_socks_support

        active_proxy = proxy
        last_err: Exception | None = None
        for attempt in range(6):
            q = {"id": str(random.randint(100000, 9999999))}
            if params:
                q.update({k: str(v) for k, v in params.items() if v is not None})
            api_url = f"https://g.api.mega.co.nz/cs?{urlencode(q)}"
            try:
                if active_proxy:
                    if "socks" in str(active_proxy).lower():
                        ensure_socks_support()
                    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True, proxy=active_proxy) as client:
                        resp = await client.post(api_url, json=payload)
                else:
                    client = shared_client("mega_api_direct", timeout=25.0, follow_redirects=True)
                    resp = await client.post(api_url, json=payload)

                if resp.status_code in (429, 509):
                    daemons = await self._ensure_mega_tor_pool()
                    if daemons:
                        active_proxy = self._isolated_proxy(daemons[attempt % len(daemons)], uuid.uuid4().hex[:10])
                        continue
                    raise ProviderFailure("PROVIDER_RATE_LIMITED", f"MEGA API rate limited (HTTP {resp.status_code})")

                resp.raise_for_status()
                data = resp.json()
            except ProviderFailure:
                raise
            except Exception as exc:
                last_err = exc
                daemons = await self._ensure_mega_tor_pool()
                if daemons and attempt < 5:
                    active_proxy = self._isolated_proxy(daemons[attempt % len(daemons)], uuid.uuid4().hex[:10])
                    await asyncio.sleep(0.3 * (attempt + 1))
                    continue
                if attempt < 5:
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue
                raise ProviderFailure("DOWNLOAD_FAILED", f"MEGA API request failed: {exc}") from exc

            code: int | None = None
            if isinstance(data, int) and data < 0:
                code = data
            elif isinstance(data, list) and data and isinstance(data[0], int) and data[0] < 0:
                code = data[0]

            if code is not None:
                if code in (-3, -4) and attempt < 5:
                    if code == -4:
                        daemons = await self._ensure_mega_tor_pool()
                        if daemons:
                            active_proxy = self._isolated_proxy(daemons[attempt % len(daemons)], uuid.uuid4().hex[:10])
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue
                if code in (-9, -16, -11):
                    raise ProviderFailure("SOURCE_FILE_NOT_FOUND", f"MEGA item not found or access denied (API error {code})")
                if code in (-17, -18):
                    daemons = await self._ensure_mega_tor_pool()
                    if daemons and attempt < 5:
                        active_proxy = self._isolated_proxy(daemons[attempt % len(daemons)], uuid.uuid4().hex[:10])
                        continue
                    raise ProviderFailure("PROVIDER_RATE_LIMITED", f"MEGA bandwidth quota or temporary unavailability (API error {code})")
                raise ProviderFailure("DOWNLOAD_FAILED", f"MEGA API error {code}")
            return data[0] if isinstance(data, list) and data else data
        raise ProviderFailure("DOWNLOAD_FAILED", f"MEGA API busy ({last_err or '-3 EAGAIN'})")

    async def _list_mega_folder_nodes(self, url: str, proxy: str | None = None) -> list[dict[str, Any]]:
        parsed = self._parse_mega_url(url)
        if parsed["kind"] != "folder":
            return []
        folder_id = parsed["id"]
        folder_key_b64 = parsed["key"]
        sub_target = parsed.get("sub_id") or ""
        master_key = self._mega_b64_decode(folder_key_b64)
        if len(master_key) not in (16, 24, 32):
            master_key = master_key[:16].ljust(16, b"\x00")

        res = await self._mega_api_req([{"a": "f", "c": 1, "r": 1}], params={"n": folder_id}, proxy=proxy)
        if not isinstance(res, dict) or "f" not in res:
            raise ProviderFailure("SOURCE_FILE_NOT_FOUND", f"Could not list MEGA folder {folder_id}")

        raw_nodes = res.get("f") or []
        nodes_by_id: dict[str, dict[str, Any]] = {}
        for n in raw_nodes:
            h = str(n.get("h") or "")
            p = str(n.get("p") or "")
            t = int(n.get("t") if n.get("t") is not None else 0)
            k_full = str(n.get("k") or "")
            if not h or not k_full:
                continue
            # A node key may contain multiple slash-separated entries: "owner:b64/owner2:b64"
            dec_k_a32: list[int] = []
            attrs: dict[str, Any] = {}
            for part in k_full.split("/"):
                k_b64 = part.split(":", 1)[1] if ":" in part else part
                if not k_b64:
                    continue
                try:
                    enc_k = self._mega_b64_decode(k_b64)
                    if len(enc_k) % 16 != 0:
                        continue
                    dec_k_bytes = self._mega_aes_ecb_decrypt(enc_k, master_key)
                    cand_k_a32 = self._mega_bytes_to_a32(dec_k_bytes)
                    cand_attrs = self._mega_decrypt_attr(str(n.get("a") or ""), cand_k_a32)
                    if cand_attrs and cand_attrs.get("n"):
                        dec_k_a32 = cand_k_a32
                        attrs = cand_attrs
                        break
                    if not dec_k_a32:
                        dec_k_a32 = cand_k_a32
                        attrs = cand_attrs
                except Exception:
                    continue
            if not dec_k_a32:
                continue
            if not attrs:
                attrs = self._mega_decrypt_attr(str(n.get("a") or ""), dec_k_a32)
            node_name = safe_name(str(attrs.get("n") or h))
            nodes_by_id[h] = {
                "h": h,
                "p": p,
                "t": t,
                "s": int(n.get("s") or 0),
                "name": node_name,
                "key_a32": dec_k_a32,
            }

        if not nodes_by_id:
            return []

        # Build relative paths for all file nodes (t == 0)
        # Note: The root folder node itself is the first t==1 node whose parent is not in nodes_by_id.
        root_handles = {h for h, info in nodes_by_id.items() if info["p"] not in nodes_by_id}
        mega_title = ""
        if sub_target and sub_target in nodes_by_id and nodes_by_id[sub_target]["t"] == 1:
            mega_title = str(nodes_by_id[sub_target]["name"] or "").strip()
        elif len(root_handles) == 1:
            only_root = next(iter(root_handles))
            if nodes_by_id[only_root]["t"] == 1:
                mega_title = str(nodes_by_id[only_root]["name"] or "").strip()
        if mega_title:
            titles = getattr(self, "_last_folder_titles", None)
            if not isinstance(titles, dict):
                titles = {}
                self._last_folder_titles = titles
            titles[url] = mega_title

        def _build_rel_path(handle: str) -> str:
            parts: list[str] = []
            curr = handle
            visited: set[str] = set()
            while curr and curr in nodes_by_id and curr not in visited:
                visited.add(curr)
                info = nodes_by_id[curr]
                # If there is a single root folder in MEGA share and transfer_job already creates folder_dir for the item,
                # check if curr is the root handle itself. Including root folder name or relative children:
                if curr in root_handles and len(root_handles) == 1 and info["t"] == 1:
                    break
                parts.append(info["name"])
                curr = info["p"]
            parts.reverse()
            return "/".join(parts) if parts else nodes_by_id[handle]["name"]

        file_items: list[dict[str, Any]] = []
        for h, info in nodes_by_id.items():
            if info["t"] != 0:
                continue
            if sub_target and h != sub_target:
                # Check if sub_target is an ancestor folder of h
                curr_p = info["p"]
                is_desc = False
                seen_p: set[str] = set()
                while curr_p and curr_p in nodes_by_id and curr_p not in seen_p:
                    if curr_p == sub_target:
                        is_desc = True
                        break
                    seen_p.add(curr_p)
                    curr_p = nodes_by_id[curr_p]["p"]
                if not is_desc:
                    continue
            rel_path = _build_rel_path(h)
            k_a32 = info["key_a32"]
            if len(k_a32) < 8:
                continue
            file_key_bytes = self._mega_a32_to_bytes([k_a32[0] ^ k_a32[4], k_a32[1] ^ k_a32[5], k_a32[2] ^ k_a32[6], k_a32[3] ^ k_a32[7]])
            file_iv_bytes = self._mega_a32_to_bytes([k_a32[4], k_a32[5], 0, 0])
            node_url = f"https://mega.nz/folder/{folder_id}#{folder_key_b64}/file/{h}"
            file_items.append({
                "id": node_url,
                "path": node_url,
                "url": node_url,
                "name": info["name"],
                "type": "file",
                "size": info["s"],
                "provider": "links",
                "_relative_path": rel_path,
                "meta": {
                    "provider": "links",
                    "mega_folder_id": folder_id,
                    "mega_node_id": h,
                    "mega_key_hex": file_key_bytes.hex(),
                    "mega_iv_hex": file_iv_bytes.hex(),
                    "size": info["s"],
                    "relative_path": rel_path,
                },
            })
        return file_items

    def _mark_mega_direct_quota_exceeded(self, progress: JobState | None = None) -> None:
        now = time.time()
        type(self)._mega_direct_cooldown_until = now + 10800.0  # 3 hours cooldown
        type(self)._mega_direct_was_throttled = True
        if progress and (now - type(self)._mega_direct_notice_ts) >= 30.0:
            type(self)._mega_direct_notice_ts = now
            progress.log("[MEGA] Direct IP bandwidth limit reached; switching to Multi-Circuit Tor Turbo pool and resuming...")

    @staticmethod
    def _pack_int_ranges(indices: set[int]) -> list[list[int]]:
        if not indices:
            return []
        ordered = sorted(indices)
        ranges: list[list[int]] = [[ordered[0], ordered[0]]]
        for val in ordered[1:]:
            if val == ranges[-1][1] + 1:
                ranges[-1][1] = val
            else:
                ranges.append([val, val])
        return ranges

    @staticmethod
    def _unpack_int_ranges(ranges: list[Any], upper_bound: int) -> set[int]:
        out: set[int] = set()
        if upper_bound <= 0 or not isinstance(ranges, list):
            return out
        for item in ranges:
            try:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    lo = max(0, int(item[0]))
                    hi = min(upper_bound - 1, int(item[1]))
                    if lo <= hi:
                        out.update(range(lo, hi + 1))
                elif isinstance(item, int) and 0 <= item < upper_bound:
                    out.add(item)
            except (TypeError, ValueError):
                continue
        return out

    async def _download_mega_stream_decrypt(
        self,
        dl_url: str,
        dest: Path,
        file_key: bytes,
        file_iv: bytes,
        expected_size: int,
        progress: JobState,
        proxy: str | None = None,
        refresh_url: Any = None,
    ) -> Path:
        import contextlib
        import threading
        from ..providers.base import shared_client, track_client
        from ..utils.http import ensure_socks_support

        dest.parent.mkdir(parents=True, exist_ok=True)
        part_path = dest.with_suffix(dest.suffix + ".part")
        ckpt_path = dest.parent / f".{dest.name}.ckpt.part"
        ckpt_tmp_path = dest.parent / f".{dest.name}.ckpt.tmp.part"
        key_fp = hashlib.sha256(file_key + file_iv + f":{expected_size}".encode("ascii")).hexdigest()[:24]
        progress.current_file = dest.name
        req_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0.0.0 Safari/537.36"}

        def _cleanup_partial_files() -> None:
            part_path.unlink(missing_ok=True)
            ckpt_path.unlink(missing_ok=True)
            ckpt_tmp_path.unlink(missing_ok=True)

        def _is_cancel_exc(exc_obj: BaseException | None) -> bool:
            if exc_obj is None:
                return False
            return isinstance(exc_obj, asyncio.CancelledError) or exc_obj.__class__.__name__ == "JobCancelled"

        def _make_decryptor(initial_iv: bytes, byte_offset: int = 0) -> Any:
            if byte_offset > 0:
                block_offset = byte_offset // 16
                iv_int = (int.from_bytes(initial_iv, "big") + block_offset) & ((1 << 128) - 1)
                ctr_iv = iv_int.to_bytes(16, "big")
            else:
                ctr_iv = initial_iv
            try:
                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                from cryptography.hazmat.backends import default_backend
                return Cipher(algorithms.AES(file_key), modes.CTR(ctr_iv), backend=default_backend()).decryptor()
            except ImportError:
                from Crypto.Cipher import AES
                from Crypto.Util import Counter
                ctr = Counter.new(128, initial_value=int.from_bytes(ctr_iv, "big"))
                return AES.new(file_key, AES.MODE_CTR, counter=ctr)

        class _SlowMegaCircuit(Exception):
            pass

        url_state = {"dl_url": dl_url}
        refresh_lock = asyncio.Lock()

        # Fallback path when exact byte size is unknown (or 0-byte node)
        if expected_size <= 0:
            done = 0
            resumed_stream = False
            if part_path.is_file() and ckpt_path.is_file():
                try:
                    meta_s = json.loads(ckpt_path.read_text(encoding="utf-8"))
                    if isinstance(meta_s, dict) and meta_s.get("key_fp") == key_fp and meta_s.get("mode") == "stream":
                        raw_sz = part_path.stat().st_size
                        done = (raw_sz // 16) * 16
                        if raw_sz != done:
                            with part_path.open("r+b") as f_tr:
                                f_tr.truncate(done)
                        resumed_stream = done > 0
                except Exception:
                    done = 0
            if not resumed_stream:
                part_path.unlink(missing_ok=True)
                done = 0
                try:
                    ckpt_path.write_text(json.dumps({"v": 1, "key_fp": key_fp, "mode": "stream"}), encoding="utf-8")
                except Exception:
                    pass
            else:
                sync_fn = getattr(progress, "sync_resumed_file_bytes", None)
                if callable(sync_fn):
                    sync_fn(dest.name, done, 0, phase="download")
                progress.log(f"[MEGA] Tiếp tục tải nối tiếp {dest.name}: đã giữ {done / (1024 * 1024):.1f} MB, tải tiếp phần còn lại...")

            initial_done = done
            worker_px: str | None = proxy
            worker_cli: httpx.AsyncClient | None = None
            saw_rl = False
            stall_attempts = 0
            max_stall_attempts = 18
            consecutive_509 = 0
            try:
                while stall_attempts < max_stall_attempts:
                    progress.check_cancelled()
                    if done % 16 != 0:
                        done = (done // 16) * 16
                        if part_path.exists():
                            with part_path.open("r+b") as f_tr:
                                f_tr.truncate(done)
                    daemons: list[str] = []
                    use_tor = bool(worker_px) or (time.time() < type(self)._mega_direct_cooldown_until)
                    if use_tor:
                        daemons = await self._ensure_mega_tor_pool(progress)
                        if not daemons:
                            raise ProviderFailure(
                                "PROVIDER_RATE_LIMITED",
                                "MEGA bandwidth quota exceeded and Tor proxy unavailable",
                                progress_bytes=max(0, done - initial_done),
                                done_bytes=done,
                            )
                        if worker_cli is None:
                            if worker_px:
                                ensure_socks_support()
                                worker_cli = track_client(httpx.AsyncClient(proxy=worker_px, follow_redirects=True, timeout=httpx.Timeout(connect=12.0, read=20.0, write=15.0, pool=20.0)))
                            else:
                                worker_px, worker_cli = self._acquire_mega_circuit(daemons)
                        active_cli = worker_cli
                    else:
                        active_cli = shared_client("mega_dl_direct", follow_redirects=True, timeout=httpx.Timeout(connect=20.0, read=45.0, write=30.0, pool=30.0))

                    used_px = worker_px
                    curr_u = url_state["dl_url"]
                    req_u = f"{curr_u.rstrip('/')}/{done}-" if done > 0 else curr_u
                    decryptor = _make_decryptor(file_iv, done)
                    rotate_cli = False
                    need_refresh = False
                    pos_before = done
                    last_stream_exc: Exception | None = None
                    try:
                        async with active_cli.stream("GET", req_u, headers=req_headers) as resp:
                            if resp.status_code in (509, 429):
                                saw_rl = True
                                if not use_tor:
                                    self._mark_mega_direct_quota_exceeded(progress)
                                    rotate_cli = True
                                    saw_rl = False
                                    await asyncio.sleep(0.5)
                                else:
                                    rotate_cli = True
                                    consecutive_509 += 1
                                    if consecutive_509 >= 2:
                                        await self._rotate_tor_newnym(progress, used_px)
                                    if refresh_url is not None and consecutive_509 >= 4 and consecutive_509 % 4 == 0:
                                        need_refresh = True
                                    await asyncio.sleep(1.0 + random.uniform(0.4, 1.2))
                            elif resp.status_code in (403, 404, 410) and refresh_url is not None:
                                rotate_cli = use_tor
                                need_refresh = True
                            else:
                                resp.raise_for_status()
                                saw_rl = False
                                consecutive_509 = 0
                                total = done + int(resp.headers.get("content-length") or 0)
                                with part_path.open("ab" if done > 0 else "wb") as fh:
                                    async for chunk in resp.aiter_bytes(131072):
                                        progress.check_cancelled()
                                        if not chunk:
                                            continue
                                        plain = decryptor.update(chunk)
                                        fh.write(plain)
                                        done += len(plain)
                                        progress.add_bytes(len(plain), total, "download", dest.name)
                                    if hasattr(decryptor, "finalize"):
                                        tail = decryptor.finalize()
                                        if tail:
                                            fh.write(tail)
                                            done += len(tail)
                                break
                    except BaseException as exc:
                        if _is_cancel_exc(exc):
                            _cleanup_partial_files()
                            raise
                        if isinstance(exc, ProviderFailure):
                            raise
                        last_stream_exc = exc if isinstance(exc, Exception) else RuntimeError(str(exc))
                        rotate_cli = use_tor

                    if done > pos_before:
                        stall_attempts = 0
                        consecutive_509 = 0
                    else:
                        stall_attempts += 1

                    if rotate_cli:
                        await self._discard_mega_circuit(worker_cli)
                        worker_px = None
                        worker_cli = None

                    if need_refresh and refresh_url is not None:
                        async with refresh_lock:
                            if url_state["dl_url"] == curr_u:
                                ref_px = worker_px
                                if ref_px is None and use_tor and daemons:
                                    ref_px = self._isolated_proxy(random.choice(daemons), uuid.uuid4().hex[:10])
                                try:
                                    fresh_u = await refresh_url(ref_px)
                                    if fresh_u:
                                        url_state["dl_url"] = fresh_u
                                except Exception as ref_exc:
                                    last_stream_exc = ref_exc

                    if stall_attempts >= max_stall_attempts:
                        prog_delta = max(0, done - initial_done)
                        if saw_rl:
                            raise ProviderFailure(
                                "PROVIDER_RATE_LIMITED",
                                f"MEGA bandwidth quota exceeded (HTTP 509) for {dest.name}",
                                progress_bytes=prog_delta,
                                done_bytes=done,
                            )
                        raise ProviderFailure(
                            "DOWNLOAD_FAILED",
                            f"MEGA file stream failed for {dest.name}: {last_stream_exc or 'stream interrupted'}",
                            progress_bytes=prog_delta,
                            done_bytes=done,
                        )
            except BaseException as outer_exc:
                if _is_cancel_exc(outer_exc):
                    _cleanup_partial_files()
                raise
            finally:
                self._release_mega_circuit(worker_px, worker_cli)

            if not part_path.exists():
                part_path.write_bytes(b"")
            ckpt_path.unlink(missing_ok=True)
            ckpt_tmp_path.unlink(missing_ok=True)
            part_path.replace(dest)
            return dest

        # Multi-Circuit Parallel Chunked AES-128-CTR Engine (for all sized files)
        # Chunk boundaries are strict multiples of 16 bytes so each chunk decrypts & resumes independently.
        if expected_size <= 2 * 1024 * 1024:
            chunk_size = expected_size
        elif expected_size < 64 * 1024 * 1024:
            chunk_size = 2 * 1024 * 1024  # 2 MB (131,072 AES blocks)
        else:
            chunk_size = 4 * 1024 * 1024  # 4 MB (262,144 AES blocks)

        total_chunks = (expected_size + chunk_size - 1) // chunk_size
        completed_chunks: set[int] = set()
        partial_offsets: dict[int, int] = {}
        loaded_valid_ckpt = False

        if part_path.is_file() and ckpt_path.is_file():
            try:
                if part_path.stat().st_size == expected_size:
                    raw_ckpt = json.loads(ckpt_path.read_text(encoding="utf-8"))
                    if (
                        isinstance(raw_ckpt, dict)
                        and raw_ckpt.get("v") == 1
                        and raw_ckpt.get("key_fp") == key_fp
                        and int(raw_ckpt.get("expected_size") or 0) == expected_size
                        and int(raw_ckpt.get("chunk_size") or 0) == chunk_size
                    ):
                        completed_chunks = self._unpack_int_ranges(raw_ckpt.get("completed") or [], total_chunks)
                        raw_partial = raw_ckpt.get("partial") or {}
                        if isinstance(raw_partial, dict):
                            for k_idx, raw_off in raw_partial.items():
                                c_idx = int(k_idx)
                                if c_idx < 0 or c_idx >= total_chunks or c_idx in completed_chunks:
                                    continue
                                c_s = c_idx * chunk_size
                                c_e = min(expected_size - 1, c_s + chunk_size - 1)
                                aligned_off = c_s + ((max(c_s, int(raw_off)) - c_s) // 16) * 16
                                if aligned_off > c_e:
                                    completed_chunks.add(c_idx)
                                elif aligned_off > c_s:
                                    partial_offsets[c_idx] = aligned_off
                        loaded_valid_ckpt = True
            except Exception:
                completed_chunks = set()
                partial_offsets = {}
                loaded_valid_ckpt = False

        ckpt_lock = threading.Lock()
        last_ckpt_save = [0.0]

        def _compute_done_bytes() -> int:
            with ckpt_lock:
                total_done = 0
                for idx in completed_chunks:
                    c_s = idx * chunk_size
                    c_e = min(expected_size - 1, c_s + chunk_size - 1)
                    total_done += (c_e - c_s + 1)
                for idx, off in partial_offsets.items():
                    if idx not in completed_chunks:
                        c_s = idx * chunk_size
                        total_done += max(0, off - c_s)
                return min(expected_size, total_done)

        def _save_ckpt(*, force: bool = False) -> None:
            now_m = time.monotonic()
            with ckpt_lock:
                if not force and (now_m - last_ckpt_save[0]) < 0.8:
                    return
                last_ckpt_save[0] = now_m
                payload_str = json.dumps(
                    {
                        "v": 1,
                        "key_fp": key_fp,
                        "expected_size": expected_size,
                        "chunk_size": chunk_size,
                        "completed": self._pack_int_ranges(completed_chunks),
                        "partial": {str(k): v for k, v in sorted(partial_offsets.items()) if k not in completed_chunks},
                    },
                    separators=(",", ":"),
                )
                try:
                    ckpt_tmp_path.write_text(payload_str, encoding="utf-8")
                    ckpt_tmp_path.replace(ckpt_path)
                except Exception:
                    pass

        if not loaded_valid_ckpt:
            with part_path.open("wb") as f_alloc:
                f_alloc.truncate(expected_size)
            _save_ckpt(force=True)

        initial_done_bytes = _compute_done_bytes()
        if initial_done_bytes > 0:
            sync_fn = getattr(progress, "sync_resumed_file_bytes", None)
            if callable(sync_fn):
                sync_fn(dest.name, initial_done_bytes, expected_size, phase="download")
            if initial_done_bytes < expected_size:
                pct_done = (initial_done_bytes / expected_size) * 100.0
                done_mb = initial_done_bytes / (1024 * 1024)
                tot_mb = expected_size / (1024 * 1024)
                rem_mb = (expected_size - initial_done_bytes) / (1024 * 1024)
                progress.log(
                    f"[MEGA] Tiếp tục tải nối tiếp {dest.name}: đã giữ {done_mb:.1f}/{tot_mb:.1f} MB "
                    f"({pct_done:.1f}%), chỉ tải bù {rem_mb:.1f} MB còn thiếu..."
                )

        if len(completed_chunks) >= total_chunks:
            ckpt_path.unlink(missing_ok=True)
            ckpt_tmp_path.unlink(missing_ok=True)
            part_path.replace(dest)
            self._validate_downloaded_files([dest])
            return dest

        chunk_queue: asyncio.Queue[tuple[int, int, int]] = asyncio.Queue()
        pending_chunk_count = 0
        for c_idx in range(total_chunks):
            if c_idx in completed_chunks:
                continue
            c_start = c_idx * chunk_size
            c_end = min(expected_size - 1, c_start + chunk_size - 1)
            chunk_queue.put_nowait((c_idx, c_start, c_end))
            pending_chunk_count += 1

        write_lock = threading.Lock()
        has_pwrite = hasattr(os, "pwrite")
        fatal_errors: list[BaseException] = []
        circuit_pool = self._get_mega_circuit_pool()
        pool_sem = circuit_pool["sem"]
        tor_sem = circuit_pool["tor_sem"]
        worker_count = min(max(1, pending_chunk_count), 12)

        with part_path.open("r+b") as f_out:
            fd = f_out.fileno()

            def _write_at(data: bytes, offset: int) -> None:
                if has_pwrite:
                    os.pwrite(fd, data, offset)
                else:
                    with write_lock:
                        f_out.seek(offset)
                        f_out.write(data)

            def _record_partial(c_idx: int, c_start: int, c_end: int, curr_write_pos: int) -> None:
                if curr_write_pos > c_end:
                    with ckpt_lock:
                        completed_chunks.add(c_idx)
                        partial_offsets.pop(c_idx, None)
                    _save_ckpt(force=False)
                elif curr_write_pos > c_start:
                    safe_pos = c_start + ((curr_write_pos - c_start) // 16) * 16
                    if safe_pos > c_start:
                        with ckpt_lock:
                            if c_idx not in completed_chunks:
                                partial_offsets[c_idx] = safe_pos
                        _save_ckpt(force=False)

            async def _chunk_worker(worker_id: int) -> None:
                w_px: str | None = None
                w_cli: httpx.AsyncClient | None = None
                used_explicit_px = False
                try:
                    while not fatal_errors:
                        try:
                            c_idx, c_start, c_end = chunk_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            return

                        with ckpt_lock:
                            write_pos = partial_offsets.get(c_idx, c_start)
                        consecutive_403 = 0
                        consecutive_509 = 0
                        saw_rate_limit = False
                        last_exc: Exception | None = None
                        stall_attempts = 0
                        max_stall_attempts = 40
                        total_attempts = 0

                        while write_pos <= c_end and stall_attempts < max_stall_attempts:
                            progress.check_cancelled()
                            if fatal_errors:
                                _record_partial(c_idx, c_start, c_end, write_pos)
                                return

                            daemons: list[str] = []
                            use_tor = bool(proxy) or (time.time() < type(self)._mega_direct_cooldown_until)
                            if use_tor:
                                daemons = await self._ensure_mega_tor_pool(progress)
                                if not daemons:
                                    _record_partial(c_idx, c_start, c_end, write_pos)
                                    raise ProviderFailure("PROVIDER_RATE_LIMITED", f"MEGA bandwidth quota exceeded and Tor proxy unavailable for {dest.name}")
                                if w_cli is None:
                                    if proxy and worker_id == 0 and not used_explicit_px:
                                        used_explicit_px = True
                                        w_px = proxy
                                        ensure_socks_support()
                                        w_cli = track_client(
                                            httpx.AsyncClient(
                                                proxy=w_px,
                                                follow_redirects=True,
                                                timeout=httpx.Timeout(connect=12.0, read=18.0, write=15.0, pool=20.0),
                                                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
                                            )
                                        )
                                    else:
                                        w_px, w_cli = self._acquire_mega_circuit(daemons)
                                active_cli = w_cli
                            else:
                                if type(self)._mega_direct_was_throttled:
                                    now_probe = time.time()
                                    if (now_probe - type(self)._mega_direct_probe_ts) >= 30.0:
                                        type(self)._mega_direct_probe_ts = now_probe
                                        progress.log("[MEGA] Direct IP cooldown expired; probing direct bandwidth recovery...")
                                active_cli = shared_client(
                                    "mega_dl_direct",
                                    follow_redirects=True,
                                    timeout=httpx.Timeout(connect=20.0, read=45.0, write=30.0, pool=30.0),
                                    limits=httpx.Limits(max_connections=160, max_keepalive_connections=80),
                                )

                            used_px = w_px
                            curr_url = url_state["dl_url"]
                            if write_pos == 0 and c_end == expected_size - 1:
                                req_url = curr_url
                            else:
                                req_url = f"{curr_url.rstrip('/')}/{write_pos}-{c_end}"

                            rotate_circuit = False
                            need_refresh = False
                            bytes_attempt = 0
                            pos_before = write_pos
                            t_stream = time.monotonic()
                            total_attempts += 1

                            try:
                                async with contextlib.AsyncExitStack() as sem_stack:
                                    await sem_stack.enter_async_context(pool_sem)
                                    if use_tor:
                                        await sem_stack.enter_async_context(tor_sem)
                                    decryptor = _make_decryptor(file_iv, write_pos)
                                    rem = b""
                                    needed = c_end - write_pos + 1
                                    async with active_cli.stream("GET", req_url, headers=req_headers) as resp:
                                        if resp.status_code in (509, 429):
                                            saw_rate_limit = True
                                            if not use_tor:
                                                self._mark_mega_direct_quota_exceeded(progress)
                                                rotate_circuit = True
                                                saw_rate_limit = False
                                                await asyncio.sleep(0.5)
                                            else:
                                                rotate_circuit = True
                                        elif resp.status_code in (403, 404, 410):
                                            consecutive_403 += 1
                                            if not use_tor:
                                                # First 403/404/410 on direct IP is often an expired MEGA ticket URL;
                                                # refresh URL first before blaming direct IP bandwidth quota.
                                                if refresh_url is not None and consecutive_403 == 1:
                                                    need_refresh = True
                                                else:
                                                    self._mark_mega_direct_quota_exceeded(progress)
                                                    if consecutive_403 >= 2 and refresh_url is not None:
                                                        need_refresh = True
                                                        consecutive_403 = 0
                                            else:
                                                rotate_circuit = True
                                                if consecutive_403 >= 2 and refresh_url is not None:
                                                    need_refresh = True
                                                    consecutive_403 = 0
                                        else:
                                            resp.raise_for_status()
                                            consecutive_403 = 0
                                            saw_rate_limit = False
                                            if not use_tor and type(self)._mega_direct_was_throttled:
                                                type(self)._mega_direct_was_throttled = False
                                                progress.log("[MEGA] Direct IP bandwidth recovered; resuming direct high-speed download!")
                                            ctype = (resp.headers.get("content-type") or "").lower()
                                            cl_hdr = resp.headers.get("content-length") or ""
                                            if "text/html" in ctype and cl_hdr.isdigit() and int(cl_hdr) < needed:
                                                raise RuntimeError("Proxy returned unexpected HTML response instead of MEGA ciphertext")

                                            t_stream = time.monotonic()
                                            async for raw in resp.aiter_bytes(131072):
                                                progress.check_cancelled()
                                                if fatal_errors:
                                                    _record_partial(c_idx, c_start, c_end, write_pos)
                                                    return
                                                if not raw:
                                                    continue
                                                max_take = (c_end + 1) - (write_pos + len(rem))
                                                if max_take <= 0:
                                                    break
                                                if len(raw) > max_take:
                                                    raw = raw[:max_take]
                                                bytes_attempt += len(raw)
                                                buf = rem + raw
                                                if write_pos + len(buf) >= c_end + 1:
                                                    plain = decryptor.update(buf)
                                                    if hasattr(decryptor, "finalize"):
                                                        plain += decryptor.finalize()
                                                    rem = b""
                                                else:
                                                    aligned = (len(buf) // 16) * 16
                                                    if aligned > 0:
                                                        plain = decryptor.update(buf[:aligned])
                                                        rem = buf[aligned:]
                                                    else:
                                                        plain = b""
                                                        rem = buf
                                                if plain:
                                                    _write_at(plain, write_pos)
                                                    write_pos += len(plain)
                                                    progress.add_bytes(len(plain), expected_size, "download", dest.name)
                                                if write_pos > c_end:
                                                    break
                                                if use_tor and stall_attempts < 24:
                                                    elapsed_s = time.monotonic() - t_stream
                                                    is_endgame = chunk_queue.empty() and (c_end - write_pos) > 512 * 1024
                                                    grace_s = 6.0 if is_endgame else 8.0
                                                    min_bps = 96 * 1024 if is_endgame else 64 * 1024
                                                    if elapsed_s >= grace_s and (bytes_attempt / elapsed_s) < min_bps:
                                                        raise _SlowMegaCircuit()
                            except _SlowMegaCircuit:
                                rotate_circuit = True
                            except asyncio.CancelledError:
                                raise
                            except ProviderFailure:
                                _record_partial(c_idx, c_start, c_end, write_pos)
                                raise
                            except Exception as exc:
                                if _is_cancel_exc(exc):
                                    raise
                                last_exc = exc
                                if use_tor:
                                    rotate_circuit = True
                                else:
                                    await asyncio.sleep(0.4)

                            _record_partial(c_idx, c_start, c_end, write_pos)

                            if write_pos > pos_before:
                                stall_attempts = 0
                                consecutive_509 = 0
                            else:
                                stall_attempts += 1
                                if saw_rate_limit:
                                    consecutive_509 += 1
                                    if use_tor and consecutive_509 >= 2:
                                        await self._rotate_tor_newnym(progress, used_px)
                                    if refresh_url is not None and consecutive_509 >= 4 and consecutive_509 % 4 == 0:
                                        need_refresh = True

                            if rotate_circuit:
                                await self._discard_mega_circuit(w_cli)
                                w_px = None
                                w_cli = None
                                if saw_rate_limit:
                                    await asyncio.sleep(1.0 + random.uniform(0.3, 1.1))

                            if need_refresh and refresh_url is not None:
                                async with refresh_lock:
                                    if url_state["dl_url"] == curr_url:
                                        ref_px = w_px
                                        if ref_px is None and use_tor and daemons:
                                            ref_px = self._isolated_proxy(daemons[total_attempts % len(daemons)], uuid.uuid4().hex[:10])
                                        try:
                                            fresh_u = await refresh_url(ref_px)
                                            if fresh_u:
                                                url_state["dl_url"] = fresh_u
                                        except Exception as ref_exc:
                                            last_exc = ref_exc

                            if write_pos > c_end:
                                if use_tor and w_cli is not None and bytes_attempt >= 512 * 1024:
                                    rate = bytes_attempt / max(0.05, time.monotonic() - t_stream)
                                    if rate < 80 * 1024:
                                        await self._discard_mega_circuit(w_cli)
                                        w_px = None
                                        w_cli = None
                                break

                            if not rotate_circuit and write_pos <= c_end:
                                if use_tor:
                                    await self._discard_mega_circuit(w_cli)
                                    w_px = None
                                    w_cli = None
                                await asyncio.sleep(0.15)
                        else:
                            if saw_rate_limit:
                                raise ProviderFailure("PROVIDER_RATE_LIMITED", f"MEGA bandwidth quota exceeded (HTTP 509) for {dest.name}")
                            raise ProviderFailure(
                                "DOWNLOAD_FAILED",
                                f"MEGA file stream failed for {dest.name}: {last_exc or f'incomplete chunk at {write_pos}/{c_end + 1}'}",
                            )
                except BaseException as worker_err:
                    if not isinstance(worker_err, asyncio.CancelledError):
                        fatal_errors.append(worker_err)
                    raise
                finally:
                    if fatal_errors:
                        await self._discard_mega_circuit(w_cli)
                    else:
                        self._release_mega_circuit(w_px, w_cli)

            tasks = [asyncio.create_task(_chunk_worker(i)) for i in range(worker_count)]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            f_out.flush()

        cancel_err = next((r for r in [*results, *fatal_errors] if isinstance(r, BaseException) and _is_cancel_exc(r)), None)
        if cancel_err is not None or bool(getattr(progress, "cancel", False)):
            _cleanup_partial_files()
            if cancel_err is not None:
                raise cancel_err
            progress.check_cancelled()

        if fatal_errors:
            _save_ckpt(force=True)
            err = fatal_errors[0]
            if isinstance(err, ProviderFailure):
                curr_done = _compute_done_bytes()
                err.details = {
                    **(err.details or {}),
                    "progress_bytes": max(0, curr_done - initial_done_bytes),
                    "done_bytes": curr_done,
                    "total_bytes": expected_size,
                }
            if isinstance(err, BaseException):
                raise err

        ckpt_path.unlink(missing_ok=True)
        ckpt_tmp_path.unlink(missing_ok=True)
        part_path.replace(dest)
        self._validate_downloaded_files([dest])
        return dest

    async def _resolve_mega_api_proxy(self, proxy: str | None, progress: JobState | None = None) -> str | None:
        if proxy:
            return proxy
        if time.time() < type(self)._mega_direct_cooldown_until:
            daemons = await self._ensure_mega_tor_pool(progress)
            if daemons:
                return self._isolated_proxy(random.choice(daemons), uuid.uuid4().hex[:10]) or daemons[0]
        return None

    async def _download_mega_file(
        self,
        url: str,
        dest_dir: Path,
        name: str | None,
        progress: JobState,
        file_ref: dict[str, Any] | None = None,
        proxy: str | None = None,
    ) -> list[Path]:
        file_ref = file_ref or {}
        meta = file_ref.get("meta") or {}

        # Case 1: Pre-resolved folder child node with key/iv in metadata
        folder_id = str(meta.get("mega_folder_id") or "")
        node_id = str(meta.get("mega_node_id") or "")
        key_hex = str(meta.get("mega_key_hex") or "")
        iv_hex = str(meta.get("mega_iv_hex") or "")

        if folder_id and node_id and key_hex and iv_hex:
            api_px = await self._resolve_mega_api_proxy(proxy, progress)
            res = await self._mega_api_req([{"a": "g", "g": 1, "n": node_id}], params={"n": folder_id}, proxy=api_px)
            dl_url = str(res.get("g") or "")
            if not dl_url:
                raise ProviderFailure("DOWNLOAD_FAILED", f"MEGA did not return download URL for folder node {node_id}")
            file_sz = int(res.get("s") if res.get("s") is not None else (meta.get("size") or file_ref.get("size") or 0))
            out_name = safe_name(name or file_ref.get("name") or f"{node_id}.bin")
            dest = dest_dir / out_name
            if not (dest_dir / f".{out_name}.ckpt.part").is_file():
                progress.log(f"[MEGA] Downloading & decrypting node {out_name} ({file_sz} bytes)...")

            async def _refresh_folder_node_url(px_hint: str | None = None) -> str:
                eff_px = px_hint or await self._resolve_mega_api_proxy(proxy, progress)
                r_new = await self._mega_api_req([{"a": "g", "g": 1, "n": node_id}], params={"n": folder_id}, proxy=eff_px)
                return str(r_new.get("g") or "")

            downloaded_path = await self._download_mega_stream_decrypt(
                dl_url, dest, bytes.fromhex(key_hex), bytes.fromhex(iv_hex), file_sz, progress,
                proxy=proxy, refresh_url=_refresh_folder_node_url,
            )
            return [downloaded_path]

        # Case 2: Public file link (mega.nz/file/ID#KEY or mega.nz/folder/FID#FKEY/file/NID)
        parsed = self._parse_mega_url(url)
        if parsed["kind"] == "folder" and parsed.get("sub_id"):
            nodes = await self._list_mega_folder_nodes(url, proxy=proxy)
            if not nodes:
                raise ProviderFailure("SOURCE_FILE_NOT_FOUND", f"MEGA folder file node not found: {url}")
            target_node = nodes[0]
            return await self._download_mega_file(target_node["url"], dest_dir, name or target_node["name"], progress, file_ref=target_node, proxy=proxy)
        if parsed["kind"] == "folder":
            return await self._download_mega_folder(url, dest_dir, progress, proxy=proxy)

        file_id = parsed["id"]
        raw_key = self._mega_b64_decode(parsed["key"])
        k_a32 = self._mega_bytes_to_a32(raw_key)
        if len(k_a32) < 8:
            raise ProviderFailure("DOWNLOAD_FAILED", f"Invalid MEGA file decryption key length for {file_id}")
        file_key = self._mega_a32_to_bytes([k_a32[0] ^ k_a32[4], k_a32[1] ^ k_a32[5], k_a32[2] ^ k_a32[6], k_a32[3] ^ k_a32[7]])
        file_iv = self._mega_a32_to_bytes([k_a32[4], k_a32[5], 0, 0])

        api_px = await self._resolve_mega_api_proxy(proxy, progress)
        res = await self._mega_api_req([{"a": "g", "g": 1, "p": file_id}], proxy=api_px)
        dl_url = str(res.get("g") or "")
        if not dl_url:
            raise ProviderFailure("DOWNLOAD_FAILED", f"MEGA did not return download URL for file {file_id}")
        attrs = self._mega_decrypt_attr(str(res.get("at") or ""), k_a32)
        real_name = str(attrs.get("n") or "").strip()
        if not name or name in ("file", file_id) or name.startswith(("download_", "video_download_")):
            out_name = safe_name(real_name or name or f"{file_id}.bin")
        else:
            out_name = safe_name(name)
        file_sz = int(res.get("s") if res.get("s") is not None else (file_ref.get("size") or 0))
        dest = dest_dir / out_name
        if not (dest_dir / f".{out_name}.ckpt.part").is_file():
            progress.log(f"[MEGA] Downloading & decrypting {out_name} ({file_sz} bytes)...")

        async def _refresh_public_file_url(px_hint: str | None = None) -> str:
            eff_px = px_hint or await self._resolve_mega_api_proxy(proxy, progress)
            r_new = await self._mega_api_req([{"a": "g", "g": 1, "p": file_id}], proxy=eff_px)
            return str(r_new.get("g") or "")

        downloaded_path = await self._download_mega_stream_decrypt(
            dl_url, dest, file_key, file_iv, file_sz, progress,
            proxy=proxy, refresh_url=_refresh_public_file_url,
        )
        return [downloaded_path]

    async def _download_mega_folder(
        self,
        url: str,
        dest_dir: Path,
        progress: JobState,
        proxy: str | None = None,
    ) -> list[Path]:
        nodes = await self._list_mega_folder_nodes(url, proxy=proxy)
        if not nodes:
            raise ProviderFailure("SOURCE_FILE_NOT_FOUND", f"MEGA folder is empty or could not be decrypted: {url}")
        progress.log(f"[MEGA] Resolved {len(nodes)} file(s) inside MEGA folder. Starting download...")
        options = (progress.payload or {}).get("options", {}) if hasattr(progress, "payload") else {}
        conc = max(1, min(24, int(options.get("download_concurrency") or 4)))
        sem = asyncio.Semaphore(conc)
        results: list[Path] = []

        async def _dl_node(node: dict[str, Any]) -> list[Path]:
            async with sem:
                progress.check_cancelled()
                rel = str(node.get("_relative_path") or node.get("name") or "file")
                target_sub = dest_dir / Path(rel).parent
                target_sub.mkdir(parents=True, exist_ok=True)
                return await self._download_mega_file(node["url"], target_sub, Path(rel).name, progress, file_ref=node, proxy=proxy)

        for batch in await asyncio.gather(*(_dl_node(n) for n in nodes)):
            results.extend(batch)
        return results

    # -------------------------------------------------------------------------
    # Gofile.io Cloud Support (Guest Token + WT Signature + Folder Tree)
    # -------------------------------------------------------------------------

    _gofile_token_cache: dict[str, Any] = {}

    @classmethod
    async def _get_gofile_guest_session(cls, proxy: str | None = None) -> tuple[str, str, str]:
        now = time.time()
        cached = cls._gofile_token_cache
        if cached.get("token") and cached.get("wt") and now - float(cached.get("ts") or 0) < 1800:
            return str(cached["token"]), str(cached["wt"]), str(cached["ua"])

        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        lang = "en-US"
        client_kw: dict[str, Any] = {"timeout": 20.0, "follow_redirects": True, "headers": {"User-Agent": ua}}
        if proxy:
            if "socks" in str(proxy).lower():
                from ..utils.http import ensure_socks_support
                ensure_socks_support()
            client_kw["proxy"] = proxy

        async with httpx.AsyncClient(**client_kw) as client:
            r_acc = await client.post("https://api.gofile.io/accounts")
            r_acc.raise_for_status()
            acc_data = r_acc.json()
            token = str((acc_data.get("data") or {}).get("token") or "")
            if not token:
                raise ProviderFailure("DOWNLOAD_FAILED", "Gofile failed to mint guest account token")

            secret = "4fd6sg89d7s6"
            try:
                r_wt = await client.get("https://gofile.io/js/wt.obf.js")
                if r_wt.status_code == 200 and r_wt.text:
                    dec = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), r_wt.text)
                    idx = dec.find("function generateWT")
                    chunk = dec[idx : idx + 700] if idx >= 0 else dec
                    # Find static secret literal in generateWT dictionary (e.g. 'Qsvxv':'12af056dacea0b')
                    for m_s in re.finditer(r"[\x22\x27]([0-9a-zA-Z_-]{8,32})[\x22\x27]", chunk):
                        cand = m_s.group(1)
                        if cand not in ("function", "toString", "userAgent", "language") and not cand.startswith("0x"):
                            secret = cand
                            break
            except Exception:
                pass

            bucket = str(int(time.time() // 14400))
            wt = hashlib.sha256(f"{ua}::{lang}::{token}::{bucket}::{secret}".encode("utf-8")).hexdigest()
            cls._gofile_token_cache = {"token": token, "wt": wt, "ua": ua, "ts": now}
            return token, wt, ua

    async def _list_gofile_items(self, url: str, proxy: str | None = None, _rel_prefix: str = "") -> list[dict[str, Any]]:
        m = re.search(r"gofile\.io/d/([a-zA-Z0-9_-]+)", url)
        content_id = m.group(1) if m else url.rstrip("/").split("/")[-1]
        if not content_id:
            raise ProviderFailure("SOURCE_FILE_NOT_FOUND", f"Invalid Gofile URL ({url}). Click Download in browser, stop it, copy the store-*.gofile.io/download/web/... URL, then paste that link.")

        token, wt, ua = await self._get_gofile_guest_session(proxy=proxy)
        headers = {
            "User-Agent": ua,
            "Authorization": f"Bearer {token}",
            "X-Website-Token": wt,
            "X-BL": "en-US",
            "Referer": "https://gofile.io/",
            "Origin": "https://gofile.io",
        }
        client_kw: dict[str, Any] = {"timeout": 25.0, "follow_redirects": True}
        if proxy:
            if "socks" in str(proxy).lower():
                from ..utils.http import ensure_socks_support
                ensure_socks_support()
            client_kw["proxy"] = proxy

        async with httpx.AsyncClient(**client_kw) as client:
            api_url = f"https://api.gofile.io/contents/{content_id}?page=1&pageSize=1000&sortField=name&sortDirection=1"
            resp = await client.get(api_url, headers=headers)
            resp.raise_for_status()
            body = resp.json()

        if body.get("status") != "ok":
            raise ProviderFailure(
                "SOURCE_FILE_NOT_FOUND",
                f"Gofile content error ({body.get('status')}): Gofile page links require valid public share or store-*.gofile.io/download/web/... URL.",
            )

        data = body.get("data") or {}
        if data.get("canAccess") is False:
            raise ProviderFailure("SOURCE_FILE_NOT_FOUND", f"Gofile content {content_id} is private or password-protected. Click Download in browser, stop it, copy the store-*.gofile.io/download/web/... URL, then paste that link.")

        if data.get("type") == "file" and data.get("link"):
            fname = safe_name(str(data.get("name") or f"{content_id}.bin"))
            rel = f"{_rel_prefix}/{fname}" if _rel_prefix else fname
            return [{
                "id": str(data["link"]),
                "path": str(data["link"]),
                "url": str(data["link"]),
                "name": fname,
                "type": "file",
                "size": int(data.get("size") or 0),
                "provider": "links",
                "_relative_path": rel,
                "cookies": f"accountToken={token}",
                "headers": {"Cookie": f"accountToken={token}", "Referer": "https://gofile.io/", "User-Agent": ua},
                "meta": {
                    "provider": "links",
                    "headers": {"Cookie": f"accountToken={token}", "Referer": "https://gofile.io/", "User-Agent": ua},
                    "cookies": f"accountToken={token}",
                    "size": int(data.get("size") or 0),
                },
            }]

        children = data.get("children") or {}
        child_list = list(children.values()) if isinstance(children, dict) else (children if isinstance(children, list) else [])
        out: list[dict[str, Any]] = []
        for child in child_list:
            if not isinstance(child, dict):
                continue
            ctype = str(child.get("type") or "file").lower()
            cname = safe_name(str(child.get("name") or child.get("id") or "file"))
            crel = f"{_rel_prefix}/{cname}" if _rel_prefix else cname
            if ctype == "folder":
                cid = str(child.get("code") or child.get("id") or "")
                if cid:
                    sub = await self._list_gofile_items(f"https://gofile.io/d/{cid}", proxy=proxy, _rel_prefix=crel)
                    out.extend(sub)
            else:
                dlink = str(child.get("link") or "")
                if not dlink:
                    continue
                out.append({
                    "id": dlink,
                    "path": dlink,
                    "url": dlink,
                    "name": cname,
                    "type": "file",
                    "size": int(child.get("size") or 0),
                    "provider": "links",
                    "_relative_path": crel,
                    "cookies": f"accountToken={token}",
                    "headers": {"Cookie": f"accountToken={token}", "Referer": "https://gofile.io/", "User-Agent": ua},
                    "meta": {
                        "provider": "links",
                        "headers": {"Cookie": f"accountToken={token}", "Referer": "https://gofile.io/", "User-Agent": ua},
                        "cookies": f"accountToken={token}",
                        "size": int(child.get("size") or 0),
                    },
                })
        return out

    async def _download_gofile_page(self, url: str, dest_dir: Path, name: str | None, progress: JobState, proxy: str | None = None) -> list[Path]:
        progress.log(f"[Gofile] Resolving Gofile share link: {url}")
        try:
            items = await self._list_gofile_items(url, proxy=proxy)
        except ProviderFailure:
            raise
        except Exception as exc:
            raise ProviderFailure(
                "SOURCE_FILE_NOT_FOUND",
                f"Gofile page links could not be resolved ({exc}). Click Download in browser, stop it, copy the store-*.gofile.io/download/web/... URL, then paste that link.",
            ) from exc

        if not items:
            raise ProviderFailure(
                "SOURCE_FILE_NOT_FOUND",
                "Gofile page links are empty or not direct downloads. Click Download in browser, stop it, copy the store-*.gofile.io/download/web/... URL, then paste that link.",
            )

        results: list[Path] = []
        for idx, it in enumerate(items):
            rel = str(it.get("_relative_path") or it.get("name") or f"file_{idx + 1}")
            sub_dir = dest_dir / Path(rel).parent
            sub_dir.mkdir(parents=True, exist_ok=True)
            target_name = name if (len(items) == 1 and name and "." in name and not name.startswith("gofile_")) else Path(rel).name
            hdrs = it.get("headers") or {}
            cks = it.get("cookies") or ""
            try:
                dl = await self._download_aria2(it["url"], sub_dir, target_name, progress, headers=hdrs, proxy=proxy, cookies=cks)
            except Exception:
                dl = await self._download_http_stream(it["url"], sub_dir, target_name, progress, headers=hdrs, proxy=proxy, cookies=cks)
            results.extend(dl)
        return results

    # -------------------------------------------------------------------------
    # Pixeldrain Cloud Support (File /u/ID & Album List /l/ID)
    # -------------------------------------------------------------------------

    async def _list_pixeldrain_items(self, url: str, proxy: str | None = None) -> list[dict[str, Any]]:
        m_list = re.search(r"pixeldrain\.com/(?:l|api/list)/([a-zA-Z0-9_-]+)", url)
        if m_list:
            list_id = m_list.group(1)
            api_url = f"https://pixeldrain.com/api/list/{list_id}"
            client_kw: dict[str, Any] = {"timeout": 20.0, "follow_redirects": True}
            if proxy:
                if "socks" in str(proxy).lower():
                    from ..utils.http import ensure_socks_support
                    ensure_socks_support()
                client_kw["proxy"] = proxy
            async with httpx.AsyncClient(**client_kw) as client:
                resp = await client.get(api_url)
                resp.raise_for_status()
                data = resp.json()
            pd_title = str(data.get("title") or "").strip()
            if pd_title:
                titles = getattr(self, "_last_folder_titles", None)
                if not isinstance(titles, dict):
                    titles = {}
                    self._last_folder_titles = titles
                titles[url] = pd_title
            files = data.get("files") or []
            out: list[dict[str, Any]] = []
            for f in files:
                fid = str(f.get("id") or "")
                if not fid:
                    continue
                fname = safe_name(str(f.get("name") or fid))
                dl_url = f"https://pixeldrain.com/api/file/{fid}?download"
                out.append({
                    "id": dl_url,
                    "path": dl_url,
                    "url": dl_url,
                    "name": fname,
                    "type": "file",
                    "size": int(f.get("size") or 0),
                    "provider": "links",
                    "_relative_path": fname,
                    "meta": {"provider": "links", "size": int(f.get("size") or 0)},
                })
            return out

        m_file = re.search(r"pixeldrain\.com/(?:u|api/file)/([a-zA-Z0-9_-]+)", url)
        if not m_file:
            raise ProviderFailure("SOURCE_FILE_NOT_FOUND", f"Invalid Pixeldrain URL: {url}")
        fid = m_file.group(1)
        info_url = f"https://pixeldrain.com/api/file/{fid}/info"
        dl_url = f"https://pixeldrain.com/api/file/{fid}?download"
        fname = f"{fid}.bin"
        fsize = 0
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                r = await client.get(info_url)
                if r.status_code == 200:
                    info = r.json()
                    if info.get("name"):
                        fname = safe_name(str(info["name"]))
                    fsize = int(info.get("size") or 0)
        except Exception:
            pass
        return [{
            "id": dl_url,
            "path": dl_url,
            "url": dl_url,
            "name": fname,
            "type": "file",
            "size": fsize,
            "provider": "links",
            "_relative_path": fname,
            "meta": {"provider": "links", "size": fsize},
        }]

    async def _download_pixeldrain(self, url: str, dest_dir: Path, name: str | None, progress: JobState, proxy: str | None = None) -> list[Path]:
        if "?download" in url and name and "." in name and not name.startswith(("download_", "video_download_")):
            target_name = safe_name(name)
            try:
                return await self._download_aria2(url, dest_dir, target_name, progress, proxy=proxy)
            except Exception:
                return await self._download_http_stream(url, dest_dir, target_name, progress, proxy=proxy)
        items = await self._list_pixeldrain_items(url, proxy=proxy)
        if not items:
            raise ProviderFailure("SOURCE_FILE_NOT_FOUND", f"No files found on Pixeldrain link: {url}")
        results: list[Path] = []
        for it in items:
            target_name = it["name"]
            if len(items) == 1 and name and "." in name and not name.startswith(("download_", "video_download_")):
                target_name = safe_name(name)
            try:
                dl = await self._download_aria2(it["url"], dest_dir, target_name, progress, proxy=proxy)
            except Exception:
                dl = await self._download_http_stream(it["url"], dest_dir, target_name, progress, proxy=proxy)
            results.extend(dl)
        return results

    # -------------------------------------------------------------------------
    # MediaFire Folder Support + Dropbox + OneDrive
    # -------------------------------------------------------------------------

    async def _list_mediafire_folder_items(self, url: str, _rel_prefix: str = "") -> list[dict[str, Any]]:
        m = re.search(r"mediafire\.com/folder/([a-zA-Z0-9,_-]+)", url)
        if not m:
            return []
        folder_keys = [k.strip() for k in m.group(1).split(",") if k.strip()]
        out: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
            for fkey in folder_keys:
                # 1. List files in folder
                r_files = await client.get(
                    "https://www.mediafire.com/api/1.4/folder/get_content.php",
                    params={"r": "utga", "content_type": "files", "filter": "all", "order_by": "name", "order_direction": "asc", "chunk": "1", "version": "1.5", "folder_key": fkey, "response_format": "json"},
                )
                if r_files.status_code == 200:
                    f_json = (r_files.json().get("response") or {}).get("folder_content") or {}
                    for f_obj in f_json.get("files") or []:
                        qkey = f_obj.get("quickkey")
                        fname = safe_name(str(f_obj.get("filename") or qkey or "file"))
                        f_url = ((f_obj.get("links") or {}).get("normal_download")) or f"https://www.mediafire.com/file/{qkey}/{fname}/file"
                        rel = f"{_rel_prefix}/{fname}" if _rel_prefix else fname
                        out.append({
                            "id": f_url,
                            "path": f_url,
                            "url": f_url,
                            "name": fname,
                            "type": "file",
                            "size": int(f_obj.get("size") or 0),
                            "provider": "links",
                            "_relative_path": rel,
                            "meta": {"provider": "links", "size": int(f_obj.get("size") or 0)},
                        })
                # 2. List subfolders recursively
                r_dirs = await client.get(
                    "https://www.mediafire.com/api/1.4/folder/get_content.php",
                    params={"r": "utga", "content_type": "folders", "filter": "all", "order_by": "name", "order_direction": "asc", "chunk": "1", "version": "1.5", "folder_key": fkey, "response_format": "json"},
                )
                if r_dirs.status_code == 200:
                    d_json = (r_dirs.json().get("response") or {}).get("folder_content") or {}
                    for d_obj in d_json.get("folders") or []:
                        sub_key = d_obj.get("folderkey")
                        sub_name = safe_name(str(d_obj.get("name") or sub_key or "folder"))
                        if sub_key:
                            sub_rel = f"{_rel_prefix}/{sub_name}" if _rel_prefix else sub_name
                            sub_items = await self._list_mediafire_folder_items(f"https://www.mediafire.com/folder/{sub_key}", _rel_prefix=sub_rel)
                            out.extend(sub_items)
        return out

    async def _download_mediafire_folder(self, url: str, dest_dir: Path, progress: JobState) -> list[Path]:
        items = await self._list_mediafire_folder_items(url)
        if not items:
            raise ProviderFailure("SOURCE_FILE_NOT_FOUND", f"MediaFire folder is empty or unavailable: {url}")
        progress.log(f"[MediaFire] Found {len(items)} file(s) in MediaFire folder.")
        results: list[Path] = []
        for it in items:
            rel = str(it.get("_relative_path") or it["name"])
            sub_dir = dest_dir / Path(rel).parent
            sub_dir.mkdir(parents=True, exist_ok=True)
            dl = await self._download_mediafire(it["url"], sub_dir, Path(rel).name, progress)
            results.extend(dl)
        return results

    @staticmethod
    def _resolve_dropbox_direct_url(url: str) -> str:
        parsed = urlparse(url)
        q = parse_qs(parsed.query, keep_blank_values=True)
        q["dl"] = ["1"]
        new_query = urlencode(q, doseq=True)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))

    async def _download_dropbox(self, url: str, dest_dir: Path, name: str | None, progress: JobState, is_folder: bool = False, proxy: str | None = None) -> list[Path]:
        direct_url = self._resolve_dropbox_direct_url(url)
        if is_folder and (not name or not name.lower().endswith(".zip")):
            base_n = safe_name(name or Path(unquote(urlparse(url).path)).name or "dropbox_folder")
            if not base_n.lower().endswith(".zip"):
                base_n += ".zip"
            name = base_n
        elif not name or name.startswith(("download_", "video_download_")):
            path_name = unquote(Path(urlparse(url).path).name)
            if path_name:
                name = safe_name(path_name)
        progress.log(f"[Dropbox] Resolved direct download URL: {direct_url[:100]}")
        try:
            files = await self._download_aria2(direct_url, dest_dir, name, progress, proxy=proxy)
        except Exception:
            files = await self._download_http_stream(direct_url, dest_dir, name, progress, proxy=proxy)
        if is_folder and files and files[0].suffix.lower() == ".zip":
            try:
                import zipfile
                zpath = files[0]
                if zipfile.is_zipfile(zpath):
                    extract_dir = dest_dir / zpath.stem
                    extract_dir.mkdir(parents=True, exist_ok=True)
                    with zipfile.ZipFile(zpath, "r") as zf:
                        zf.extractall(extract_dir)
                    extracted = [p for p in sorted(extract_dir.rglob("*")) if p.is_file()]
                    if extracted:
                        zpath.unlink(missing_ok=True)
                        return extracted
            except Exception as exc:
                progress.log(f"[Dropbox] Notice extracting folder zip ({exc}); keeping zip file.")
        return files

    async def _download_onedrive(self, url: str, dest_dir: Path, name: str | None, progress: JobState, proxy: str | None = None) -> list[Path]:
        progress.log(f"[OneDrive] Resolving OneDrive/SharePoint link: {url[:100]}")
        direct_url = url
        if "1drv.ms" in url.lower() or "onedrive.live.com" in url.lower():
            b64 = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")
            share_token = f"u!{b64}"
            api_url = f"https://api.onedrive.com/v1.0/shares/{share_token}/root?expand=children"
            try:
                async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                    r = await client.get(api_url)
                    if r.status_code == 200:
                        data = r.json()
                        if "@content.downloadUrl" in data:
                            direct_url = str(data["@content.downloadUrl"])
                            if not name or name.startswith(("download_", "video_download_")):
                                name = safe_name(str(data.get("name") or name or "onedrive_file"))
                        elif "children" in data and isinstance(data["children"], list):
                            results: list[Path] = []
                            for child in data["children"]:
                                c_dl = child.get("@content.downloadUrl")
                                c_name = safe_name(str(child.get("name") or "file"))
                                if c_dl:
                                    dl = await self._download_aria2(str(c_dl), dest_dir, c_name, progress, proxy=proxy)
                                    results.extend(dl)
                            if results:
                                return results
            except Exception:
                pass
            if direct_url == url:
                direct_url = f"https://api.onedrive.com/v1.0/shares/{share_token}/root/content"
        elif "sharepoint.com" in url.lower():
            parsed = urlparse(url)
            q = parse_qs(parsed.query, keep_blank_values=True)
            q["download"] = ["1"]
            direct_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(q, doseq=True), parsed.fragment))

        try:
            return await self._download_aria2(direct_url, dest_dir, name, progress, proxy=proxy)
        except Exception:
            return await self._download_http_stream(direct_url, dest_dir, name, progress, proxy=proxy)

    async def _resolve_mediafire_url(self, url: str) -> str:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        text = resp.text
        patterns = (
            r'href=["\']([^"\']+)["\'][^>]*id=["\']downloadButton["\']',
            r'id=["\']downloadButton["\'][^>]*href=["\']([^"\']+)["\']',
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.I | re.S)
            if match:
                return html.unescape(match.group(1))
        raise ProviderFailure("DOWNLOAD_FAILED", "MediaFire download link not found")

    async def _download_mediafire(self, url: str, dest_dir: Path, name: str | None, progress: JobState) -> list[Path]:
        direct = await self._resolve_mediafire_url(url)
        if not name or name == "file":
            parsed_name = unquote(Path(urlparse(direct).path).name).replace("+", " ")
            name = safe_name(parsed_name) if parsed_name else None
        progress.log("Resolved MediaFire direct download URL")
        return await self._download_aria2(direct, dest_dir, name, progress)

    # -------------------------------------------------------------------------
    # Yandex Disk Cloud Support (Public Files /i/, Folders & Files /d/, Subpaths, Hash Links)
    # -------------------------------------------------------------------------

    _YANDEX_PUBLIC_RESOURCES_API = "https://cloud-api.yandex.net/v1/disk/public/resources"
    _YANDEX_PUBLIC_DOWNLOAD_API = "https://cloud-api.yandex.net/v1/disk/public/resources/download"

    @staticmethod
    def _is_yandex_disk_host(netloc: str) -> bool:
        host = str(netloc or "").lower().split(":", 1)[0]
        if host in ("yadi.sk", "www.yadi.sk"):
            return True
        return bool(re.search(r"(?:^|\.)disk\.(?:360\.)?yandex\.[a-z.]+$", host))

    @staticmethod
    def _parse_yandex_disk_url(url: str) -> dict[str, str]:
        raw = str(url or "").strip()
        if not raw:
            return {"kind": "", "public_key": "", "fallback_public_key": "", "path": "", "short_id": ""}
        parsed = urlparse(raw)
        netloc = parsed.netloc.lower().split(":", 1)[0]
        scheme = parsed.scheme or "https"
        api_host = re.sub(r"^(?:www\.)?disk\.360\.yandex\.", "disk.yandex.", netloc)
        if api_host == "www.yadi.sk":
            api_host = "yadi.sk"
        parts = [p for p in parsed.path.split("/") if p]
        qs = parse_qs(parsed.query)

        if parts and parts[0].lower() == "client":
            return {"kind": "private", "public_key": "", "fallback_public_key": "", "path": "", "short_id": ""}

        if len(parts) >= 2 and parts[0].lower() in ("d", "i"):
            kind = parts[0].lower()
            key = parts[1]
            public_key = f"{scheme}://{api_host}/{kind}/{key}"
            fallback_pk = f"https://disk.yandex.ru/{kind}/{key}"
            sub_path = ""
            if kind == "d" and len(parts) > 2:
                decoded_segs = [unquote(seg) for seg in parts[2:] if seg]
                if decoded_segs:
                    sub_path = "/" + "/".join(decoded_segs)
            elif qs.get("vb_path") or qs.get("path"):
                q_sub = str((qs.get("vb_path") or qs.get("path") or [""])[0]).strip()
                if q_sub and q_sub != "/":
                    sub_path = "/" + q_sub.lstrip("/")
            return {
                "kind": kind,
                "public_key": public_key,
                "fallback_public_key": fallback_pk if fallback_pk != public_key else "",
                "path": sub_path,
                "short_id": key,
            }

        if qs.get("hash"):
            raw_hash = str(qs["hash"][0]).strip()
            q_sub = str((qs.get("vb_path") or qs.get("path") or [""])[0]).strip()
            sub_path = ("/" + q_sub.lstrip("/")) if (q_sub and q_sub != "/") else ""
            short_id = hashlib.sha1(raw_hash.encode("utf-8", errors="ignore")).hexdigest()[:10]
            return {
                "kind": "hash",
                "public_key": raw_hash,
                "fallback_public_key": "",
                "path": sub_path,
                "short_id": short_id,
            }

        return {
            "kind": "unknown",
            "public_key": raw,
            "fallback_public_key": "",
            "path": "",
            "short_id": parts[-1] if parts else "yandex",
        }

    @staticmethod
    def _build_yandex_subitem_url(public_key: str, sub_path: str) -> str:
        clean_sub = ("/" + sub_path.strip("/")) if (sub_path and sub_path.strip("/")) else ""
        if public_key.startswith(("http://", "https://")) and "/d/" in public_key:
            if not clean_sub:
                return public_key
            encoded = "/".join(quote(seg, safe="") for seg in clean_sub.strip("/").split("/") if seg)
            return f"{public_key.rstrip('/')}/{encoded}"
        if clean_sub:
            return f"https://disk.yandex.ru/public?{urlencode({'hash': public_key, 'path': clean_sub})}"
        return f"https://disk.yandex.ru/public?{urlencode({'hash': public_key})}"

    @staticmethod
    def _normalize_yandex_child_path(child: dict[str, Any], parent_subpath: str = "", root_api_path: str = "") -> str:
        raw_p = str(child.get("path") or "").strip()
        cname = str(child.get("name") or "").strip("/")
        parent_clean = str(parent_subpath or "").rstrip("/")
        if raw_p.startswith("disk:"):
            root_clean = str(root_api_path or "").rstrip("/")
            if root_clean.startswith("disk:") and raw_p.startswith(root_clean + "/"):
                rel_tail = raw_p[len(root_clean):]
                return f"{parent_clean}{rel_tail}" if parent_clean else rel_tail
            return f"{parent_clean}/{cname}" if cname else (parent_clean or "/")
        if raw_p.startswith("/"):
            return raw_p
        if raw_p:
            return f"{parent_clean}/{raw_p.lstrip('/')}"
        return f"{parent_clean}/{cname}" if cname else (parent_clean or "/")

    async def _yandex_api_get(
        self,
        endpoint: str,
        params: dict[str, Any],
        *,
        fallback_public_key: str = "",
        progress: JobState | None = None,
        proxy: str | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        async def _req_once(q_params: dict[str, Any], active_px: str | None) -> tuple[int, dict[str, Any], str]:
            client_kw: dict[str, Any] = {
                "timeout": 25.0,
                "follow_redirects": True,
                "headers": {
                    "User-Agent": _GDRIVE_BROWSER_UA,
                    "Accept": "application/json",
                },
            }
            if active_px:
                if "socks" in str(active_px).lower():
                    from ..utils.http import ensure_socks_support
                    ensure_socks_support()
                client_kw["proxy"] = active_px
            async with httpx.AsyncClient(**client_kw) as client:
                resp = await client.get(endpoint, params=q_params)
                body_dict: dict[str, Any] = {}
                try:
                    parsed_json = resp.json()
                    if isinstance(parsed_json, dict):
                        body_dict = parsed_json
                except Exception:
                    pass
                return resp.status_code, body_dict, resp.text

        code, data, raw_txt = 0, {}, ""
        for attempt in range(2):
            try:
                code, data, raw_txt = await _req_once(params, proxy)
                if code == 200 and data:
                    return data, proxy
                if code not in (429, 500, 502, 503, 504):
                    break
            except Exception as exc:
                if attempt == 1 and proxy is not None:
                    raise ProviderFailure("DOWNLOAD_FAILED", f"Yandex Disk API request failed: {exc}") from exc
                raw_txt = str(exc)
            if attempt == 0:
                await asyncio.sleep(1.2)

        err_code = str(data.get("error") or "").strip()
        desc = str(data.get("description") or data.get("message") or raw_txt[:200] or f"HTTP {code}").strip()

        if code == 404 or err_code == "DiskNotFoundError":
            if fallback_public_key and fallback_public_key != params.get("public_key"):
                alt_params = {**params, "public_key": fallback_public_key}
                try:
                    f_code, f_data, f_txt = await _req_once(alt_params, proxy)
                    if f_code == 200 and f_data:
                        params["public_key"] = fallback_public_key
                        return f_data, proxy
                    code, data, raw_txt = f_code, f_data, f_txt
                    err_code = str(data.get("error") or err_code).strip()
                    desc = str(data.get("description") or data.get("message") or desc).strip()
                except Exception:
                    pass
            if code == 404 or err_code == "DiskNotFoundError":
                raise ProviderFailure("SOURCE_FILE_NOT_FOUND", f"Yandex Disk resource not found or link expired: {desc}")

        if err_code == "DiskResourceDownloadDisabledError":
            raise ProviderFailure("YANDEX_DOWNLOAD_DISABLED", f"Yandex Disk owner disabled downloading for this link: {desc}")

        is_rate_or_ip_block = (
            code in (0, 401, 403, 429, 451, 500, 502, 503, 504)
            or any(k in err_code.lower() for k in ("limit", "many", "quota", "throttle"))
        )
        if is_rate_or_ip_block and proxy is None:
            base_px = await self._ensure_tor_proxy(progress)
            if base_px:
                if progress:
                    progress.log(f"[Yandex] Direct API returned {err_code or code}; retrying via isolated Tor circuit...")
                for _ in range(2):
                    cand_px = self._isolated_proxy(base_px, uuid.uuid4().hex[:8])
                    if not cand_px:
                        continue
                    try:
                        p_code, p_data, p_txt = await _req_once(params, cand_px)
                        if p_code == 200 and p_data:
                            return p_data, cand_px
                        p_err = str(p_data.get("error") or "").strip()
                        p_desc = str(p_data.get("description") or p_data.get("message") or p_txt[:200]).strip()
                        if p_code == 404 or p_err == "DiskNotFoundError":
                            raise ProviderFailure("SOURCE_FILE_NOT_FOUND", f"Yandex Disk resource not found or link expired: {p_desc}")
                        if p_err == "DiskResourceDownloadDisabledError":
                            raise ProviderFailure("YANDEX_DOWNLOAD_DISABLED", f"Yandex Disk owner disabled downloading for this link: {p_desc}")
                        code, data, err_code, desc = p_code, p_data, p_err or err_code, p_desc or desc
                    except ProviderFailure:
                        raise
                    except Exception:
                        continue

        if code == 429 or any(k in err_code.lower() for k in ("limit", "many", "quota", "throttle")):
            raise ProviderFailure("PROVIDER_RATE_LIMITED", f"Yandex Disk download/API limit exceeded ({err_code or code}): {desc}")

        raise ProviderFailure("DOWNLOAD_FAILED", f"Yandex Disk API error ({err_code or f'HTTP {code}'}): {desc}")

    async def _resolve_yandex_download_url(
        self,
        public_key: str,
        sub_path: str = "",
        *,
        fallback_public_key: str = "",
        progress: JobState | None = None,
        proxy: str | None = None,
    ) -> tuple[str, str | None]:
        q: dict[str, Any] = {"public_key": public_key}
        if sub_path:
            q["path"] = sub_path
        data, active_px = await self._yandex_api_get(
            self._YANDEX_PUBLIC_DOWNLOAD_API,
            q,
            fallback_public_key=fallback_public_key,
            progress=progress,
            proxy=proxy,
        )
        href = str(data.get("href") or "").strip()
        if not href:
            raise ProviderFailure("DOWNLOAD_FAILED", "Yandex Disk did not return a direct download URL")
        return href, active_px

    async def _list_yandex_folder_items(
        self,
        url: str,
        *,
        progress: JobState | None = None,
        proxy: str | None = None,
        prefetched_root: dict[str, Any] | None = None,
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
        parsed = self._parse_yandex_disk_url(url)
        if parsed["kind"] == "private":
            raise ProviderFailure(
                "SOURCE_FILE_NOT_FOUND",
                f"Yandex Disk private account URL cannot be accessed directly ({url}). Click Share in Yandex Disk and paste the public disk.yandex.*/d/... or /i/... link.",
            )
        if not parsed["public_key"]:
            raise ProviderFailure("SOURCE_FILE_NOT_FOUND", f"Invalid Yandex Disk URL: {url}")

        page_limit = 1000
        q_params: dict[str, Any] = {
            "public_key": parsed["public_key"],
            "limit": page_limit,
            "offset": 0,
        }
        if parsed["path"]:
            q_params["path"] = parsed["path"]

        active_px = proxy
        if prefetched_root is not None:
            data = prefetched_root
        else:
            data, active_px = await self._yandex_api_get(
                self._YANDEX_PUBLIC_RESOURCES_API,
                q_params,
                fallback_public_key=parsed["fallback_public_key"],
                progress=progress,
                proxy=active_px,
            )

        eff_public_key = str(q_params.get("public_key") or parsed["public_key"])
        res_type = str(data.get("type") or "").lower()
        res_title = str(data.get("name") or "").strip()
        if res_type == "file":
            return res_title, [], data

        embedded = data.get("_embedded") if isinstance(data.get("_embedded"), dict) else {}
        raw_items: list[dict[str, Any]] = list(embedded.get("items") or [])
        total_count = int(embedded.get("total") or 0)
        eff_limit = max(1, int(embedded.get("limit") or page_limit))
        offset = len(raw_items)

        while raw_items and ((total_count > 0 and len(raw_items) < total_count) or (total_count == 0 and len(raw_items) >= eff_limit)):
            page_q: dict[str, Any] = {
                "public_key": eff_public_key,
                "limit": page_limit,
                "offset": offset,
            }
            if parsed["path"]:
                page_q["path"] = parsed["path"]
            page_data, active_px = await self._yandex_api_get(
                self._YANDEX_PUBLIC_RESOURCES_API,
                page_q,
                progress=progress,
                proxy=active_px,
            )
            page_emb = page_data.get("_embedded") if isinstance(page_data.get("_embedded"), dict) else {}
            batch = list(page_emb.get("items") or [])
            if not batch:
                break
            raw_items.extend(batch)
            offset += len(batch)
            if total_count == 0 and len(batch) < eff_limit:
                break

        root_api_path = str(data.get("path") or "")
        now_ts = time.time()
        items: list[dict[str, Any]] = []
        for child in raw_items:
            if not isinstance(child, dict):
                continue
            c_type = str(child.get("type") or "file").lower()
            raw_cname = str(child.get("name") or "").strip()
            if not raw_cname:
                continue
            child_api_path = self._normalize_yandex_child_path(child, parent_subpath=parsed["path"], root_api_path=root_api_path)
            sub_url = self._build_yandex_subitem_url(eff_public_key, child_api_path)
            if c_type == "dir":
                d_name = safe_name(raw_cname, is_dir=True)
                items.append({
                    "id": sub_url,
                    "path": sub_url,
                    "url": sub_url,
                    "name": d_name,
                    "raw_name": raw_cname,
                    "type": "folder",
                    "is_folder": True,
                    "size": 0,
                    "provider": "links",
                    "meta": {
                        "provider": "links",
                        "cloud_host": "Yandex Disk",
                        "type": "folder",
                        "yandex_public_key": eff_public_key,
                        "yandex_path": child_api_path,
                    },
                })
            else:
                f_name = safe_name(raw_cname)
                f_size = max(0, int(child.get("size") or 0))
                f_mime = str(child.get("mime_type") or "")
                f_direct = str(child.get("file") or "").strip()
                items.append({
                    "id": sub_url,
                    "path": sub_url,
                    "url": sub_url,
                    "name": f_name,
                    "raw_name": raw_cname,
                    "type": "file",
                    "is_folder": False,
                    "mimeType": f_mime,
                    "size": f_size,
                    "provider": "links",
                    "meta": {
                        "provider": "links",
                        "cloud_host": "Yandex Disk",
                        "type": "file",
                        "size": f_size,
                        "mimeType": f_mime,
                        "yandex_public_key": eff_public_key,
                        "yandex_path": child_api_path,
                        "yandex_direct_url": f_direct,
                        "yandex_direct_ts": now_ts if f_direct else 0.0,
                    },
                })

        if res_title:
            titles = getattr(self, "_last_folder_titles", None)
            if not isinstance(titles, dict):
                titles = {}
                self._last_folder_titles = titles
            titles[url] = res_title

        return res_title, items, data

    async def _download_yandex_folder(
        self,
        url: str,
        dest_dir: Path,
        progress: JobState,
        *,
        proxy: str | None = None,
        prefetched_root: dict[str, Any] | None = None,
    ) -> list[Path]:
        folder_title, items, raw_meta = await self._list_yandex_folder_items(
            url, progress=progress, proxy=proxy, prefetched_root=prefetched_root,
        )
        if raw_meta and str(raw_meta.get("type") or "").lower() == "file":
            return await self._download_yandex_disk(
                url, dest_dir, str(raw_meta.get("name") or ""), progress, proxy=proxy, _prefetched_meta=raw_meta,
            )
        if not items:
            raise ProviderFailure("SOURCE_FILE_NOT_FOUND", f"Yandex Disk folder is empty or unavailable: {url}")

        progress.log(f"[Yandex] Found {len(items)} item(s) in Yandex Disk folder '{folder_title or url[:60]}'.")
        options = (progress.payload or {}).get("options", {}) if hasattr(progress, "payload") else {}
        conc = max(1, min(16, int(options.get("download_concurrency") or 4)))
        sem = asyncio.Semaphore(conc)
        results: list[Path] = []

        async def _dl_entry(entry: dict[str, Any]) -> list[Path]:
            progress.check_cancelled()
            if entry.get("type") == "folder" or entry.get("is_folder"):
                sub_dir = dest_dir / safe_name(str(entry.get("name") or "folder"), is_dir=True)
                sub_dir.mkdir(parents=True, exist_ok=True)
                return await self._download_yandex_folder(entry["url"], sub_dir, progress, proxy=proxy)
            async with sem:
                progress.check_cancelled()
                return await self._download_yandex_disk(
                    entry["url"], dest_dir, entry.get("name"), progress, file_ref=entry, proxy=proxy,
                )

        for batch in await asyncio.gather(*(_dl_entry(it) for it in items)):
            results.extend(batch)
        return results

    async def _download_yandex_disk(
        self,
        url: str,
        dest_dir: Path,
        name: str | None,
        progress: JobState,
        file_ref: dict[str, Any] | None = None,
        proxy: str | None = None,
        _prefetched_meta: dict[str, Any] | None = None,
    ) -> list[Path]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        parsed = self._parse_yandex_disk_url(url)
        if parsed["kind"] == "private":
            raise ProviderFailure(
                "SOURCE_FILE_NOT_FOUND",
                f"Yandex Disk private account URL cannot be accessed directly ({url}). Click Share in Yandex Disk and paste the public disk.yandex.*/d/... or /i/... link.",
            )

        meta = (file_ref or {}).get("meta") if isinstance((file_ref or {}).get("meta"), dict) else {}
        public_key = str((meta or {}).get("yandex_public_key") or parsed["public_key"]).strip()
        sub_path = str((meta or {}).get("yandex_path") if "yandex_path" in (meta or {}) else parsed["path"]).strip()
        fallback_pk = parsed["fallback_public_key"] if public_key == parsed["public_key"] else ""
        if not public_key:
            raise ProviderFailure("SOURCE_FILE_NOT_FOUND", f"Invalid Yandex Disk URL: {url}")

        active_px = proxy
        cached_direct = str((meta or {}).get("yandex_direct_url") or "").strip()
        cached_ts = float((meta or {}).get("yandex_direct_ts") or 0.0)
        use_cached_direct = bool(cached_direct and active_px is None and (time.time() - cached_ts) < 240.0)

        real_name = str((file_ref or {}).get("raw_name") or "").strip()
        direct_url = cached_direct if use_cached_direct else ""

        try:
            if not direct_url:
                res_meta = _prefetched_meta
                need_meta = res_meta is None and (not real_name and (not name or is_placeholder_link_name(name, url)) or (parsed["kind"] in ("d", "hash") and not (meta or {}).get("type")))
                if need_meta:
                    q_res: dict[str, Any] = {"public_key": public_key, "limit": 1000, "offset": 0}
                    if sub_path:
                        q_res["path"] = sub_path
                    res_meta, active_px = await self._yandex_api_get(
                        self._YANDEX_PUBLIC_RESOURCES_API,
                        q_res,
                        fallback_public_key=fallback_pk,
                        progress=progress,
                        proxy=active_px,
                    )
                    public_key = str(q_res.get("public_key") or public_key)

                if res_meta is not None:
                    if str(res_meta.get("type") or "").lower() == "dir":
                        return await self._download_yandex_folder(
                            url, dest_dir, progress, proxy=active_px, prefetched_root=res_meta,
                        )
                    real_name = str(res_meta.get("name") or real_name).strip()
                    if isinstance(file_ref, dict) and res_meta.get("size") and not file_ref.get("size"):
                        file_ref["size"] = int(res_meta["size"])
                    direct_url = str(res_meta.get("file") or "").strip()

                if not direct_url:
                    direct_url, active_px = await self._resolve_yandex_download_url(
                        public_key,
                        sub_path,
                        fallback_public_key=fallback_pk,
                        progress=progress,
                        proxy=active_px,
                    )
        except ProviderFailure as api_exc:
            if api_exc.code in ("YANDEX_DOWNLOAD_DISABLED", "PROVIDER_RATE_LIMITED") and not sub_path:
                progress.log(f"[Yandex] Direct download restricted ({api_exc.message}); attempting media stream fallback via yt-dlp...")
                try:
                    fallback_name = safe_name(name) if (name and not is_placeholder_link_name(name, url)) else (safe_name(real_name) if real_name else None)
                    return await self._download_ytdlp(url, dest_dir, fallback_name, progress, proxy=active_px)
                except Exception as ytdlp_exc:
                    progress.log(f"[Yandex] yt-dlp stream fallback unavailable: {ytdlp_exc}")
            if api_exc.code == "YANDEX_DOWNLOAD_DISABLED":
                raise ProviderFailure("SOURCE_FILE_NOT_FOUND", api_exc.message) from api_exc
            raise

        out_name: str | None = None
        if name and not is_placeholder_link_name(name, url):
            out_name = safe_name(name)
        elif real_name:
            out_name = safe_name(real_name)
        else:
            q_fn = (parse_qs(urlparse(direct_url).query).get("filename") or [""])[0]
            if q_fn:
                out_name = safe_name(unquote(q_fn))
            else:
                out_name = safe_name(f"yandex_{parsed['short_id'] or 'file'}.bin")

        progress.log(f"[Yandex] Downloading {out_name}...")
        try:
            return await self._download_aria2(direct_url, dest_dir, out_name, progress, proxy=active_px)
        except Exception as first_dl_exc:
            if use_cached_direct:
                progress.log(f"[Yandex] Cached download URL expired or rejected ({first_dl_exc}); minting fresh download URL...")
                direct_url, active_px = await self._resolve_yandex_download_url(
                    public_key,
                    sub_path,
                    fallback_public_key=fallback_pk,
                    progress=progress,
                    proxy=active_px,
                )
                try:
                    return await self._download_aria2(direct_url, dest_dir, out_name, progress, proxy=active_px)
                except Exception:
                    return await self._download_http_stream(direct_url, dest_dir, out_name, progress, proxy=active_px)
            return await self._download_http_stream(direct_url, dest_dir, out_name, progress, proxy=active_px)

    @staticmethod
    def _evp_bytes_to_key(password: bytes, salt: bytes, key_len: int = 32, iv_len: int = 16) -> tuple[bytes, bytes]:
        d = b""
        d_i = b""
        while len(d) < (key_len + iv_len):
            d_i = hashlib.md5(d_i + password + salt).digest()
            d += d_i
        return d[:key_len], d[key_len:key_len + iv_len]

    @classmethod
    def _decrypt_sorafolder_ciphertext(cls, encrypted_b64: str) -> str:
        raw = base64.b64decode(encrypted_b64)
        if not raw.startswith(b"Salted__"):
            raise ValueError("Invalid OpenSSL ciphertext header")
        salt = raw[8:16]
        ciphertext = raw[16:]
        password = b"Ak7qrvvH4WKYxV2OgaeHAEg2a5eh16vE"
        key, iv = cls._evp_bytes_to_key(password, salt, 32, 16)

        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.backends import default_backend
            cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
            decryptor = cipher.decryptor()
            padded = decryptor.update(ciphertext) + decryptor.finalize()
        except ImportError:
            try:
                from Crypto.Cipher import AES
                cipher = AES.new(key, AES.MODE_CBC, iv)
                padded = cipher.decrypt(ciphertext)
            except ImportError:
                raise ProviderFailure("DOWNLOAD_FAILED", "Neither cryptography nor pycryptodome is installed for SoraFolder decryption")

        pad_len = padded[-1]
        if pad_len < 1 or pad_len > 16:
            raise ValueError("Invalid PKCS7 padding")
        return padded[:-pad_len].decode("utf-8")

    async def _resolve_sorafolder_url(self, url: str) -> tuple[str, str | None, dict[str, str]]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            text = resp.text

            m_key = re.search(r'keyEncrypte\s*[:=]\s*[\x22\x27]([^\x22\x27]+)[\x22\x27]', text)
            m_name = re.search(r'fileName\s*[:=]\s*[\x22\x27]([^\x22\x27]+)[\x22\x27]', text)
            if not m_key:
                raise ProviderFailure("DOWNLOAD_FAILED", "Could not extract encryption key from SoraFolder page")

            key_enc = m_key.group(1)
            file_name = m_name.group(1) if m_name else None

            post_headers = {
                **headers,
                "Content-Type": "application/json",
                "Referer": url,
                "Origin": "https://sorafolder.com",
            }
            api_resp = await client.post("https://sorafolder.com/file-down", json={"keyEncrypte": key_enc}, headers=post_headers)
            api_resp.raise_for_status()
            data = api_resp.json()
            if not data or "url" not in data:
                raise ProviderFailure("DOWNLOAD_FAILED", "Invalid response from SoraFolder API")

            cdn_url = self._decrypt_sorafolder_ciphertext(data["url"])
            if not file_name:
                try:
                    token_part = urlparse(cdn_url).path.strip("/").split("/")[-1]
                    parts = token_part.split(".")
                    if len(parts) >= 2:
                        payload = parts[1]
                        pad = len(payload) % 4
                        if pad:
                            payload += "=" * (4 - pad)
                        jwt_data = json.loads(base64.urlsafe_b64decode(payload))
                        file_name = jwt_data.get("filename")
                except Exception:
                    pass

            download_headers = {
                "User-Agent": headers["User-Agent"],
                "Referer": "https://sorafolder.com/",
            }
            return cdn_url, file_name, download_headers

    async def _download_sorafolder(self, url: str, dest_dir: Path, name: str | None, progress: JobState) -> list[Path]:
        progress.log(f"Resolving SoraFolder URL: {url}")
        direct_url, resolved_name, headers = await self._resolve_sorafolder_url(url)
        if not name or name == "file" or name.startswith("download_") or "." not in name:
            name = safe_name(resolved_name) if resolved_name else name
        progress.log(f"Resolved SoraFolder direct URL for file: {name or 'unknown'}")
        try:
            try:
                if headers:
                    return await self._download_aria2(direct_url, dest_dir, name, progress, headers=headers)
                else:
                    return await self._download_aria2(direct_url, dest_dir, name, progress)
            except TypeError:
                return await self._download_aria2(direct_url, dest_dir, name, progress)
        except Exception as exc:
            progress.log(f"aria2c download failed ({exc}); re-resolving fresh token and retrying with HTTP stream")
            direct_url, resolved_name, headers = await self._resolve_sorafolder_url(url)
            return await self._download_http_stream(direct_url, dest_dir, name, progress, headers=headers)

    @staticmethod
    def _unpack_dean_edwards(packed_js: str) -> str:
        """Unpack Dean Edwards / p,a,c,k,e,d JavaScript used by video hosts like MixDrop/MxDrop/Filemoon/Streamwish."""
        m = re.search(r'}\s*\(\s*[\x27\x22](.*)[\x27\x22]\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*[\x27\x22](.*)[\x27\x22]\.split\([\x27\x22]\|[\x27\x22]\)', packed_js, re.DOTALL)
        if not m:
            return ""
        p, a, c, k = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).split('|')
        def unbase(val: str, base: int) -> int:
            digits = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'
            res = 0
            for char in val:
                res = res * base + digits.index(char)
            return res
        def repl(match: re.Match) -> str:
            word = match.group(0)
            try:
                idx = unbase(word, a)
                if idx < len(k) and k[idx]:
                    return k[idx]
            except Exception:
                pass
            return word
        return re.sub(r'\b[0-9a-zA-Z]+\b', repl, p)

    async def _resolve_mxdrop(self, url: str, file_ref: dict[str, Any], progress: JobState, proxy: str | None = None) -> tuple[str, str | None, dict[str, str]]:
        """Resolve a dynamic Mixdrop / Mxdrop URL directly on Colab so the security token binds to Colab's/Proxy's IP."""
        progress.log(f"Resolving fresh stream token on Colab for: {url[:80]}...")
        headers = file_ref.get("headers") or (file_ref.get("meta") or {}).get("headers") or {}
        referer = str(headers.get("Referer") or headers.get("referer") or "")

        file_id = ""
        m_id = re.search(r'([a-zA-Z0-9]{10,25})(?:\.mp4)?', urlparse(url).path)
        if m_id:
            file_id = m_id.group(1)
        if not file_id:
            raw_name = str(file_ref.get("name") or "")
            m_id = re.search(r'([a-zA-Z0-9]{10,25})', raw_name)
            if m_id:
                file_id = m_id.group(1)

        if not file_id:
            raise ProviderFailure("DOWNLOAD_FAILED", f"Could not determine Mixdrop file ID from URL {url}")

        embed_domain = "https://mxdrop.top"
        if "mixdrop" in referer.lower():
            m_dom = re.search(r'https?://[^/]+', referer)
            if m_dom:
                embed_domain = m_dom.group(0)
        elif "mxdrop" in url.lower():
            m_dom = re.search(r'https?://[^/]+', url)
            if m_dom:
                embed_domain = m_dom.group(0)

        embed_url = f"{embed_domain}/e/{file_id}"
        progress.log(f"Fetching embed player: {embed_url}")

        fetch_headers = {
            "User-Agent": str(headers.get("User-Agent") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
            "Referer": referer or "https://archivebate.com/",
        }

        _, page_html, _, active_proxy = await self._fetch_webpage(
            embed_url, fetch_headers, progress, proxy=proxy, auto_proxy=True,
        )

        unpacked = self._unpack_dean_edwards(page_html)
        m_wurl = re.search(r'MDCore\.wurl\s*=\s*[\x22\x27]([^\x22\x27]+)[\x22\x27]', unpacked)
        if not m_wurl:
            raise ProviderFailure("DOWNLOAD_FAILED", f"Could not extract MDCore.wurl from embed page on {embed_domain}")

        wurl = m_wurl.group(1)
        if wurl.startswith("//"):
            wurl = "https:" + wurl

        out_name = f"{file_id}.mp4"
        m_vfile = re.search(r'MDCore\.vfile\s*=\s*[\x22\x27]([^\x22\x27]+)[\x22\x27]', unpacked)
        if m_vfile and m_vfile.group(1):
            out_name = file_ref.get("name") or f"{file_id}.mp4"

        dl_headers: dict[str, str] = {
            "User-Agent": fetch_headers["User-Agent"],
            "Referer": f"{embed_domain}/",
            "Origin": embed_domain,
        }
        if active_proxy:
            dl_headers["_active_proxy"] = active_proxy
        progress.log(f"Successfully generated Colab-bound stream URL: {wurl[:80]}...")
        return wurl, out_name, dl_headers

    async def _download_mxdrop(self, url: str, dest_dir: Path, name: str | None, progress: JobState, file_ref: dict[str, Any], proxy: str | None = None) -> list[Path]:
        resolved_url = None
        dl_headers: dict[str, str] = {}
        final_name = name
        active_proxy = proxy
        try:
            res = await self._resolve_mxdrop(url, file_ref, progress, proxy=active_proxy)
            resolved_url, resolved_name, dl_headers = res[0], res[1], dict(res[2] or {})
            active_proxy = dl_headers.pop("_active_proxy", None) or active_proxy
            if not final_name:
                final_name = resolved_name
        except Exception as exc:
            if "mxcontent.net" in url.lower():
                raise ProviderFailure(
                    "DOWNLOAD_FAILED",
                    f"[IP-Guard] Stopped direct download of IP-bound MixDrop CDN URL because embed token resolution failed ({exc}).",
                )
            progress.log(f"Notice: Could not resolve embed token on Colab ({exc}), trying direct URL...")
            resolved_url = url
            dl_headers = dict(file_ref.get("headers") or (file_ref.get("meta") or {}).get("headers") or {})

        progress.log("Downloading MixDrop stream...")
        try:
            try:
                if active_proxy:
                    return await self._download_aria2(resolved_url, dest_dir, final_name, progress, headers=dl_headers, proxy=active_proxy)
                elif dl_headers:
                    return await self._download_aria2(resolved_url, dest_dir, final_name, progress, headers=dl_headers)
                else:
                    return await self._download_aria2(resolved_url, dest_dir, final_name, progress)
            except TypeError:
                return await self._download_aria2(resolved_url, dest_dir, final_name, progress)
        except Exception as exc:
            if isinstance(exc, ProviderFailure) and self._is_ip_bound_token_error(exc.message):
                raise
            progress.log(f"aria2c failed ({exc}); falling back to HTTP stream downloader")
            return await self._download_http_stream(resolved_url, dest_dir, final_name, progress, headers=dl_headers, proxy=active_proxy)

    async def _resolve_doodstream(
        self,
        url: str,
        file_ref: dict[str, Any],
        progress: JobState,
        proxy: str | None = None,
    ) -> tuple[str, str | None, dict[str, str]]:
        progress.log(f"[doodstream] Resolving fresh stream token on Colab for: {url[:80]}...")
        headers = dict(file_ref.get("headers") or (file_ref.get("meta") or {}).get("headers") or {})
        cookies = str(file_ref.get("cookies") or (file_ref.get("meta") or {}).get("cookies") or "").strip()
        page_url = str(file_ref.get("page_url") or (file_ref.get("meta") or {}).get("page_url") or "").strip()
        referer = str(headers.get("Referer") or headers.get("referer") or "").strip()

        dood_hosts = ("dood", "playmogo", "ds2play", "d000", "do0od", "dsvplay", "myvidplay", "doods", "dooood", "cloudatacdn")
        embed_url = ""
        url_low = url.lower()
        if not page_url and any(h in url_low for h in ("archivebate.com", "livecamsrip.com")):
            page_url = url
        if any(h in url_low for h in dood_hosts) and ("/e/" in url or "/d/" in url):
            embed_url = url
        elif page_url and any(h in page_url.lower() for h in dood_hosts) and ("/e/" in page_url or "/d/" in page_url):
            embed_url = page_url
        elif referer and any(h in referer.lower() for h in dood_hosts) and ("/e/" in referer or "/d/" in referer):
            embed_url = referer
        if "/d/" in embed_url and any(h in embed_url.lower() for h in dood_hosts):
            embed_url = embed_url.replace("/d/", "/e/", 1)

        req_headers = {
            "User-Agent": str(headers.get("User-Agent") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
            "Accept-Language": "en-US,en;q=0.9",
        }
        if cookies:
            req_headers["Cookie"] = cookies

        session_cookies: dict[str, str] = {}
        if cookies:
            for item in str(cookies).split(";"):
                if "=" in item:
                    ck, cv = item.strip().split("=", 1)
                    if ck.strip():
                        session_cookies[ck.strip()] = cv.strip()

        active_proxy = proxy
        if not embed_url and page_url and page_url.startswith(("http://", "https://")):
            progress.log(f"[doodstream] Fetching canonical page: {page_url[:80]}...")
            _, page_html, canonical_final_url, active_proxy = await self._fetch_webpage(
                page_url, req_headers, progress, proxy=active_proxy, auto_proxy=True, session_cookies=session_cookies,
            )
            # 1. Look for player iframe in HTML
            for m_iframe in re.finditer(r'<iframe[^>]+src=[\x22\x27]([^\x22\x27]+)[\x22\x27]', page_html, re.I):
                iframe_src = m_iframe.group(1).strip()
                if iframe_src.startswith("//"):
                    iframe_src = "https:" + iframe_src
                if iframe_src.startswith("http"):
                    embed_url = iframe_src
                    progress.log(f"[doodstream] Found player iframe: {embed_url}")
                    break

            # 2. Look for direct embed URL in HTML
            if not embed_url:
                m_direct = re.search(
                    r'https?://[^\x22\x27\s<>]*(?:' + '|'.join(dood_hosts) + r')[^\x22\x27\s<>]*/(?:e|d)/[a-zA-Z0-9_-]+',
                    page_html,
                    re.I,
                )
                if m_direct:
                    embed_url = m_direct.group(0).strip()
                    progress.log(f"[doodstream] Found direct embed URL in page HTML: {embed_url}")

            # 3. Livewire 3 component support (e.g. livecamsrip.com)
            if not embed_url and "wire:snapshot" in page_html:
                progress.log("[doodstream] Detected Livewire component; querying video stream...")
                csrf = ""
                m_csrf = re.search(r'<meta\s+name=[\x22\x27]csrf-token[\x22\x27]\s+content=[\x22\x27]([^\x22\x27]+)[\x22\x27]', page_html, re.I)
                if m_csrf:
                    csrf = m_csrf.group(1).strip()
                if not csrf:
                    csrf = session_cookies.get("XSRF-TOKEN", "")

                m_snap = re.search(r'wire:snapshot=[\x22\x27]([^\x22\x27]+)[\x22\x27]', page_html)
                if m_snap:
                    import html as html_lib
                    snap_raw = html_lib.unescape(m_snap.group(1))
                    lw_url = urljoin(canonical_final_url or page_url, "/livewire/update")
                    lw_payload = {
                        "_token": csrf,
                        "components": [
                            {
                                "snapshot": snap_raw,
                                "updates": {},
                                "calls": [{"path": "", "method": "updateView", "params": []}],
                            }
                        ]
                    }
                    lw_headers = dict(req_headers)
                    lw_headers["Accept"] = "application/json"
                    lw_headers["Content-Type"] = "application/json"
                    lw_headers["Referer"] = canonical_final_url or page_url
                    if csrf:
                        lw_headers["X-CSRF-TOKEN"] = csrf
                    lw_headers["X-Livewire"] = "true"

                    try:
                        _, lw_body, _, active_proxy = await self._fetch_webpage(
                            lw_url,
                            lw_headers,
                            progress,
                            proxy=active_proxy,
                            auto_proxy=True,
                            session_cookies=session_cookies,
                            post_data=json.dumps(lw_payload).encode("utf-8"),
                        )
                        # Look for video URL in Livewire JSON response
                        try:
                            lw_json = json.loads(lw_body)
                            for comp in lw_json.get("components", []):
                                snap_inner = comp.get("snapshot")
                                if isinstance(snap_inner, str):
                                    snap_obj = json.loads(snap_inner)
                                    vid = (snap_obj.get("data") or {}).get("video")
                                    if vid and isinstance(vid, str) and vid.startswith("http"):
                                        embed_url = vid
                                        progress.log(f"[doodstream] Resolved embed URL from Livewire snapshot: {embed_url}")
                                        break
                                html_inner = (comp.get("effects") or {}).get("html")
                                if html_inner:
                                    m_if = re.search(r'<iframe[^>]+src=[\x22\x27]([^\x22\x27]+)[\x22\x27]', html_inner, re.I)
                                    if m_if:
                                        embed_url = m_if.group(1).replace("\\/", "/")
                                        progress.log(f"[doodstream] Resolved embed iframe from Livewire effects: {embed_url}")
                                        break
                        except Exception:
                            pass

                        if not embed_url:
                            text_clean = lw_body.replace("\\/", "/").replace("\\\\", "\\")
                            m_lw_embed = re.search(
                                r'https?://[^\x22\x27\s<>]+/(?:e|d)/[a-zA-Z0-9_-]+',
                                text_clean,
                                re.I,
                            )
                            if m_lw_embed:
                                embed_url = m_lw_embed.group(0)
                                progress.log(f"[doodstream] Resolved embed URL from Livewire response regex: {embed_url}")
                    except Exception as lw_err:
                        progress.log(f"[doodstream] Livewire update failed ({lw_err})")

        if not embed_url:
            if "cloudatacdn.com" in url_low:
                raise ProviderFailure(
                    "DOWNLOAD_FAILED",
                    "Cannot resolve fresh DoodStream token: no canonical page_url or embed /e/ URL available for IP-bound cloudatacdn link.",
                )
            progress.log("[doodstream] Notice: No embed player found; using direct URL with embed referer")
            dl_headers = {
                "User-Agent": req_headers["User-Agent"],
                "Referer": referer or "https://playmogo.com/",
            }
            return url, file_ref.get("name"), dl_headers

        if "/d/" in embed_url and any(h in embed_url.lower() for h in dood_hosts):
            embed_url = embed_url.replace("/d/", "/e/", 1)

        embed_headers = dict(req_headers)
        if page_url:
            embed_headers["Referer"] = page_url
        progress.log(f"[doodstream] Fetching embed player: {embed_url}")
        _, embed_html, final_embed_url, active_proxy = await self._fetch_webpage(
            embed_url, embed_headers, progress, proxy=active_proxy, auto_proxy=True, session_cookies=session_cookies,
        )

        m_pass = re.search(r'/pass_md5/([^\x22\x27\s<>]+)', embed_html)
        if not m_pass:
            raise ProviderFailure("DOWNLOAD_FAILED", f"Could not find /pass_md5/ in DoodStream player HTML on {embed_url}")

        pass_path = m_pass.group(0)
        token = pass_path.rstrip("/").split("/")[-1]

        embed_parsed = urlparse(final_embed_url)
        embed_domain = f"{embed_parsed.scheme}://{embed_parsed.netloc}"
        pass_url = f"{embed_domain}{pass_path}"

        pass_headers = dict(req_headers)
        pass_headers["Referer"] = final_embed_url
        if session_cookies:
            pass_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in session_cookies.items())

        # Must use the exact same proxy circuit so the CDN binds the token to the same exit IP
        _, pass_body, _, active_proxy = await self._fetch_webpage(
            pass_url, pass_headers, progress, proxy=active_proxy, auto_proxy=False, session_cookies=session_cookies,
        )
        cdn_base = pass_body.strip()
        if not cdn_base.startswith("http"):
            raise ProviderFailure("DOWNLOAD_FAILED", f"Invalid pass_md5 response from {pass_url}: {cdn_base[:100]}")

        chars = string.ascii_letters + string.digits
        rand_str = ''.join(random.choices(chars, k=10))
        resolved_url = f"{cdn_base}{rand_str}?token={token}&expiry={int(time.time() * 1000)}"

        out_name = file_ref.get("name")
        if not out_name or out_name.startswith("video_download_"):
            m_title = re.search(r'<title>(.*?)\s*-\s*DoodStream</title>', embed_html, re.I)
            if m_title and m_title.group(1):
                clean_title = safe_name(m_title.group(1).strip())
                if clean_title:
                    out_name = f"{clean_title}.mp4"

        dl_headers = {
            "User-Agent": req_headers["User-Agent"],
            "Referer": f"{embed_domain}/",
        }
        if session_cookies:
            dl_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in session_cookies.items())
        if active_proxy:
            dl_headers["_active_proxy"] = active_proxy
        progress.log(f"[doodstream] Successfully generated stream URL: {resolved_url[:80]}...")
        return resolved_url, out_name, dl_headers

    async def _download_doodstream(
        self,
        url: str,
        dest_dir: Path,
        name: str | None,
        progress: JobState,
        file_ref: dict[str, Any],
        proxy: str | None = None,
    ) -> list[Path]:
        resolved_url = url
        dl_headers = dict(file_ref.get("headers") or (file_ref.get("meta") or {}).get("headers") or {})
        final_name = name
        active_proxy = proxy
        try:
            res = await self._resolve_doodstream(url, file_ref, progress, proxy=active_proxy)
            resolved_url, resolved_name, dl_headers = res[0], res[1], dict(res[2] or {})
            active_proxy = dl_headers.pop("_active_proxy", None) or active_proxy
            if not final_name or final_name.startswith("video_download_"):
                final_name = resolved_name or final_name
        except Exception as exc:
            # Fail-fast ("tự dừng"): never fall back to downloading a browser-captured cloudatacdn URL
            # because its token is bound to the user's home IP and will always return 14B "error_wrong_ip".
            if "cloudatacdn.com" in url.lower() or "/e/" in url.lower() or any(h in url.lower() for h in ("archivebate.com", "livecamsrip.com", "myvidplay.com")):
                raise ProviderFailure(
                    "DOWNLOAD_FAILED",
                    f"[IP-Guard] Stopped direct download of IP-bound DoodStream link because fresh token resolution failed ({exc}).",
                )
            progress.log(f"[doodstream] Notice: Resolution error ({exc}), trying direct URL...")
            if "referer" not in {k.lower() for k in dl_headers}:
                dl_headers["Referer"] = "https://playmogo.com/"

        progress.log("Downloading DoodStream media...")

        async def _attempt_dood_download(
            target_url: str,
            target_name: str | None,
            headers_to_use: dict[str, str],
            proxy_to_use: str | None,
        ) -> list[Path]:
            try:
                if proxy_to_use:
                    dl = await self._download_aria2(target_url, dest_dir, target_name, progress, headers=headers_to_use, proxy=proxy_to_use)
                else:
                    dl = await self._download_aria2(target_url, dest_dir, target_name, progress, headers=headers_to_use)
                self._validate_downloaded_files(dl)
                return dl
            except Exception as dl_exc:
                if self._is_ip_bound_token_error(str(dl_exc)):
                    raise
                progress.log(f"aria2c failed ({dl_exc}); falling back to HTTP stream downloader...")
                dl = await self._download_http_stream(target_url, dest_dir, target_name, progress, headers=headers_to_use, proxy=proxy_to_use)
                self._validate_downloaded_files(dl)
                return dl

        try:
            return await _attempt_dood_download(resolved_url, final_name, dl_headers, active_proxy)
        except Exception as exc:
            # If the resolved token reported error_wrong_ip, rotate to a fresh isolated Tor circuit and re-resolve once
            if self._is_ip_bound_token_error(str(exc)):
                base_tor = await self._ensure_tor_proxy(progress)
                if base_tor:
                    fresh_circuit = self._isolated_proxy(base_tor, uuid.uuid4().hex[:8])
                    progress.log("[IP-Guard] Token IP mismatch detected; rotating isolated proxy circuit and re-resolving token...")
                    res = await self._resolve_doodstream(url, file_ref, progress, proxy=fresh_circuit)
                    resolved_url, resolved_name, dl_headers = res[0], res[1], dict(res[2] or {})
                    active_proxy = dl_headers.pop("_active_proxy", None) or fresh_circuit
                    if resolved_name and (not final_name or final_name.startswith("video_download_")):
                        final_name = resolved_name
                    dl = await self._download_http_stream(resolved_url, dest_dir, final_name, progress, headers=dl_headers, proxy=active_proxy)
                    self._validate_downloaded_files(dl)
                    return dl
            raise

    async def _resolve_universal_page(
        self,
        url: str,
        file_ref: dict[str, Any],
        progress: JobState,
        proxy: str | None = None,
    ) -> tuple[str, str | None, dict[str, str]]:
        """Universal webpage/embed resolver for any video or file hosting website when direct CDN links are IP-blocked."""
        headers = dict(file_ref.get("headers") or (file_ref.get("meta") or {}).get("headers") or {})
        cookies = str(file_ref.get("cookies") or (file_ref.get("meta") or {}).get("cookies") or "").strip()
        page_url = str(file_ref.get("page_url") or (file_ref.get("meta") or {}).get("page_url") or "").strip()
        referer = str(headers.get("Referer") or headers.get("referer") or "").strip()

        target_page = page_url
        if not target_page and referer.startswith(("http://", "https://")) and referer.rstrip("/").count("/") >= 3:
            target_page = referer
        if not target_page:
            for cand in (url, file_ref.get("url"), file_ref.get("original_url"), (file_ref.get("meta") or {}).get("original_url")):
                cand_str = str(cand or "").strip()
                if cand_str.startswith(("http://", "https://")) and not any(ext in cand_str.lower() for ext in (".ts", ".m4s", ".key", ".m3u8", ".mpd", "/hls/")):
                    target_page = cand_str
                    break
        if not target_page:
            raise ProviderFailure("DOWNLOAD_FAILED", "No canonical page_url or embed Referer available for universal page resolution")

        req_headers = {
            "User-Agent": str(headers.get("User-Agent") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
            "Accept-Language": "en-US,en;q=0.9",
        }
        if cookies:
            req_headers["Cookie"] = cookies

        session_cookies: dict[str, str] = {}
        if cookies:
            for item in str(cookies).split(";"):
                if "=" in item:
                    ck, cv = item.strip().split("=", 1)
                    if ck.strip():
                        session_cookies[ck.strip()] = cv.strip()

        async def _do_web_fetch(fetch_url: str, fetch_hdrs: dict[str, str], fetch_px: str | None, fetch_ap: bool, post_data: bytes | str | None = None) -> tuple[int, str, str, str | None]:
            try:
                return await self._fetch_webpage(fetch_url, fetch_hdrs, progress, proxy=fetch_px, auto_proxy=fetch_ap, session_cookies=session_cookies, post_data=post_data)
            except TypeError:
                return await self._fetch_webpage(fetch_url, fetch_hdrs, progress, proxy=fetch_px, auto_proxy=fetch_ap)
        progress.log(f"[IP-Guard] Resolving fresh stream/file token from page: {target_page[:80]}...")
        _, page_html, final_page_url, active_proxy = await _do_web_fetch(
            target_page, req_headers, proxy, True,
        )
        if not page_html or not page_html.strip():
            raise ProviderFailure("DOWNLOAD_FAILED", f"Could not load webpage content from {target_page}")

        # Follow player iframe if the canonical page embeds an external video host
        active_html = page_html
        active_url = final_page_url
        found_iframe = False
        for m_iframe in re.finditer(r'<iframe[^>]+src=[\x22\x27]([^\x22\x27]+)[\x22\x27]', page_html, re.I):
            iframe_src = m_iframe.group(1).strip()
            if iframe_src.startswith("//"):
                iframe_src = "https:" + iframe_src
            if iframe_src.startswith(("http://", "https://")) and not any(
                ad in iframe_src.lower() for ad in (
                    "google", "doubleclick", "exoclick", "juicyads", "magsrv",
                    "recaptcha", "turnstile", "bunny", "b-cdn", "shield",
                    "challenge", "captcha", "adserver", "ads", "track", "analytics",
                    "cloudflare", "traffic", "popunder", "banner",
                )
            ):
                iframe_headers = dict(req_headers)
                iframe_headers["Referer"] = final_page_url
                if session_cookies:
                    iframe_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in session_cookies.items())
                progress.log(f"[IP-Guard] Following embedded player iframe: {iframe_src[:80]}...")
                try:
                    _, iframe_html, final_iframe_url, active_proxy = await _do_web_fetch(
                        iframe_src, iframe_headers, active_proxy, True,
                    )
                    if iframe_html.strip() and not self._is_ip_or_captcha_blocked(iframe_html, 200):
                        active_html = iframe_html
                        active_url = final_iframe_url
                        found_iframe = True
                        break
                except Exception as if_err:
                    progress.log(f"[IP-Guard] Embedded iframe fetch notice: {if_err}")

        # Livewire 3 lazy component resolution (e.g. livecamsrip.com)
        if not found_iframe and "wire:snapshot" in page_html:
            csrf = ""
            m_csrf = re.search(r'<meta\s+name=[\x22\x27]csrf-token[\x22\x27]\s+content=[\x22\x27]([^\x22\x27]+)[\x22\x27]', page_html, re.I)
            if m_csrf:
                csrf = m_csrf.group(1).strip()
            if not csrf:
                csrf = session_cookies.get("XSRF-TOKEN", "")

            m_snap = re.search(r'wire:snapshot=[\x22\x27]([^\x22\x27]+)[\x22\x27]', page_html)
            if m_snap:
                import html as html_lib
                snap_raw = html_lib.unescape(m_snap.group(1))
                lw_url = urljoin(final_page_url, "/livewire/update")
                lw_payload = {
                    "_token": csrf,
                    "components": [
                        {
                            "snapshot": snap_raw,
                            "updates": {},
                            "calls": [{"path": "", "method": "updateView", "params": []}],
                        }
                    ]
                }
                lw_headers = dict(req_headers)
                lw_headers["Accept"] = "application/json"
                lw_headers["Content-Type"] = "application/json"
                lw_headers["Referer"] = final_page_url
                if csrf:
                    lw_headers["X-CSRF-TOKEN"] = csrf
                lw_headers["X-Livewire"] = "true"

                try:
                    _, lw_body, _, active_proxy = await _do_web_fetch(
                        lw_url, lw_headers, active_proxy, True, post_data=json.dumps(lw_payload).encode("utf-8")
                    )
                    lw_embed = None
                    try:
                        lw_json = json.loads(lw_body)
                        for comp in lw_json.get("components", []):
                            snap_inner = comp.get("snapshot")
                            if isinstance(snap_inner, str):
                                snap_obj = json.loads(snap_inner)
                                vid = (snap_obj.get("data") or {}).get("video")
                                if vid and isinstance(vid, str) and vid.startswith("http"):
                                    lw_embed = vid
                                    break
                            html_inner = (comp.get("effects") or {}).get("html")
                            if html_inner:
                                m_if = re.search(r'<iframe[^>]+src=[\x22\x27]([^\x22\x27]+)[\x22\x27]', html_inner, re.I)
                                if m_if:
                                    lw_embed = m_if.group(1).replace("\\/", "/")
                                    break
                    except Exception:
                        pass

                    if not lw_embed:
                        text_clean = lw_body.replace("\\/", "/").replace("\\\\", "\\")
                        m_lw = re.search(r'https?://[^\x22\x27\s<>]+/(?:e|d)/[a-zA-Z0-9_-]+', text_clean, re.I)
                        if m_lw:
                            lw_embed = m_lw.group(0)

                    if lw_embed:
                        progress.log(f"[IP-Guard] Following Livewire player embed: {lw_embed[:80]}...")
                        embed_hdrs = dict(req_headers)
                        embed_hdrs["Referer"] = final_page_url
                        if session_cookies:
                            embed_hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in session_cookies.items())
                        _, emb_html, emb_url, active_proxy = await _do_web_fetch(
                            lw_embed, embed_hdrs, active_proxy, True
                        )
                        if emb_html.strip() and not self._is_ip_or_captcha_blocked(emb_html, 200):
                            active_html = emb_html
                            active_url = emb_url
                except Exception as lw_err:
                    progress.log(f"[IP-Guard] Livewire query failed ({lw_err})")

        parsed_active = urlparse(active_url)
        active_domain = f"{parsed_active.scheme}://{parsed_active.netloc}"
        parsed_target = urlparse(target_page)
        target_domain = f"{parsed_target.scheme}://{parsed_target.netloc}"
        unpacked = self._unpack_dean_edwards(active_html)
        combined_text = f"{unpacked}\n{page_html}\n{active_html}" if unpacked else f"{page_html}\n{active_html}"

        dl_headers: dict[str, str] = {
            "User-Agent": req_headers["User-Agent"],
            "Referer": f"{active_domain}/",
            "Origin": active_domain,
        }
        if session_cookies:
            dl_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in session_cookies.items())
        if active_proxy:
            dl_headers["_active_proxy"] = active_proxy

        # 1. Check /pass_md5/ pattern (DoodStream and all clone hosts)
        m_pass = re.search(r'/pass_md5/([^\x22\x27\s<>]+)', combined_text)
        if m_pass:
            pass_path = m_pass.group(0)
            token = pass_path.rstrip("/").split("/")[-1]
            pass_url = f"{active_domain}{pass_path}"
            pass_headers = dict(req_headers)
            pass_headers["Referer"] = active_url
            if session_cookies:
                pass_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in session_cookies.items())
            _, pass_body, _, active_proxy = await _do_web_fetch(
                pass_url, pass_headers, active_proxy, False,
            )
            cdn_base = pass_body.strip()
            if cdn_base.startswith("http"):
                chars = string.ascii_letters + string.digits
                rand_str = ''.join(random.choices(chars, k=10))
                resolved_url = f"{cdn_base}{rand_str}?token={token}&expiry={int(time.time() * 1000)}"
                if active_proxy:
                    dl_headers["_active_proxy"] = active_proxy
                if session_cookies:
                    dl_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in session_cookies.items())
                return resolved_url, file_ref.get("name"), dl_headers

        # 2. Check MDCore.wurl pattern (MixDrop / MxDrop clones)
        m_wurl = re.search(r'MDCore\.wurl\s*=\s*[\x22\x27]([^\x22\x27]+)[\x22\x27]', combined_text)
        if m_wurl:
            wurl = m_wurl.group(1).strip()
            if wurl.startswith("//"):
                wurl = "https:" + wurl
            if wurl.startswith("http"):
                return wurl, file_ref.get("name"), dl_headers

        def _score_stream_candidate(u: str) -> int:
            low = u.lower()
            if any(m in low for m in ("master.m3u8", "playlist.m3u8", "index.m3u8", "manifest.m3u8", "all.m3u8")):
                return 0
            if (".m3u8" in low or ".mpd" in low) and not any(sub in low for sub in ("video_only", "audio_only", "/audio/", "_audio")):
                return 1
            if any(ext in low for ext in (".mp4", ".mkv", ".webm")):
                return 2
            return 3

        clean_combined_text = html.unescape(combined_text.replace(r"\/", "/"))

        # 3. Check HLS (.m3u8) or direct media (.mp4/.mkv/.webm) in unpacked JS / player config
        media_patterns = (
            r'(?:file|src|source|hls|mp4)\s*[:=]\s*[\x22\x27](https?://[^\x22\x27\s<>]+?\.(?:m3u8|mpd|mp4|mkv|webm)[^\x22\x27\s<>]*)[\x22\x27]',
            r'[\x22\x27](https?://[^\x22\x27\s<>]+?\.(?:m3u8|mpd|mp4|mkv|webm)(?:\?[^\x22\x27\s<>]*)?)[\x22\x27]',
        )
        found_cands: list[str] = []
        for pat in media_patterns:
            for m_media in re.finditer(pat, clean_combined_text, re.I):
                candidate = m_media.group(1).strip()
                low_cand = candidate.lower()
                if any(skip in low_cand for skip in ("preview", "thumb", "sprite", ".vtt", ".srt", ".jpg", ".png")):
                    continue
                if candidate not in found_cands:
                    found_cands.append(candidate)

        if found_cands:
            found_cands.sort(key=_score_stream_candidate)
            best_cand = found_cands[0]
            progress.log(f"[IP-Guard] Extracted fresh media URL from page: {best_cand[:80]}...")
            if session_cookies:
                dl_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in session_cookies.items())
            return best_cand, file_ref.get("name"), dl_headers

        # 4. Check data-video-id and data-token / API pattern (e.g. StripTube and similar video CMS)
        m_vid = (
            re.search(r'data-video-id=[\x22\x27](\d+)[\x22\x27]', clean_combined_text)
            or re.search(r'["\'](?:video_id|videoId|vid)["\']\s*:\s*["\']?(\d+)', clean_combined_text)
            or re.search(r'/video/(\d+)', active_url)
            or re.search(r'/video/(\d+)', target_page)
        )
        tokens_found: list[str] = []
        for m_t in re.finditer(r'data-token=[\x22\x27]([^\x22\x27]+)[\x22\x27]', clean_combined_text):
            tok_cand = m_t.group(1).strip()
            if tok_cand and tok_cand not in tokens_found:
                tokens_found.append(tok_cand)
        for m_t in re.finditer(r'data-url=[\x22\x27][^\x22\x27]*?[?&]token=([^&\x22\x27\s]+)', clean_combined_text):
            tok_cand = m_t.group(1).strip()
            if tok_cand and tok_cand not in tokens_found:
                tokens_found.append(tok_cand)
        for m_t in re.finditer(r'["\'](?:token|stream_token|video_token)["\']\s*:\s*["\']([a-zA-Z0-9_\-\.]+)["\']', clean_combined_text):
            tok_cand = m_t.group(1).strip()
            if tok_cand and tok_cand not in tokens_found:
                tokens_found.append(tok_cand)
        for m_t in re.finditer(r'[?&]token=([a-zA-Z0-9_\-\.]{12,})', clean_combined_text):
            tok_cand = m_t.group(1).strip()
            if tok_cand and tok_cand not in tokens_found:
                tokens_found.append(tok_cand)

        if m_vid:
            vid_id = m_vid.group(1)
            api_base = target_domain if ("striptube" in target_domain or "/api/video" in page_html) else active_domain
            test_tokens = tokens_found if tokens_found else [""]

            for tok_attempt in test_tokens:
                api_url = f"{api_base}/api/video/{vid_id}" + (f"?token={tok_attempt}" if tok_attempt else "")
                api_headers = dict(req_headers)
                api_headers["Referer"] = target_page
                api_headers["Origin"] = api_base
                api_headers["X-Requested-With"] = "XMLHttpRequest"
                api_headers["Accept"] = "*/*"
                if session_cookies:
                    api_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in session_cookies.items())
                tok_hint = f"?token={tok_attempt[:16]}..." if tok_attempt else ""
                progress.log(f"[IP-Guard] Fetching fresh stream token from API: /api/video/{vid_id}{tok_hint}...")
                try:
                    _, api_body, _, active_proxy = await _do_web_fetch(
                        api_url, api_headers, active_proxy, False,
                    )
                    clean_api_body = html.unescape(api_body.replace(r"\/", "/"))
                    api_cands: list[str] = []

                    # Attempt A: Parse JSON and inspect all string values
                    try:
                        import json
                        api_json = json.loads(api_body)
                        def _collect_json_urls(val: Any) -> None:
                            if isinstance(val, str):
                                s = html.unescape(val.replace("\\/", "/").replace(r"\/", "/")).strip()
                                if s.startswith("http") and any(ext in s.lower() for ext in (".m3u8", ".mpd", ".mp4", ".mkv", "/hl/")):
                                    if "preview" not in s.lower() and s not in api_cands:
                                        api_cands.append(s)
                            elif isinstance(val, dict):
                                for v in val.values():
                                    _collect_json_urls(v)
                            elif isinstance(val, list):
                                for item in val:
                                    _collect_json_urls(item)
                        _collect_json_urls(api_json)
                    except Exception:
                        pass

                    # Attempt B: Regex on unescaped API body
                    for m_media in re.finditer(r'https?://[^\x22\x27\s<>]+\.(?:m3u8|mpd|mp4|mkv|webm)[^\x22\x27\s<>]*', clean_api_body):
                        cand = html.unescape(m_media.group(0).replace("\\/", "/")).strip()
                        if "preview" not in cand.lower() and cand not in api_cands:
                            api_cands.append(cand)

                    # Attempt C: URLs containing mediastrmx or /hl/ or index.m3u8
                    for m_media in re.finditer(r'https?://[^\x22\x27\s<>]+', clean_api_body):
                        cand = html.unescape(m_media.group(0).replace("\\/", "/")).strip()
                        if any(frag in cand.lower() for frag in ("/hl/", "index.m3u8", "master.m3u8")) and cand not in api_cands:
                            api_cands.append(cand)

                    if api_cands:
                        api_cands.sort(key=_score_stream_candidate)
                        best_cand = html.unescape(api_cands[0].replace("\\/", "/"))
                        progress.log(f"[IP-Guard] Extracted fresh stream URL from video API: {best_cand[:80]}...")
                        if active_proxy:
                            dl_headers["_active_proxy"] = active_proxy
                        if session_cookies:
                            dl_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in session_cookies.items())
                        return best_cand, file_ref.get("name"), dl_headers
                    else:
                        progress.log(f"[IP-Guard] Video API response returned no media stream: {api_body[:200]}")
                except Exception as api_err:
                    progress.log(f"[IP-Guard] Video API fetch notice: {api_err}")

        raise ProviderFailure("DOWNLOAD_FAILED", f"Could not extract fresh media stream from {active_url}")

    async def _download_universal_page(
        self,
        url: str,
        dest_dir: Path,
        name: str | None,
        progress: JobState,
        file_ref: dict[str, Any],
        proxy: str | None = None,
    ) -> list[Path]:
        res = await self._resolve_universal_page(url, file_ref, progress, proxy=proxy)
        resolved_url, resolved_name, dl_headers = res[0], res[1], dict(res[2] or {})
        active_proxy = dl_headers.pop("_active_proxy", None) or proxy
        final_name = name or resolved_name
        cookies = str(file_ref.get("cookies") or (file_ref.get("meta") or {}).get("cookies") or "").strip()
        if ".m3u8" in resolved_url.lower() or ".mpd" in resolved_url.lower():
            target_p = str(file_ref.get("page_url") or (file_ref.get("meta") or {}).get("page_url") or url).strip()
            downloaded = await self._download_stream_nm3u8dl(
                resolved_url, dest_dir, final_name, progress,
                headers=dl_headers, cookies=cookies, proxy=active_proxy,
                page_url=target_p, file_ref=file_ref,
            )
        else:
            downloaded = await self._download_aria2(
                resolved_url, dest_dir, final_name, progress,
                headers=dl_headers, proxy=active_proxy, cookies=cookies,
            )
        self._validate_downloaded_files(downloaded)
        return downloaded

    async def _ensure_tdl_binary(self, progress: JobState | None = None) -> str:
        """Locate or install the tdl CLI binary (Linux on Colab/Kaggle, .exe on Windows)."""
        from ..providers.base import _loop_store, dict_lock

        def _find_existing() -> str | None:
            for cand in (shutil.which("tdl"), "/usr/local/bin/tdl", str(Path.home() / ".local" / "bin" / "tdl")):
                if cand and Path(cand).is_file():
                    return str(cand)
            if os.name == "nt":
                for cand in (shutil.which("tdl.exe"), r"D:\My Application\Telegram Downloader\tdl.exe"):
                    if cand and Path(cand).is_file():
                        return str(cand)
            return None

        existing = _find_existing()
        if existing:
            return existing

        if not sys.platform.startswith("linux"):
            raise ProviderFailure("MISSING_DEPENDENCY", "tdl CLI binary not found on non-Linux host")

        lock = dict_lock(_loop_store("tdl_install_locks"), "tdl")
        async with lock:
            existing = _find_existing()
            if existing:
                return existing

            import platform
            import tarfile

            machine = platform.machine().lower()
            arch = "arm64" if machine in ("aarch64", "arm64") else "64bit"
            if progress:
                progress.log(f"[Telegram] Installing tdl Linux ({arch}) binary...")

            url = f"https://github.com/iyear/tdl/releases/latest/download/tdl_Linux_{arch}.tar.gz"
            with tempfile.TemporaryDirectory(prefix="vb_tdl_dl_") as tmpdir:
                tgz_path = Path(tmpdir) / "tdl.tar.gz"
                async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        api_resp = await client.get("https://api.github.com/repos/iyear/tdl/releases/latest")
                        api_resp.raise_for_status()
                        tag = api_resp.json().get("tag_name")
                        if not tag:
                            raise ProviderFailure("DOWNLOAD_FAILED", "Could not resolve latest tdl release tag")
                        resp = await client.get(f"https://github.com/iyear/tdl/releases/download/{tag}/tdl_Linux_{arch}.tar.gz")
                    resp.raise_for_status()
                    tgz_path.write_bytes(resp.content)

                with tarfile.open(tgz_path, "r:gz") as tf:
                    member = next((m for m in tf.getmembers() if m.isfile() and Path(m.name).name == "tdl"), None)
                    if not member:
                        raise ProviderFailure("DOWNLOAD_FAILED", "tdl binary not found inside release archive")
                    extracted = tf.extractfile(member)
                    if extracted is None:
                        raise ProviderFailure("DOWNLOAD_FAILED", "Failed to extract tdl binary")
                    bin_bytes = extracted.read()

            for target_dir in (Path("/usr/local/bin"), Path.home() / ".local" / "bin"):
                try:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    dest_bin = target_dir / "tdl"
                    dest_bin.write_bytes(bin_bytes)
                    os.chmod(dest_bin, 0o755)
                    if progress:
                        progress.log(f"[Telegram] Installed tdl binary to {dest_bin}")
                    return str(dest_bin)
                except PermissionError:
                    continue

            raise ProviderFailure("MISSING_DEPENDENCY", "No writable bin directory to install tdl binary")

    @staticmethod
    def _parse_telegram_url(url: str) -> tuple[str, Any, int]:
        """Parse t.me or tg:// message link into (kind, target, msg_id)."""
        raw = str(url or "").strip()
        parsed = urlparse(raw)
        if parsed.scheme.lower() == "tg":
            qs = parse_qs(parsed.query)
            if parsed.netloc.lower() == "privatepost":
                ch = (qs.get("channel") or [""])[0].strip()
                post = (qs.get("post") or [""])[0].strip()
                if ch.isdigit() and post.isdigit():
                    return ("channel_id", int(ch), int(post))
            elif parsed.netloc.lower() == "resolve":
                dom = (qs.get("domain") or [""])[0].strip().lstrip("@")
                post = (qs.get("post") or [""])[0].strip()
                if dom and post.isdigit():
                    return ("username", dom, int(post))
            raise ProviderFailure("DOWNLOAD_FAILED", f"Unsupported Telegram URL format: {url}")

        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 3 and parts[0].lower() == "c" and parts[1].isdigit():
            for seg in reversed(parts[2:]):
                if seg.isdigit():
                    return ("channel_id", int(parts[1]), int(seg))
        elif len(parts) >= 3 and parts[0].lower() == "s" and parts[2].isdigit():
            return ("username", parts[1].lstrip("@"), int(parts[2]))
        elif len(parts) >= 2 and parts[1].isdigit():
            return ("username", parts[0].lstrip("@"), int(parts[1]))
        raise ProviderFailure("DOWNLOAD_FAILED", f"Unsupported Telegram URL format: {url}")

    @staticmethod
    def _parse_tdl_gotd_session(kv_ns_data: dict[str, Any]) -> dict[str, Any]:
        """Decode tdl KV namespace (gotd session + app) into Telethon parameters in RAM."""
        if not isinstance(kv_ns_data, dict):
            raise ProviderFailure("AUTH_REQUIRED", "Invalid Telegram KV session payload.")

        def _b64_decode_bytes(val: Any) -> bytes:
            s = str(val or "").strip()
            if not s:
                return b""
            padded = s + ("=" * ((4 - len(s) % 4) % 4))
            return base64.b64decode(padded)

        app_raw = str(kv_ns_data.get("app") or "").strip()
        app_name = "desktop"
        if app_raw:
            if app_raw.lower() in ("desktop", "builtin") or ":" in app_raw:
                app_name = app_raw
            else:
                try:
                    decoded_app = _b64_decode_bytes(app_raw).decode("utf-8", errors="ignore").strip()
                    if decoded_app:
                        app_name = decoded_app
                except Exception:
                    app_name = "desktop"

        tdl_apps = {
            "builtin": (15055931, "021d433426cbb920eeb95164498fe3d3"),
            "desktop": (2040, "b18441a1ff607e10a989891a5462e627"),
        }
        if ":" in app_name:
            parts = app_name.split(":", 1)
            if parts[0].strip().isdigit() and parts[1].strip():
                app_id, app_hash = int(parts[0].strip()), parts[1].strip()
            else:
                app_id, app_hash = tdl_apps["desktop"]
        else:
            app_id, app_hash = tdl_apps.get(app_name.lower(), tdl_apps["desktop"])

        sess_raw = kv_ns_data.get("session")
        if not sess_raw:
            raise ProviderFailure("AUTH_REQUIRED", "Missing gotd session data in Telegram KV credentials.")
        try:
            sess_bytes = _b64_decode_bytes(sess_raw)
            sess_json = json.loads(sess_bytes.decode("utf-8"))
        except Exception as exc:
            raise ProviderFailure("AUTH_REQUIRED", f"Corrupted Telegram session payload: {exc}") from exc

        data = sess_json.get("Data") if isinstance(sess_json.get("Data"), dict) else sess_json
        if not isinstance(data, dict):
            raise ProviderFailure("AUTH_REQUIRED", "Missing Data object inside gotd session JSON.")

        try:
            dc_id = int(data.get("DC") or 0)
        except (TypeError, ValueError):
            dc_id = 0
        auth_key_bytes = _b64_decode_bytes(data.get("AuthKey"))
        if dc_id <= 0 or len(auth_key_bytes) != 256:
            raise ProviderFailure(
                "AUTH_REQUIRED",
                f"Invalid gotd session DC ({dc_id}) or AuthKey length ({len(auth_key_bytes)} bytes).",
            )

        dc_ip = ""
        dc_port = 443
        addr = str(data.get("Addr") or "").strip()
        if addr and ":" in addr and not addr.startswith("["):
            host, _, port_str = addr.rpartition(":")
            if host and port_str.isdigit():
                dc_ip = host.strip()
                dc_port = int(port_str)

        if not dc_ip:
            dc_opts = (data.get("Config") or {}).get("DCOptions") or []
            primary = [
                opt for opt in dc_opts
                if isinstance(opt, dict)
                and int(opt.get("ID") or 0) == dc_id
                and not opt.get("Ipv6")
                and not opt.get("MediaOnly")
                and not opt.get("CDN")
                and not opt.get("TcpoOnly")
                and str(opt.get("IPAddress") or "").strip()
            ]
            fallback = [
                opt for opt in dc_opts
                if isinstance(opt, dict)
                and int(opt.get("ID") or 0) == dc_id
                and not opt.get("Ipv6")
                and not opt.get("CDN")
                and str(opt.get("IPAddress") or "").strip()
            ]
            chosen = (primary or fallback or [None])[0]
            if chosen:
                dc_ip = str(chosen["IPAddress"]).strip()
                try:
                    dc_port = int(chosen.get("Port") or 443)
                except (TypeError, ValueError):
                    dc_port = 443

        if not dc_ip:
            prod_dc_ips = {
                1: "149.154.175.53",
                2: "149.154.167.51",
                3: "149.154.175.100",
                4: "149.154.167.91",
                5: "91.108.56.130",
            }
            dc_ip = prod_dc_ips.get(dc_id, "149.154.167.51")

        return {
            "app_name": app_name,
            "app_id": app_id,
            "app_hash": app_hash,
            "dc_id": dc_id,
            "dc_ip": dc_ip,
            "dc_port": dc_port,
            "auth_key": auth_key_bytes,
        }

    @staticmethod
    async def _verify_downloaded_media_integrity(
        file_path: Path,
        *,
        expected_size: int = 0,
        suffix_hint: str = "",
    ) -> None:
        """Verify exact byte completeness and non-corrupt video container via ffprobe."""
        if not file_path.is_file():
            raise ProviderFailure("DOWNLOAD_FAILED", f"Missing downloaded file: {file_path.name}")
        actual_size = file_path.stat().st_size
        if actual_size <= 0:
            raise ProviderFailure("DOWNLOAD_FAILED", f"Downloaded Telegram file {file_path.name} is empty (0 bytes)")
        if expected_size > 0 and actual_size != expected_size:
            raise ProviderFailure(
                "DOWNLOAD_FAILED",
                f"Telegram file {file_path.name} incomplete: got {actual_size} bytes, expected {expected_size} bytes",
            )

        ext = (suffix_hint or file_path.suffix or "").lower()
        video_exts = (".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".ts", ".m4v")
        if ext in video_exts and actual_size > 4096 and shutil.which("ffprobe"):
            def _probe() -> tuple[int, str, str]:
                res = subprocess.run(
                    [
                        "ffprobe",
                        "-v", "error",
                        "-show_entries", "format=duration",
                        "-show_entries", "stream=codec_type",
                        "-of", "json",
                        str(file_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=25,
                )
                return res.returncode, res.stdout, res.stderr

            try:
                rc, stdout_txt, stderr_txt = await asyncio.to_thread(_probe)
            except Exception:
                return
            if rc != 0:
                err_msg = (stderr_txt or "").strip()[:180]
                raise ProviderFailure(
                    "DOWNLOAD_FAILED",
                    f"Video integrity check failed for {file_path.name}: {err_msg or f'ffprobe rc={rc}'}",
                )
            try:
                probe_data = json.loads(stdout_txt or "{}")
            except Exception:
                probe_data = {}
            streams = probe_data.get("streams") or []
            if not streams:
                raise ProviderFailure(
                    "DOWNLOAD_FAILED",
                    f"Video integrity check failed for {file_path.name}: no valid media streams found",
                )

    async def _salvage_truncated_video(
        self,
        part_path: Path,
        dest_file: Path,
        total_size: int,
        progress: JobState,
        file_ref: dict[str, Any] | None = None,
    ) -> Path | None:
        """Attempt lossless ffmpeg remux when >= 95% of a video downloaded before a tail chunk error."""
        if total_size <= 0 or not part_path.is_file():
            return None
        actual_size = part_path.stat().st_size
        pct = (actual_size * 100.0) / total_size
        if actual_size < int(total_size * 0.95):
            return None

        ext = dest_file.suffix.lower()
        video_exts = (".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".ts", ".m4v")
        if ext not in video_exts or not shutil.which("ffmpeg"):
            return None

        salvaged_tmp = part_path.with_name(f".salvage_{uuid.uuid4().hex[:8]}{ext}")
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-err_detect", "ignore_err",
            "-i", str(part_path),
            "-map", "0:v?", "-map", "0:a?",
            "-c", "copy",
        ]
        if ext in (".mp4", ".mov", ".m4v"):
            cmd.extend(["-movflags", "+faststart"])
        cmd.append(str(salvaged_tmp))

        progress.log(
            f"[Telegram/Salvage] Đã tải {pct:.1f}% ({actual_size / 1048576:.1f}/{total_size / 1048576:.1f} MB) "
            f"trước lỗi cuối file — đang thử bỏ đoạn lỗi và cứu video bằng ffmpeg..."
        )
        try:
            res = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=180,
            )
            if res.returncode != 0 or not salvaged_tmp.is_file():
                salvaged_tmp.unlink(missing_ok=True)
                err_hint = (res.stderr or "").strip()[:140]
                if err_hint:
                    progress.log(f"[Telegram/Salvage] Không thể cứu video ({err_hint}).")
                return None

            salvaged_size = salvaged_tmp.stat().st_size
            if salvaged_size < max(4096, int(actual_size * 0.50)):
                salvaged_tmp.unlink(missing_ok=True)
                progress.log("[Telegram/Salvage] File sau remux quá nhỏ; hủy để tránh upload video lỗi.")
                return None

            await self._verify_downloaded_media_integrity(
                salvaged_tmp, expected_size=0, suffix_hint=ext
            )
            os.replace(salvaged_tmp, dest_file)
            part_path.unlink(missing_ok=True)
            if isinstance(file_ref, dict):
                file_ref["_salvaged"] = True
                file_ref["size"] = dest_file.stat().st_size
            dropped_kb = max(1, (total_size - actual_size) // 1024)
            progress.log(
                f"[Telegram/Salvage] Đã bỏ {dropped_kb} KB cuối bị lỗi server và cứu toàn vẹn "
                f"{dest_file.stat().st_size / 1048576:.1f} MB ({pct:.1f}%)!"
            )
            return dest_file
        except Exception as exc:
            salvaged_tmp.unlink(missing_ok=True)
            progress.log(f"[Telegram/Salvage] Bỏ qua cứu video ({str(exc)[:120]}).")
            return None

    async def _download_telegram_telethon(
        self,
        url: str,
        dest_dir: Path,
        name: str | None,
        progress: JobState,
        *,
        credentials: dict[str, Any] | None = None,
        file_ref: dict[str, Any] | None = None,
        fallback_reason: str = "",
        probe_hint_offset: int = 0,
        num_workers: int | None = None,
    ) -> list[Path]:
        """Download Telegram media via Telethon multi-connection ordered chunks with contiguous .part resume."""
        progress.check_cancelled()
        creds = credentials or (file_ref or {}).get("credentials") or {}
        kv_file = creds.get("kv_file") if isinstance(creds, dict) else None
        ns = str((creds.get("namespace") if isinstance(creds, dict) else "") or "colab").strip() or "colab"
        if not isinstance(kv_file, dict) or not kv_file.get(ns):
            raise ProviderFailure("AUTH_REQUIRED", "Missing Telegram KV session for Telethon downloader.")

        sess_info = self._parse_tdl_gotd_session(kv_file[ns])
        kind, target, msg_id = self._parse_telegram_url(url)

        await self._ensure_telethon(progress)
        from telethon import TelegramClient, errors as tl_errors
        from telethon.crypto import AuthKey
        from telethon.sessions import MemorySession
        from telethon.tl.types import PeerChannel

        opt = (progress.payload or {}).get("options", {}) if hasattr(progress, "payload") else {}
        source_items = ((progress.payload or {}).get("source") or {}).get("items") or [] if hasattr(progress, "payload") else []
        total_items = max(int(getattr(progress, "files_to_download", 0) or 0), len(source_items))
        dl_conc = 1 if opt.get("parallel_download") is False else max(1, int(opt.get("download_concurrency") or 4))

        try:
            expected_ref_size = int((file_ref or {}).get("size") or (file_ref or {}).get("file_size") or 0)
        except (TypeError, ValueError):
            expected_ref_size = 0

        if num_workers is None:
            try:
                req_t = int(opt.get("telegram_threads") or opt.get("download_threads") or opt.get("threads_per_item") or 0)
            except (TypeError, ValueError):
                req_t = 0
            if req_t == 1:
                num_workers = 1
            elif total_items >= 2 and dl_conc >= 2:
                num_workers = 2
            elif 0 < expected_ref_size <= 8 * 1024 * 1024:
                num_workers = 1
            else:
                num_workers = 4
        num_workers = max(1, min(4, int(num_workers)))

        if fallback_reason:
            progress.log(f"[Telegram] Switching to Telethon byte-resume engine ({fallback_reason[:120]})...")
        else:
            progress.log(f"[Telegram] Downloading via Telethon (ns={ns}, workers={num_workers}, ordered-part): {url}")

        mem_session = MemorySession()
        mem_session.set_dc(sess_info["dc_id"], sess_info["dc_ip"], sess_info["dc_port"])
        mem_session.auth_key = AuthKey(data=sess_info["auth_key"])

        client = TelegramClient(
            mem_session,
            sess_info["app_id"],
            sess_info["app_hash"],
            flood_sleep_threshold=0,
            use_ipv6=False,
            connection_retries=5,
            request_retries=5,
            retry_delay=3,
            receive_updates=False,
        )

        async def _sleep_cancellable(total_sec: float) -> None:
            end_at = time.monotonic() + max(0.0, float(total_sec))
            while True:
                progress.check_cancelled()
                rem = end_at - time.monotonic()
                if rem <= 0:
                    return
                await asyncio.sleep(min(0.5, rem))

        async def _with_flood_retry(coro_fn, op_label: str):
            for attempt in range(1, 7):
                progress.check_cancelled()
                try:
                    return await coro_fn()
                except tl_errors.FloodWaitError as fw:
                    wait_s = max(1, int(getattr(fw, "seconds", 5) or 5)) + 1
                    progress.log(f"[Telegram/Telethon] FloodWait {wait_s}s during {op_label}; waiting safely...")
                    await _sleep_cancellable(wait_s)
                except (tl_errors.AuthKeyUnregisteredError, tl_errors.SessionRevokedError, tl_errors.UnauthorizedError) as auth_exc:
                    raise ProviderFailure(
                        "AUTH_REQUIRED",
                        "Session Telegram đã hết hạn hoặc bị thu hồi. Vui lòng đăng nhập lại Session Colab và Export lại.",
                    ) from auth_exc
                except (ConnectionError, TimeoutError, asyncio.TimeoutError, OSError) as net_exc:
                    if attempt >= 6:
                        raise ProviderFailure("DOWNLOAD_FAILED", f"Telethon {op_label} failed: {net_exc}") from net_exc
                    await _sleep_cancellable(min(15.0, float(2 ** min(attempt, 4))))
            raise ProviderFailure("DOWNLOAD_FAILED", f"Telethon {op_label} failed after retries")

        dest_dir.mkdir(parents=True, exist_ok=True)
        part_path: Path | None = None

        import logging

        last_dc_issue = ""

        class _TelethonIssueCapture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                nonlocal last_dc_issue
                msg_txt = self.format(record)
                if "internal issues" in msg_txt or "Timeout" in msg_txt:
                    last_dc_issue = msg_txt[-220:]

        tl_logger = logging.getLogger("telethon")
        issue_handler = _TelethonIssueCapture()
        tl_logger.addHandler(issue_handler)

        server_err_types = tuple(
            cls for cls in (
                getattr(tl_errors, "TimeoutError", None),
                getattr(tl_errors, "TimedOutError", None),
                getattr(tl_errors, "ServerError", None),
                getattr(tl_errors, "RpcCallFailError", None),
                getattr(tl_errors, "RpcMcgetFailError", None),
            )
            if isinstance(cls, type)
        )

        try:
            await _with_flood_retry(lambda: client.connect(), "connect")
            is_auth = await _with_flood_retry(lambda: client.is_user_authorized(), "auth check")
            if not is_auth:
                raise ProviderFailure(
                    "AUTH_REQUIRED",
                    "Session Telegram không còn hiệu lực. Vui lòng đăng nhập lại Session Colab và Export lại.",
                )

            async def _resolve_entity():
                if kind == "channel_id":
                    try:
                        return await client.get_input_entity(PeerChannel(int(target)))
                    except Exception:
                        async for dialog in client.iter_dialogs(limit=None):
                            progress.check_cancelled()
                            ent = getattr(dialog, "entity", None)
                            if ent is not None and getattr(ent, "id", None) == int(target):
                                return await client.get_input_entity(ent)
                        raise ProviderFailure(
                            "DOWNLOAD_FAILED",
                            f"Cannot resolve Telegram private channel c/{target}. Ensure account is a member.",
                        )
                return await client.get_input_entity(str(target))

            entity = await _with_flood_retry(_resolve_entity, "entity resolve")
            msg = await _with_flood_retry(lambda: client.get_messages(entity, ids=msg_id), "get_messages")
            if isinstance(msg, list):
                msg = msg[0] if msg else None
            if not msg or not getattr(msg, "media", None):
                raise ProviderFailure("DOWNLOAD_FAILED", f"Telegram message {url} has no downloadable media")

            msg_file = getattr(msg, "file", None)
            total_size = int(getattr(msg_file, "size", 0) or expected_ref_size or 0)
            raw_fname = ""
            if name and Path(name).suffix:
                raw_fname = name
            elif msg_file and getattr(msg_file, "name", None):
                raw_fname = str(msg_file.name)
            else:
                ext_guess = str(getattr(msg_file, "ext", "") or "")
                raw_fname = f"tg_{target}_{msg_id}{ext_guess}"
            target_name = safe_name(raw_fname)
            dest_file = dest_dir / target_name
            if dest_file.exists():
                stem, ext = dest_file.stem, dest_file.suffix
                dest_file = dest_dir / f"{stem}_{msg_id}{ext}"

            safe_part_id = hashlib.sha1(f"{url}:{dest_file.name}".encode("utf-8", errors="ignore")).hexdigest()[:12]
            part_path = dest_dir / f".tg_part_{safe_part_id}.part"

            chunk_align = 4096
            request_size = 512 * 1024
            existing_size = part_path.stat().st_size if part_path.is_file() else 0
            if total_size > 0 and existing_size > total_size:
                existing_size = 0
            offset = (existing_size // chunk_align) * chunk_align

            if total_size > 0 and existing_size == total_size:
                await self._verify_downloaded_media_integrity(
                    part_path, expected_size=total_size, suffix_hint=dest_file.suffix
                )
                os.replace(part_path, dest_file)
                return [dest_file]

            mode = "r+b" if offset > 0 and part_path.is_file() else "wb"
            if offset > 0:
                progress.log(
                    f"[Telegram/Telethon] Resuming {dest_file.name} from {offset / 1048576:.1f} MB"
                    + (f" / {total_size / 1048576:.1f} MB" if total_size > 0 else "")
                )

            # Fast tail-probe before re-downloading hundreds of MBs if tdl died near the end
            if probe_hint_offset >= 1024 * 1024 and offset == 0 and (total_size <= 0 or probe_hint_offset < total_size):
                probe_off = (int(probe_hint_offset) // chunk_align) * chunk_align
                try:
                    async for _probe_chunk in client.iter_download(
                        msg.media, offset=probe_off, limit=64 * 1024, request_size=64 * 1024
                    ):
                        break
                except Exception as probe_exc:
                    err_low = str(probe_exc).lower()
                    is_server_timeout = (
                        (server_err_types and isinstance(probe_exc, server_err_types))
                        or ("request was unsuccessful" in err_low)
                    )
                    if is_server_timeout:
                        detail = last_dc_issue or str(probe_exc)
                        raise ProviderFailure(
                            "DOWNLOAD_FAILED",
                            f"Telegram server DC{sess_info['dc_id']} trả lỗi -503 Timeout tại cuối file "
                            f"(offset {probe_off}/{total_size}): {detail}. Chunk cuối trên Telegram đang không tải được.",
                        ) from probe_exc

            max_cycles = 16
            cycle = 0
            last_ui_ts = 0.0
            win_start_ts = time.monotonic()
            win_start_bytes = offset
            cur_request_size = request_size
            same_offset_fails = 0
            last_fail_offset = -1

            async def _run_parallel_ordered(fh_obj: Any, start_off: int) -> tuple[int, bool]:
                nonlocal last_ui_ts, win_start_ts, win_start_bytes
                if num_workers < 2 or total_size <= 0:
                    return start_off, False
                try:
                    from collections import deque
                    from telethon import functions as tl_functions, types as tl_types, utils as tl_utils
                except Exception:
                    return start_off, False

                try:
                    file_dc_id, input_loc = tl_utils.get_input_location(msg.media)
                except Exception:
                    return start_off, False
                if input_loc is None:
                    return start_off, False

                target_dc = int(file_dc_id or sess_info["dc_id"])
                exported_file_ref_cls = tuple(
                    c for c in (
                        getattr(tl_errors, "FileReferenceExpiredError", None),
                        getattr(tl_errors, "FilerefUpgradeNeededError", None),
                    )
                    if isinstance(c, type)
                )
                migrate_err_cls = getattr(tl_errors, "FileMigrateError", None)
                get_file_req_cls = tl_functions.upload.GetFileRequest
                cdn_redir_cls = getattr(tl_types.upload, "FileCdnRedirect", None)

                aux_clients: list[Any] = []
                aux_senders: list[Any] = []
                worker_callers: list[Any] = []
                loc_state = {"loc": input_loc, "dc": target_dc}

                try:
                    if target_dc == sess_info["dc_id"]:
                        async def _call_primary(req):
                            return await client(req)
                        worker_callers.append(_call_primary)
                    else:
                        if not hasattr(client, "_borrow_exported_sender") or not hasattr(client, "_call"):
                            return start_off, False
                        s0 = await client._borrow_exported_sender(target_dc)

                        async def _call_exported0(req, _s=s0):
                            return await client._call(_s, req)
                        worker_callers.append(_call_exported0)

                    for _w_idx in range(1, num_workers):
                        try:
                            if target_dc == sess_info["dc_id"]:
                                aux_mem = MemorySession()
                                aux_mem.set_dc(sess_info["dc_id"], sess_info["dc_ip"], sess_info["dc_port"])
                                aux_mem.auth_key = AuthKey(data=sess_info["auth_key"])
                                aux_c = TelegramClient(
                                    aux_mem,
                                    sess_info["app_id"],
                                    sess_info["app_hash"],
                                    flood_sleep_threshold=0,
                                    use_ipv6=False,
                                    connection_retries=5,
                                    request_retries=5,
                                    retry_delay=3,
                                    receive_updates=False,
                                )
                                await aux_c.connect()
                                aux_clients.append(aux_c)

                                async def _call_aux(req, _c=aux_c):
                                    return await _c(req)
                                worker_callers.append(_call_aux)
                            else:
                                if not hasattr(client, "_create_exported_sender"):
                                    break
                                s_i = await client._create_exported_sender(target_dc)
                                aux_senders.append(s_i)

                                async def _call_exp(req, _s=s_i):
                                    return await client._call(_s, req)
                                worker_callers.append(_call_exp)
                        except Exception:
                            # Keep going with surviving connections rather than aborting parallel transfer
                            continue

                    if len(worker_callers) < 2:
                        return start_off, False

                    max_window_chunks = max(16, len(worker_callers) * 8)
                    write_off = start_off
                    next_off = start_off
                    ready_chunks: dict[int, bytes] = {}
                    retry_q: deque[int] = deque()
                    chunk_fails: dict[int, int] = {}
                    window_evt = asyncio.Event()
                    ref_lock = asyncio.Lock()
                    p_state: dict[str, Any] = {
                        "pause_until": 0.0,
                        "last_flood_log": 0.0,
                        "in_flight": 0,
                        "stop": False,
                        "fallback_seq": False,
                        "fatal": None,
                    }

                    async def _refresh_loc() -> None:
                        async with ref_lock:
                            refetched = await _with_flood_retry(
                                lambda: client.get_messages(entity, ids=msg_id), "refresh file_reference"
                            )
                            if isinstance(refetched, list):
                                refetched = refetched[0] if refetched else None
                            if refetched and getattr(refetched, "media", None):
                                _, new_loc = tl_utils.get_input_location(refetched.media)
                                if new_loc is not None:
                                    loc_state["loc"] = new_loc

                    async def _fetch_chunk_64k(call_fn, base_off: int) -> bytes:
                        buf_parts: list[bytes] = []
                        sub_off = base_off
                        end_off = min(total_size, base_off + request_size)
                        while sub_off < end_off:
                            progress.check_cancelled()
                            req = get_file_req_cls(location=loc_state["loc"], offset=sub_off, limit=64 * 1024)
                            resp = await call_fn(req)
                            if cdn_redir_cls and isinstance(resp, cdn_redir_cls):
                                raise RuntimeError("__CDN_REDIRECT__")
                            chunk_b = getattr(resp, "bytes", b"") or b""
                            if not chunk_b:
                                break
                            buf_parts.append(chunk_b)
                            sub_off += len(chunk_b)
                            if len(chunk_b) < 64 * 1024:
                                break
                        return b"".join(buf_parts)

                    async def _worker_loop(call_fn) -> None:
                        nonlocal write_off, next_off, last_ui_ts, win_start_ts, win_start_bytes
                        while not p_state["stop"] and write_off < total_size:
                            progress.check_cancelled()
                            wait_rem = p_state["pause_until"] - time.monotonic()
                            if wait_rem > 0:
                                await _sleep_cancellable(wait_rem)
                                continue

                            claimed_off: int | None = None
                            if retry_q:
                                claimed_off = retry_q.popleft()
                            elif next_off < total_size:
                                if (len(ready_chunks) + p_state["in_flight"]) >= max_window_chunks:
                                    window_evt.clear()
                                    try:
                                        await asyncio.wait_for(window_evt.wait(), timeout=0.5)
                                    except asyncio.TimeoutError:
                                        pass
                                    continue
                                claimed_off = next_off
                                next_off += request_size
                            else:
                                # Tail chunks are in flight or retrying in another worker
                                window_evt.clear()
                                try:
                                    await asyncio.wait_for(window_evt.wait(), timeout=0.5)
                                except asyncio.TimeoutError:
                                    pass
                                continue

                            p_state["in_flight"] += 1
                            try:
                                use_small = chunk_fails.get(claimed_off, 0) >= 1
                                if use_small:
                                    data = await _fetch_chunk_64k(call_fn, claimed_off)
                                else:
                                    req = get_file_req_cls(
                                        location=loc_state["loc"],
                                        offset=claimed_off,
                                        limit=request_size,
                                    )
                                    resp = await call_fn(req)
                                    if cdn_redir_cls and isinstance(resp, cdn_redir_cls):
                                        p_state["fallback_seq"] = True
                                        p_state["stop"] = True
                                        window_evt.set()
                                        return
                                    data = getattr(resp, "bytes", b"") or b""

                                if not data and claimed_off < total_size:
                                    raise TimeoutError(f"Empty chunk returned at offset {claimed_off}")

                                ready_chunks[claimed_off] = data
                                while write_off in ready_chunks:
                                    blk = ready_chunks.pop(write_off)
                                    if blk:
                                        fh_obj.write(blk)
                                        write_off += len(blk)

                                now = time.monotonic()
                                if now - last_ui_ts >= 0.4 or write_off >= total_size:
                                    elapsed = max(0.05, now - win_start_ts)
                                    speed_bps = max(0.0, (write_off - win_start_bytes) / elapsed)
                                    if elapsed >= 5.0:
                                        win_start_ts = now
                                        win_start_bytes = write_off
                                    pct = (write_off * 100.0 / total_size) if total_size > 0 else 0.0
                                    if hasattr(progress, "set_phase_progress"):
                                        progress.set_phase_progress(
                                            done_bytes=write_off,
                                            total_bytes=total_size or getattr(progress, "bytes_total", 0),
                                            pct=min(100.0, pct),
                                            speed=speed_bps,
                                            phase="download",
                                        )
                                    last_ui_ts = now
                                if write_off >= total_size:
                                    p_state["stop"] = True
                                window_evt.set()
                            except Exception as w_exc:
                                err_str = str(w_exc)
                                if "__CDN_REDIRECT__" in err_str:
                                    p_state["fallback_seq"] = True
                                    p_state["stop"] = True
                                    window_evt.set()
                                    return
                                if isinstance(w_exc, tl_errors.FloodWaitError):
                                    wait_s = max(1, int(getattr(w_exc, "seconds", 5) or 5)) + 1
                                    now_t = time.monotonic()
                                    p_state["pause_until"] = max(p_state["pause_until"], now_t + wait_s)
                                    if now_t - p_state["last_flood_log"] > 5.0:
                                        p_state["last_flood_log"] = now_t
                                        progress.log(
                                            f"[Telegram/Telethon] FloodWait {wait_s}s at {write_off / 1048576:.1f}/{total_size / 1048576:.1f} MB "
                                            "— .part saved, resuming automatically..."
                                        )
                                    retry_q.appendleft(claimed_off)
                                    window_evt.set()
                                    continue
                                if exported_file_ref_cls and isinstance(w_exc, exported_file_ref_cls):
                                    try:
                                        await _refresh_loc()
                                        retry_q.appendleft(claimed_off)
                                        window_evt.set()
                                        continue
                                    except Exception as ref_err:
                                        p_state["fatal"] = ref_err
                                        p_state["stop"] = True
                                        window_evt.set()
                                        return
                                if migrate_err_cls and isinstance(w_exc, migrate_err_cls):
                                    p_state["fallback_seq"] = True
                                    p_state["stop"] = True
                                    window_evt.set()
                                    return

                                err_low = err_str.lower()
                                is_rpc_retryable = (
                                    (server_err_types and isinstance(w_exc, server_err_types))
                                    or isinstance(w_exc, (ConnectionError, TimeoutError, asyncio.TimeoutError, OSError))
                                    or (isinstance(w_exc, ValueError) and "request was unsuccessful" in err_low)
                                )
                                if not is_rpc_retryable:
                                    p_state["fatal"] = w_exc
                                    p_state["stop"] = True
                                    window_evt.set()
                                    return

                                fails = chunk_fails.get(claimed_off, 0) + 1
                                chunk_fails[claimed_off] = fails
                                detail = last_dc_issue or err_str
                                if fails >= 3:
                                    # Downgrade from multi-worker to 1-connection sequential iter_download before failing
                                    p_state["fallback_seq"] = True
                                    p_state["stop"] = True
                                    progress.log(
                                        f"[Telegram/Telethon] DC timeout at {claimed_off / 1048576:.1f} MB ({detail[:90]}); "
                                        "switching to 1-connection sequential recovery..."
                                    )
                                    window_evt.set()
                                    return
                                retry_q.append(claimed_off)
                                backoff_s = min(16.0, float(2 ** (fails + 1)))
                                p_state["pause_until"] = max(p_state["pause_until"], time.monotonic() + min(8.0, backoff_s))
                                window_evt.set()
                            finally:
                                p_state["in_flight"] -= 1

                    await asyncio.gather(*(_worker_loop(fn) for fn in worker_callers))
                    while write_off in ready_chunks:
                        blk = ready_chunks.pop(write_off)
                        if blk:
                            fh_obj.write(blk)
                            write_off += len(blk)
                    fh_obj.flush()
                    os.fsync(fh_obj.fileno())

                    if p_state["fatal"] is not None:
                        raise p_state["fatal"]
                    if p_state["fallback_seq"] or write_off < total_size:
                        return write_off, False
                    return write_off, True
                finally:
                    for ac in aux_clients:
                        try:
                            await ac.disconnect()
                        except Exception:
                            pass
                    for asnd in aux_senders:
                        try:
                            await asnd.disconnect()
                        except Exception:
                            pass

            salvage_exc: ProviderFailure | None = None
            try:
                with open(part_path, mode) as fh:
                    if mode == "r+b":
                        fh.seek(offset)
                        fh.truncate(offset)

                    parallel_done = False
                    if num_workers >= 2 and total_size > 0 and offset < total_size:
                        offset, parallel_done = await _run_parallel_ordered(fh, offset)
                        win_start_ts = time.monotonic()
                        win_start_bytes = offset

                    while (not parallel_done) and (total_size <= 0 or offset < total_size):
                        progress.check_cancelled()
                        iter_bytes = 0
                        try:
                            async for chunk in client.iter_download(
                                msg.media,
                                offset=offset,
                                request_size=cur_request_size,
                            ):
                                progress.check_cancelled()
                                if not chunk:
                                    continue
                                fh.write(chunk)
                                iter_bytes += len(chunk)
                                offset += len(chunk)
                                same_offset_fails = 0
                                cur_request_size = request_size
                                now = time.monotonic()
                                if now - last_ui_ts >= 0.4 or (total_size > 0 and offset >= total_size):
                                    elapsed = max(0.05, now - win_start_ts)
                                    speed_bps = max(0.0, (offset - win_start_bytes) / elapsed)
                                    if elapsed >= 5.0:
                                        win_start_ts = now
                                        win_start_bytes = offset
                                    pct = (offset * 100.0 / total_size) if total_size > 0 else 0.0
                                    if hasattr(progress, "set_phase_progress"):
                                        progress.set_phase_progress(
                                            done_bytes=offset,
                                            total_bytes=total_size or getattr(progress, "bytes_total", 0),
                                            pct=min(100.0, pct),
                                            speed=speed_bps,
                                            phase="download",
                                        )
                                    last_ui_ts = now
                                if total_size > 0 and offset >= total_size:
                                    break
                            if total_size <= 0 or offset >= total_size:
                                break
                            if iter_bytes > 0:
                                continue
                            cycle += 1
                            if cycle > max_cycles:
                                raise ProviderFailure(
                                    "DOWNLOAD_FAILED",
                                    f"Telethon stream returned empty EOF at {offset}/{total_size} bytes",
                                )
                            await _sleep_cancellable(2.0)
                        except tl_errors.FloodWaitError as fw:
                            fh.flush()
                            os.fsync(fh.fileno())
                            cycle += 1
                            if cycle > max_cycles:
                                raise ProviderFailure(
                                    "DOWNLOAD_FAILED",
                                    f"Telethon exceeded max FloodWait retries at {offset}/{total_size} bytes",
                                ) from fw
                            wait_s = max(1, int(getattr(fw, "seconds", 5) or 5)) + 1
                            progress.log(
                                f"[Telegram/Telethon] FloodWait {wait_s}s at {offset / 1048576:.1f}"
                                + (f"/{total_size / 1048576:.1f} MB" if total_size > 0 else " MB")
                                + " — .part saved, resuming automatically..."
                            )
                            await _sleep_cancellable(wait_s)
                            offset = (fh.tell() // chunk_align) * chunk_align
                            fh.seek(offset)
                            fh.truncate(offset)
                            win_start_ts = time.monotonic()
                            win_start_bytes = offset
                        except Exception as net_exc:
                            err_low = str(net_exc).lower()
                            is_retryable_rpc = (
                                (server_err_types and isinstance(net_exc, server_err_types))
                                or isinstance(net_exc, (ConnectionError, TimeoutError, asyncio.TimeoutError, OSError))
                                or (isinstance(net_exc, ValueError) and "request was unsuccessful" in err_low)
                            )
                            if not is_retryable_rpc:
                                raise
                            fh.flush()
                            os.fsync(fh.fileno())
                            offset = (fh.tell() // chunk_align) * chunk_align
                            fh.seek(offset)
                            fh.truncate(offset)

                            if offset == last_fail_offset:
                                same_offset_fails += 1
                            else:
                                last_fail_offset = offset
                                same_offset_fails = 1

                            detail = last_dc_issue or str(net_exc)
                            if same_offset_fails >= 3:
                                if isinstance(file_ref, dict) and total_size > 0 and offset >= int(total_size * 0.90):
                                    file_ref["_tg_dead_tail"] = True
                                raise ProviderFailure(
                                    "DOWNLOAD_FAILED",
                                    f"Telegram DC{sess_info['dc_id']} không phản hồi dữ liệu tại offset "
                                    f"{offset}/{total_size} ({detail}).",
                                ) from net_exc

                            cycle += 1
                            if cycle > max_cycles:
                                raise ProviderFailure(
                                    "DOWNLOAD_FAILED",
                                    f"Telethon interrupted after {max_cycles} retries at {offset}/{total_size} bytes: {detail}",
                                ) from net_exc

                            if same_offset_fails == 2 and cur_request_size > 64 * 1024:
                                cur_request_size = 64 * 1024

                            backoff_s = min(30.0, float(2 ** min(cycle + 1, 4)))
                            progress.log(
                                f"[Telegram/Telethon] DC timeout/reset at {offset / 1048576:.1f} MB ({detail[:100]}); "
                                f"retrying in {backoff_s:.0f}s (chunk={cur_request_size // 1024}KB)..."
                            )
                            await _sleep_cancellable(backoff_s)
                            win_start_ts = time.monotonic()
                            win_start_bytes = offset

                    fh.flush()
                    os.fsync(fh.fileno())
            except ProviderFailure as dl_exc:
                salvage_exc = dl_exc

            if salvage_exc is not None:
                if salvage_exc.code != "AUTH_REQUIRED":
                    salvaged = await self._salvage_truncated_video(
                        part_path, dest_file, total_size or expected_ref_size, progress, file_ref=file_ref
                    )
                    if salvaged is not None:
                        return [salvaged]
                raise salvage_exc

            verify_size = total_size or expected_ref_size
            await self._verify_downloaded_media_integrity(
                part_path, expected_size=verify_size, suffix_hint=dest_file.suffix
            )
            os.replace(part_path, dest_file)
            return [dest_file]
        finally:
            try:
                tl_logger.removeHandler(issue_handler)
            except Exception:
                pass
            try:
                await client.disconnect()
            except Exception:
                pass

    async def _download_telegram(
        self,
        url: str,
        dest_dir: Path,
        name: str | None,
        progress: JobState,
        *,
        credentials: dict[str, Any] | None = None,
        file_ref: dict[str, Any] | None = None,
    ) -> list[Path]:
        creds = credentials or (file_ref or {}).get("credentials") or {}
        kv_file = creds.get("kv_file") if isinstance(creds, dict) else None
        ns = str((creds.get("namespace") if isinstance(creds, dict) else "") or "colab").strip() or "colab"
        if not isinstance(kv_file, dict) or not kv_file.get(ns):
            raise ProviderFailure(
                "AUTH_REQUIRED",
                "Thiếu session Telegram (namespace: colab) trong file Packed JSON. Vui lòng đăng nhập Session Colab và Export lại.",
            )

        opt = (progress.payload or {}).get("options", {}) if hasattr(progress, "payload") else {}
        try:
            expected_size = int((file_ref or {}).get("size") or (file_ref or {}).get("file_size") or 0)
        except (TypeError, ValueError):
            expected_size = 0

        source_items = ((progress.payload or {}).get("source") or {}).get("items") or [] if hasattr(progress, "payload") else []
        total_items = max(int(getattr(progress, "files_to_download", 0) or 0), len(source_items))
        dl_conc = 1 if opt.get("parallel_download") is False else max(1, int(opt.get("download_concurrency") or 4))
        try:
            requested_threads = int(opt.get("telegram_threads") or opt.get("download_threads") or opt.get("threads_per_item") or 0)
        except (TypeError, ValueError):
            requested_threads = 0

        if requested_threads == 1:
            base_threads, pool_size, profile_mode = 1, 4, "manual-1t"
            tl_workers = 1
        elif total_items >= 2 and dl_conc >= 2:
            base_threads, pool_size, profile_mode = 1, 4, "auto-batch"
            tl_workers = 1 if (0 < expected_size <= 20 * 1024 * 1024) else 2
        elif 0 < expected_size <= 25 * 1024 * 1024:
            base_threads, pool_size, profile_mode = 1, 4, "auto-small"
            tl_workers = 1 if (0 < expected_size <= 2 * 1024 * 1024) else 3
        else:
            base_threads = 2 if requested_threads == 2 else 3
            pool_size = 8
            profile_mode = "auto-single"
            tl_workers = 4

        engine_pref = str(opt.get("telegram_engine") or "auto").strip().lower()
        has_valid_gotd = False
        try:
            self._parse_tdl_gotd_session(kv_file[ns])
            has_valid_gotd = True
        except ProviderFailure:
            has_valid_gotd = False

        if engine_pref != "tdl" and has_valid_gotd:
            try:
                return await self._download_telegram_telethon(
                    url,
                    dest_dir,
                    name,
                    progress,
                    credentials=creds,
                    file_ref=file_ref,
                    num_workers=tl_workers,
                )
            except ProviderFailure as tl_exc:
                err_txt = str(tl_exc.message or "")
                is_dead_tail_err = ("tại cuối file" in err_txt) or (
                    isinstance(file_ref, dict) and bool(file_ref.get("_tg_dead_tail"))
                )
                # Fail immediately only on dead session auth or confirmed >=90% end-of-file DC5 dead chunk
                if tl_exc.code == "AUTH_REQUIRED" or is_dead_tail_err:
                    raise
                if engine_pref == "telethon":
                    raise
                progress.log(f"[Telegram] Telethon engine error ({err_txt[:120]}); falling back to tdl CLI...")

        tdl_bin: str | None = None
        tdl_init_err = ""
        try:
            tdl_bin = await self._ensure_tdl_binary(progress)
        except Exception as init_exc:
            tdl_init_err = str(init_exc)

        if not tdl_bin:
            return await self._download_telegram_telethon(
                url, dest_dir, name, progress,
                credentials=creds, file_ref=file_ref,
                fallback_reason=tdl_init_err or "tdl unavailable",
                num_workers=tl_workers,
            )

        dest_dir.mkdir(parents=True, exist_ok=True)
        tmp_root = Path(tempfile.mkdtemp(prefix="vb_tdl_"))
        kv_path = tmp_root / "tdl_kv.json"
        dl_stage = dest_dir / f".tdl_stage_{uuid.uuid4().hex[:8]}"
        ansi_re = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
        tdl_prog_re = re.compile(
            r"(\d+(?:\.\d+)?)%\s+\[[^\]]+\]\s+\[([0-9.]+\s*[KMGT]?i?B)\s+in\s+([^;\]]+)(?:;\s*~?ETA:\s*([^;\]]+))?(?:;\s*([0-9.]+\s*[KMGT]?i?B/s))?\]",
            re.IGNORECASE,
        )

        def _parse_size_str(raw_sz: str) -> int:
            m = re.match(r"([0-9.]+)\s*([KMGT]?i?B)", str(raw_sz or "").strip(), re.IGNORECASE)
            if not m:
                return 0
            val = float(m.group(1))
            unit = m.group(2).upper().replace("I", "")
            mult = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}.get(unit, 1)
            return int(val * mult)

        def _read_tdl_log_tail(max_lines: int = 12) -> str:
            try:
                tdl_log = Path.home() / ".tdl" / "log" / "latest.log"
                if tdl_log.is_file():
                    lines = tdl_log.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
                    interesting = [
                        ln for ln in lines
                        if any(k in ln.lower() for k in ("flood", "wait", "error", "warn", "timeout", "retry", "recover"))
                    ]
                    chosen = interesting[-3:] if interesting else lines[-3:]
                    if chosen:
                        return " ; ".join(chosen)
            except Exception:
                pass
            return ""

        last_error_msg = ""
        downloaded_files: list[Path] = []
        max_done_bytes = -1
        last_pct_seen = -1.0
        try:
            kv_path.write_text(json.dumps({ns: kv_file[ns]}), encoding="utf-8")
            try:
                os.chmod(kv_path, 0o600)
            except Exception:
                pass

            progress.check_cancelled()
            dl_stage.mkdir(parents=True, exist_ok=True)

            cmd = [
                tdl_bin,
                "--storage", f"type=file,path={kv_path.as_posix()}",
                "-n", ns,
                "--pool", str(pool_size),
                "--reconnect-timeout", "2m",
                "--debug",
                "dl",
                "-u", url,
                "-d", str(dl_stage),
                "--continue",
                "--rewrite-ext",
                "--template", "{{ filenamify .FileName }}",
                "--threads", str(base_threads),
            ]
            progress.log(f"[Telegram] Downloading via tdl (ns={ns}, threads={base_threads}, pool={pool_size}, {profile_mode}): {url}")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            output_lines: list[str] = []
            buf = ""
            last_progress_at = time.monotonic()
            stalled = False

            def _handle_line(raw_line: str) -> None:
                nonlocal max_done_bytes, last_pct_seen, last_progress_at
                clean = ansi_re.sub("", raw_line).strip()
                if not clean:
                    return
                if clean.startswith("CPU:") and "Goroutines:" in clean:
                    return
                if re.match(r"^\[[.#]+\]\s+\[[^\]]+\]$", clean):
                    return

                m_prog = tdl_prog_re.search(clean)
                if m_prog:
                    pct = float(m_prog.group(1))
                    done_str = m_prog.group(2)
                    spd_str = (m_prog.group(5) or "").strip()
                    done_bytes = _parse_size_str(done_str)
                    speed_bps = float(_parse_size_str(spd_str[:-2])) if spd_str.lower().endswith("/s") else 0.0
                    total_bytes = expected_size or (int(done_bytes / (pct / 100.0)) if pct > 0.2 and done_bytes > 0 else 0)
                    if done_bytes > max_done_bytes or pct > last_pct_seen:
                        max_done_bytes = max(max_done_bytes, done_bytes)
                        last_pct_seen = max(last_pct_seen, pct)
                        last_progress_at = time.monotonic()
                    if hasattr(progress, "set_phase_progress"):
                        progress.set_phase_progress(
                            done_bytes=done_bytes,
                            total_bytes=total_bytes or getattr(progress, "bytes_total", 0),
                            pct=pct,
                            speed=speed_bps,
                            phase="download",
                        )
                    return

                output_lines.append(clean)
                if len(output_lines) > 50:
                    del output_lines[:-50]
                if any(k in clean.lower() for k in ("error", "fail", "flood", "deleted", "skipped")):
                    progress.log(f"[tdl] {clean[:200]}")

            while True:
                progress.check_cancelled()
                # Allow FloodWait / DC -503 recovery pauses near the end to finish before declaring a stall
                if last_pct_seen >= 90.0:
                    stall_limit = 150.0
                elif max_done_bytes > 0:
                    stall_limit = 90.0
                else:
                    stall_limit = 45.0
                if time.monotonic() - last_progress_at > stall_limit:
                    stalled = True
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    await proc.wait()
                    break

                if proc.stdout is None:
                    break
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=1.0)
                except asyncio.TimeoutError:
                    if proc.returncode is not None:
                        break
                    continue
                if not chunk:
                    break
                buf += chunk.decode("utf-8", errors="replace")
                parts = re.split(r"[\r\n]+", buf)
                buf = parts.pop()
                for part in parts:
                    _handle_line(part)

            if not stalled and buf.strip():
                _handle_line(buf)

            if stalled:
                log_tail = _read_tdl_log_tail()
                last_error_msg = (
                    f"tdl stalled at {max(0.0, last_pct_seen):.1f}% ({max(0, max_done_bytes)} bytes)"
                    + (f" | {log_tail}" if log_tail else "")
                )
            else:
                rc = await proc.wait()
                if rc != 0:
                    log_tail = _read_tdl_log_tail()
                    tail = " | ".join(output_lines[-5:])
                    last_error_msg = f"tdl exited with code {rc}: {tail}" + (f" | tdl.log: {log_tail}" if log_tail else "")
                else:
                    cands = [
                        p for p in dl_stage.rglob("*")
                        if p.is_file() and not p.name.endswith(".tmp") and p.stat().st_size > 0
                    ]
                    if not cands:
                        log_tail = _read_tdl_log_tail()
                        last_error_msg = f"tdl finished without producing any file for {url}" + (f" ({log_tail})" if log_tail else "")
                    else:
                        valid_cands: list[Path] = []
                        try:
                            for cand in cands:
                                await self._verify_downloaded_media_integrity(cand, expected_size=expected_size)
                                valid_cands.append(cand)
                            downloaded_files = valid_cands
                        except ProviderFailure as val_exc:
                            last_error_msg = str(val_exc.message)

            if downloaded_files:
                out_paths: list[Path] = []
                for idx, src_f in enumerate(downloaded_files):
                    if name and idx == 0 and Path(name).suffix:
                        target_name = safe_name(name)
                    else:
                        target_name = safe_name(src_f.name)
                    dest_file = dest_dir / target_name
                    if dest_file.exists():
                        stem, ext = dest_file.stem, dest_file.suffix
                        dest_file = dest_dir / f"{stem}_{idx + 1}{ext}"
                    shutil.move(str(src_f), str(dest_file))
                    out_paths.append(dest_file)
                return out_paths
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)
            shutil.rmtree(dl_stage, ignore_errors=True)

        # Fallback to Telethon contiguous .part engine when tdl stalled, exited non-zero, or failed integrity validation
        probe_hint = max_done_bytes if (last_pct_seen >= 90.0 and max_done_bytes >= 1024 * 1024) else 0
        return await self._download_telegram_telethon(
            url,
            dest_dir,
            name,
            progress,
            credentials=creds,
            file_ref=file_ref,
            fallback_reason=last_error_msg or "tdl failed",
            probe_hint_offset=probe_hint,
            num_workers=tl_workers,
        )

    async def _dispatch_download(
        self, link_type: str, is_stream: bool, url: str, urls: list[str],
        dest_dir: Path, name: str | None, progress: JobState, *,
        headers: dict[str, str] | None = None, cookies: str | None = None,
        dec_key: dict[str, Any] | None = None, file_ref: dict[str, Any] | None = None,
        proxy: str | None = None,
        page_url: str | None = None,
        credentials: dict[str, Any] | None = None,
    ) -> list[Path]:
        """Central download dispatch. Extracted so proxy retry can re-call it."""
        file_ref = file_ref or {}
        if link_type == "telegram":
            return await self._download_telegram(
                url, dest_dir, name, progress, credentials=credentials, file_ref=file_ref,
            )
        if link_type == "torrent":
            return await self._download_torrent(url, dest_dir, progress)
        elif link_type == "mxdrop":
            return await self._download_mxdrop(url, dest_dir, name, progress, file_ref, proxy=proxy)
        elif link_type == "doodstream":
            return await self._download_doodstream(url, dest_dir, name, progress, file_ref, proxy=proxy)
        elif is_stream:
            return await self._download_stream_nm3u8dl(
                url, dest_dir, name, progress,
                headers=headers, cookies=cookies, dec_key=dec_key,
                proxy=proxy, page_url=page_url, file_ref=file_ref,
            )
        elif link_type == "mega_file":
            return await self._download_mega_file(url, dest_dir, name, progress, file_ref=file_ref, proxy=proxy)
        elif link_type == "mega_folder":
            return await self._download_mega_folder(url, dest_dir, progress, proxy=proxy)
        elif link_type == "gofile_page":
            return await self._download_gofile_page(url, dest_dir, name, progress, proxy=proxy)
        elif link_type in ("pixeldrain", "pixeldrain_list"):
            return await self._download_pixeldrain(url, dest_dir, name, progress, proxy=proxy)
        elif link_type == "mediafire":
            return await self._download_mediafire(url, dest_dir, name, progress)
        elif link_type == "mediafire_folder":
            return await self._download_mediafire_folder(url, dest_dir, progress)
        elif link_type in ("dropbox", "dropbox_folder"):
            return await self._download_dropbox(url, dest_dir, name, progress, is_folder=(link_type == "dropbox_folder"), proxy=proxy)
        elif link_type == "onedrive":
            return await self._download_onedrive(url, dest_dir, name, progress, proxy=proxy)
        elif link_type in ("yandex_disk", "yandex_folder"):
            return await self._download_yandex_disk(url, dest_dir, name, progress, file_ref=file_ref, proxy=proxy)
        elif link_type == "sorafolder":
            return await self._download_sorafolder(url, dest_dir, name, progress)
        elif link_type == "ytdlp":
            try:
                return await self._download_ytdlp(url, dest_dir, name, progress, headers=headers, cookies=cookies, proxy=proxy)
            except TypeError:
                return await self._download_ytdlp(url, dest_dir, name, progress)
        elif link_type == "gdrive":
            return await self._download_gdrive(url, dest_dir, name, progress)
        elif link_type == "gdrive_folder":
            return await self._download_gdrive_folder(url, dest_dir, progress)
        else:
            opt = (progress.payload or {}).get("options", {}) if hasattr(progress, "payload") else {}
            tc = int(opt.get("download_threads") or opt.get("threads_per_item") or opt.get("downloadThreads") or DOWNLOAD_THREADS)
            progress.log(f"Downloading via aria2c ({tc} threads)...")
            try:
                try:
                    if proxy or cookies:
                        return await self._download_aria2(urls, dest_dir, name, progress, headers=headers, proxy=proxy, cookies=cookies)
                    elif headers:
                        return await self._download_aria2(urls, dest_dir, name, progress, headers=headers)
                    else:
                        return await self._download_aria2(urls, dest_dir, name, progress)
                except TypeError:
                    try:
                        return await self._download_aria2(urls, dest_dir, name, progress, headers=headers)
                    except TypeError:
                        return await self._download_aria2(urls, dest_dir, name, progress)
            except Exception as exc:
                if isinstance(exc, ProviderFailure) and self._is_ip_bound_token_error(exc.message):
                    progress.log(f"[IP-Guard] Stopped direct HTTP fallback because URL token is IP-bound or expired ({exc.message}).")
                    raise
                progress.log(f"aria2c failed ({exc}); falling back to browser-compatible HTTP stream downloader...")
                return await self._download_http_stream(urls, dest_dir, name, progress, headers=headers, proxy=proxy, cookies=cookies)

    async def validate_credentials(self, credentials: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    def is_folder_link(self, url: str) -> bool:
        """Return True if URL points to a multi-file cloud folder or album list."""
        u = str(url or "").strip()
        ltype = self._classify_link(u)
        if ltype == "yandex_folder":
            return True
        if ltype == "yandex_disk":
            titles = getattr(self, "_last_folder_titles", None)
            if isinstance(titles, dict) and u in titles:
                return True
        return ltype in ("mega_folder", "gdrive_folder", "mediafire_folder", "pixeldrain_list", "dropbox_folder")

    async def list_files(self, credentials: dict[str, Any], path_or_id: str) -> dict[str, Any]:
        """List files inside a supported cloud folder/album URL."""
        url = str(path_or_id or "").strip()
        if not url:
            return {"items": []}
        ltype = self._classify_link(url)
        titles = getattr(self, "_last_folder_titles", None)
        if not isinstance(titles, dict):
            titles = {}
            self._last_folder_titles = titles
        if ltype == "gdrive_folder":
            title, items = await self._list_gdrive_folder_items(url, credentials=credentials)
            return {"name": title, "items": items}
        if ltype in ("yandex_disk", "yandex_folder"):
            title, items, _ = await self._list_yandex_folder_items(url)
            return {"name": title, "items": items}
        if ltype == "mega_folder":
            items = await self._list_mega_folder_nodes(url)
            return {"name": titles.get(url, ""), "items": items}
        if ltype == "gofile_page":
            items = await self._list_gofile_items(url)
            return {"name": titles.get(url, ""), "items": items}
        if ltype == "pixeldrain_list":
            items = await self._list_pixeldrain_items(url)
            return {"name": titles.get(url, ""), "items": items}
        if ltype == "mediafire_folder":
            items = await self._list_mediafire_folder_items(url)
            return {"name": titles.get(url, ""), "items": items}
        return {"items": []}

    async def download_folder(
        self,
        credentials: dict[str, Any],
        folder_ref: dict[str, Any],
        local_dir: Path,
        progress: JobState,
        _sem: asyncio.Semaphore | None = None,
    ) -> list[Path]:
        await self._ensure_deps()
        url = str(folder_ref.get("id") or folder_ref.get("path") or folder_ref.get("url") or "").strip()
        if not url:
            raise ProviderFailure("DOWNLOAD_FAILED", "No folder URL provided")
        local_dir.mkdir(parents=True, exist_ok=True)
        ltype = self._classify_link(url)

        listing = folder_ref.pop("_prefetched_listing", None) if isinstance(folder_ref.get("_prefetched_listing"), dict) else None
        if listing is None:
            listing = await self.list_files(credentials, url)
        resolved_folder_title = str(listing.get("name") or "").strip()
        if resolved_folder_title and is_placeholder_link_name(folder_ref.get("name"), url):
            folder_ref["name"] = safe_name(resolved_folder_title, is_dir=True)
        items = listing.get("items") or []
        if items:
            progress.log(f"[links] Resolved {len(items)} item(s) in {ltype} folder: {folder_ref.get('name') or url[:80]}")
            options = (progress.payload or {}).get("options", {}) if hasattr(progress, "payload") else {}
            conc = max(1, int(options.get("download_concurrency") or 4))
            sem = _sem or asyncio.Semaphore(conc)

            subfolders = [c for c in items if (c.get("type") == "folder" or c.get("is_folder") or c.get("isdir"))]
            file_children = [c for c in items if not (c.get("type") == "folder" or c.get("is_folder") or c.get("isdir"))]

            from ..utils.media_converter import is_file_filter_enabled, should_download_file_item
            if file_children and is_file_filter_enabled(options):
                kept_children: list[dict[str, Any]] = []
                for child in file_children:
                    if should_download_file_item(child, options):
                        kept_children.append(child)
                    else:
                        progress.files_skipped += 1
                        progress.log(f"[File-Filter] Skipped file excluded by filter: {child.get('name') or child.get('_relative_path') or 'file'}")
                file_children = kept_children
                if not file_children and not subfolders:
                    progress.log(f"[File-Filter] No matching files to download in link folder '{folder_ref.get('name') or url[:60]}'")
                    return []

            checker = getattr(progress, "_target_checker", None)
            if checker is not None and file_children and (options.get("auto_skip") or options.get("autoSkip")):
                f_name = safe_name(str(folder_ref.get("name") or local_dir.name or "folder"), is_dir=True)
                missing_items: list[dict[str, Any]] = []
                for child in file_children:
                    rel_raw = str(child.get("_relative_path") or child.get("name") or "file").replace("\\", "/").strip("/")
                    check_child = {**child, "_relative_path": f"{f_name}/{rel_raw}" if f_name else rel_raw}
                    if await checker.item_exists(check_child, options, folder_name=f_name):
                        progress.files_skipped += 1
                        progress.log(f"[Auto-Skip] File đã tồn tại ở đích, bỏ qua tải: {rel_raw}")
                    else:
                        missing_items.append(child)
                file_children = missing_items
                if not file_children and not subfolders:
                    progress.log(f"[Auto-Skip] Toàn bộ file trong link folder '{folder_ref.get('name') or url[:60]}' đã tồn tại ở đích!")
                    return []

            progress.files_to_download += len(file_children)

            async def _dl_child(child: dict[str, Any]) -> list[Path]:
                progress.check_cancelled()
                async with sem:
                    rel_raw = str(child.get("_relative_path") or child.get("name") or "file").replace("\\", "/").strip("/")
                    rel_parts = [safe_name(p, is_dir=(idx < len(rel_raw.split("/")) - 1)) for idx, p in enumerate(rel_raw.split("/")) if p and p not in (".", "..")]
                    dest_dir = local_dir.joinpath(*rel_parts[:-1]) if len(rel_parts) > 1 else local_dir
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    fname = rel_parts[-1] if rel_parts else safe_name(str(child.get("name") or "file"))
                    fsize = int(child.get("size") or 0)
                    progress.start_file(fname, phase="download", size=fsize)
                    try:
                        from .base import download_with_retry
                        path = await download_with_retry(
                            lambda: self.download_file(credentials, child, dest_dir, progress),
                            progress=progress,
                            label=fname,
                        )
                        actual_sz = path.stat().st_size if (path and path.exists()) else fsize
                        progress.finish_file(fname, phase="download", size=actual_sz)
                        progress.files_downloaded += 1
                        progress.log(f"[{progress.files_downloaded}/{progress.files_to_download}] Downloaded: {path.name if path else fname}")
                        return [path]
                    except ProviderFailure as exc:
                        progress.finish_file(fname, phase="download")
                        from .base import is_skippable_download_failure
                        if not is_skippable_download_failure(exc):
                            raise
                        progress.fail_file(fname, exc.code, exc.message)
                        return []
                    except Exception:
                        progress.finish_file(fname, phase="download")
                        raise

            async def _dl_subfolder(sub: dict[str, Any]) -> list[Path]:
                sub_name = safe_name(str(sub.get("name") or "folder"), is_dir=True)
                sub_dir = local_dir / sub_name
                sub_dir.mkdir(parents=True, exist_ok=True)
                return await self.download_folder(credentials, sub, sub_dir, progress, _sem=sem)

            batches = await asyncio.gather(
                *(_dl_child(c) for c in file_children),
                *(_dl_subfolder(s) for s in subfolders),
            )
            return [p for batch in batches for p in batch if p and p.is_file()]

        # Direct folder handlers (e.g. gdrive_folder via gdown, dropbox_folder via zip extract)
        downloaded = await self._dispatch_download(
            ltype, False, url, [url], local_dir, folder_ref.get("name"), progress, file_ref=folder_ref
        )
        valid_files = [p for p in downloaded if p and p.is_file()]
        progress.files_to_download += len(valid_files)
        progress.files_downloaded += len(valid_files)
        return valid_files

    async def download_file(self, credentials: dict[str, Any], file_ref: dict[str, Any], local_path: Path, progress: JobState) -> Path:
        """Download a single file from a URL.

        ``local_path`` follows the BaseProvider convention: it may be either
        the input directory or an intended file destination.
        """
        await self._ensure_deps()

        url = file_ref.get("id") or file_ref.get("path") or file_ref.get("url") or ""
        if not url:
            raise ProviderFailure("DOWNLOAD_FAILED", "No URL provided")
        urls = [str(item) for item in (file_ref.get("urls") or []) if str(item or "").startswith(("http://", "https://"))]
        urls = urls or [str(url)]
        proxy: str | None = None
        try:
            expected_size = int(file_ref.get("size") or file_ref.get("file_size") or file_ref.get("bytes") or (file_ref.get("meta") or {}).get("size") or 0)
        except (TypeError, ValueError):
            expected_size = 0
        headers = file_ref.get("headers") or (file_ref.get("meta") or {}).get("headers") or {}
        if not isinstance(headers, dict):
            headers = {}
        if expected_size and len(urls) > 1:
            urls = await self._filter_urls_by_size(urls, expected_size, headers)

        is_existing_dir = local_path.exists() and local_path.is_dir()
        raw_name = file_ref.get("name") or (local_path.name if (not is_existing_dir and local_path.suffix) else "")
        meta_dict = file_ref.get("meta") or {}
        page_url = str(file_ref.get("page_url") or meta_dict.get("page_url") or "").strip()
        if not page_url:
            ref_hdr = str(headers.get("Referer") or headers.get("referer") or "").strip()
            if ref_hdr.startswith("http") and ref_hdr.rstrip("/").count("/") >= 3:
                page_url = ref_hdr

        if "pornhub.com" in page_url:
            page_url = re.sub(r'https?://[a-zA-Z0-9_-]+\.pornhub\.com', 'https://www.pornhub.com', page_url)

        page_title = meta_dict.get("page_title") or file_ref.get("page_title")
        if page_title and (not raw_name or any(raw_name.startswith(p) for p in ("1080P_", "720P_", "480P_", "360P_", "master", "index", "chunklist", "stream_download_"))):
            raw_name = f"{safe_name(page_title)}.mp4"

        name = safe_name(raw_name) if raw_name else None
        link_type = "telegram" if str(file_ref.get("type") or "").lower() == "telegram" else self._classify_link(url)
        if name and link_type in ("gdrive", "mega_file", "mediafire", "pixeldrain", "gofile_page", "dropbox", "telegram", "yandex_disk", "yandex_folder") and is_placeholder_link_name(raw_name, url):
            name = None
        if link_type == "direct" and "downloader.disk.yandex." in str(url).lower() and (not name or is_placeholder_link_name(raw_name, url)):
            q_fn = (parse_qs(urlparse(str(url)).query).get("filename") or [""])[0]
            if q_fn:
                name = safe_name(unquote(q_fn))
        if link_type == "direct":
            check_sources = f"{page_url} {headers.get('Referer', '')} {headers.get('referer', '')}".lower()
            if any(d in check_sources for d in ("archivebate.com", "playmogo.com", "doods.pro", "dooood.com", "doodstream", "cloudatacdn.com", "myvidplay.com", "livecamsrip.com", "ds2play", "dsvplay", "dood.")):
                link_type = "doodstream"

        progress.log(f"[links] {link_type}: {url[:120]}")

        dest_dir = local_path if is_existing_dir else (local_path.parent if local_path.suffix else local_path)
        dest_dir.mkdir(parents=True, exist_ok=True)

        cookies = file_ref.get("cookies") or (file_ref.get("meta") or {}).get("cookies") or ""
        dec_key = file_ref.get("decryption_key") or (file_ref.get("meta") or {}).get("decryption_key")
        is_stream = (
            (link_type == "stream"
            or str(file_ref.get("type") or "").lower() in ("hls", "m3u8", "dash", "stream")
            or bool(dec_key)
            or ".m3u8" in url.lower()
            or ".mpd" in url.lower())
            and link_type != "ytdlp"
        )

        # Check token expiration timestamp in stream/direct URL
        exp_val = None
        try:
            parsed_url = urlparse(url)
            parsed_query = parse_qs(parsed_url.query)
            exp_val = (
                parsed_query.get("expires", [None])[0]
                or parsed_query.get("e", [None])[0]
                or parsed_query.get("exp", [None])[0]
                or parsed_query.get("expire", [None])[0]
            )
            if exp_val and str(exp_val).isdigit():
                exp_ts = int(exp_val)
                if exp_ts < 1_000_000_000:
                    start_val = (
                        parsed_query.get("s", [None])[0]
                        or parsed_query.get("st", [None])[0]
                        or parsed_query.get("start", [None])[0]
                    )
                    if start_val and str(start_val).isdigit() and int(start_val) >= 1_000_000_000:
                        exp_ts = int(start_val) + exp_ts
                    else:
                        exp_ts = 0
                        exp_val = None
                if exp_ts >= 1_000_000_000:
                    exp_val = str(exp_ts)
                    now_ts = int(time.time())
                    can_pre_resolve = page_url or (file_ref and file_ref.get("page_url"))
                    if exp_ts <= now_ts + 60 and can_pre_resolve:
                        diff_m = max(1, (now_ts - exp_ts) // 60) if exp_ts < now_ts else 0
                        msg_txt = f"Link token expired {diff_m} minute(s) ago" if exp_ts < now_ts else "Link token expires in < 1 minute"
                        progress.log(f"[Auto-Resolve] {msg_txt}! Proactively resolving fresh stream URL and cookies from webpage...")
                        try:
                            ref_payload = {**(file_ref or {}), "page_url": page_url or (file_ref or {}).get("page_url")}
                            fresh_url, _, fresh_hdrs = await self._resolve_universal_page(url, ref_payload, progress, proxy=proxy)
                            if fresh_url and fresh_url != url:
                                url = fresh_url
                                if fresh_hdrs:
                                    headers.update(fresh_hdrs)
                                    if "Cookie" in fresh_hdrs:
                                        cookies = fresh_hdrs["Cookie"]
                                progress.log(f"[Auto-Resolve] Successfully acquired fresh stream URL and cookies before download!")
                        except Exception as pre_err:
                            progress.log(f"[Auto-Resolve] Preemptive token resolution notice: {pre_err}")
                    elif exp_ts < now_ts:
                        diff_m = max(1, (now_ts - exp_ts) // 60)
                        progress.log(f"[WARNING] Link token expired {diff_m} minute(s) ago! (Expires: {exp_ts}, Now: {now_ts}). If download fails with 410/403, please refresh the webpage and copy a fresh link.")
                    elif (exp_ts - now_ts) < 300:
                        diff_m = max(1, (exp_ts - now_ts) // 60)
                        progress.log(f"[WARNING] Link token will expire in ~{diff_m} minute(s)! Ensure download completes before expiration or copy a fresh link.")
        except Exception:
            pass

        downloaded = None
        try:
            downloaded = await self._dispatch_download(
                link_type, is_stream, url, urls, dest_dir, name, progress,
                headers=headers, cookies=cookies, dec_key=dec_key, file_ref=file_ref,
                page_url=page_url, credentials=credentials,
            )
        except ProviderFailure as exc:
            msg = str(exc.message).lower()
            is_blocked = (
                exc.code in ("DOWNLOAD_FAILED",)
                and self._is_ip_or_captcha_blocked(msg)
            )
            is_token_ip_bound = self._is_ip_bound_token_error(msg)

            # Check FIRST if failure was caused by an expired token (HTTP 410 Gone / expired token)
            is_expired_token = (
                exp_val and str(exp_val).isdigit() and int(exp_val) < int(time.time())
                and any(k in msg for k in ("410", "gone", "expired", "403", "forbidden"))
            )
            if not downloaded and is_expired_token:
                # If we have a canonical page_url, auto-resolve fresh token directly from the page
                if page_url and page_url != url and page_url.startswith(("http://", "https://")):
                    progress.log(f"[Token-Refresh] Link token expired; attempting auto-refresh from page: {page_url[:80]}...")
                    try:
                        downloaded = await self._download_universal_page(
                            url, dest_dir, name, progress, file_ref,
                        )
                    except Exception as refresh_exc:
                        progress.log(f"[Token-Refresh] Auto-refresh from page failed: {refresh_exc}")
                        downloaded = None

                if not downloaded:
                    exp_ts = int(exp_val)
                    now_ts = int(time.time())
                    diff_m = max(1, (now_ts - exp_ts) // 60)
                    raise ProviderFailure(
                        "LINK_EXPIRED",
                        f"Link token expired {diff_m} minute(s) ago on {urlparse(url).netloc}. "
                        "The video hosting CDN strictly rejects expired links with HTTP 410. "
                        "Please refresh the video page in your browser, press play, and copy/send a fresh link."
                    )

            # Fallback DoodStream: If not already tried as doodstream and indicators exist
            if not downloaded and link_type != "doodstream":
                dood_ctx = f"{url} {page_url} {headers.get('Referer', '')} {headers.get('referer', '')}".lower()
                if any(d in dood_ctx for d in ("archivebate.com", "playmogo.com", "doods.pro", "dooood.com", "cloudatacdn.com", "myvidplay.com", "livecamsrip.com", "doodstream", "ds2play", "dsvplay", "dood.")):
                    progress.log("[Fallback] Direct download failed. Retrying with native DoodStream resolver...")
                    try:
                        downloaded = await self._download_doodstream(
                            page_url or url, dest_dir, name, progress, file_ref,
                        )
                    except Exception as dood_exc:
                        progress.log(f"[Fallback] DoodStream resolver failed: {dood_exc}")
                        downloaded = None

            # Universal Page/Embed Resolver: For ANY website with a canonical page_url or embed Referer when blocked/IP-bound
            if (
                not downloaded
                and is_blocked
                and link_type not in ("doodstream", "mxdrop", "torrent", "gofile_page")
                and page_url
                and page_url != url
                and page_url.startswith(("http://", "https://"))
            ):
                progress.log(f"[IP-Guard] Direct link blocked/IP-bound; resolving fresh token from webpage: {page_url[:80]}...")
                try:
                    downloaded = await self._download_universal_page(
                        url, dest_dir, name, progress, file_ref,
                    )
                except Exception as univ_exc:
                    progress.log(f"[IP-Guard] Universal page resolver notice: {univ_exc}")
                    downloaded = None

            # Fallback 1: If stream/direct failed and we have canonical page_url, retry with yt-dlp (and auto-proxy if yt-dlp is IP-blocked)
            if (
                not downloaded
                and link_type != "doodstream"
                and page_url
                and page_url != url
                and page_url.startswith(("http://", "https://"))
                and not any(d in page_url.lower() for d in ("archivebate.com", "playmogo.com", "dooood.com", "doods.pro", "myvidplay.com", "livecamsrip.com"))
            ):
                clean_page_url = page_url
                if "pornhub.com" in clean_page_url:
                    clean_page_url = re.sub(r'https?://[a-zA-Z0-9_-]+\.pornhub\.com', 'https://www.pornhub.com', clean_page_url)
                progress.log(f"[Fallback] Retrying download from canonical page URL with yt-dlp: {clean_page_url[:80]}...")
                try:
                    downloaded = await self._download_ytdlp(
                        clean_page_url, dest_dir, name, progress,
                        headers=None, cookies=cookies,
                    )
                except Exception as page_exc:
                    page_err_str = str(page_exc).lower()
                    if any(unsupported in page_err_str for unsupported in ("unsupported url", "is not a valid url", "no suitable extractor")):
                        progress.log(f"[Fallback] Page URL not supported by yt-dlp extractor; skipping redundant proxy retry.")
                    else:
                        progress.log(f"[Fallback] Page URL yt-dlp failed ({page_exc}); checking proxy fallback...")
                        if "pornhub.com" not in clean_page_url:
                            tor_px = await self._ensure_tor_proxy(progress)
                            if tor_px:
                                try:
                                    downloaded = await self._download_ytdlp(
                                        clean_page_url, dest_dir, name, progress,
                                        headers=None, cookies=cookies, proxy=self._isolated_proxy(tor_px, uuid.uuid4().hex[:8]),
                                    )
                                except Exception as ytdlp_px_exc:
                                    progress.log(f"[Fallback] Page URL yt-dlp via proxy failed: {ytdlp_px_exc}")
                                    downloaded = None

            # Fallback 2: If still blocked and not downloaded, retry with SOCKS5 Proxy
            # Fail-fast ("tự dừng"): If a raw direct/stream URL failed with an IP-bound token error (like error_wrong_ip)
            # and has no page resolver, retrying the same browser-bound URL via a Tor exit IP will also fail with wrong_ip.
            if not downloaded and is_blocked and "pornhub.com" not in url and "pornhub.com" not in (page_url or ""):
                if is_token_ip_bound and link_type in ("direct", "stream", "doodstream", "mxdrop"):
                    progress.log("[IP-Guard] Stopped redundant proxy retry on raw IP-bound token URL.")
                else:
                    progress.log(f"[Proxy] Download blocked ({exc.message}). Activating TCP SOCKS5 proxy and retrying...")
                    base_proxy = await self._ensure_tor_proxy(progress)
                    if base_proxy:
                        proxy = self._isolated_proxy(base_proxy, uuid.uuid4().hex[:8])
                        progress.log(f"[Proxy] Retrying via isolated circuit {proxy}...")
                        try:
                            downloaded = await self._dispatch_download(
                                link_type, is_stream, url, urls, dest_dir, name, progress,
                                headers=headers, cookies=cookies, dec_key=dec_key, file_ref=file_ref, proxy=proxy,
                                page_url=page_url,
                            )
                        except Exception as stream_proxy_exc:
                            progress.log(f"[Proxy] Retry via proxy failed: {stream_proxy_exc}")
                            downloaded = None
                    else:
                        progress.log("[Proxy] Proxy unavailable.")

            if not downloaded:
                raise exc

        if not downloaded:
            raise ProviderFailure("DOWNLOAD_FAILED", "Download completed but no files found on disk")

        result = downloaded[0]
        self._validate_downloaded_files([result])
        if expected_size and not (isinstance(file_ref, dict) and file_ref.get("_salvaged")) and result.stat().st_size < expected_size:
            raise ProviderFailure("DOWNLOAD_INCOMPLETE", f"Downloaded {result.stat().st_size} bytes, expected {expected_size}")
        if is_placeholder_link_name(file_ref.get("name"), url):
            file_ref["name"] = result.name
        return result

    async def upload_file(self, credentials: dict[str, Any], local_path: Path, target_ref: dict[str, Any], progress: JobState) -> dict[str, Any]:
        raise ProviderFailure("NOT_SUPPORTED", "Links provider does not support upload")


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
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

TOR_PROXY = "socks5://127.0.0.1:9050"
PROXY_SETUP_LOCK: asyncio.Lock | None = None
_proxy_ready = False
_PROXY_REQUIRED_DOMAINS: set[str] = set()

import httpx

from ..providers.base import BaseProvider, ProviderFailure, _download_error_payload, safe_name, stream_download
_default_stream_download = stream_download
from ..jobs.progress import JobState

BT_TRACKERS = "udp://tracker.opentrackr.org:1337/announce,udp://open.stealth.si:80/announce,udp://tracker.openbittorrent.com:6969/announce,udp://exodus.desync.com:6969/announce"

class LinksProvider(BaseProvider):
    """Provider for downloading files from user-provided URLs."""
    
    name = "links"
    _deps_checked = False

    @classmethod
    async def _ensure_deps(cls) -> None:
        if cls._deps_checked:
            return
            
        if not shutil.which("aria2c"):
            try:
                subprocess.check_call(['apt-get', 'install', '-y', '-qq', 'aria2'])
            except Exception:
                pass

        need_nm_install = False
        nm_bin = shutil.which("N_m3u8DL-RE")
        if not nm_bin and sys.platform.startswith("linux"):
            need_nm_install = True
        elif nm_bin and sys.platform.startswith("linux"):
            try:
                res = subprocess.run([nm_bin, "--version"], capture_output=True, text=True, timeout=5)
                out_ver = (res.stdout or "") + (res.stderr or "")
                # If older than 0.6.0 or from 2024, upgrade to modern 0.6.0-beta
                if "2024" in out_ver or "0.2." in out_ver:
                    need_nm_install = True
            except Exception:
                pass

        if need_nm_install:
            try:
                nm_tar = "/tmp/N_m3u8DL-RE.tar.gz"
                candidate_urls = [
                    "https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v0.6.0-beta/N_m3u8DL-RE_v0.6.0-beta_linux-x64_20260629.tar.gz",
                    "https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v0.2.1-beta/N_m3u8DL-RE_Beta_linux-x64_20240828.tar.gz",
                ]
                for url in candidate_urls:
                    try:
                        res = subprocess.run(["curl", "-fsSL", url, "-o", nm_tar], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
                        if res.returncode == 0 and os.path.exists(nm_tar) and os.path.getsize(nm_tar) > 10000:
                            break
                    except Exception:
                        pass

                if os.path.exists(nm_tar) and os.path.getsize(nm_tar) > 10000:
                    subprocess.run(["tar", "-xzf", nm_tar, "-C", "/tmp"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    for extracted in Path("/tmp").glob("**/N_m3u8DL-RE"):
                        if extracted.is_file():
                            subprocess.run(["install", "-m", "755", str(extracted), "/usr/local/bin/N_m3u8DL-RE"])
                            break
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

        try:
            import httpx_socks
        except ImportError:
            missing.append("httpx[socks]")
            
        if missing:
            try:
                subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q'] + missing)
            except Exception:
                pass
            
        cls._deps_checked = True

    @classmethod
    async def _ensure_tor_proxy(cls, progress: JobState | None = None) -> str | None:
        """Set up Tor as SOCKS5 proxy on 127.0.0.1:9050 (pure TCP, works 100% in Colab)."""
        global _proxy_ready, PROXY_SETUP_LOCK
        import socket

        def _is_port_open(port: int = 9050) -> bool:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                    return True
            except Exception:
                return False

        if _proxy_ready and _is_port_open(9050):
            return TOR_PROXY
        if PROXY_SETUP_LOCK is None:
            PROXY_SETUP_LOCK = asyncio.Lock()
        async with PROXY_SETUP_LOCK:
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
                                    + "SocksPort 127.0.0.1:9050 IsolateSOCKSAuth KeepAliveIsolateSOCKSAuth\n"
                                    + "NumEntryGuards 8\n"
                                    + "CircuitBuildTimeout 10\n"
                                    + "LearnCircuitBuildTimeout 0\n"
                                    + "MaxCircuitDirtiness 7200\n"
                                    + "CircuitStreamTimeout 30\n"
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
                                "--SocksPort", "127.0.0.1:9050 IsolateSOCKSAuth KeepAliveIsolateSOCKSAuth",
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
        if "127.0.0.1:9050" in proxy and circuit_tag:
            return f"socks5://vb_{circuit_tag}:pass@127.0.0.1:9050"
        return proxy

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
        return any(k in low for k in (
            "error_wrong_ip", "wrong_ip", "ip not allowed", "403", "forbidden",
            "429", "blocked", "access denied", "file not found", "error response",
            "error instead of file", "error payload", "captcha-player",
            "cf-challenge", "cf-turnstile", "turnstile", "just a moment",
            "attention required", "ddos-guard", "security check",
        ))

    async def _fetch_webpage(
        self,
        target_url: str,
        req_headers: dict[str, str],
        progress: JobState,
        proxy: str | None = None,
        auto_proxy: bool = True,
    ) -> tuple[int, str, str, str | None]:
        """Universal webpage fetcher with browser TLS impersonation, domain proxy memory, and parallel 3-circuit racing."""
        def _do_fetch(active_px: str | None) -> tuple[int, str, str]:
            try:
                from curl_cffi import requests as cffi_requests
                s = cffi_requests.Session(impersonate="chrome")
                r = s.get(target_url, headers=req_headers, proxy=active_px, timeout=20)
                return r.status_code, r.text, str(r.url)
            except Exception:
                with httpx.Client(follow_redirects=True, timeout=20.0, proxy=active_px) as client:
                    r = client.get(target_url, headers=req_headers)
                    return r.status_code, r.text, str(r.url)

        def _is_page_challenged(code: int, body: str) -> bool:
            if code in (401, 403, 429, 451, 503):
                return True
            low = (body or "").lower()
            if len(low) < 25000 and any(m in low for m in (
                "captcha-player", "cf-challenge", "cf-turnstile", "just a moment...",
                "attention required! | cloudflare", "ddos-guard", "verify you are human",
            )):
                return True
            return False

        target_host = urlparse(target_url).netloc.lower()
        code, body, final_url = 0, "", target_url

        # Skip wasted direct attempt if this domain is already known to challenge datacenter IPs
        if proxy or not (auto_proxy and target_host in _PROXY_REQUIRED_DOMAINS):
            try:
                code, body, final_url = await asyncio.to_thread(_do_fetch, proxy)
                if not _is_page_challenged(code, body) and body.strip():
                    return code, body, final_url, proxy
                if not proxy and auto_proxy:
                    _PROXY_REQUIRED_DOMAINS.add(target_host)
                    final_host = urlparse(final_url).netloc.lower()
                    if final_host:
                        _PROXY_REQUIRED_DOMAINS.add(final_host)
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
                if not _is_page_challenged(c_code, c_body) and c_body.strip():
                    return c_code, c_body, c_url, proxy
            except Exception:
                pass

        # Race 3 isolated Tor circuits in parallel so the lowest-latency, highest-bandwidth exit node wins
        progress.log(f"[IP-Guard] Racing 3 parallel proxy circuits on {target_host} to select fastest exit IP...")

        async def _race_circuit(circuit_px: str) -> tuple[int, str, str, str]:
            r_code, r_body, r_url = await asyncio.to_thread(_do_fetch, circuit_px)
            if _is_page_challenged(r_code, r_body) or not r_body.strip():
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
                        return w_code, w_body, w_url, w_px
                    except Exception:
                        pass
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()

        return code, body, final_url, proxy or base_proxy

    def _classify_link(self, url: str) -> str:
        url_lower = url.lower()
        if url_lower.startswith("magnet:?xt=") or url_lower.endswith(".torrent"):
            return "torrent"

        parsed = urlparse(url_lower)
        if parsed.netloc.endswith("gofile.io"):
            return "direct" if parsed.path.startswith("/download/web/") else "gofile_page"
            
        if "mediafire.com" in url_lower:
            return "mediafire"

        if "sorafolder.com" in url_lower:
            return "sorafolder"

        # Direct & Embed resolvers for Mixdrop / Mxdrop / Mxcontent
        if any(h in url_lower for h in ["mxcontent.net", "mxdrop.to", "mxdrop.top", "mixdrop.co", "mixdrop.to", "mixdrop.bz", "mixdrop.ch"]):
            return "mxdrop"

        # DoodStream / Playmogo / CloudataCDN / Archivebate
        dood_domains = [
            "cloudatacdn.com", "playmogo.com", "archivebate.com", "doods.pro",
            "dooood.com", "dooood", "doodstream", "ds2play", "do0od", "d000d", "d0000d",
            "dood.to", "dood.watch", "dood.so", "dood.ws", "dood.pm",
            "dood.re", "dood.li", "dood.cx", "dood.wf", "dood.la",
            "dood.sh", "dood.video", "dood.stream",
        ]
        if any(d in url_lower for d in dood_domains):
            return "doodstream"

        ytdlp_domains = [
            "youtube", "youtu.be", "tiktok", "bilibili", "vimeo", 
            "dailymotion", "twitch", "mega.nz", 
            "pixeldrain.com", "1fichier.com", "pornhub.com"
        ]
        
        if url_lower.endswith(".m3u8") or url_lower.endswith(".mpd") or ".m3u8?" in url_lower or ".mpd?" in url_lower or "/hls/" in url_lower:
            return "stream"
            
        for domain in ytdlp_domains:
            if domain in url_lower:
                return "ytdlp"
                
        if "drive.google.com" in url_lower:
            return "gdrive"
            
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
        num_connections: int = 16,
    ) -> Path:
        """High-speed multi-connection HTTP Range downloader that multiplexes 16 parallel streams over the same SOCKS5 circuit or direct connection."""
        import threading
        from ..providers import base as _base_mod

        dest.parent.mkdir(parents=True, exist_ok=True)
        part_path = dest.with_suffix(dest.suffix + ".part")
        base_headers = {k: v for k, v in (headers or {}).items() if str(k).lower() != "range"}

        limits = httpx.Limits(
            max_connections=max(24, num_connections + 4),
            max_keepalive_connections=max(20, num_connections),
        )
        timeout = httpx.Timeout(connect=30.0, read=45.0, write=30.0, pool=60.0)
        client_kwargs: dict[str, Any] = {"follow_redirects": True, "timeout": timeout, "limits": limits}
        if proxy:
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
                        raise ProviderFailure(error["code"], error["message"], **(error.get("details") or {}))
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
                    raise ProviderFailure(error["code"], error["message"], **(error.get("details") or {}))

            first_len = len(first_bytes)
            if total_size <= first_len:
                part_path.write_bytes(first_bytes)
                if error := _download_error_payload(part_path, content_type, False):
                    part_path.unlink(missing_ok=True)
                    raise ProviderFailure(error["code"], error["message"], **(error.get("details") or {}))
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
            raise ProviderFailure(error["code"], error["message"], **(error.get("details") or {}))
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
        for one in urls:
            out_name = name or safe_name(unquote(Path(urlparse(one).path).name) or "download")
            try:
                progress.log(f"Starting browser-compatible download: {out_name}")
                if use_parallel:
                    downloaded = [await self._download_parallel_ranges(
                        one,
                        dest_dir / out_name,
                        progress,
                        headers=self._http_headers(headers, cookies=cookies),
                        proxy=proxy,
                        num_connections=16,
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
        cmd = [
            "aria2c", 
            f"--dir={dest_dir}",
            "--max-connection-per-server=16",
            "--split=16",
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
        text_errors = (
            b"file not found", b"404 not found", b"not found", b"403 forbidden", b"forbidden",
            b"access denied", b"link expired", b"url expired", b"invalid token", b"video deleted",
            b"ip not allowed", b"bad request", b"not authorized", b"unauthorized",
            b"<html", b"<!doctype html", b"error"
        )
        for f in files:
            if not f.is_file():
                continue
            sz = f.stat().st_size
            if sz == 0:
                f.unlink(missing_ok=True)
                raise ProviderFailure("DOWNLOAD_FAILED", f"Downloaded file {f.name} is empty (0 bytes)")
            if sz <= 4096:
                content = f.read_bytes()
                raw_low = content.lower()
                if any(err in raw_low for err in text_errors):
                    f.unlink(missing_ok=True)
                    sample = content.decode("utf-8", errors="ignore").strip()
                    raise ProviderFailure("DOWNLOAD_FAILED", f"Server returned error payload ({sample[:100]}) instead of media content for {f.name}")

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
    ) -> list[Path]:
        # Universal High-Speed Strategy:
        # If a canonical webpage URL is present (from Sniffer JSON), attempt downloading via yt-dlp first.
        # yt-dlp supports 1,800+ sites and will generate fresh, unthrottled CDN tokens directly on Colab's IP,
        # bypassing the single-connection 600KB/s throttle imposed by CDNs on remote browser tokens!
        if page_url and page_url.startswith(("http://", "https://")) and page_url != url:
            clean_page = page_url
            if "pornhub.com" in clean_page:
                clean_page = re.sub(r'https?://[a-zA-Z0-9_-]+\.pornhub\.com', 'https://www.pornhub.com', clean_page)
            progress.log(f"[links] Canonical page detected. Attempting high-speed extraction with yt-dlp: {clean_page[:80]}...")
            try:
                return await self._download_ytdlp(clean_page, dest_dir, name, progress, headers=None, cookies=cookies, proxy=proxy)
            except Exception as page_err:
                progress.log(f"[links] Canonical page extraction bypassed ({str(page_err)[:100]}). Falling back to multi-threaded N_m3u8DL-RE...")

        if not shutil.which("N_m3u8DL-RE"):
            progress.log("N_m3u8DL-RE not found in PATH, falling back to yt-dlp...")
            try:
                return await self._download_ytdlp(url, dest_dir, name, progress, headers=headers, cookies=cookies, proxy=proxy)
            except TypeError:
                return await self._download_ytdlp(url, dest_dir, name, progress, headers=headers)

        dest_dir.mkdir(parents=True, exist_ok=True)
        before = set(dest_dir.iterdir()) if dest_dir.exists() else set()
        clean_name = (name or safe_name(unquote(Path(urlparse(url).path).name) or "stream")).replace(".mp4", "").replace(".ts", "")
        tmp_dir = dest_dir / f".tmp_nm3u8dl_{uuid.uuid4().hex[:8]}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        progress.current_file = f"{clean_name}.mp4"

        parsed_url = urlparse(url)
        default_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        default_ref = f"{parsed_url.scheme}://{parsed_url.netloc}/" if parsed_url.netloc else ""
        default_origin = f"{parsed_url.scheme}://{parsed_url.netloc}" if parsed_url.netloc else ""

        req_headers = dict(headers or {})
        has_ua = any(k.lower() == "user-agent" for k in req_headers)
        has_ref = any(k.lower() == "referer" for k in req_headers)
        has_origin = any(k.lower() == "origin" for k in req_headers)

        if not has_ua:
            req_headers["User-Agent"] = default_ua
        if not has_ref and default_ref:
            req_headers["Referer"] = default_ref
        if not has_origin and default_origin:
            req_headers["Origin"] = default_origin

        clean_name = re.sub(r'[,;:*?"<>|/\\`]', '_', clean_name)
        cmd = [
            "N_m3u8DL-RE",
            url,
            "--save-name", clean_name,
            "--save-dir", str(dest_dir),
            "--tmp-dir", str(tmp_dir),
            "--thread-count", "8",
            "--download-retry-count", "5",
            "--check-segments-count", "false",
            "--del-after-done",
            "--no-ansi-color",
            "--auto-select",
        ]
        if shutil.which("ffmpeg"):
            cmd.extend(["-M", "format=mp4:muxer=ffmpeg"])
        else:
            cmd.append("--binary-merge")

        skip_hdrs = (
            "host", "content-length", "accept-encoding", "connection", "range", "transfer-encoding",
            "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
            "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "sec-fetch-user"
        )
        for k, v in req_headers.items():
            low = str(k).lower()
            if v and low not in skip_hdrs:
                if low == "accept" and "vnd." in str(v):
                    cmd.extend(["-H", "Accept: */*"])
                else:
                    clean_v = re.sub(r'[\r\n\t]+', ' ', str(v)).strip()
                    cmd.extend(["-H", f"{k}: {clean_v}"])

        if cookies and "cookie" not in {str(k).lower() for k in req_headers}:
            clean_cookie = re.sub(r'[\r\n\t]+', ' ', str(cookies)).strip()
            if clean_cookie:
                cmd.extend(["-H", f"Cookie: {clean_cookie}"])

        if dec_key and isinstance(dec_key, dict):
            key_hex = dec_key.get("key_hex") or dec_key.get("key")
            if key_hex:
                cmd.extend(["--key", str(key_hex)])
                progress.log(f"[*] Applying Offline AES-128 Key: {key_hex}")

        if proxy:
            cmd.extend(["--custom-proxy", proxy])

        progress.log(f"Starting N_m3u8DL-RE stream download: {clean_name}")
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        last_lines: list[str] = []
        if process.stdout:
            while True:
                chunk = await process.stdout.read(2048)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="ignore").replace("\r", "\n")
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if not any(k in line for k in ("Writing meta json", "ANSI colors")):
                        last_lines = (last_lines + [line])[-10:]
                    progress.check_cancelled()
                    self._update_stream_progress(line, progress)

        await process.wait()
        shutil.rmtree(tmp_dir, ignore_errors=True)

        after = set(dest_dir.iterdir()) if dest_dir.exists() else set()
        new_files = [
            p for p in (after - before) 
            if p.is_file() 
            and not p.name.startswith(".")
            and not p.name.startswith(".tmp") 
            and not p.name.endswith((".aria2", ".part", ".ytdl", ".tmp", ".txt", ".json"))
            and not (p.name.lower() in ("cookies.txt", "cookie.txt") or p.name.lower().startswith("cookie"))
            and ".part-Frag" not in p.name
            and p.stat().st_size > 0
        ]
        if not new_files:
            new_files = [
                p for p in dest_dir.iterdir() 
                if p.is_file() 
                and p.name.startswith(clean_name)
                and not p.name.startswith(".")
                and not p.name.startswith(".tmp") 
                and not p.name.endswith((".aria2", ".part", ".ytdl", ".tmp", ".txt", ".json"))
                and not (p.name.lower() in ("cookies.txt", "cookie.txt") or p.name.lower().startswith("cookie"))
                and ".part-Frag" not in p.name
                and p.stat().st_size > 0
            ]

        if not new_files or process.returncode != 0:
            err_detail = ""
            if process.returncode != 0:
                err_detail = f" (exit code {process.returncode}: {' | '.join(last_lines[-2:])})"
            elif not new_files:
                err_detail = f" (no output file produced: {' | '.join(last_lines[-2:])})"
            progress.log(f"N_m3u8DL-RE failed or produced no output file{err_detail}, falling back to yt-dlp for direct stream...")
            try:
                return await self._download_ytdlp(url, dest_dir, name, progress, headers=headers, cookies=cookies, proxy=proxy)
            except Exception as direct_err:
                if page_url and page_url != url and page_url.startswith(("http://", "https://")):
                    clean_page_url = page_url
                    if "pornhub.com" in clean_page_url:
                        clean_page_url = re.sub(r'https?://[a-zA-Z0-9_-]+\.pornhub\.com', 'https://www.pornhub.com', clean_page_url)
                    progress.log(f"[Fallback] Direct stream download failed ({direct_err}). Retrying download from canonical page URL with yt-dlp: {clean_page_url[:80]}...")
                    try:
                        return await self._download_ytdlp(clean_page_url, dest_dir, name, progress, headers=None, cookies=cookies, proxy=proxy)
                    except Exception as page_err:
                        progress.log(f"[Fallback] Canonical page URL download failed: {page_err}")
                raise ProviderFailure("DOWNLOAD_FAILED", f"Stream download failed: N_m3u8DL-RE error{err_detail}; yt-dlp direct error: {direct_err}")

        return new_files

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

        lines = ["# Netscape HTTP Cookie File\n", "# https://curl.se/docs/http-cookies.html\n\n"]
        for item in str(cookies_str).split(";"):
            item = item.strip()
            if not item or "=" not in item:
                continue
            k, v = item.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k:
                lines.append(f"{base_domain}\tTRUE\t/\tTRUE\t2147483647\t{k}\t{v}\n")

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
    ) -> list[Path]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        # Use a temporary staging directory to isolate downloading fragments
        stage_dir = dest_dir / f".tmp_ytdlp_{uuid.uuid4().hex[:8]}"
        stage_dir.mkdir(parents=True, exist_ok=True)

        out_name = name or safe_name(unquote(Path(urlparse(url).path).name) or "stream.mp4")
        if not out_name.lower().endswith((".mp4", ".mkv", ".webm", ".ts")):
            out_name += ".mp4"
        out_tpl = str(stage_dir / "%(title)s.%(ext)s") if not name else str(stage_dir / out_name)
        progress.current_file = out_name

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

        has_ua = any(k.lower() == "user-agent" for k in req_headers)
        has_ref = any(k.lower() == "referer" for k in req_headers)

        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--no-playlist",
            "--no-check-certificates",
            "--newline",
            "--progress",
            "-f", "bestvideo*+bestaudio/best",
            "-N", "8",
            "--concurrent-fragments", "8",
            "--socket-timeout", "20",
            "--fragment-retries", "10",
            "--retries", "10",
            "--retry-sleep", "fragment:exp=1:2:8",
            "--buffer-size", "16M",
            "-o", out_tpl,
        ]

        # Enable browser impersonation via curl_cffi to bypass Cloudflare/TLS fingerprint detection
        has_curl_cffi = False
        try:
            import curl_cffi
            has_curl_cffi = True
        except ImportError:
            pass
        if has_curl_cffi:
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
            "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "sec-fetch-user"
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

        cookie_dir: Path | None = None
        if cookies:
            ref_domain = req_headers.get("referer") or req_headers.get("Referer") or default_ref or url
            cookie_dir = Path(tempfile.gettempdir()) / f"vb_cookies_{uuid.uuid4().hex[:8]}"
            cookie_dir.mkdir(parents=True, exist_ok=True)
            c_file = cookie_dir / "cookies.txt"
            if self._write_netscape_cookies(cookies, ref_domain, c_file):
                cmd.extend(["--cookies", str(c_file)])
            elif "cookie" not in {str(k).lower() for k in req_headers}:
                cmd.extend(["--add-header", f"Cookie: {cookies}"])

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

            # Find finished valid video files in stage_dir (strictly exclude cookies, metadata, fragments)
            completed_files = [
                p for p in stage_dir.iterdir()
                if p.is_file()
                and not p.name.startswith(".")
                and not p.name.endswith((".part", ".ytdl", ".aria2", ".tmp", ".txt", ".json", ".log"))
                and not (p.name.lower() in ("cookies.txt", "cookie.txt") or p.name.lower().startswith("cookie"))
                and ".part-Frag" not in p.name
                and p.stat().st_size > 0
            ]

            if not completed_files:
                raise ProviderFailure("DOWNLOAD_FAILED", "yt-dlp finished but no valid merged video file was produced (only incomplete fragments)")

            final_files = []
            for src_file in completed_files:
                target_file = dest_dir / (out_name if len(completed_files) == 1 and name else src_file.name)
                shutil.move(str(src_file), str(target_file))
                final_files.append(target_file)

            # Clean any stray cookie files in dest_dir
            for extra in dest_dir.glob("*cookie*"):
                if extra.is_file():
                    extra.unlink(missing_ok=True)

            return final_files
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)
            if cookie_dir:
                shutil.rmtree(cookie_dir, ignore_errors=True)

    async def _download_gdrive(self, url: str, dest_dir: Path, name: str | None, progress: JobState) -> list[Path]:
        progress.log(f"Starting gdown download for: {url}")
        try:
            import gdown
            before = set(dest_dir.iterdir()) if dest_dir.exists() else set()
            def run_gdown() -> None:
                output = str(dest_dir / name) if name else str(dest_dir) + "/"
                gdown.download(url, output, quiet=False, fuzzy=True)
            await asyncio.to_thread(run_gdown)
            after = set(dest_dir.iterdir()) if dest_dir.exists() else set()
            new_files = [p for p in (after - before) if p.is_file()]
            return new_files or [p for p in dest_dir.iterdir() if p.is_file()]
        except Exception as e:
            progress.log(f"gdown failed, falling back to yt-dlp: {e}")
            return await self._download_ytdlp(url, dest_dir, name, progress)

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

        dood_hosts = ("dood", "playmogo", "ds2play", "d000", "do0od", "dSVplay")
        embed_url = ""
        url_low = url.lower()
        if not page_url and "archivebate.com" in url_low:
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

        active_proxy = proxy
        if not embed_url and page_url and page_url.startswith(("http://", "https://")):
            progress.log(f"[doodstream] Fetching canonical page: {page_url[:80]}...")
            _, page_html, _, active_proxy = await self._fetch_webpage(
                page_url, req_headers, progress, proxy=active_proxy, auto_proxy=True,
            )
            for m_iframe in re.finditer(r'<iframe[^>]+src=[\x22\x27]([^\x22\x27]+)[\x22\x27]', page_html, re.I):
                iframe_src = m_iframe.group(1).strip()
                if iframe_src.startswith("//"):
                    iframe_src = "https:" + iframe_src
                if iframe_src.startswith("http"):
                    embed_url = iframe_src
                    progress.log(f"[doodstream] Found player iframe: {embed_url}")
                    break

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

        embed_headers = dict(req_headers)
        if page_url:
            embed_headers["Referer"] = page_url
        progress.log(f"[doodstream] Fetching embed player: {embed_url}")
        _, embed_html, final_embed_url, active_proxy = await self._fetch_webpage(
            embed_url, embed_headers, progress, proxy=active_proxy, auto_proxy=True,
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
        # Must use the exact same proxy circuit so the CDN binds the token to the same exit IP
        _, pass_body, _, active_proxy = await self._fetch_webpage(
            pass_url, pass_headers, progress, proxy=active_proxy, auto_proxy=False,
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
            if "cloudatacdn.com" in url.lower() or "/e/" in url.lower() or "archivebate.com" in url.lower():
                raise ProviderFailure(
                    "DOWNLOAD_FAILED",
                    f"[IP-Guard] Stopped direct download of IP-bound DoodStream link because fresh token resolution failed ({exc}).",
                )
            progress.log(f"[doodstream] Notice: Resolution error ({exc}), trying direct URL...")
            if "referer" not in {k.lower() for k in dl_headers}:
                dl_headers["Referer"] = "https://playmogo.com/"

        progress.log("Downloading DoodStream media...")
        try:
            if active_proxy:
                downloaded = await self._download_aria2(resolved_url, dest_dir, final_name, progress, headers=dl_headers, proxy=active_proxy)
            else:
                downloaded = await self._download_aria2(resolved_url, dest_dir, final_name, progress, headers=dl_headers)
            self._validate_downloaded_files(downloaded)
            return downloaded
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
                    downloaded = await self._download_http_stream(resolved_url, dest_dir, final_name, progress, headers=dl_headers, proxy=active_proxy)
                    self._validate_downloaded_files(downloaded)
                    return downloaded
                raise
            progress.log(f"aria2c failed ({exc}); falling back to HTTP stream downloader...")
            downloaded = await self._download_http_stream(resolved_url, dest_dir, final_name, progress, headers=dl_headers, proxy=active_proxy)
            self._validate_downloaded_files(downloaded)
            return downloaded

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
            raise ProviderFailure("DOWNLOAD_FAILED", "No canonical page_url or embed Referer available for universal page resolution")

        req_headers = {
            "User-Agent": str(headers.get("User-Agent") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
            "Accept-Language": "en-US,en;q=0.9",
        }
        if cookies:
            req_headers["Cookie"] = cookies

        progress.log(f"[IP-Guard] Resolving fresh stream/file token from page: {target_page[:80]}...")
        _, page_html, final_page_url, active_proxy = await self._fetch_webpage(
            target_page, req_headers, progress, proxy=proxy, auto_proxy=True,
        )

        # Follow player iframe if the canonical page embeds an external video host
        active_html = page_html
        active_url = final_page_url
        for m_iframe in re.finditer(r'<iframe[^>]+src=[\x22\x27]([^\x22\x27]+)[\x22\x27]', page_html, re.I):
            iframe_src = m_iframe.group(1).strip()
            if iframe_src.startswith("//"):
                iframe_src = "https:" + iframe_src
            if iframe_src.startswith(("http://", "https://")) and not any(
                ad in iframe_src.lower() for ad in ("google", "doubleclick", "exoclick", "juicyads", "magsrv", "recaptcha", "turnstile")
            ):
                iframe_headers = dict(req_headers)
                iframe_headers["Referer"] = final_page_url
                progress.log(f"[IP-Guard] Following embedded player iframe: {iframe_src[:80]}...")
                _, iframe_html, final_iframe_url, active_proxy = await self._fetch_webpage(
                    iframe_src, iframe_headers, progress, proxy=active_proxy, auto_proxy=True,
                )
                if iframe_html.strip():
                    active_html = iframe_html
                    active_url = final_iframe_url
                    break

        parsed_active = urlparse(active_url)
        active_domain = f"{parsed_active.scheme}://{parsed_active.netloc}"
        unpacked = self._unpack_dean_edwards(active_html)
        combined_text = f"{unpacked}\n{active_html}" if unpacked else active_html

        dl_headers: dict[str, str] = {
            "User-Agent": req_headers["User-Agent"],
            "Referer": f"{active_domain}/",
            "Origin": active_domain,
        }
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
            _, pass_body, _, active_proxy = await self._fetch_webpage(
                pass_url, pass_headers, progress, proxy=active_proxy, auto_proxy=False,
            )
            cdn_base = pass_body.strip()
            if cdn_base.startswith("http"):
                chars = string.ascii_letters + string.digits
                rand_str = ''.join(random.choices(chars, k=10))
                resolved_url = f"{cdn_base}{rand_str}?token={token}&expiry={int(time.time() * 1000)}"
                if active_proxy:
                    dl_headers["_active_proxy"] = active_proxy
                return resolved_url, file_ref.get("name"), dl_headers

        # 2. Check MDCore.wurl pattern (MixDrop / MxDrop clones)
        m_wurl = re.search(r'MDCore\.wurl\s*=\s*[\x22\x27]([^\x22\x27]+)[\x22\x27]', combined_text)
        if m_wurl:
            wurl = m_wurl.group(1).strip()
            if wurl.startswith("//"):
                wurl = "https:" + wurl
            if wurl.startswith("http"):
                return wurl, file_ref.get("name"), dl_headers

        # 3. Check HLS (.m3u8) or direct media (.mp4/.mkv/.webm) in unpacked JS / player config
        media_patterns = (
            r'(?:file|src|source|hls|mp4)\s*[:=]\s*[\x22\x27](https?://[^\x22\x27\s<>]+?\.(?:m3u8|mpd|mp4|mkv|webm)[^\x22\x27\s<>]*)[\x22\x27]',
            r'[\x22\x27](https?://[^\x22\x27\s<>\\]+?\.(?:m3u8|mpd|mp4|mkv|webm)(?:\?[^\x22\x27\s<>\\]*)?)[\x22\x27]',
        )
        for pat in media_patterns:
            for m_media in re.finditer(pat, combined_text, re.I):
                candidate = html.unescape(m_media.group(1).replace(r"\/", "/")).strip()
                low_cand = candidate.lower()
                if any(skip in low_cand for skip in ("preview", "thumb", "sprite", ".vtt", ".srt", ".jpg", ".png")):
                    continue
                progress.log(f"[IP-Guard] Extracted fresh media URL from page: {candidate[:80]}...")
                return candidate, file_ref.get("name"), dl_headers

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
            downloaded = await self._download_stream_nm3u8dl(
                resolved_url, dest_dir, final_name, progress,
                headers=dl_headers, cookies=cookies, proxy=active_proxy,
            )
        else:
            downloaded = await self._download_aria2(
                resolved_url, dest_dir, final_name, progress,
                headers=dl_headers, proxy=active_proxy, cookies=cookies,
            )
        self._validate_downloaded_files(downloaded)
        return downloaded

    async def _dispatch_download(
        self, link_type: str, is_stream: bool, url: str, urls: list[str],
        dest_dir: Path, name: str | None, progress: JobState, *,
        headers: dict[str, str] | None = None, cookies: str | None = None,
        dec_key: dict[str, Any] | None = None, file_ref: dict[str, Any] | None = None,
        proxy: str | None = None,
        page_url: str | None = None,
    ) -> list[Path]:
        """Central download dispatch. Extracted so proxy retry can re-call it."""
        file_ref = file_ref or {}
        if link_type == "torrent":
            return await self._download_torrent(url, dest_dir, progress)
        elif link_type == "mxdrop":
            return await self._download_mxdrop(url, dest_dir, name, progress, file_ref, proxy=proxy)
        elif link_type == "doodstream":
            return await self._download_doodstream(url, dest_dir, name, progress, file_ref, proxy=proxy)
        elif is_stream:
            return await self._download_stream_nm3u8dl(url, dest_dir, name, progress, headers=headers, cookies=cookies, dec_key=dec_key, proxy=proxy, page_url=page_url)
        elif link_type == "gofile_page":
            raise ProviderFailure("SOURCE_FILE_NOT_FOUND", "Gofile page links are not direct downloads. Click Download in browser, stop it, copy the store-*.gofile.io/download/web/... URL, then paste that link.")
        elif link_type == "mediafire":
            return await self._download_mediafire(url, dest_dir, name, progress)
        elif link_type == "sorafolder":
            return await self._download_sorafolder(url, dest_dir, name, progress)
        elif link_type == "ytdlp":
            try:
                return await self._download_ytdlp(url, dest_dir, name, progress, headers=headers, cookies=cookies, proxy=proxy)
            except TypeError:
                return await self._download_ytdlp(url, dest_dir, name, progress)
        elif link_type == "gdrive":
            return await self._download_gdrive(url, dest_dir, name, progress)
        else:
            progress.log("Downloading via aria2c (16 connections)...")
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
        try:
            expected_size = int(file_ref.get("size") or file_ref.get("file_size") or file_ref.get("bytes") or (file_ref.get("meta") or {}).get("size") or 0)
        except (TypeError, ValueError):
            expected_size = 0
        headers = file_ref.get("headers") or (file_ref.get("meta") or {}).get("headers") or {}
        if not isinstance(headers, dict):
            headers = {}
        if expected_size and len(urls) > 1:
            urls = await self._filter_urls_by_size(urls, expected_size, headers)

        raw_name = file_ref.get("name") or (local_path.name if local_path.suffix else "")
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
        link_type = self._classify_link(url)
        if link_type == "direct":
            check_sources = f"{page_url} {headers.get('Referer', '')} {headers.get('referer', '')}".lower()
            if any(d in check_sources for d in ("archivebate.com", "playmogo.com", "doods.pro", "dooood.com", "doodstream", "cloudatacdn.com", "dood.")):
                link_type = "doodstream"

        progress.log(f"[links] {link_type}: {url[:120]}")

        dest_dir = local_path.parent if local_path.suffix else local_path
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
                now_ts = int(time.time())
                if exp_ts < now_ts:
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
                page_url=page_url,
            )
        except ProviderFailure as exc:
            msg = str(exc.message).lower()
            is_blocked = (
                exc.code in ("DOWNLOAD_FAILED",)
                and self._is_ip_or_captcha_blocked(msg)
            )
            is_token_ip_bound = self._is_ip_bound_token_error(msg)

            # Fallback DoodStream: If not already tried as doodstream and indicators exist
            if not downloaded and link_type != "doodstream":
                dood_ctx = f"{url} {page_url} {headers.get('Referer', '')} {headers.get('referer', '')}".lower()
                if any(d in dood_ctx for d in ("archivebate.com", "playmogo.com", "doods.pro", "dooood.com", "cloudatacdn.com", "doodstream", "ds2play", "dood.")):
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
                and not any(d in page_url.lower() for d in ("archivebate.com", "playmogo.com", "dooood.com", "doods.pro"))
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

            # Check if failure was caused by an expired token (HTTP 410 Gone)
            if not downloaded and exp_val and str(exp_val).isdigit():
                exp_ts = int(exp_val)
                now_ts = int(time.time())
                if exp_ts < now_ts and any(k in msg for k in ("410", "gone", "expired")):
                    diff_m = max(1, (now_ts - exp_ts) // 60)
                    raise ProviderFailure(
                        "DOWNLOAD_FAILED",
                        f"Link token expired {diff_m} minute(s) ago on {urlparse(url).netloc}. "
                        "The video hosting CDN strictly rejects expired links with HTTP 410. "
                        "Please refresh the video page in your browser, press play, and copy/send a fresh link."
                    )

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
        if expected_size and result.stat().st_size < expected_size:
            raise ProviderFailure("DOWNLOAD_INCOMPLETE", f"Downloaded {result.stat().st_size} bytes, expected {expected_size}")
        return result

    async def upload_file(self, credentials: dict[str, Any], local_path: Path, target_ref: dict[str, Any], progress: JobState) -> dict[str, Any]:
        raise ProviderFailure("NOT_SUPPORTED", "Links provider does not support upload")


from __future__ import annotations
import asyncio
import html
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from ..providers.base import BaseProvider, ProviderFailure, safe_name
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
            subprocess.check_call(['apt-get', 'install', '-y', '-qq', 'aria2'])
            
        missing = []
        if not shutil.which("yt-dlp"):
            missing.append("yt-dlp")
            
        try:
            import gdown
        except ImportError:
            missing.append("gdown")
            
        if missing:
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q'] + missing)
            
        cls._deps_checked = True

    def _classify_link(self, url: str) -> str:
        url_lower = url.lower()
        if url_lower.startswith("magnet:?xt=") or url_lower.endswith(".torrent"):
            return "torrent"

        parsed = urlparse(url_lower)
        if parsed.netloc.endswith("gofile.io"):
            return "direct" if parsed.path.startswith("/download/web/") else "gofile_page"
            
        if "mediafire.com" in url_lower:
            return "mediafire"

        ytdlp_domains = [
            "youtube", "youtu.be", "tiktok", "bilibili", "vimeo", 
            "dailymotion", "twitch", "mega.nz", 
            "pixeldrain.com", "1fichier.com"
        ]
        
        if url_lower.endswith(".m3u8") or url_lower.endswith(".mpd"):
            return "ytdlp"
            
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

    def _parse_ytdlp_progress(self, line: str) -> float | None:
        match = re.search(r'([0-9.]+)%', line)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                pass
        return None

    async def _download_aria2(self, url: str | list[str], dest_dir: Path, name: str | None, progress: JobState) -> list[Path]:
        urls = [str(item) for item in (url if isinstance(url, list) else [url]) if str(item or "")]
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
        if name:
            cmd.append(f"--out={name}")
        cmd.extend(urls)
        
        return await self._run_aria2_cmd(cmd, dest_dir, progress)

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
                        if diff > 0:
                            progress.add_bytes(diff, total)
                        last_done = done

        await process.wait()
        if process.returncode != 0:
            detail = f": {' | '.join(output_tail)}" if output_tail else ""
            raise ProviderFailure("DOWNLOAD_FAILED", f"aria2c exited with code {process.returncode}{detail}")

        after = set(dest_dir.iterdir()) if dest_dir.exists() else set()
        new_files = [p for p in (after - before) if p.is_file() and not p.name.endswith(".aria2")]
        return new_files or [p for p in dest_dir.iterdir() if p.is_file() and not p.name.endswith(".aria2")]

    async def _download_ytdlp(self, url: str, dest_dir: Path, name: str | None, progress: JobState) -> list[Path]:
        before = set(dest_dir.iterdir()) if dest_dir.exists() else set()
        out_tpl = str(dest_dir / "%(title)s.%(ext)s")
        if name:
            out_tpl = str(dest_dir / name)

        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--newline",
            "--progress",
            "-o",
            out_tpl,
            url
        ]
        progress.log(f"Starting yt-dlp download for: {url}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        last_pct = 0.0
        if process.stdout:
            while True:
                line_bytes = await process.stdout.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode('utf-8', errors='ignore').strip()
                progress.check_cancelled()
                pct = self._parse_ytdlp_progress(line)
                if pct is not None and pct > last_pct:
                    # Approximate byte progress: report percentage increments as bytes (100 total)
                    diff = pct - last_pct
                    progress.add_bytes(int(diff * 1024), int(100 * 1024))
                    last_pct = pct

        await process.wait()
        if process.returncode != 0:
            stderr_out = await process.stderr.read() if process.stderr else b""
            raise ProviderFailure("DOWNLOAD_FAILED", f"yt-dlp failed: {stderr_out.decode('utf-8', errors='ignore')}")

        after = set(dest_dir.iterdir()) if dest_dir.exists() else set()
        new_files = [p for p in (after - before) if p.is_file()]
        return new_files or [p for p in dest_dir.iterdir() if p.is_file()]

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

    async def validate_credentials(self, credentials: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    async def download_file(self, credentials: dict[str, Any], file_ref: dict[str, Any], local_path: Path, progress: JobState) -> Path:
        """Download a single file from a URL.

        ``local_path`` follows the BaseProvider convention: it may be either
        the input directory or an intended file destination.
        """
        await self._ensure_deps()

        url = file_ref.get("id") or file_ref.get("path") or ""
        if not url:
            raise ProviderFailure("DOWNLOAD_FAILED", "No URL provided")
        urls = [str(item) for item in (file_ref.get("urls") or []) if str(item or "").startswith(("http://", "https://"))]
        urls = urls or [str(url)]

        raw_name = file_ref.get("name") or (local_path.name if local_path.suffix else "")
        name = safe_name(raw_name) if raw_name else None
        link_type = self._classify_link(url)
        progress.log(f"[links] {link_type}: {url[:120]}")

        dest_dir = local_path.parent if local_path.suffix else local_path
        dest_dir.mkdir(parents=True, exist_ok=True)

        if link_type == "torrent":
            downloaded = await self._download_torrent(url, dest_dir, progress)
        elif link_type == "gofile_page":
            raise ProviderFailure("SOURCE_FILE_NOT_FOUND", "Gofile page links are not direct downloads. Click Download in browser, stop it, copy the store-*.gofile.io/download/web/... URL, then paste that link.")
        elif link_type == "mediafire":
            downloaded = await self._download_mediafire(url, dest_dir, name, progress)
        elif link_type == "ytdlp":
            downloaded = await self._download_ytdlp(url, dest_dir, name, progress)
        elif link_type == "gdrive":
            downloaded = await self._download_gdrive(url, dest_dir, name, progress)
        else:
            downloaded = await self._download_aria2(urls, dest_dir, name, progress)

        if not downloaded:
            raise ProviderFailure("DOWNLOAD_FAILED", "Download completed but no files found on disk")

        result = downloaded[0]
        progress.files_downloaded += 1
        progress.log(f"[{progress.files_downloaded}/{progress.files_to_download}] Downloaded: {result.name}")
        return result

    async def upload_file(self, credentials: dict[str, Any], local_path: Path, target_ref: dict[str, Any], progress: JobState) -> dict[str, Any]:
        raise ProviderFailure("NOT_SUPPORTED", "Links provider does not support upload")

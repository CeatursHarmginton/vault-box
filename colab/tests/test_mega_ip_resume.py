from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

try:
    import src.providers.base as base_mod
    from src.jobs.progress import JobCancelled, JobState
    from src.providers.base import download_with_retry
    from src.providers.links import LinksProvider
except ImportError:
    import colab.src.providers.base as base_mod  # type: ignore[no-redef]
    from colab.src.jobs.progress import JobCancelled, JobState  # type: ignore[no-redef]
    from colab.src.providers.base import download_with_retry  # type: ignore[no-redef]
    from colab.src.providers.links import LinksProvider  # type: ignore[no-redef]


def _mega_ctr_encrypt(plain: bytes, key: bytes, iv: bytes) -> bytes:
    enc = Cipher(algorithms.AES(key), modes.CTR(iv), backend=default_backend()).encryptor()
    return enc.update(plain) + enc.finalize()


def _parse_mega_range_url(req_url: str, total_size: int) -> tuple[int, int]:
    m = re.search(r"/(\d+)-(\d*)$", req_url)
    if not m:
        return 0, total_size - 1
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else (total_size - 1)
    return start, min(total_size - 1, end)


class _FakeStreamResponse:
    def __init__(self, status_code: int, body: bytes = b"", cut_after: int | None = None, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._body = body
        self._cut_after = cut_after
        self.headers = headers or {"content-type": "application/octet-stream", "content-length": str(len(body))}

    async def __aenter__(self) -> "_FakeStreamResponse":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=None, response=None)  # type: ignore[arg-type]

    async def aiter_bytes(self, chunk_size: int = 131072):
        sent = 0
        pos = 0
        while pos < len(self._body):
            piece = self._body[pos : pos + min(chunk_size, 65536)]
            pos += len(piece)
            if self._cut_after is not None and sent + len(piece) > self._cut_after:
                rem = self._cut_after - sent
                if rem > 0:
                    yield piece[:rem]
                raise httpx.ReadError("Simulated mid-chunk bandwidth cutoff")
            sent += len(piece)
            yield piece


class _FakeClient:
    def __init__(self, label: str, handler: Any) -> None:
        self.label = label
        self._handler = handler
        self.is_closed = False

    def stream(self, method: str, url: str, headers: dict[str, str] | None = None) -> _FakeStreamResponse:
        return self._handler(self.label, url, headers or {})

    async def aclose(self) -> None:
        self.is_closed = True


def test_mega_ip_switch_mid_chunk_resumes_exact_missing_bytes_and_matches_sha256(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 5.25 MB + 19 odd tail bytes -> 3 chunks of 2 MB (0..2MB, 2MB..4MB, 4MB..end)
    total_size = (5 * 1024 * 1024) + (256 * 1024) + 19
    plaintext = os.urandom(total_size)
    expected_sha256 = hashlib.sha256(plaintext).hexdigest()
    file_key = bytes.fromhex("0123456789abcdef0123456789abcdef")
    file_iv = bytes.fromhex("fedcba98765432100000000000000000")
    ciphertext = _mega_ctr_encrypt(plaintext, file_key, file_iv)

    monkeypatch.setattr(LinksProvider, "_mega_direct_cooldown_until", 0.0)
    monkeypatch.setattr(LinksProvider, "_mega_direct_was_throttled", False)
    monkeypatch.setattr(LinksProvider, "_tor_newnym_ts", {})

    provider = LinksProvider()
    requested_ranges: list[tuple[str, int, int, str]] = []
    newnym_calls: list[str | None] = []
    refreshed_urls: list[str | None] = []
    tor_seq = [0]

    def handle_stream(client_label: str, req_url: str, _hdrs: dict[str, str]) -> _FakeStreamResponse:
        start, end = _parse_mega_range_url(req_url, total_size)
        requested_ranges.append((client_label, start, end, req_url))
        slice_bytes = ciphertext[start : end + 1]

        if client_label == "direct":
            # Direct IP finishes chunk 0 completely, cuts off mid-chunk 1 at non-16-aligned 700,005 bytes,
            # and returns 509 on chunk 2 (and any subsequent direct attempt).
            if start == 0:
                return _FakeStreamResponse(200, slice_bytes)
            if start == 2 * 1024 * 1024:
                return _FakeStreamResponse(200, slice_bytes, cut_after=700_005)
            return _FakeStreamResponse(509)

        if client_label in ("tor_bad_1", "tor_bad_2", "tor_bad_3", "tor_bad_4"):
            return _FakeStreamResponse(509)

        # Clean Tor exit IPs serve remaining requested ranges cleanly
        return _FakeStreamResponse(200, slice_bytes)

    direct_client = _FakeClient("direct", handle_stream)

    async def fake_ensure_tor_pool(progress: Any = None) -> list[str]:
        return ["socks5://127.0.0.1:9050", "socks5://127.0.0.1:9051"]

    def fake_acquire_circuit(daemons: list[str]):
        tor_seq[0] += 1
        idx = tor_seq[0]
        lbl = f"tor_bad_{idx}" if idx <= 4 else f"tor_good_{idx}"
        px = f"socks5://vb_{idx}:pass@127.0.0.1:{9050 + (idx % 2)}"
        return px, _FakeClient(lbl, handle_stream)  # type: ignore[return-value]

    async def fake_rotate_newnym(progress: Any = None, proxy_hint: str | None = None) -> bool:
        newnym_calls.append(proxy_hint)
        return True

    async def fake_refresh_url(px_hint: str | None = None) -> str:
        refreshed_urls.append(px_hint)
        return "https://gfs2.mega.co.nz/dl/ticket_v2"

    monkeypatch.setattr(base_mod, "shared_client", lambda *a, **kw: direct_client)
    monkeypatch.setattr(provider, "_ensure_mega_tor_pool", fake_ensure_tor_pool)
    monkeypatch.setattr(provider, "_acquire_mega_circuit", fake_acquire_circuit)
    monkeypatch.setattr(provider, "_rotate_tor_newnym", fake_rotate_newnym)

    async def fast_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    dest = tmp_path / "big_video.m4v"
    job = JobState("test-mega-ip-switch", {})
    job.start_file(dest.name, phase="download", size=total_size)

    async def _run() -> Path:
        return await provider._download_mega_stream_decrypt(
            "https://gfs1.mega.co.nz/dl/ticket_v1",
            dest,
            file_key,
            file_iv,
            total_size,
            job,
            refresh_url=fake_refresh_url,
        )

    out_path = asyncio.run(_run())
    assert out_path == dest
    assert out_path.is_file()
    assert hashlib.sha256(out_path.read_bytes()).hexdigest() == expected_sha256

    # Checkpoint and .part files must be cleaned up upon completion
    assert not (tmp_path / "big_video.m4v.part").exists()
    assert not (tmp_path / ".big_video.m4v.ckpt.part").exists()

    # Chunk 0 (start == 0) must only be downloaded ONCE (never re-downloaded after switching to Tor!)
    chunk0_starts = [r for r in requested_ranges if r[1] == 0]
    assert len(chunk0_starts) == 1

    # Chunk 1 cut off at 700,005 bytes -> aligned to 16 bytes = 699,984 bytes -> resumed at exact offset!
    expected_chunk1_resume = (2 * 1024 * 1024) + (700_005 // 16) * 16
    assert any(r[1] == expected_chunk1_resume for r in requested_ranges)

    # Tor NEWNYM rotation was invoked when Tor exits returned 509
    assert len(newnym_calls) >= 1
    assert job.bytes_done == total_size


def test_mega_outer_retry_preserves_part_checkpoint_across_many_rounds_without_double_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 6.5 MB -> 4 chunks of 2 MB: [0..2M), [2M..4M), [4M..6M), [6M..6.5M)
    total_size = (6 * 1024 * 1024) + (512 * 1024)
    plaintext = os.urandom(total_size)
    expected_sha256 = hashlib.sha256(plaintext).hexdigest()
    file_key = bytes.fromhex("abcdef0123456789abcdef0123456789")
    file_iv = bytes.fromhex("11223344556677880000000000000000")
    ciphertext = _mega_ctr_encrypt(plaintext, file_key, file_iv)

    monkeypatch.setattr(LinksProvider, "_mega_direct_cooldown_until", 0.0)
    monkeypatch.setattr(LinksProvider, "_mega_direct_was_throttled", False)

    provider = LinksProvider()
    round_budget = [0]
    completed_starts_seen: list[int] = []

    def handle_stream(_label: str, req_url: str, _hdrs: dict[str, str]) -> _FakeStreamResponse:
        start, end = _parse_mega_range_url(req_url, total_size)
        if round_budget[0] <= 0:
            return _FakeStreamResponse(509)
        completed_starts_seen.append(start)
        slice_bytes = ciphertext[start : end + 1]
        if len(slice_bytes) <= round_budget[0]:
            round_budget[0] -= len(slice_bytes)
            return _FakeStreamResponse(200, slice_bytes)
        allow = round_budget[0]
        round_budget[0] = 0
        return _FakeStreamResponse(200, slice_bytes, cut_after=allow)

    fake_cli = _FakeClient("tor_rot", handle_stream)

    async def fake_ensure_tor_pool(progress: Any = None) -> list[str]:
        if round_budget[0] <= 0:
            return []
        return ["socks5://127.0.0.1:9050"]

    monkeypatch.setattr(base_mod, "shared_client", lambda *a, **kw: fake_cli)
    monkeypatch.setattr(provider, "_ensure_mega_tor_pool", fake_ensure_tor_pool)
    monkeypatch.setattr(provider, "_acquire_mega_circuit", lambda daemons: ("socks5://127.0.0.1:9050", fake_cli))

    async def fast_sleep(_s: float, *_a: Any) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(base_mod, "sleep_cancellable", fast_sleep)

    dest = tmp_path / "huge_episode.m4v"
    job = JobState("test-mega-outer-retry", {})
    job.start_file(dest.name, phase="download", size=total_size)

    call_count = [0]

    async def attempt_download() -> Path:
        call_count[0] += 1
        LinksProvider._mega_direct_cooldown_until = 0.0
        round_budget[0] = (1024 * 1024) + (128 * 1024) + 7
        if call_count[0] > 1:
            assert (tmp_path / "huge_episode.m4v.part").is_file()
            assert (tmp_path / ".huge_episode.m4v.ckpt.part").is_file()
        return await provider._download_mega_stream_decrypt(
            "https://gfs1.mega.co.nz/dl/ep",
            dest,
            file_key,
            file_iv,
            total_size,
            job,
        )

    out_path = asyncio.run(download_with_retry(attempt_download, progress=job, label=dest.name, retries=3, delay=0.01))

    assert call_count[0] >= 6  # More rounds than retries=3/5, succeeded because every round made forward progress!
    assert out_path == dest
    assert hashlib.sha256(out_path.read_bytes()).hexdigest() == expected_sha256

    # Byte accounting must never double-count resumed chunks
    assert job.bytes_done == total_size
    assert job._phase_done == total_size
    assert job.active_files[dest.name]["bytes_done"] == total_size

    # Offset 0 must only have been fetched once on round 1!
    assert completed_starts_seen.count(0) == 1
    assert any("Tiếp tục tải nối tiếp" in line for line in job.logs)


def test_mega_cancel_cleans_up_part_and_checkpoint_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    total_size = 4 * 1024 * 1024
    plaintext = os.urandom(total_size)
    file_key = b"\x11" * 16
    file_iv = b"\x22" * 8 + b"\x00" * 8
    ciphertext = _mega_ctr_encrypt(plaintext, file_key, file_iv)

    monkeypatch.setattr(LinksProvider, "_mega_direct_cooldown_until", 0.0)
    provider = LinksProvider()
    job = JobState("test-mega-cancel", {})

    def handle_stream(_label: str, req_url: str, _hdrs: dict[str, str]) -> _FakeStreamResponse:
        start, end = _parse_mega_range_url(req_url, total_size)
        job.cancel = True
        return _FakeStreamResponse(200, ciphertext[start : end + 1])

    monkeypatch.setattr(base_mod, "shared_client", lambda *a, **kw: _FakeClient("direct", handle_stream))

    dest = tmp_path / "cancelled_video.m4v"
    with pytest.raises(JobCancelled):
        asyncio.run(
            provider._download_mega_stream_decrypt(
                "https://gfs1.mega.co.nz/dl/cancel",
                dest,
                file_key,
                file_iv,
                total_size,
                job,
            )
        )

    assert not dest.exists()
    assert not (tmp_path / "cancelled_video.m4v.part").exists()
    assert not (tmp_path / ".cancelled_video.m4v.ckpt.part").exists()


def test_tor_newnym_sends_control_signal_and_preserves_other_daemon_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LinksProvider, "_tor_newnym_ts", {})
    monkeypatch.setattr(LinksProvider, "_tor_newnym_notice_ts", 0.0)
    provider = LinksProvider()
    received_cmds: list[bytes] = []

    async def _scenario() -> None:
        async def handle_ctrl(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            data = await reader.read(256)
            received_cmds.append(data)
            writer.write(b"250 OK\r\n250 OK\r\n250 closing connection\r\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle_ctrl, "127.0.0.1", 0)
        ctrl_port = server.sockets[0].getsockname()[1]
        real_open = asyncio.open_connection

        async def redirect_open(host: str, port: int, *a: Any, **kw: Any):
            if port == 9061:
                return await real_open(host, ctrl_port, *a, **kw)
            raise OSError("port closed")

        orig_open = asyncio.open_connection
        asyncio.open_connection = redirect_open  # type: ignore[assignment]
        try:
            pool = provider._get_mega_circuit_pool()
            cli_9050 = _FakeClient("keep_9050", lambda *a: _FakeStreamResponse(200))
            cli_9051 = _FakeClient("drop_9051", lambda *a: _FakeStreamResponse(509))
            pool["idle"] = [
                ("socks5://vb_a:pass@127.0.0.1:9050", cli_9050),
                ("socks5://vb_b:pass@127.0.0.1:9051", cli_9051),
            ]
            job = JobState("test-newnym", {})
            ok = await provider._rotate_tor_newnym(job, "socks5://vb_b:pass@127.0.0.1:9051")
            assert ok is True
            assert any(b"SIGNAL NEWNYM" in cmd for cmd in received_cmds)
            assert cli_9051.is_closed is True
            assert cli_9050.is_closed is False
            assert len(pool["idle"]) == 1
            assert "9050" in pool["idle"][0][0]
        finally:
            asyncio.open_connection = orig_open  # type: ignore[assignment]
            server.close()
            await server.wait_closed()

    asyncio.run(_scenario())

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from .config import COLAB_RELAY_ROOM_ID, COLAB_RELAY_TOKEN, COLAB_RELAY_URL, ENV_NAME, PUBLIC_URL
from .jobs.job_manager import JobManager
from .ai_subtitles import (
    ColabAISetupRequest,
    ColabAISubtitleStreamRequest,
    ColabAITranslateRequest,
    colab_ai_cancel,
    colab_ai_health,
    colab_ai_setup,
    colab_ai_subtitles_stream,
    colab_ai_translate,
)

TERMINAL = {"completed", "failed", "cancelled", "error"}
PROGRESS_POLL_INTERVAL = 0.5
PROGRESS_MIN_SEND_INTERVAL = 1.0
STABLE_CONNECTION_SECONDS = 15.0

def _ws_url() -> str:
    raw = (COLAB_RELAY_URL or "").strip().rstrip("/")
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlsplit(raw)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, "/api/colab-relay/ws", f"room={quote(COLAB_RELAY_ROOM_ID)}&role=colab&token={quote(COLAB_RELAY_TOKEN)}", ""))

def start_colab_relay(manager: JobManager) -> asyncio.Task | None:
    if not (_ws_url() and COLAB_RELAY_ROOM_ID and COLAB_RELAY_TOKEN):
        return None
    return asyncio.create_task(_relay_loop(manager))

async def _job_sync_and_heartbeat_loop(ws, manager: JobManager, monitors: dict[str, asyncio.Task]) -> None:
    """Continuously monitors manager.jobs to detect and stream all new and active jobs."""
    synced_terminal_jobs: set[str] = set()
    heartbeat_tick = 0

    while True:
        try:
            await asyncio.sleep(0.5)
            heartbeat_tick += 1

            # 1. Detect newly created or resumed jobs and launch monitors
            for job_id, job in list(manager.jobs.items()):
                st = str(job.status).lower()
                if st not in TERMINAL:
                    if job_id in synced_terminal_jobs:
                        synced_terminal_jobs.discard(job_id)
                    if job_id not in monitors or monitors[job_id].done():
                        snap_view = job.view()
                        await ws.send(json.dumps({
                            "type": "snapshot",
                            "jobId": job.job_id,
                            "job": snap_view,
                            "environment": ENV_NAME,
                        }))
                        monitors[job_id] = asyncio.create_task(_monitor(ws, manager, job_id, initial_view=snap_view))
                else:
                    if job_id not in synced_terminal_jobs:
                        synced_terminal_jobs.add(job_id)
                        await ws.send(json.dumps({
                            "type": "snapshot",
                            "jobId": job.job_id,
                            "job": job.view(),
                            "environment": ENV_NAME,
                        }))

            # 2. Periodic heartbeat every 5 seconds
            if heartbeat_tick % 10 == 0:
                await ws.send(json.dumps({
                    "type": "heartbeat",
                    "role": "colab",
                    "environment": ENV_NAME,
                    "serverName": f"VaultBox {ENV_NAME} Worker",
                    "activeJobs": manager.active_job_count(),
                    "totalJobs": len(manager.jobs),
                    "timestamp": time.time(),
                }))
        except asyncio.CancelledError:
            break
        except Exception:
            break

async def _relay_loop(manager: JobManager) -> None:
    import websockets

    monitors: dict[str, asyncio.Task] = {}
    heartbeat_task: asyncio.Task | None = None
    backoff = 2.0
    while True:
        connected_at = 0.0
        try:
            async with websockets.connect(
                _ws_url(),
                ping_interval=25,
                ping_timeout=20,
                close_timeout=5,
                max_size=4 * 1024 * 1024,
            ) as ws:
                connected_at = time.monotonic()
                await ws.send(json.dumps({
                    "type": "hello",
                    "role": "colab",
                    "environment": ENV_NAME,
                    "envName": ENV_NAME,
                    "serverName": f"VaultBox {ENV_NAME} Worker",
                    "publicUrl": PUBLIC_URL,
                }))
                await ws.send(json.dumps({
                    "type": "ready",
                    "colabReady": True,
                    "environment": ENV_NAME,
                }))
                print(f"[✓] Successfully connected to Cloudflare Relay room '{COLAB_RELAY_ROOM_ID}' as {ENV_NAME} Worker!", flush=True)

                # Send initial snapshot of active jobs first, then recent terminal jobs (up to 30 total)
                all_jobs = sorted(
                    list(manager.jobs.values()),
                    key=lambda j: (0 if str(j.status).lower() not in TERMINAL else 1, -(j.updated_at or j.created_at or 0)),
                )
                for job in all_jobs[:30]:
                    snap_view = job.view()
                    await ws.send(json.dumps({
                        "type": "snapshot",
                        "jobId": job.job_id,
                        "job": snap_view,
                        "environment": ENV_NAME,
                    }))
                    if str(job.status).lower() not in TERMINAL:
                        monitors[job.job_id] = asyncio.create_task(_monitor(ws, manager, job.job_id, initial_view=snap_view))
                    await asyncio.sleep(0)

                if heartbeat_task and not heartbeat_task.done():
                    heartbeat_task.cancel()
                heartbeat_task = asyncio.create_task(_job_sync_and_heartbeat_loop(ws, manager, monitors))

                async for raw in ws:
                    if connected_at and (time.monotonic() - connected_at) >= STABLE_CONNECTION_SECONDS:
                        backoff = 2.0
                    msg = json.loads(raw)
                    typ = msg.get("type")
                    job_id = str(msg.get("jobId") or "")
                    if typ == "start_transfer":
                        payload = dict(msg.get("payload") or {})
                        payload["jobId"] = job_id or payload.get("jobId")
                        job = manager.start(payload)
                        snap_view = job.view()
                        await ws.send(json.dumps({
                            "type": "snapshot",
                            "jobId": job.job_id,
                            "job": snap_view,
                            "environment": ENV_NAME,
                        }))
                        monitors[job.job_id] = asyncio.create_task(_monitor(ws, manager, job.job_id, initial_view=snap_view))
                    elif typ == "cancel":
                        manager.cancel(job_id)
                    elif typ == "update_options":
                        if msg.get("ai_request"):
                            asyncio.create_task(_handle_ws_ai_request(ws, msg))
                            continue
                        job = manager.get(job_id)
                        if job:
                            raw_opts = msg.get("options") if isinstance(msg.get("options"), dict) else (msg.get("payload") or {})
                            if isinstance(raw_opts, dict):
                                opts = raw_opts.get("options") if isinstance(raw_opts.get("options"), dict) else raw_opts
                                job.update_options(opts)
                                await ws.send(json.dumps({
                                    "type": "progress",
                                    "jobId": job.job_id,
                                    "job": job.view(),
                                    "environment": ENV_NAME,
                                }))
                    elif typ == "confirm":
                        job = manager.get(job_id)
                        if job:
                            if msg.get("action") == "update_options":
                                payload_obj = msg.get("payload") if isinstance(msg.get("payload"), dict) else {}
                                opts = (
                                    msg.get("options")
                                    if isinstance(msg.get("options"), dict)
                                    else (payload_obj.get("options") if isinstance(payload_obj.get("options"), dict) else payload_obj)
                                )
                                if isinstance(opts, dict):
                                    job.update_options(opts)
                                await ws.send(json.dumps({
                                    "type": "progress",
                                    "jobId": job.job_id,
                                    "job": job.view(),
                                    "environment": ENV_NAME,
                                }))
                                continue
                            if msg.get("action") == "retry_upload":
                                inc_target = (
                                    msg.get("target")
                                    if isinstance(msg.get("target"), dict)
                                    else ((msg.get("payload") or {}).get("target") if isinstance(msg.get("payload"), dict) else {})
                                )
                                if isinstance(inc_target, dict) and inc_target.get("credentials"):
                                    from .jobs.transfer_job import apply_retry_target_update
                                    apply_retry_target_update(job, inc_target)
                                    job.log(f"Retry upload with target account: {inc_target.get('accountId') or inc_target.get('account_id') or '-'}")
                            job.confirm_action = msg.get("action")
                            job.confirm_event.set()
                    elif typ == "ai_request":
                        asyncio.create_task(_handle_ws_ai_request(ws, msg))
        except asyncio.CancelledError:
            if heartbeat_task and not heartbeat_task.done():
                heartbeat_task.cancel()
            _stop_monitors(monitors)
            raise
        except Exception as exc:
            print(f"Cloudflare relay disconnected: {exc}", flush=True)
            if heartbeat_task and not heartbeat_task.done():
                heartbeat_task.cancel()
            if connected_at and (time.monotonic() - connected_at) >= STABLE_CONNECTION_SECONDS:
                backoff = 2.0
            jitter = random.uniform(0.0, min(1.5, backoff * 0.25))
            await asyncio.sleep(backoff + jitter)
            backoff = min(backoff * 2.0, 30.0)
        finally:
            if heartbeat_task and not heartbeat_task.done():
                heartbeat_task.cancel()
            _stop_monitors(monitors)

def _stop_monitors(monitors: dict[str, asyncio.Task]) -> None:
    for task in list(monitors.values()):
        task.cancel()
    monitors.clear()


class _JobProgressDelta:
    """Tracks previously sent job fields so periodic progress frames only emit changed slices."""

    _LIGHTWEIGHT_KEYS = (
        "jobId",
        "status",
        "step",
        "progress",
        "phases",
        "currentFile",
        "lastActiveFile",
        "activeFiles",
        "bytesDone",
        "bytesTotal",
        "bytesOverallDone",
        "bytesOverallTotal",
        "bytesCumulative",
        "speed",
        "error",
        "createdAt",
        "updatedAt",
        "filesDownloaded",
        "filesToDownload",
        "filesUploaded",
        "filesSkipped",
        "filesFailed",
        "filesToUpload",
        "confirmAction",
        "targetProvider",
        "targetAccountId",
    )

    def __init__(self) -> None:
        self.initialized = False
        self.sent_logs: list[str] = []
        self.sent_downloaded: set[str] = set()
        self.sent_uploaded: set[str] = set()
        self.sent_file_sizes: dict[str, Any] = {}
        self.sent_item_timings_json: dict[str, str] = {}
        self.sent_json_blobs: dict[str, str] = {}

    def _record_baseline(self, view: dict[str, Any]) -> None:
        self.initialized = True
        self.sent_logs = [str(x) for x in (view.get("logs") or [])]
        self.sent_downloaded = {str(x) for x in (view.get("downloadedFiles") or []) if x}
        self.sent_uploaded = {str(x) for x in (view.get("uploadedFiles") or []) if x}
        self.sent_file_sizes = dict(view.get("fileSizes") or {})
        self.sent_item_timings_json = {
            str(k): json.dumps(v, sort_keys=True)
            for k, v in (view.get("itemTimings") or {}).items()
        }
        for key in ("items", "options", "completedItems", "failedItems", "optimizedFiles", "failedFiles"):
            self.sent_json_blobs[key] = json.dumps(view.get(key), sort_keys=True)

    def seed(self, view: dict[str, Any] | None) -> None:
        if isinstance(view, dict):
            self._record_baseline(view)

    def _diff_logs(self, cur_logs: list[str]) -> list[str] | None:
        if cur_logs == self.sent_logs:
            return None
        if not self.sent_logs:
            self.sent_logs = list(cur_logs)
            return list(cur_logs)
        if len(cur_logs) > len(self.sent_logs) and cur_logs[:len(self.sent_logs)] == self.sent_logs:
            overlap_start = max(0, len(self.sent_logs) - 5)
            delta = cur_logs[overlap_start:]
            self.sent_logs = list(cur_logs)
            return delta
        anchor_len = min(5, len(self.sent_logs))
        anchor = self.sent_logs[-anchor_len:]
        for idx in range(len(cur_logs) - anchor_len, -1, -1):
            if cur_logs[idx:idx + anchor_len] == anchor:
                overlap_start = max(0, idx)
                delta = cur_logs[overlap_start:]
                self.sent_logs = list(cur_logs)
                return delta
        self.sent_logs = list(cur_logs)
        return list(cur_logs[-500:])

    def build_progress_payload(self, view: dict[str, Any]) -> dict[str, Any]:
        if not self.initialized:
            self._record_baseline(view)
            return view

        payload: dict[str, Any] = {k: view.get(k) for k in self._LIGHTWEIGHT_KEYS if k in view}

        cur_logs = [str(x) for x in (view.get("logs") or [])]
        logs_delta = self._diff_logs(cur_logs)
        if logs_delta is not None:
            payload["logs"] = logs_delta

        cur_dl = [str(x) for x in (view.get("downloadedFiles") or []) if x]
        new_dl = [x for x in cur_dl if x not in self.sent_downloaded]
        if new_dl:
            self.sent_downloaded.update(new_dl)
            payload["downloadedFiles"] = new_dl

        cur_ul = [str(x) for x in (view.get("uploadedFiles") or []) if x]
        new_ul = [x for x in cur_ul if x not in self.sent_uploaded]
        if new_ul:
            self.sent_uploaded.update(new_ul)
            payload["uploadedFiles"] = new_ul

        cur_sizes = view.get("fileSizes") or {}
        new_sizes = {k: v for k, v in cur_sizes.items() if self.sent_file_sizes.get(k) != v}
        if new_sizes:
            self.sent_file_sizes.update(new_sizes)
            payload["fileSizes"] = new_sizes

        cur_timings = view.get("itemTimings") or {}
        new_timings: dict[str, Any] = {}
        for k, v in cur_timings.items():
            ks = str(k)
            ser = json.dumps(v, sort_keys=True)
            if self.sent_item_timings_json.get(ks) != ser:
                self.sent_item_timings_json[ks] = ser
                new_timings[k] = v
        if new_timings:
            payload["itemTimings"] = new_timings

        for key in ("items", "options", "completedItems", "failedItems", "optimizedFiles", "failedFiles"):
            val = view.get(key)
            ser = json.dumps(val, sort_keys=True)
            if self.sent_json_blobs.get(key) != ser:
                self.sent_json_blobs[key] = ser
                payload[key] = val

        return payload


async def _monitor(ws, manager: JobManager, job_id: str, initial_view: dict[str, Any] | None = None) -> None:
    delta = _JobProgressDelta()
    if initial_view is not None:
        delta.seed(initial_view)
    last_raw = json.dumps(initial_view, sort_keys=True) if initial_view is not None else ""
    last_lifecycle = (
        (
            initial_view.get("status"),
            initial_view.get("step"),
            initial_view.get("confirmAction"),
            bool(initial_view.get("error")),
        )
        if initial_view is not None
        else None
    )
    last_sent_at = time.monotonic() if initial_view is not None else 0.0
    try:
        while True:
            job = manager.get(job_id)
            if not job:
                return
            is_terminal = str(job.status).lower() in TERMINAL
            if is_terminal:
                view = job.view()
                await ws.send(json.dumps({
                    "type": "done" if job.status == "completed" else "error",
                    "jobId": job_id,
                    "job": view,
                    "environment": ENV_NAME,
                }))
                return
            lifecycle = (
                getattr(job, "status", None),
                getattr(job, "step", None),
                getattr(job, "confirm_action", None),
                bool(getattr(job, "error", None)),
            )
            now = time.monotonic()
            if lifecycle != last_lifecycle or (now - last_sent_at) >= PROGRESS_MIN_SEND_INTERVAL:
                view = job.view()
                raw = json.dumps(view, sort_keys=True)
                if raw != last_raw:
                    last_raw = raw
                    last_lifecycle = lifecycle
                    last_sent_at = now
                    progress_job = delta.build_progress_payload(view)
                    await ws.send(json.dumps({
                        "type": "progress",
                        "jobId": job_id,
                        "job": progress_job,
                        "environment": ENV_NAME,
                    }))
            await asyncio.sleep(PROGRESS_POLL_INTERVAL)
    except asyncio.CancelledError:
        raise
    except Exception:
        return


async def _handle_ws_ai_request(ws, msg: dict) -> None:
    req_id = str(msg.get("requestId") or "")
    action = str(msg.get("action") or "")
    payload = dict(msg.get("payload") or {})
    try:
        if action == "health":
            res = await colab_ai_health()
            await ws.send(json.dumps({"type": "ai_response", "requestId": req_id, "data": res}))
        elif action == "setup":
            res = await colab_ai_setup(ColabAISetupRequest(**payload))
            await ws.send(json.dumps({"type": "ai_response", "requestId": req_id, "data": res}))
        elif action == "translate":
            res = await colab_ai_translate(ColabAITranslateRequest(**payload))
            await ws.send(json.dumps({"type": "ai_response", "requestId": req_id, "data": res}))
        elif action == "subtitles_cancel":
            res = await colab_ai_cancel(payload)
            await ws.send(json.dumps({"type": "ai_response", "requestId": req_id, "data": res}))
        elif action == "subtitles_stream":
            stream_resp = await colab_ai_subtitles_stream(ColabAISubtitleStreamRequest(**payload))
            async for chunk in stream_resp.body_iterator:
                text = chunk.decode("utf-8") if isinstance(chunk, (bytes, bytearray)) else str(chunk)
                await ws.send(json.dumps({"type": "ai_stream_chunk", "requestId": req_id, "sse": text}))
            await ws.send(json.dumps({"type": "ai_stream_done", "requestId": req_id}))
    except Exception as exc:
        try:
            await ws.send(json.dumps({"type": "ai_error", "requestId": req_id, "message": str(exc)}))
        except Exception:
            pass

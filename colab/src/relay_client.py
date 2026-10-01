from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import quote, urlsplit, urlunsplit

from .config import COLAB_RELAY_ROOM_ID, COLAB_RELAY_TOKEN, COLAB_RELAY_URL, ENV_NAME
from .jobs.job_manager import JobManager

TERMINAL = {"completed", "failed", "cancelled", "error"}

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
            await asyncio.sleep(1.0)
            heartbeat_tick += 1

            # 1. Detect newly created or resumed jobs and launch monitors
            for job_id, job in list(manager.jobs.items()):
                st = str(job.status).lower()
                if st not in TERMINAL:
                    if job_id in synced_terminal_jobs:
                        synced_terminal_jobs.discard(job_id)
                    if job_id not in monitors or monitors[job_id].done():
                        await ws.send(json.dumps({
                            "type": "snapshot",
                            "jobId": job.job_id,
                            "job": job.view(),
                            "environment": ENV_NAME,
                        }))
                        monitors[job_id] = asyncio.create_task(_monitor(ws, manager, job_id))
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
            if heartbeat_tick % 5 == 0:
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
    backoff = 2
    while True:
        try:
            async with websockets.connect(_ws_url(), ping_interval=25, ping_timeout=20, close_timeout=5) as ws:
                backoff = 2
                await ws.send(json.dumps({
                    "type": "hello",
                    "role": "colab",
                    "environment": ENV_NAME,
                    "envName": ENV_NAME,
                    "serverName": f"VaultBox {ENV_NAME} Worker",
                }))
                await ws.send(json.dumps({
                    "type": "ready",
                    "colabReady": True,
                    "environment": ENV_NAME,
                }))

                # Send initial snapshot of all existing jobs
                for job in list(manager.jobs.values()):
                    await ws.send(json.dumps({
                        "type": "snapshot",
                        "jobId": job.job_id,
                        "job": job.view(),
                        "environment": ENV_NAME,
                    }))
                    if str(job.status).lower() not in TERMINAL:
                        monitors[job.job_id] = asyncio.create_task(_monitor(ws, manager, job.job_id))

                if heartbeat_task and not heartbeat_task.done():
                    heartbeat_task.cancel()
                heartbeat_task = asyncio.create_task(_job_sync_and_heartbeat_loop(ws, manager, monitors))

                async for raw in ws:
                    msg = json.loads(raw)
                    typ = msg.get("type")
                    job_id = str(msg.get("jobId") or "")
                    if typ == "start_transfer":
                        payload = dict(msg.get("payload") or {})
                        payload["jobId"] = job_id or payload.get("jobId")
                        job = manager.start(payload)
                        monitors[job.job_id] = asyncio.create_task(_monitor(ws, manager, job.job_id))
                    elif typ == "cancel":
                        manager.cancel(job_id)
                    elif typ == "confirm":
                        job = manager.get(job_id)
                        if job:
                            if msg.get("action") == "retry_upload" and isinstance(msg.get("payload"), dict):
                                job.payload.setdefault("target", {}).update((msg["payload"].get("target") or {}))
                            job.confirm_action = msg.get("action")
                            job.confirm_event.set()
        except asyncio.CancelledError:
            if heartbeat_task and not heartbeat_task.done():
                heartbeat_task.cancel()
            _stop_monitors(monitors)
            raise
        except Exception as exc:
            print(f"Cloudflare relay disconnected: {exc}", flush=True)
            if heartbeat_task and not heartbeat_task.done():
                heartbeat_task.cancel()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
        finally:
            if heartbeat_task and not heartbeat_task.done():
                heartbeat_task.cancel()
            _stop_monitors(monitors)

def _stop_monitors(monitors: dict[str, asyncio.Task]) -> None:
    for task in list(monitors.values()):
        task.cancel()
    monitors.clear()

async def _monitor(ws, manager: JobManager, job_id: str) -> None:
    last = ""
    try:
        while True:
            job = manager.get(job_id)
            if not job:
                return
            view = job.view()
            raw = json.dumps(view, sort_keys=True)
            if raw != last:
                last = raw
                await ws.send(json.dumps({
                    "type": "progress",
                    "jobId": job_id,
                    "job": view,
                    "environment": ENV_NAME,
                }))
            if str(job.status).lower() in TERMINAL:
                await ws.send(json.dumps({
                    "type": "done" if job.status == "completed" else "error",
                    "jobId": job_id,
                    "job": view,
                    "environment": ENV_NAME,
                }))
                return
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        raise
    except Exception:
        return

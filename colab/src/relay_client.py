from __future__ import annotations

import asyncio
import json
from urllib.parse import quote, urlsplit, urlunsplit

from .config import COLAB_RELAY_ROOM_ID, COLAB_RELAY_TOKEN, COLAB_RELAY_URL
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

async def _relay_loop(manager: JobManager) -> None:
    import websockets

    monitors: dict[str, asyncio.Task] = {}
    while True:
        try:
            async with websockets.connect(_ws_url(), ping_interval=25, ping_timeout=20) as ws:
                await ws.send(json.dumps({"type": "hello", "role": "colab"}))
                await ws.send(json.dumps({"type": "ready", "colabReady": True}))
                for job in manager.jobs.values():
                    await ws.send(json.dumps({"type": "snapshot", "jobId": job.job_id, "job": job.view()}))
                    if str(job.status).lower() not in TERMINAL:
                        monitors[job.job_id] = asyncio.create_task(_monitor(ws, manager, job.job_id))
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
            raise
        except Exception as exc:
            print(f"Cloudflare relay disconnected: {exc}", flush=True)
            await asyncio.sleep(2)
        finally:
            for task in list(monitors.values()):
                if task.done():
                    monitors = {k: v for k, v in monitors.items() if not v.done()}

async def _monitor(ws, manager: JobManager, job_id: str) -> None:
    last = ""
    while True:
        job = manager.get(job_id)
        if not job:
            return
        view = job.view()
        raw = json.dumps(view, sort_keys=True)
        if raw != last:
            last = raw
            await ws.send(json.dumps({"type": "progress", "jobId": job_id, "job": view}))
        if str(job.status).lower() in TERMINAL:
            await ws.send(json.dumps({"type": "done" if job.status == "completed" else "error", "jobId": job_id, "job": view}))
            return
        await asyncio.sleep(0.5)

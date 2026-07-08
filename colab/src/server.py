from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from .config import PUBLIC_URL, SERVER_TOKEN
from .jobs.job_manager import JobManager
from .providers import PROVIDERS
from .providers.base import ProviderFailure
from .security import require_token

app = FastAPI(title="VaultBox Colab Transfer Worker", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
manager = JobManager()

@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "vaultbox-colab-worker", "version": "2.0.0", "jobs": len(manager.jobs)}

@app.post("/connect/verify", dependencies=[Depends(require_token)])
async def connect_verify() -> dict[str, Any]:
    return {"ok": True, "providers": list(PROVIDERS), "publicUrl": PUBLIC_URL}

@app.get("/providers", dependencies=[Depends(require_token)])
async def providers() -> dict[str, Any]:
    return {"providers": list(PROVIDERS)}

@app.post("/providers/{provider}/validate", dependencies=[Depends(require_token)])
async def validate_provider(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    p = PROVIDERS.get(provider.lower())
    if not p:
        raise HTTPException(status_code=404, detail={"code": "UNKNOWN_PROVIDER", "message": "Unknown provider"})
    try:
        return await p.validate_credentials(payload.get("credentials") or payload)
    except ProviderFailure as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message, "details": exc.details})

@app.post("/transfer/start", dependencies=[Depends(require_token)])
async def transfer_start(payload: dict[str, Any]) -> dict[str, Any]:
    for side in ("source", "target"):
        if (payload.get(side) or {}).get("provider") not in PROVIDERS:
            raise HTTPException(status_code=400, detail={"code": "UNKNOWN_PROVIDER", "message": f"Unknown {side} provider"})
    return manager.start(payload).view()

@app.get("/transfer/status/{job_id}", dependencies=[Depends(require_token)])
async def transfer_status(job_id: str) -> dict[str, Any]:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"code": "JOB_NOT_FOUND", "message": "Job not found"})
    return job.view()

@app.get("/transfer/list", dependencies=[Depends(require_token)])
async def transfer_list() -> dict[str, Any]:
    return {"jobs": manager.list()}

@app.post("/transfer/cancel/{job_id}", dependencies=[Depends(require_token)])
async def transfer_cancel(job_id: str) -> dict[str, Any]:
    if not manager.cancel(job_id):
        raise HTTPException(status_code=404, detail={"code": "JOB_NOT_FOUND", "message": "Job not found"})
    return {"ok": True, "jobId": job_id}

@app.get("/transfer/logs/{job_id}", dependencies=[Depends(require_token)])
async def transfer_logs(job_id: str) -> dict[str, Any]:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"code": "JOB_NOT_FOUND", "message": "Job not found"})
    return {"jobId": job_id, "logs": job.logs}

@app.get("/events/{job_id}", dependencies=[Depends(require_token)])
async def events(job_id: str) -> StreamingResponse:
    async def gen():
        while True:
            job = manager.get(job_id)
            if not job:
                yield "event: error\ndata: {}\n\n"
                return
            yield f"data: {json.dumps(job.view())}\n\n"
            if job.status in {"completed", "failed", "cancelled"}:
                return
            await asyncio.sleep(1)
    return StreamingResponse(gen(), media_type="text/event-stream")

@app.get("/")
async def root() -> dict[str, Any]:
    return {"ok": True, "serverUrl": PUBLIC_URL or "http://127.0.0.1:8000", "connectionToken": SERVER_TOKEN}

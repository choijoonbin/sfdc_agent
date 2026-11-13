"""Loopback API for interactive model sessions and background pilot runs."""

import re

from fastapi import FastAPI, HTTPException, Request
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import store_for_project
from .contracts import PilotRequest
from .kiro_gateway import SessionError, error_response, kiro_lifespan, router
from .storage import read_json

app = FastAPI(title="SFDC Agent", lifespan=kiro_lifespan)
app.include_router(router)
app.add_exception_handler(SessionError, error_response)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])


@app.get("/health")
def health() -> dict:
    return {"status": "ready", "kiroSessionProtocol": 1}


@app.post("/runs", status_code=202)
def create_run(request: Request, pilot: PilotRequest) -> dict:
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Use the local API origin.")
    if pilot.reference_manifest is not None:
        raise HTTPException(422, "Reference manifests are selected via the local CLI.")
    return {"id": store_for_project().enqueue(pilot), "status": "queued"}


def require_run(run_id: str):
    store = store_for_project()
    row = store.get(run_id) if re.fullmatch(r"[a-f0-9]{32}", run_id) else None
    if row is None:
        raise HTTPException(404, "Run not found.")
    return store, row


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    _, row = require_run(run_id)
    return {key: row[key] for key in ("id", "status", "stage", "created_at", "updated_at")}


@app.get("/runs/{run_id}/comparison")
def get_comparison(run_id: str) -> dict:
    store, _ = require_run(run_id)
    path = store.directory(run_id) / "comparison.json"
    if not path.is_file():
        raise HTTPException(409, "Comparison is not available yet.")
    return read_json(path)

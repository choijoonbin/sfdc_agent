"""Loopback API for interactive model sessions and background pilot runs."""

import re
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import store_for_project
from .contracts import PilotRequest
from .design_candidate import router as design_candidate_router
from .kiro_gateway import SessionError, error_response, kiro_lifespan, kiro_mode, router
from .managed_execution import router as managed_execution_router
from .kiro_readiness import router as kiro_readiness_router
from .managed_figma_scope import FigmaProviderScopeError, figma_scope_response
from .managed_source_reference import router as managed_source_reference_router
from .managed_source_revisions import router as managed_source_revisions_router
from .managed_prompts import router as managed_prompts_router
from .managed_visual import router as managed_visual_router
from .managed_prompt_observability import router as managed_prompt_observability_router
from .managed_prompt_translation import router as managed_prompt_translation_router
from .managed_design import router as managed_design_router
from .managed_views import router as managed_views_router
from .managed_execution_progress import execution_lifespan, router as managed_execution_progress_router
from .prompt_api import router as prompt_router
from .prompt_registry import RegistryError, runtime_release
from .storage import read_json
from .service_mode import ManagedModeBoundary, managed_only, trusted_hosts

@asynccontextmanager
async def lifespan(app):
    async with kiro_lifespan(app), execution_lifespan(app):
        yield


app = FastAPI(title="SFDC Agent", lifespan=lifespan)
app.include_router(router)
app.include_router(prompt_router)
app.include_router(design_candidate_router)
app.include_router(managed_execution_router)
app.include_router(kiro_readiness_router)
app.include_router(managed_source_reference_router)
app.include_router(managed_source_revisions_router)
app.include_router(managed_prompts_router)
app.include_router(managed_visual_router)
app.include_router(managed_prompt_observability_router)
app.include_router(managed_prompt_translation_router)
app.include_router(managed_design_router)
app.include_router(managed_views_router)
app.include_router(managed_execution_progress_router)
app.add_exception_handler(SessionError, error_response)
app.add_exception_handler(FigmaProviderScopeError, figma_scope_response)
app.add_middleware(ManagedModeBoundary)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts())


@app.get("/health")
def health() -> dict:
    if managed_only():
        return {"status": "ready", "mode": "managed", "kiroSessionProtocol": 1,
                "screenKiroMode": kiro_mode("screen"), "storyKiroMode": kiro_mode("story")}
    try:
        release_id = runtime_release().id
    except (RegistryError, sqlite3.Error):
        raise HTTPException(503, "The active prompt release is unavailable.") from None
    return {"status": "ready", "kiroSessionProtocol": 1, "screenKiroMode": kiro_mode("screen"),
            "storyKiroMode": kiro_mode("story"), "promptReleaseId": release_id}


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

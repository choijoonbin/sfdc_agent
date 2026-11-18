"""Bounded service-only approval of an explicitly pinned official Figma export."""

import asyncio
import os
import tempfile
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse

from .managed_execution import (
    _WORKER_SLOTS, _decode_json, _encode_json, _run_process, _runner_configuration,
    _wait_for_disconnect, _finish_owned_cleanup, _cancel_request_tasks, engine_root, IncompleteExecution,
)
from .managed_prompts import _authorize
from .managed_figma_scope import require_figma_scope

router = APIRouter(prefix="/internal/v1", include_in_schema=False)


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenantId: UUID
    workspaceId: UUID


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: str = Field(min_length=1, max_length=2048)
    executionScope: Scope


async def _approve(payload):
    require_figma_scope(payload.executionScope)
    binary, _, _ = _runner_configuration()
    runner = (engine_root() / "nextbss/server/managed-source-reference.mjs").resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="sfdc-source-reference-") as scratch:
        output = await _run_process(binary, runner, scratch, 90,
            _encode_json(payload.model_dump(mode="json")),
            {"FIGMA_PLAN_ACCESS_TOKEN": os.environ.get("FIGMA_PLAN_ACCESS_TOKEN", "")}, output_limit=23 * 1024 * 1024)
    require_figma_scope(payload.executionScope)
    if len(output) > 23 * 1024 * 1024:
        raise IncompleteExecution
    response = _decode_json(output)
    if not isinstance(response, dict):
        raise IncompleteExecution
    if "code" in response:
        code = response.get("code")
        allowed = {"FIGMA_PLAN_TOKEN_REQUIRED", "FIGMA_TOKEN_REQUIRED", "FIGMA_RATE_LIMITED", "FIGMA_RATE_LIMIT",
            "FIGMA_IMAGE_VERSION_REQUIRED", "FIGMA_SOURCE_MISMATCH", "FIGMA_IMAGE_SIZE_MISMATCH"}
        code = code if code in allowed else "FIGMA_REFERENCE_UNAVAILABLE"
        rate_limited = code in {"FIGMA_RATE_LIMITED", "FIGMA_RATE_LIMIT"}
        if rate_limited:
            seconds = response.get("retryAfterSeconds", 60)
            seconds = seconds if type(seconds) is int and 1 <= seconds <= 3600 else 60
            return JSONResponse({"code": "FIGMA_RATE_LIMITED", "retryAfterSeconds": seconds},
                status_code=429, headers={"Retry-After": str(seconds)})
        return JSONResponse({"code": code}, status_code=503 if code.endswith("TOKEN_REQUIRED") else 422)
    result = response.get("result")
    if not isinstance(result, dict) or result.get("provenance") != "OFFICIAL_FIGMA_EXPORT":
        raise IncompleteExecution
    return result


@router.post("/source-reference")
async def approve_source_reference(request: Request):
    _authorize(request)
    body = bytearray()
    try:
        async with asyncio.timeout(15):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 8192:
                    raise HTTPException(413, "Source reference request is too large.")
        payload = Approval.model_validate(_decode_json(body))
    except (ValidationError, ValueError, RecursionError):
        raise HTTPException(422, "Invalid source reference request.") from None
    except TimeoutError:
        raise HTTPException(408, "Source reference request timed out.") from None
    except ClientDisconnect:
        raise HTTPException(499, "Source reference request disconnected.") from None
    require_figma_scope(payload.executionScope)
    if not _WORKER_SLOTS.acquire(blocking=False):
        return JSONResponse({"code": "EXECUTION_BUSY"}, status_code=503, headers={"Retry-After": "5"})
    tasks = ()
    try:
        execution = asyncio.create_task(_approve(payload))
        disconnect = asyncio.create_task(_wait_for_disconnect(request))
        tasks = (execution, disconnect)
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if execution.done():
            return execution.result()
        raise HTTPException(499, "Source reference request disconnected.")
    except TimeoutError:
        raise HTTPException(504, "Source reference request timed out.") from None
    except (IncompleteExecution, ValueError, OSError, RecursionError):
        raise HTTPException(422, "The official Figma export could not be verified.") from None
    finally:
        try:
            await _finish_owned_cleanup(_cancel_request_tasks(tasks))
        finally:
            _WORKER_SLOTS.release()

"""Bounded offline deterministic revisions of Spring-owned immutable native results."""

import asyncio
import base64
from hashlib import sha256
import json
from pathlib import Path
import re
import tempfile
from time import time
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse

from .config import engine_root
from .managed_execution import (
    IncompleteExecution, _WORKER_SLOTS, _decode_json, _encode_json, _run_process,
    _runner_configuration, _wait_for_disconnect, _finish_owned_cleanup, _cancel_request_tasks,
)
from .managed_execution_progress import ExecutionScope
from .managed_prompts import _authorize
from .managed_execution import ArchiveSelection
from .prompt_release_id import PromptReleaseId
from .component_library import ComponentLibrary
from .managed_object_storage import ManagedObjectStorage, storage_io

router = APIRouter(prefix="/internal/v1", include_in_schema=False)
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Label = Annotated[str, Field(min_length=1, max_length=160)]
Object = dict[str, JsonValue]
_INPUT_LIMIT = 24 * 1024 * 1024
_OUTPUT_LIMIT = 12 * 1024 * 1024
_ARTIFACT_LIMIT = 8 * 1024 * 1024
_PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*")


class DesignPin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sourceId: Annotated[str, Field(pattern=r"^nextbss-\d+(?:\.\d+){1,2}-[a-f0-9]{64}$")]
    version: Label | None = None
    manifestSha256: Digest


class PromptPin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sourceId: PromptReleaseId
    version: Label | None = None
    manifestSha256: Digest


class Pins(BaseModel):
    model_config = ConfigDict(extra="forbid")
    design: DesignPin
    prompt: PromptPin
    maxConcurrentJobs: Annotated[int, Field(strict=True, ge=1, le=1000)]
    timeoutSeconds: Annotated[int, Field(strict=True, ge=1, le=3600)]


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    digest: Digest
    bytes: Annotated[str, Field(min_length=1, max_length=12 * 1024 * 1024)]


class InputArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(min_length=1, max_length=240)]
    mediaType: Literal["application/octet-stream", "image/png", "application/zip"]
    sha256: Digest
    base64: Annotated[str, Field(max_length=_ARTIFACT_LIMIT * 2)]


class OutputArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(min_length=1, max_length=240)]
    mediaType: Literal["application/octet-stream", "image/png", "application/zip"]
    base64: Annotated[str, Field(max_length=_ARTIFACT_LIMIT * 2)]


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revisionId: UUID
    parentResultId: UUID
    parentDigest: Digest
    adjustmentKind: Literal["RUNTIME_LAYOUT", "SURFACE"]
    producerKind: Literal["DETERMINISTIC_SOURCE_ADJUSTMENT"]
    modelCalls: Annotated[int, Field(strict=True, ge=0, le=0)]
    executionScope: ExecutionScope
    kind: Literal["SCREEN"]
    engine: Literal["NOVA"]
    sourceReference: Annotated[str, Field(min_length=1, max_length=2048)]
    pins: Pins
    view: Object
    metadata: Object
    analysisViewId: UUID
    analysisPayloadSha256: Digest
    analysisView: Object
    analysisCheckpoint: Checkpoint
    archive: ArchiveSelection | None = None
    deadlineEpochSeconds: Annotated[int, Field(strict=True, gt=0)] | None = None
    artifacts: Annotated[list[InputArtifact], Field(min_length=1, max_length=400)]

    @model_validator(mode="after")
    def verify_owned_inputs(self):
        if self.revisionId == self.parentResultId:
            raise ValueError("A revision requires a distinct candidate identity.")
        if self.archive and (self.archive.tenantId != self.executionScope.tenantId
                             or self.archive.workspaceId != self.executionScope.workspaceId):
            raise ValueError("The archive belongs to a different scope.")
        ComponentLibrary.model_validate(self.metadata.get("componentLibrary"))
        files = _artifact_bytes(self.artifacts, input_hashes=True)
        if {"result.json", "managed-original-view.json", "managed-result-meta.json"}.intersection(files):
            raise ValueError("Parent view and metadata travel in their explicit server-owned fields.")
        decoded = base64.b64decode(self.analysisCheckpoint.bytes, validate=True)
        if len(decoded) > _ARTIFACT_LIMIT or sha256(decoded).hexdigest() != self.analysisCheckpoint.digest:
            raise ValueError("The immutable analysis checkpoint changed.")
        return self


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    revisionId: UUID
    parentResultId: UUID
    parentDigest: Digest
    adjustmentKind: Literal["RUNTIME_LAYOUT", "SURFACE"]
    source: Object
    analysisViewId: UUID
    analysisPayloadSha256: Digest
    componentLibrary: ComponentLibrary
    compiled: Annotated[bool, Field(strict=True)]
    evidence: Object
    view: Object


class RevisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["COMPLETED", "UNCHANGED"]
    revisionId: UUID
    parentResultId: UUID
    parentDigest: Digest
    producerKind: Literal["DETERMINISTIC_SOURCE_ADJUSTMENT"]
    modelCalls: Annotated[int, Field(strict=True, ge=0, le=0)]
    ruleDigest: Digest
    receipt: Receipt | None = None
    artifacts: Annotated[list[OutputArtifact], Field(max_length=512)]


def _artifact_bytes(artifacts, *, input_hashes=False):
    files, total = {}, 0
    for artifact in artifacts:
        name = artifact.name
        if not _PATH.fullmatch(name) or name.casefold() in files:
            raise ValueError("Invalid artifact path.")
        decoded = base64.b64decode(artifact.base64, validate=True)
        total += len(decoded)
        if total > _ARTIFACT_LIMIT or input_hashes and sha256(decoded).hexdigest() != artifact.sha256:
            raise ValueError("Invalid artifact inventory.")
        files[name.casefold()] = decoded
    return files


def _validate_result(payload, value):
    result = RevisionResult.model_validate(value)
    if (result.revisionId, result.parentResultId, result.parentDigest) != (payload.revisionId, payload.parentResultId, payload.parentDigest):
        raise IncompleteExecution
    if result.status == "UNCHANGED":
        if result.artifacts or result.receipt is not None:
            raise IncompleteExecution
        return result
    receipt = result.receipt
    if not result.artifacts or receipt is None or receipt.compiled is not True:
        raise IncompleteExecution
    if (receipt.revisionId, receipt.parentResultId, receipt.parentDigest, receipt.adjustmentKind,
        receipt.analysisViewId, receipt.analysisPayloadSha256) != (
            payload.revisionId, payload.parentResultId, payload.parentDigest, payload.adjustmentKind,
            payload.analysisViewId, payload.analysisPayloadSha256):
        raise IncompleteExecution
    if receipt.source != payload.metadata.get("source") or receipt.componentLibrary.model_dump() != payload.metadata.get("componentLibrary"):
        raise IncompleteExecution
    if receipt.evidence.get("checkpointSha256") != payload.analysisCheckpoint.digest:
        raise IncompleteExecution
    view = receipt.view
    if (view.get("id") != str(payload.revisionId) or view.get("generator") != "nova-deterministic-runtime"
        or view.get("conversionMode") != "deterministic-adjustment" or view.get("canApply") is not True or view.get("applyBlockers") != []):
        raise IncompleteExecution
    for key in ("source", "standard", "dimensions", "analysis", "analysisId"):
        if (key in view) != (key in payload.view) or view.get(key) != payload.view.get(key):
            raise IncompleteExecution
    files = _artifact_bytes(result.artifacts)
    if any(name in {"result.json", "managed-original-view.json", "managed-derived-view.json"} for name in files):
        raise IncompleteExecution
    if not {"managed-deployment.json", "managed-result-meta.json", "managed-preview/manifest.json"}.issubset(files):
        raise IncompleteExecution
    metadata = _decode_json(files["managed-result-meta.json"])
    for key in ("source", "standard", "componentLibrary"):
        if metadata.get(key) != payload.metadata.get(key):
            raise IncompleteExecution
    if metadata.get("eligible") is not True:
        raise IncompleteExecution
    return result


async def run_revision(payload):
    binary, _, physical = _runner_configuration()
    runner = (engine_root() / "nextbss/server/managed-source-revision.mjs").resolve(strict=True)
    if not runner.is_file():
        raise HTTPException(503, "The source revision runner is unavailable.")
    timeout = min(120, physical, payload.pins.timeoutSeconds)
    if payload.deadlineEpochSeconds is not None:
        remaining = payload.deadlineEpochSeconds - time()
        if remaining <= 0:
            raise HTTPException(410, "The source revision deadline expired.")
        if remaining > 125:
            raise HTTPException(422, "Invalid source revision deadline.")
        timeout = min(timeout, remaining)
    # No Figma, MCP, model or service credentials are forwarded. Both local and
    # remote parents use the same immutable recorded native plan offline.
    with tempfile.TemporaryDirectory(prefix="sfdc-source-revision-") as scratch:
        deadline = time() + timeout
        storage = ManagedObjectStorage.configured()
        environment = {}
        if storage:
            environment.update(await storage_io(storage.prepare, scratch, archive=payload.archive,
                release_id=payload.pins.design.sourceId, manifest_sha=payload.pins.design.manifestSha256,
                deadline=deadline))
        remaining = deadline - time() if storage else timeout
        if remaining <= 0:
            raise TimeoutError
        output = await _run_process(binary, runner, scratch, remaining,
            _encode_json(payload.model_dump(mode="json", exclude_none=True)), environment, output_limit=_OUTPUT_LIMIT)
    if len(output) > _OUTPUT_LIMIT:
        raise IncompleteExecution
    value = _decode_json(output)
    if not isinstance(value, dict):
        raise IncompleteExecution
    if "code" in value:
        code = value.get("code")
        allowed = (isinstance(code, str) and re.fullmatch(r"(?:SOURCE_REVISION|SURFACE|RUNTIME_LAYOUT|PINNED_RELEASE|PINNED_COMPONENT_LIBRARY|COMPONENT_LIBRARY|RELEASE_RUNTIME)_[A-Z0-9_]{1,65}", code))
        return JSONResponse({"code": code if allowed else "SOURCE_REVISION_FAILED"}, status_code=422)
    return _validate_result(payload, value)


@router.post("/source-revisions", response_model=RevisionResult, response_model_exclude_none=True)
async def create_source_revision(request: Request):
    _authorize(request)
    body = bytearray()
    try:
        async with asyncio.timeout(15):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > _INPUT_LIMIT:
                    raise HTTPException(413, "Source revision request is too large.")
        payload = RevisionRequest.model_validate(_decode_json(body))
    except (ValidationError, ValueError, RecursionError):
        raise HTTPException(422, "Invalid source revision request.") from None
    except TimeoutError:
        raise HTTPException(408, "Source revision request timed out.") from None
    except ClientDisconnect:
        raise HTTPException(499, "Source revision request disconnected.") from None
    if not _WORKER_SLOTS.acquire(blocking=False):
        return JSONResponse({"code": "EXECUTION_BUSY"}, status_code=503, headers={"Retry-After": "5"})
    tasks = ()
    try:
        execution = asyncio.create_task(run_revision(payload))
        disconnect = asyncio.create_task(_wait_for_disconnect(request))
        tasks = (execution, disconnect)
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if execution.done():
            return execution.result()
        raise HTTPException(499, "Source revision request disconnected.")
    except TimeoutError:
        raise HTTPException(504, "Source revision timed out.") from None
    except (IncompleteExecution, ValueError, OSError, RecursionError):
        raise HTTPException(422, "The deterministic source revision could not be verified.") from None
    finally:
        try:
            await _finish_owned_cleanup(_cancel_request_tasks(tasks))
        finally:
            _WORKER_SLOTS.release()

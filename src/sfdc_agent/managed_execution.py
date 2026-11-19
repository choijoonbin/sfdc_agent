"""Execute a Spring-owned conversion attempt without carrying its service credentials downstream."""

import asyncio
import base64
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import signal
import tempfile
from time import time
from threading import BoundedSemaphore
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from starlette.requests import ClientDisconnect

from .managed_prompts import PromptBundle, execution_prompts, runner_token
from .prompt_release_id import PromptReleaseId
from .managed_execution_progress import MAX_ATTEMPT, ExecutionScope, execution_mode, execution_progress
from .managed_figma_scope import remote_figma_environment, require_figma_scope
from .config import engine_root
from .component_library import ComponentLibrary
from .managed_analysis_output import output_directory, verify as verify_analysis_output
from .kiro_readiness import AUTH_ERROR_CODES
from .managed_object_storage import ManagedObjectStorage, storage_io

router = APIRouter(prefix="/internal/v1", include_in_schema=False)

_REQUEST_BYTES = 16 * 1024 * 1024
_CHECKPOINT_BYTES = 3 * 1024 * 1024
_SUMMARY_BYTES = 256 * 1024
_ARTIFACT_BYTES = 8 * 1024 * 1024
_OUTPUT_BYTES = 12 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024
_WORKER_SLOTS = BoundedSemaphore(2)
_RUNNER_ENVIRONMENT = (
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    "SFDC_MANAGED_RELEASE_ROOT", "SFDC_MANAGED_AGENT_ORIGIN",
    "SFDC_MANAGED_SOURCE_ROOT",
)

ReleasePin = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")]
SourceReference = Annotated[str, Field(min_length=1, max_length=2048, pattern=r"^[^\x00-\x1f\x7f]+$")]
RegistryLabel = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[^\x00-\x1f\x7f]+$")]
ManifestDigest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
JsonObject = dict[str, JsonValue]
_ARTIFACT_PART = re.compile(r'^[^./\\:<>|?*\x00-\x1f\x7f][^/\\:<>|?*\x00-\x1f\x7f]*$')


class ArchiveSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenantId: UUID
    workspaceId: UUID
    fileKey: Annotated[str, Field(pattern=r"^[A-Za-z0-9]{1,160}$")]
    sha256: ManifestDigest


class SourceDiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    executionScope: ExecutionScope
    archive: ArchiveSelection | None = None
    sourceReference: SourceReference | None = None
    pageId: Annotated[str, Field(pattern=r"^\d+:\d+$")] | None = None
    version: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,160}$")] | None = None
    previewNode: Annotated[str, Field(pattern=r"^\d+:\d+$")] | None = None

    @model_validator(mode="after")
    def source_kind(self):
        if bool(self.archive) == bool(self.sourceReference):
            raise ValueError("Select a local archive or a registered remote source.")
        if self.archive and (self.archive.tenantId != self.executionScope.tenantId or self.archive.workspaceId != self.executionScope.workspaceId):
            raise ValueError('The selected archive belongs to a different execution scope.')
        return self


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    jobId: UUID
    attempt: Annotated[int, Field(ge=1, le=MAX_ATTEMPT, strict=True)]
    executionScope: ExecutionScope
    timeoutSeconds: Annotated[int, Field(ge=1, le=3600, strict=True)]
    deadlineEpochSeconds: Annotated[int, Field(gt=0, strict=True)]
    kind: Literal["SCREEN", "STORY"]
    engine: Literal["NOVA", "KIRO"]
    operation: Literal["ANALYZE", "GENERATE"] = "ANALYZE"
    sourceReference: SourceReference
    designReleaseId: ReleasePin
    promptReleaseId: PromptReleaseId
    promptBundle: PromptBundle | None = None
    designManifestSha256: ManifestDigest | None = None
    componentLibrary: ComponentLibrary | None = None
    promptReleaseVersion: RegistryLabel | None = None
    analysisJobId: UUID | None = None
    analysis: JsonObject | None = None
    workflowId: RegistryLabel | None = None
    archive: ArchiveSelection | None = None
    inputMode: Literal['SOURCE', 'MEASURED'] = 'SOURCE'
    sourceVersion: RegistryLabel | None = None

    @model_validator(mode="after")
    def validate_review(self):
        if self.promptBundle is not None and self.promptBundle.releaseId != self.promptReleaseId:
            raise ValueError("Prompt release pin does not match.")
        if self.archive and (self.archive.tenantId != self.executionScope.tenantId
                             or self.archive.workspaceId != self.executionScope.workspaceId):
            raise ValueError("The selected archive belongs to a different execution scope.")
        if self.inputMode == 'MEASURED':
            if self.kind != 'SCREEN' or self.engine != 'NOVA' or not self.archive or self.sourceVersion != 'local-' + self.archive.sha256:
                raise ValueError('Measured input requires the exact registered Screen archive.')
        elif self.sourceVersion is not None:
            raise ValueError('Source version is only supplied for measured input.')
        if self.operation == "GENERATE":
            if self.analysisJobId is None or self.analysisJobId == self.jobId or not self.analysis:
                raise ValueError("Generation requires a prior analysis checkpoint.")
            if len(_encode_json(self.analysis)) > _CHECKPOINT_BYTES:
                raise ValueError("Analysis checkpoint is too large.")
        elif self.analysisJobId is not None or self.analysis is not None or self.workflowId is not None:
            raise ValueError("Analysis starts without a prior checkpoint.")
        return self


class ExecutionArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: Annotated[str, Field(min_length=1, max_length=240)]
    mediaType: Literal["image/png", "application/zip", "application/octet-stream"]
    base64: str


class AnalysisView(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    protocolVersion: Annotated[int, Field(ge=1, le=1, strict=True)]
    manifestSha256: ManifestDigest


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["REVIEW_REQUIRED", "COMPLETED"]
    title: Annotated[str, Field(min_length=1, max_length=240)]
    artifacts: Annotated[list[ExecutionArtifact], Field(max_length=512)]
    summary: JsonObject | None = None
    checkpoint: JsonObject | None = None
    promptTraces: list[JsonObject] | None = None
    analysisView: AnalysisView | None = None


class IncompleteExecution(Exception):
    """Unexpected runner response or process state."""


def _encode_json(value: JsonValue) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> JsonObject:
    value = {}
    for key, member in pairs:
        if key in value:
            raise ValueError("Repeated JSON key.")
        value[key] = member
    return value


def _reject_nonfinite(value: str):
    raise ValueError("Non-finite JSON number.")


def _decode_json(body: bytes | bytearray) -> JsonValue:
    return json.loads(body.decode(), object_pairs_hook=_unique_object, parse_constant=_reject_nonfinite)


def _authorize(request: Request) -> None:
    token = os.environ.get("SFDC_AGENT_EXECUTION_TOKEN")
    if not token or not token.strip():
        raise HTTPException(503, "Managed execution is disabled.")
    headers = request.headers.getlist("authorization")
    scheme, _, provided = (headers[0] if len(headers) == 1 else "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(provided.encode("latin-1"), token.encode()):
        raise HTTPException(401, "Service authentication required.", headers={"WWW-Authenticate": "Bearer"})
    if 'origin' in request.headers or 'sec-fetch-site' in request.headers:
        raise HTTPException(403, 'Service requests only.')


async def _parse_request(request: Request, model=ExecutionRequest):
    body = bytearray()
    try:
        async with asyncio.timeout(15):
            async for chunk in request.stream():
                if len(body) + len(chunk) > _REQUEST_BYTES:
                    raise HTTPException(413, "Managed execution request is too large.")
                body.extend(chunk)
        return model.model_validate(_decode_json(body))
    except (ValueError, RecursionError):
        raise HTTPException(422, "Invalid managed execution request.") from None
    except TimeoutError:
        raise HTTPException(408, "Managed execution request timed out.") from None
    except ClientDisconnect:
        raise HTTPException(499, "Managed execution request disconnected.") from None


def _runner_configuration() -> tuple[str, Path, int]:
    binary = shutil.which(os.environ.get("SFDC_MANAGED_EXECUTION_NODE", "node"))
    runner = Path(os.environ.get("SFDC_MANAGED_EXECUTION_RUNNER", engine_root() / "nextbss/server/managed-execution.mjs"))
    try:
        timeout = int(os.environ.get("SFDC_MANAGED_EXECUTION_TIMEOUT_SECONDS", "540"))
        runner = runner.resolve(strict=True)
        if not binary or not runner.is_file() or not 1 <= timeout <= 900:
            raise ValueError
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(503, "Managed execution runner is unavailable.") from None
    return str(Path(binary).resolve()), runner, timeout


def _execution_budget(payload: ExecutionRequest, physical: int) -> float:
    if payload.timeoutSeconds > physical:
        raise HTTPException(409, "LIMIT_MISMATCH")
    remaining = payload.deadlineEpochSeconds - time()
    if remaining <= 0:
        raise HTTPException(410, "EXECUTION_DEADLINE_EXPIRED")
    if remaining > payload.timeoutSeconds + 30:
        raise HTTPException(422, "EXECUTION_DEADLINE_INVALID")
    return min(payload.timeoutSeconds, remaining)


def _check_kiro_configuration(request: Request, payload: ExecutionRequest) -> None:
    if payload.engine != "KIRO":
        return
    origin = os.environ.get("SFDC_MANAGED_AGENT_ORIGIN", "")
    server = request.scope.get("server")
    local_port = server[1] if server else None
    if not local_port or origin != f"http://127.0.0.1:{local_port}":
        raise HTTPException(503, "Managed Kiro runtime is unavailable.")
    if execution_mode(payload) == 'headless':
        from .kiro_headless import SessionError, read_key
        try:
            read_key()
        except SessionError as error:
            if error.code in AUTH_ERROR_CODES:
                raise HTTPException(502, error.code) from None
            raise


def _check_component_library_configuration(payload: ExecutionRequest) -> None:
    # Null is only the established pre-integration transport representation.
    # Spring classifies new commands explicitly; the Node selected-release guard
    # also fences any unversioned request before model work.
    if payload.componentLibrary is None:
        return
    try:
        path = engine_root() / 'nextbss/engine/managed-runtime-capabilities.json'
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
            raise ValueError
        capabilities = _decode_json(path.read_bytes())
        if capabilities.get('schemaVersion') != 1 or not isinstance(capabilities.get('generationFamilies'), list):
            raise ValueError
        supported = any(item == {'family': payload.componentLibrary.family,
                                 'libraryVersion': payload.componentLibrary.libraryVersion}
                        for item in capabilities['generationFamilies'])
    except (OSError, ValueError, TypeError, AttributeError):
        raise HTTPException(503, 'RUNTIME_CAPABILITIES_UNAVAILABLE') from None
    if not supported:
        raise HTTPException(409, 'RELEASE_RUNTIME_INCOMPATIBLE')


async def _write_input(process: asyncio.subprocess.Process, payload: bytes) -> None:
    try:
        process.stdin.write(payload)
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        process.stdin.close()


async def _read_output(stream: asyncio.StreamReader, limit: int = _OUTPUT_BYTES) -> bytes:
    output = bytearray()
    while chunk := await stream.read(_CHUNK_BYTES):
        if len(output) + len(chunk) > limit:
            raise IncompleteExecution
        output.extend(chunk)
    return bytes(output)


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()
    except ProcessLookupError:
        pass
    # Drain the killed process's pipe in small chunks so the transport can reap it.
    while await process.stdout.read(_CHUNK_BYTES):
        pass
    await process.wait()


async def _finish_owned_cleanup(cleanup):
    task = asyncio.create_task(cleanup)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            pass
    return task.result()


async def _cancel_request_tasks(tasks):
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _run_process(binary: str, runner: Path, scratch: str, timeout: int, payload: bytes,
                       environment_overrides: dict[str, str] | None = None,
                       output_limit: int = _OUTPUT_BYTES) -> bytes:
    if type(output_limit) is not int or not 1 <= output_limit <= 68 * 1024 * 1024:
        raise ValueError('Invalid managed output bound.')
    environment = {key: os.environ[key] for key in _RUNNER_ENVIRONMENT if key in os.environ}
    environment.update({"SFDC_EXECUTION_SCRATCH": scratch, "TMPDIR": scratch, "TEMP": scratch, "TMP": scratch})
    environment.update(environment_overrides or {})
    token = runner_token()
    if token:
        environment["SFDC_MANAGED_PROMPT_CONTEXT"] = token
    opening = asyncio.create_task(asyncio.create_subprocess_exec(
        binary, str(runner), cwd=scratch, env=environment, start_new_session=os.name == "posix",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    ))
    try:
        process = await asyncio.shield(opening)
    except BaseException:
        async def reap_opening():
            try:
                child = await opening
                await _stop_process(child)
            except (OSError, asyncio.CancelledError):
                pass
        await _finish_owned_cleanup(reap_opening())
        raise
    tasks = (asyncio.create_task(_write_input(process, payload)), asyncio.create_task(_read_output(process.stdout, output_limit)))
    finished = False
    try:
        async with asyncio.timeout(timeout):
            _, output = await asyncio.gather(*tasks)
            exit_code = await process.wait()
        finished = True
        if exit_code != 0:
            raise IncompleteExecution
        return output
    finally:
        if not finished:
            async def cleanup():
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await _stop_process(process)
            await _finish_owned_cleanup(cleanup())


def _parse_result(output: bytes, operation: str) -> ExecutionResult:
    result = ExecutionResult.model_validate(_decode_json(output))
    if result.summary is not None and len(_encode_json(result.summary)) > _SUMMARY_BYTES:
        raise IncompleteExecution
    if not result.title.strip():
        raise IncompleteExecution
    if operation == "ANALYZE":
        if result.status != "REVIEW_REQUIRED" or result.artifacts or not result.summary or not result.checkpoint or result.analysisView is None:
            raise IncompleteExecution
        if len(_encode_json(result.checkpoint)) > _CHECKPOINT_BYTES:
            raise IncompleteExecution
        return result
    if result.status != "COMPLETED" or not result.artifacts or result.checkpoint is not None or result.analysisView is not None:
        raise IncompleteExecution
    names = set()
    total = 0
    for artifact in result.artifacts:
        name = artifact.name.casefold()
        parts = artifact.name.split("/")
        if name in names or any(not _ARTIFACT_PART.fullmatch(part) or part != part.strip() or part.endswith(".")
                                for part in parts):
            raise IncompleteExecution
        names.add(name)
        content = base64.b64decode(artifact.base64, validate=True)
        total += len(content)
        empty_css = artifact.mediaType == "application/octet-stream" and artifact.name.endswith(".css")
        if (not content and not empty_css) or total > _ARTIFACT_BYTES:
            raise IncompleteExecution
    return result


async def _execute(payload: ExecutionRequest) -> ExecutionResult:
    provider_environment = remote_figma_environment(payload.executionScope) if not payload.archive and payload.sourceReference.startswith('https://') else {}
    binary, runner, physical = _runner_configuration()
    timeout = _execution_budget(payload, physical)
    directory = None
    keep = False
    context = None
    try:
        storage = ManagedObjectStorage.configured()
        async with asyncio.timeout(timeout):
            with execution_prompts(payload.promptBundle) as traces, tempfile.TemporaryDirectory(prefix="sfdc-execution-") as scratch:
                encoded = _encode_json(payload.model_dump(mode="json", exclude_none=True))
                async with execution_progress(payload, runner_token(), timeout) as context:
                    environment = dict(provider_environment)
                    if storage:
                        environment.update(await storage_io(storage.prepare, scratch, archive=payload.archive,
                            release_id=payload.designReleaseId, manifest_sha=payload.designManifestSha256,
                            deadline=payload.deadlineEpochSeconds))
                    if payload.engine == 'KIRO':
                        environment['SFDC_MANAGED_KIRO_MODE'] = context.mode
                    if payload.operation == 'ANALYZE':
                        if storage:
                            ingress = Path(scratch).resolve(strict=True) / 'ingress'
                            ingress.mkdir(mode=0o700)
                            directory = output_directory(payload, root=ingress)
                        else:
                            directory = output_directory(payload)
                        environment['SFDC_ANALYSIS_OUTPUT'] = str(directory)
                    output = await _run_process(binary, runner, scratch, timeout, encoded, environment)
                    result = _parse_result(output, payload.operation)
                    if directory:
                        verify_analysis_output(directory, payload, result, engine_root().resolve(strict=True))
                        if storage:
                            await storage_io(storage.publish_output, directory,
                                f'analysis/{payload.jobId}/{payload.attempt}', 'manifest.json',
                                result.analysisView.manifestSha256, analysis=True, deadline=payload.deadlineEpochSeconds)
                if time() >= payload.deadlineEpochSeconds:
                    raise TimeoutError
                if not payload.archive and payload.sourceReference.startswith('https://'):
                    require_figma_scope(payload.executionScope)
                keep = True
                return result.model_copy(update={"promptTraces": traces or None})
    except TimeoutError:
        raise HTTPException(504, "Managed execution timed out.") from None
    except (IncompleteExecution, ValueError, OSError, RecursionError):
        if context is not None and context.auth_error in AUTH_ERROR_CODES:
            raise HTTPException(502, context.auth_error) from None
        raise HTTPException(502, "Managed execution did not return a usable result.") from None
    finally:
        if directory is not None and not keep:
            shutil.rmtree(directory, ignore_errors=True)


async def _wait_for_disconnect(request: Request) -> None:
    while (await request.receive())["type"] != "http.disconnect":
        pass


async def _discover_source(payload: SourceDiscoveryRequest):
    environment = remote_figma_environment(payload.executionScope) if payload.sourceReference else {}
    binary, _, _ = _runner_configuration()
    runner = (engine_root() / "nextbss/server/managed-source-discovery.mjs").resolve(strict=True)
    encoded = _encode_json(payload.model_dump(mode="json", exclude_none=True))
    with tempfile.TemporaryDirectory(prefix="sfdc-source-discovery-") as scratch:
        deadline = time() + 90
        storage = ManagedObjectStorage.configured()
        if storage and payload.archive:
            environment.update(await storage_io(storage.prepare, scratch, archive=payload.archive, deadline=deadline))
        remaining = deadline - time() if storage else 90
        if remaining <= 0:
            raise TimeoutError
        output = await _run_process(binary, runner, scratch, remaining, encoded, environment)
    if payload.sourceReference:
        require_figma_scope(payload.executionScope)
    response = _decode_json(output)
    if isinstance(response, dict) and response.get("code") == "FIGMA_TOKEN_REQUIRED":
        return JSONResponse({"code": "FIGMA_TOKEN_REQUIRED"}, status_code=503)
    if isinstance(response, dict) and response.get("code") in {"FIGMA_RATE_LIMITED", "FIGMA_RATE_LIMIT"}:
        return JSONResponse({"code": "FIGMA_RATE_LIMITED"}, status_code=429)
    if not isinstance(response, dict) or not isinstance(response.get("result"), dict):
        raise IncompleteExecution
    return response["result"]


@router.post("/source-discovery")
async def source_discovery(request: Request):
    _authorize(request)
    if not _WORKER_SLOTS.acquire(blocking=False):
        raise HTTPException(503, "Managed execution is busy.", headers={"Retry-After": "5"})
    tasks = ()
    try:
        payload = await _parse_request(request, SourceDiscoveryRequest)
        if payload.sourceReference:
            require_figma_scope(payload.executionScope)
        execution = asyncio.create_task(_discover_source(payload))
        disconnect = asyncio.create_task(_wait_for_disconnect(request))
        tasks = (execution, disconnect)
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if execution.done():
            return execution.result()
        raise HTTPException(499, "Source discovery request disconnected.")
    except TimeoutError:
        raise HTTPException(504, "Source discovery timed out.") from None
    except (IncompleteExecution, ValueError, OSError, RecursionError):
        raise HTTPException(422, "The Figma archive could not be inspected.") from None
    finally:
        try:
            await _finish_owned_cleanup(_cancel_request_tasks(tasks))
        finally:
            _WORKER_SLOTS.release()


@router.post("/executions", response_model=ExecutionResult, response_model_exclude_none=True)
async def execute(request: Request) -> ExecutionResult:
    _authorize(request)
    if not _WORKER_SLOTS.acquire(blocking=False):
        return JSONResponse({'code': 'EXECUTION_BUSY'}, status_code=503, headers={'Retry-After': '5'})
    tasks = ()
    try:
        payload = await _parse_request(request)
        if not payload.archive and payload.sourceReference.startswith('https://'):
            require_figma_scope(payload.executionScope)
        _execution_budget(payload, _runner_configuration()[2])
        _check_component_library_configuration(payload)
        _check_kiro_configuration(request, payload)
        execution = asyncio.create_task(_execute(payload))
        disconnect = asyncio.create_task(_wait_for_disconnect(request))
        tasks = (execution, disconnect)
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if execution.done():
            if execution.cancelled():
                raise HTTPException(499, 'EXECUTION_CANCELLED')
            return execution.result()
        raise HTTPException(499, "Managed execution request disconnected.")
    except HTTPException as failure:
        if isinstance(failure.detail, str) and (failure.detail.startswith('EXECUTION_') or failure.detail in AUTH_ERROR_CODES | {'LIMIT_MISMATCH', 'RELEASE_RUNTIME_INCOMPATIBLE', 'RUNTIME_CAPABILITIES_UNAVAILABLE'}):
            return JSONResponse({'code': failure.detail}, status_code=failure.status_code, headers=failure.headers)
        raise
    finally:
        try:
            await _finish_owned_cleanup(_cancel_request_tasks(tasks))
        finally:
            _WORKER_SLOTS.release()

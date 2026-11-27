"""Execute a Spring-owned conversion attempt without carrying its service credentials downstream."""

import asyncio
import base64
import os
from pathlib import Path
import re
import shutil
import tempfile
from time import time
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from .execution_diagnostics import WorkerExecutionFailure, safe_diagnostic

from .managed_prompts import execution_prompts, runner_token
from .managed_execution_progress import execution_mode, execution_progress
from .managed_figma_scope import remote_figma_environment, require_figma_scope
from .config import engine_root
from .design_core import preserve_core_summary, verify_selected_core
from .managed_analysis_output import output_directory, verify as verify_analysis_output
from .kiro_readiness import AUTH_ERROR_CODES
from .managed_object_storage import ManagedObjectStorage, storage_io

from .strict_json import decode_json as _decode_json, encode_json as _encode_json

# Compatibility exports remain here; internal consumers import the shared modules.
from .managed_contracts import (
    AnalysisView, ArchiveSelection, ExecutionArtifact, ExecutionRequest, ExecutionResult,
    SourceDiscoveryRequest, CHECKPOINT_BYTES as _CHECKPOINT_BYTES,
)
from .managed_http import (
    authorize_request as _authorize, parse_request as _parse_managed_request,
    runner_configuration as _runner_configuration, wait_for_disconnect as _wait_for_disconnect,
)
from .managed_runtime import (
    IncompleteExecution, WORKER_SLOTS as _WORKER_SLOTS, OUTPUT_BYTES as _OUTPUT_BYTES,
    run_process as _run_process, read_output as _read_output, write_input as _write_input,
    stop_process as _stop_process, finish_owned_cleanup as _finish_owned_cleanup,
    cancel_request_tasks as _cancel_request_tasks,
)

router = APIRouter(prefix="/internal/v1", include_in_schema=False)

_REQUEST_BYTES = 16 * 1024 * 1024
_SUMMARY_BYTES = 256 * 1024
_ARTIFACT_BYTES = 8 * 1024 * 1024
_ARTIFACT_PART = re.compile(r'^[^./\\:<>|?*\x00-\x1f\x7f][^/\\:<>|?*\x00-\x1f\x7f]*$')


async def _parse_request(request: Request, model=ExecutionRequest):
    return await _parse_managed_request(request, model, max_bytes=_REQUEST_BYTES)


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
    if payload.componentLibrary is None and payload.sourceAcquisition is None:
        return
    try:
        path = engine_root() / 'nextbss/engine/managed-runtime-capabilities.json'
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
            raise ValueError
        capabilities = _decode_json(path.read_bytes())
        if capabilities.get('schemaVersion') != 1 or not isinstance(capabilities.get('generationFamilies'), list):
            raise ValueError
        supported = payload.componentLibrary is None or any(item == {'family': payload.componentLibrary.family,
                                 'libraryVersion': payload.componentLibrary.libraryVersion}
                        for item in capabilities['generationFamilies'])
        if payload.sourceAcquisition is not None:
            supported = supported and capabilities.get('sourceAcquisitionVersion') == 1 and (
                payload.sourceAcquisition in capabilities.get('sourceAcquisitions', []))
        if payload._screen_contract is not None:
            supported = supported and capabilities.get('screenAnalysisContractVersion') == 1
    except (OSError, ValueError, TypeError, AttributeError):
        raise HTTPException(503, 'RUNTIME_CAPABILITIES_UNAVAILABLE') from None
    if not supported:
        raise HTTPException(409, 'RELEASE_RUNTIME_INCOMPATIBLE')


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


def _requires_remote_figma(payload: ExecutionRequest) -> bool:
    if payload.operation=='GENERATE' and payload.kind=='SCREEN' and payload._screen_contract is not None:
        return False
    return (payload.sourceAcquisition != 'MCP_ONLY' and urlsplit(payload.sourceReference).scheme.lower() == 'https'
            and (payload.sourceAcquisition in {'REST_FIRST','REST_ONLY'} or not payload.archive))


async def _execute(payload: ExecutionRequest) -> ExecutionResult:
    provider_environment = remote_figma_environment(payload.executionScope) if _requires_remote_figma(payload) else {}
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
                        saved_source=payload.operation=='GENERATE' and payload.kind=='SCREEN' and payload._screen_contract is not None
                        environment.update(await storage_io(storage.prepare, scratch, archive=None if saved_source else payload.archive,
                            release_id=payload.designReleaseId, manifest_sha=payload.designManifestSha256,
                            deadline=payload.deadlineEpochSeconds))
                    core = verify_selected_core(release_id=payload.designReleaseId,
                        manifest_sha=payload.designManifestSha256,
                        release_root=environment.get('SFDC_MANAGED_RELEASE_ROOT') or os.environ.get('SFDC_MANAGED_RELEASE_ROOT'),
                        runtime_root=engine_root(), expected=payload.designCore)
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
                    result = preserve_core_summary(result, core)
                    if directory:
                        verify_analysis_output(directory, payload, result, engine_root().resolve(strict=True))
                        if storage:
                            await storage_io(storage.publish_output, directory,
                                f'analysis/{payload.jobId}/{payload.attempt}', 'manifest.json',
                                result.analysisView.manifestSha256, analysis=True, deadline=payload.deadlineEpochSeconds)
                if time() >= payload.deadlineEpochSeconds:
                    raise TimeoutError
                if _requires_remote_figma(payload):
                    require_figma_scope(payload.executionScope)
                keep = True
                return result.model_copy(update={"promptTraces": traces or None})
    except TimeoutError:
        raise HTTPException(504, "Managed execution timed out.") from None
    except WorkerExecutionFailure as failure:
        raise HTTPException(502, failure.diagnostic) from None
    except (IncompleteExecution, ValueError, OSError, RecursionError):
        if context is not None and context.provider_error in AUTH_ERROR_CODES:
            raise HTTPException(502, context.provider_error) from None
        raise HTTPException(502, "Managed execution did not return a usable result.") from None
    finally:
        if directory is not None and not keep:
            shutil.rmtree(directory, ignore_errors=True)


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
        if _requires_remote_figma(payload):
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
        if failure.status_code == 502 and safe_diagnostic(failure.detail) is not None:
            return JSONResponse(failure.detail, status_code=502)
        if isinstance(failure.detail, str) and (failure.detail.startswith('EXECUTION_') or failure.detail in AUTH_ERROR_CODES | {'LIMIT_MISMATCH', 'RELEASE_RUNTIME_INCOMPATIBLE', 'RUNTIME_CAPABILITIES_UNAVAILABLE'}):
            return JSONResponse({'code': failure.detail}, status_code=failure.status_code, headers=failure.headers)
        raise
    finally:
        try:
            await _finish_owned_cleanup(_cancel_request_tasks(tasks))
        finally:
            _WORKER_SLOTS.release()

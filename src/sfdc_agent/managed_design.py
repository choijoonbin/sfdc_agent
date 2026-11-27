"""Build design archives with a service identity and explicit output directory."""

import asyncio
import os
from pathlib import Path
import shutil
import stat
import tempfile
from time import time
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import engine_root
from .managed_execution_progress import ExecutionScope
from .managed_figma_connections import FigmaConnectionProof, arguments as figma_arguments
from .managed_figma_scope import remote_figma_environment, require_figma_scope
from .managed_object_storage import ManagedObjectStorage, storage_io
from .managed_runtime import IncompleteExecution, WORKER_SLOTS as _WORKER_SLOTS, run_process as _run_process
from .managed_http import (
    authorize_request as _authorize,
    parse_request as _parse_request,
    runner_configuration as _runner_configuration,
    wait_for_disconnect as _wait_for_disconnect,
)
from .strict_json import decode_json as _decode_json, encode_json as _encode_json

router = APIRouter(prefix="/internal/v1/design-standards", include_in_schema=False)
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class DesignSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fileKey: Annotated[str, Field(pattern=r"^[A-Za-z0-9]{1,160}$")]
    revision: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,160}$")]
    nodeId: Annotated[str, Field(pattern=r"^\d+:\d+$")] | None = None


class NovaSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commit: Annotated[str, Field(pattern=r"^[a-f0-9]{40}(?:[a-f0-9]{24})?$")]
    treeSha256: Digest


class DesignSources(BaseModel):
    model_config = ConfigDict(extra="forbid")
    guide: DesignSource | None = None
    library: DesignSource | None = None
    nova: NovaSource | None = None

    @model_validator(mode="after")
    def separate_nova_from_figma(self):
        if self.nova is not None and (self.guide is not None or self.library is not None):
            raise ValueError("NOVA Git and Figma source changes require separate candidates.")
        return self


class DesignBuild(BaseModel):
    model_config = ConfigDict(extra="forbid")
    buildId: UUID
    baseReleaseId: Annotated[str, Field(pattern=r"^nextbss-\d+(?:\.\d+){1,2}-[a-f0-9]{64}$")]
    baseManifestSha256: Digest
    designVersion: Annotated[str, Field(pattern=r"^\d+\.\d+(?:\.\d+)?$")]
    sources: DesignSources
    executionScope: ExecutionScope | None = None
    figmaConnection: FigmaConnectionProof | None = None

    @model_validator(mode="after")
    def remote_scope(self):
        # Spring normalizes identical base tuples out of sources. Only actual
        # remote changes receive provider credentials; offline rebuilds do not.
        if (self.sources.guide or self.sources.library) and self.executionScope is None:
            raise ValueError("Remote design sources require an explicit execution scope.")
        return self


class BuildCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Annotated[str, Field(min_length=1, max_length=200)]
    status: Literal["passed", "failed"]
    detail: Annotated[str, Field(max_length=1000)] | None = None
    code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{1,80}$")] | None = None
    httpStatus: Annotated[int, Field(ge=400, le=599, strict=True)] | None = None
    # Match requestFigma's bounded ten-digit delta-seconds parser. Long genuine
    # provider waits must remain visible; this value never schedules a retry.
    retryAfterSeconds: Annotated[int, Field(ge=0, le=9_999_999_999, strict=True)] | None = None


class DesignBuildResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["VALIDATED", "BLOCKED"]
    buildId: UUID
    releaseId: Annotated[str, Field(pattern=r"^nextbss-\d+(?:\.\d+){1,2}-[a-f0-9]{64}$")] | None = None
    manifestSha256: Digest | None = None
    outputManifestSha256: Digest | None = None
    checks: Annotated[list[BuildCheck], Field(max_length=200)]
    compatible: bool
    reproduced: bool


def output_directory(build_id: UUID, *, root=None) -> Path:
    configured = root if root is not None else os.environ.get("SFDC_MANAGED_RELEASE_INGRESS", "")
    if not configured:
        raise HTTPException(503, "Design build ingress is not configured.")
    try:
        root = Path(configured).resolve(strict=True)
        directory = root / str(build_id)
        directory.mkdir(mode=0o700)
        return directory
    except FileExistsError:
        raise HTTPException(409, "The design build already has an output directory.") from None
    except OSError:
        raise HTTPException(503, "Design build ingress is unavailable.") from None


def check_output_directory(directory: Path) -> None:
    total = 0
    count = 0
    for file in directory.rglob("*"):
        metadata = file.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise IncompleteExecution
        if file.is_dir():
            continue
        count += 1
        total += metadata.st_size
        if not stat.S_ISREG(metadata.st_mode) or count > 5000 or total > 100 * 1024 * 1024:
            raise IncompleteExecution


def nova_source_root() -> str:
    """Resolve only the server operator's Git root, never a request-supplied path."""
    configured = os.environ.get("SFDC_NOVA_PROJECT_ROOT", "")
    try:
        root = Path(configured)
        if not configured or not root.is_absolute() or not stat.S_ISDIR(root.lstat().st_mode):
            raise ValueError()
        resolved = root.resolve(strict=True)
        if root != resolved:
            raise ValueError()
        return str(resolved)
    except (OSError, ValueError):
        raise HTTPException(503, "The operator-managed NOVA Git source root is unavailable.") from None


async def run_build(payload: DesignBuild) -> DesignBuildResult:
    remote = bool(payload.sources.guide or payload.sources.library)
    provider = remote_figma_environment(payload.executionScope, **figma_arguments(payload, "DESIGN_BUILD")) if remote else {}
    nova_root = nova_source_root() if payload.sources.nova is not None else None
    binary, _, _ = _runner_configuration()
    runner = engine_root() / "nextbss/server/managed-design-release.mjs"
    if not runner.is_file():
        raise HTTPException(503, "The design build runner is unavailable.")
    directory = None
    keep = False
    try:
        storage = ManagedObjectStorage.configured()
        with tempfile.TemporaryDirectory(prefix="sfdc-design-build-") as scratch:
            if storage:
                ingress = Path(scratch).resolve(strict=True) / 'ingress'
                ingress.mkdir(mode=0o700)
                directory = output_directory(payload.buildId, root=ingress)
            else:
                directory = output_directory(payload.buildId)
            overrides = {"SFDC_DESIGN_BUILD_ID": str(payload.buildId), "SFDC_DESIGN_BUILD_OUTPUT": str(directory),
                         "SFDC_MANAGED_RELEASE_INGRESS": str(directory.parent)}
            deadline = time() + 900
            if storage:
                overrides.update(await storage_io(storage.prepare, scratch, release_id=payload.baseReleaseId,
                    manifest_sha=payload.baseManifestSha256, design_sources=nova_root is None, deadline=deadline))
            if nova_root is not None:
                overrides["SFDC_NOVA_PROJECT_ROOT"] = nova_root
            # This operation reads normal REST source metadata only. It must
            # never inherit a Plan/MCP identity or another workspace's token.
            if remote:
                overrides["FIGMA_ACCESS_TOKEN"] = provider["FIGMA_ACCESS_TOKEN"]
            sources = os.environ.get("SFDC_MANAGED_DESIGN_SOURCES")
            if sources and not storage and nova_root is None:
                overrides["SFDC_MANAGED_DESIGN_SOURCES"] = sources
            remaining = deadline - time() if storage else 900
            if remaining <= 0:
                raise TimeoutError
            output = await _run_process(binary, runner, scratch, remaining,
                                        _encode_json(payload.model_dump(mode="json", exclude_none=True, exclude={"figmaConnection"})), overrides)
            if len(output) > 128 * 1024:
                raise IncompleteExecution
            result = DesignBuildResult.model_validate(_decode_json(output))
            if remote:
                require_figma_scope(payload.executionScope, **figma_arguments(payload, "DESIGN_BUILD"))
            if result.buildId != payload.buildId:
                raise IncompleteExecution
            if result.status == "VALIDATED" and (not result.releaseId or not result.manifestSha256
                                                or not result.outputManifestSha256
                                                or not result.checks or any(check.status != "passed" for check in result.checks)):
                raise IncompleteExecution
            if result.status == "VALIDATED":
                check_output_directory(directory)
                if storage:
                    await storage_io(storage.publish_output, directory, str(payload.buildId),
                        'output-manifest.json', result.outputManifestSha256, deadline=deadline)
                keep = not storage
            return result
    except TimeoutError:
        raise HTTPException(504, "Design build timed out.") from None
    except (IncompleteExecution, ValueError, OSError):
        raise HTTPException(502, "The design build did not return a verified result.") from None
    finally:
        if directory is not None and not keep:
            shutil.rmtree(directory, ignore_errors=True)


@router.post("/builds", response_model=DesignBuildResult, response_model_exclude_none=True)
async def build(request: Request):
    _authorize(request)
    if not _WORKER_SLOTS.acquire(blocking=False):
        raise HTTPException(503, "Managed execution is busy.", headers={"Retry-After": "5"})
    tasks = ()
    try:
        payload = await _parse_request(request, DesignBuild)
        execution = asyncio.create_task(run_build(payload))
        disconnect = asyncio.create_task(_wait_for_disconnect(request))
        tasks = (execution, disconnect)
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if execution.done():
            return execution.result()
        raise HTTPException(499, "Design build disconnected.")
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        _WORKER_SLOTS.release()

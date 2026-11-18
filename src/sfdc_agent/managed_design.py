"""Build design archives with a service identity and explicit output directory."""

import asyncio
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .config import engine_root
from .managed_execution import (
    IncompleteExecution, _WORKER_SLOTS, _authorize, _decode_json, _encode_json,
    _parse_request, _run_process, _runner_configuration, _wait_for_disconnect,
)

router = APIRouter(prefix="/internal/v1/design-standards", include_in_schema=False)
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class DesignSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fileKey: Annotated[str, Field(pattern=r"^[A-Za-z0-9]{1,160}$")]
    revision: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,160}$")]
    nodeId: Annotated[str, Field(pattern=r"^\d+:\d+$")] | None = None


class DesignSources(BaseModel):
    model_config = ConfigDict(extra="forbid")
    guide: DesignSource | None = None
    library: DesignSource | None = None


class DesignBuild(BaseModel):
    model_config = ConfigDict(extra="forbid")
    buildId: UUID
    baseReleaseId: Annotated[str, Field(pattern=r"^nextbss-\d+(?:\.\d+){1,2}-[a-f0-9]{64}$")]
    baseManifestSha256: Digest
    designVersion: Annotated[str, Field(pattern=r"^\d+\.\d+(?:\.\d+)?$")]
    sources: DesignSources


class BuildCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Annotated[str, Field(min_length=1, max_length=200)]
    status: Literal["passed", "failed"]
    detail: Annotated[str, Field(max_length=1000)] | None = None


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


def output_directory(build_id: UUID) -> Path:
    configured = os.environ.get("SFDC_MANAGED_RELEASE_INGRESS", "")
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


async def run_build(payload: DesignBuild) -> DesignBuildResult:
    binary, _, _ = _runner_configuration()
    runner = engine_root() / "nextbss/server/managed-design-release.mjs"
    if not runner.is_file():
        raise HTTPException(503, "The design build runner is unavailable.")
    directory = output_directory(payload.buildId)
    keep = False
    try:
        with tempfile.TemporaryDirectory(prefix="sfdc-design-build-") as scratch:
            overrides = {"SFDC_DESIGN_BUILD_ID": str(payload.buildId), "SFDC_DESIGN_BUILD_OUTPUT": str(directory),
                         "SFDC_MANAGED_RELEASE_INGRESS": str(directory.parent)}
            sources = os.environ.get("SFDC_MANAGED_DESIGN_SOURCES")
            if sources:
                overrides["SFDC_MANAGED_DESIGN_SOURCES"] = sources
            output = await _run_process(binary, runner, scratch, 900,
                                        _encode_json(payload.model_dump(mode="json", exclude_none=True)), overrides)
        if len(output) > 128 * 1024:
            raise IncompleteExecution
        result = DesignBuildResult.model_validate(_decode_json(output))
        if result.buildId != payload.buildId:
            raise IncompleteExecution
        if result.status == "VALIDATED" and (not result.releaseId or not result.manifestSha256
                                            or not result.outputManifestSha256
                                            or not result.checks or any(check.status != "passed" for check in result.checks)):
            raise IncompleteExecution
        if result.status == "VALIDATED":
            check_output_directory(directory)
            keep = True
        return result
    except TimeoutError:
        raise HTTPException(504, "Design build timed out.") from None
    except (IncompleteExecution, ValueError, OSError):
        raise HTTPException(502, "The design build did not return a verified result.") from None
    finally:
        if not keep:
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

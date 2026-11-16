"""Loopback management API for prompt versions, called by the NOVA server."""

import hmac
import os
import sqlite3
import stat
from typing import Annotated

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import project_root
from .prompt_registry import PromptRegistry, RegistryError, runtime_release


router = APIRouter(prefix="/internal/prompts")
Version = Annotated[str, Field(pattern=r"^v[1-9][0-9]{0,8}$")]
Release = Annotated[str, Field(pattern=r"^prompts-(?:v1|[a-f0-9]{32})$")]


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    operation: str = Field(pattern=r"^(?:screen|story)\.[a-z.\-]{1,42}$")
    actor: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9:._-]+$")


class Draft(Change):
    templates: dict[str, str] = Field(min_length=1, max_length=1)
    expectedVersion: Version | None
    expectedReleaseId: Release
    reason: str = Field(min_length=1, max_length=300)

    # Preserve the source template bytes, including a meaningful final newline.
    model_config = ConfigDict(str_strip_whitespace=False)


class Validate(Change):
    version: Version


class Activate(Validate):
    expectedReleaseId: Release


def _error(code, message, status):
    return JSONResponse({"code": code, "message": message}, status_code=status)


def _admin_token():
    configured = os.environ.get("SFDC_AGENT_PROMPT_ADMIN_TOKEN")
    if configured is not None:
        return configured
    try:
        path = project_root() / ".prompt-management" / "admin-token"
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "r", encoding="ascii") as stream:
            details = os.fstat(stream.fileno())
            if not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o600 or details.st_uid != os.getuid():
                return ""
            result = stream.read(513).strip()
            return result if len(result) <= 512 else ""
    except (OSError, UnicodeError):
        return ""


def _authorized(request):
    if (request.headers.get("x-sfdc-agent") != "1" or "origin" in request.headers
            or "sec-fetch-site" in request.headers):
        raise RegistryError("PROMPT_INTERNAL_ONLY", "NOVA 서버에서 연결해 주세요.", 403)
    if request.method == "POST":
        configured = _admin_token()
        provided = request.headers.get("x-sfdc-prompt-admin", "")
        if not configured or not hmac.compare_digest(configured.encode(), provided.encode()):
            raise RegistryError("PROMPT_WRITE_FORBIDDEN", "프롬프트 변경 권한을 확인할 수 없습니다.", 403)


async def _read(request, contract):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > 256 * 1024:
            raise RegistryError("PROMPT_REQUEST_TOO_LARGE", "요청이 허용 크기를 초과했습니다.", 413)
        data.extend(chunk)
    try:
        return contract.model_validate_json(data)
    except ValidationError:
        raise RegistryError("PROMPT_REQUEST_INVALID", "프롬프트 관리 요청 형식을 확인해 주세요.", 422) from None


async def _response(request, action, contract=None):
    try:
        _authorized(request)
        data = await _read(request, contract) if contract else None
        return await run_in_threadpool(action, data)
    except RegistryError as error:
        return _error(error.code, str(error), error.status)
    except sqlite3.Error:
        return _error("PROMPT_STORE_UNAVAILABLE", "프롬프트 저장소에 접근할 수 없습니다.", 503)


@router.get("")
async def catalog(request: Request):
    return await _response(request, lambda _: PromptRegistry().catalog())


@router.get("/active")
async def active(request: Request):
    return await _response(request, lambda _: {"releaseId": runtime_release().id})


@router.get("/{operation}")
async def detail(request: Request, operation: str):
    return await _response(request, lambda _: PromptRegistry().detail(operation))


@router.post("/drafts")
async def save_draft(request: Request):
    return await _response(request, lambda data: PromptRegistry().save_draft(
        data.operation, data.templates, data.expectedVersion, data.expectedReleaseId, data.reason.strip(), data.actor), Draft)


@router.post("/validate")
async def validate(request: Request):
    return await _response(request, lambda data: PromptRegistry().validate(data.operation, data.version), Validate)


@router.post("/activate")
async def activate(request: Request):
    return await _response(request, lambda data: PromptRegistry().promote(
        data.operation, data.version, data.expectedReleaseId, data.actor), Activate)


@router.post("/restore")
async def restore(request: Request):
    return await _response(request, lambda data: PromptRegistry().promote(
        data.operation, data.version, data.expectedReleaseId, data.actor, restore=True), Activate)

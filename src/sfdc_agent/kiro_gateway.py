"""Local model sessions for Screen and Story conversion."""

import asyncio
import hashlib
import hmac
import json
import logging
import sqlite3
from contextlib import asynccontextmanager, nullcontext
from time import monotonic
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, model_validator

from .kiro_headless import HeadlessKiroSession, IDLE_SECONDS, SessionError, read_key
from .image_input import ImageBinding, validate_image_input
from .kiro_screen_projection import prepare_screen_fields
from .screen_prompt_context import ScreenPromptContext, SCREEN_PROMPT_CONTEXT_CAPABILITY_VERSION
from .prompt_registry import PROMPTS, RegistryError, runtime_release
from .provider_capabilities import provider_receipt
from .prompt_release_id import PromptReleaseId
from .managed_prompts import (
    managed_context_alive,
    managed_trace_enabled,
    record_managed_trace,
    require_managed_context,
    session_prompt_release,
)
from .prompt_run_values import PromptRunValues, capture_prompt_run, trace_owner
from .service_mode import managed_only
from .managed_execution_progress import managed_execution_context
from .managed_figma_scope import require_figma_scope

router = APIRouter(prefix="/internal/kiro/sessions")
logger = logging.getLogger("uvicorn.error")
REQUEST_LIMIT = 18 * 1024 * 1024
MAX_SESSIONS = 12
OPERATIONS = {
    "screen.analyze", "screen.analyze.local", "screen.capture", "screen.discovery", "screen.metadata",
    "screen.generate", "screen.repair", "screen.plan", "screen.plan.repair", "screen.review-plan",
    "story.classification", "story.analysis", "story.generate",
}
_PURPOSES = {"screen", "story"}
_HEADLESS_OPERATIONS = {
    "screen": {"screen.analyze.local", "screen.review-plan", "screen.generate", "screen.repair",
               "screen.plan", "screen.plan.repair"},
    "story": {"story.classification", "story.analysis", "story.generate"},
}


def kiro_mode(purpose: str) -> str:
    if purpose not in _PURPOSES:
        raise ValueError("Unsupported Kiro purpose.")
    return "headless"


def _validate_headless_prompt(prompt):
    if prompt.fields is None:
        raise ValueError("Headless tasks require structured evidence.")
    fields = prompt.fields
    if prompt.operation in {"screen.repair", "screen.plan.repair"}:
        fields = fields.get("evidence")
        if not isinstance(fields, dict) or not isinstance(prompt.fields.get("previousDraft"), str):
            raise ValueError("Headless repairs require the original evidence and draft.")
    if prompt.operation in {"screen.generate", "screen.repair"}:
        if not isinstance(fields.get("reviewedAnalysis"), dict):
            raise ValueError("Headless generation requires a reviewed analysis.")
    if prompt.operation in {"screen.analyze.local", "screen.review-plan", "screen.generate", "screen.repair"}:
        if not (fields.get("sourceContext") or fields.get("capturedContext") or fields.get("sourceSections")):
            raise ValueError("Headless Screen tasks require captured Figma evidence.")


class OpenSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fileKey: str | None = Field(default=None, pattern=r"^[A-Za-z0-9]{1,128}$")
    readFigma: bool = False
    promptReleaseId: PromptReleaseId | None = None

    @model_validator(mode="after")
    def restrict_figma(self):
        if self.readFigma:
            raise ValueError("Key-only Kiro sessions use captured Figma evidence.")
        return self


class ManagedOpenSession(OpenSession):
    fileKey: str | None = Field(default=None, pattern=r"^[A-Za-z0-9]{1,160}$")


class Prompt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str | None = None
    text: str | None = Field(default=None, min_length=1, max_length=8 * 1024 * 1024)
    fields: dict | None = None
    png: str | None = None
    imageBinding: ImageBinding | None = None
    _screen_prompt_context: ScreenPromptContext | None = PrivateAttr(default=None)

    def build(self, *, templates=None) -> str:
        if self.operation is not None and self.operation not in OPERATIONS:
            raise ValueError("Unsupported Kiro operation.")
        if self.fields is None and self.text:
            text = self.text
        elif self.text is None and self.fields is not None and self.operation:
            if self.operation.startswith("screen."):
                from .screen_tasks import build_screen_task
                text = build_screen_task(self.operation, self.fields, templates=templates,
                                         prepared_context=self._screen_prompt_context)
            else:
                from .story_tasks import build_story_task
                text = build_story_task(self.operation, self.fields, templates=templates)
        else:
            raise ValueError("Send a prompt or structured task fields.")
        if not text or len(text.encode()) > 8 * 1024 * 1024:
            raise ValueError("Kiro prompt exceeds the supported size.")
        return text


@asynccontextmanager
async def kiro_lifespan(app):
    sessions = app.state.kiro_sessions = {}
    prompt_run_values = app.state.prompt_run_values = PromptRunValues()

    async def reap():
        while True:
            await asyncio.sleep(30)
            prompt_run_values.purge()
            now = asyncio.get_running_loop().time()
            expired = [key for key, session in sessions.items()
                       if session.expiry <= now or session.closed
                       or managed_only() and not managed_context_alive(getattr(session, "managed_prompt_token", ""))]
            await asyncio.gather(*(sessions.pop(key).close() for key in expired))

    reaper = asyncio.create_task(reap())
    try:
        yield
    finally:
        reaper.cancel()
        await asyncio.gather(reaper, return_exceptions=True)
        await asyncio.gather(*(session.close() for session in sessions.values()))


def authorize(request):
    if (request.headers.get("x-sfdc-agent") != "1" or "origin" in request.headers
            or "sec-fetch-site" in request.headers):
        raise HTTPException(403, "Use the NOVA local server connection.")
    if managed_only():
        require_managed_context(request)
    return request.app.state.kiro_sessions


async def read_request(request, contract, limit=REQUEST_LIMIT):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit:
            raise HTTPException(413, "Kiro request exceeds the supported size.")
        data.extend(chunk)
    try:
        return contract.model_validate_json(data)
    except ValidationError:
        raise HTTPException(422, "Invalid Kiro request.") from None


def get_session(request, session_id):
    session = authorize(request).get(session_id)
    if session is None or session.closed:
        raise SessionError("KIRO_CLOSED", "Kiro 세션이 종료됐습니다. 다시 실행해 주세요.")
    require_session_owner(request, session)
    if managed_only() and (context := getattr(session, 'managed_execution_context', None)) and context.remote and getattr(context, "source_acquisition", None) != "MCP_ONLY":
        require_figma_scope(context.execution_scope, **getattr(context, "figma_authorization", {}))
    return session


def require_session_owner(request, session):
    if managed_only() and not hmac.compare_digest(require_managed_context(request), getattr(session, "managed_prompt_token", "")):
        raise HTTPException(403, "The session belongs to a different managed job.")
    if context := getattr(session, 'managed_execution_context', None):
        if not context.alive():
            raise HTTPException(403, 'The execution attempt ended.')


def error_response(request, error):
    # SessionError messages are bounded application diagnostics; provider
    # output is never carried through this boundary.
    if managed_only():
        from .kiro_readiness import AUTH_ERROR_CODES
        context = managed_execution_context(request)
        if context is not None and error.code in AUTH_ERROR_CODES:
            context.provider_error = error.code
    return JSONResponse({"error": {"code": error.code, "message": str(error)}}, status_code=502)


@router.post("")
async def start_session(request: Request):
    sessions = authorize(request)
    options = await read_request(request, ManagedOpenSession if managed_only() else OpenSession)
    purpose = request.headers.get("x-sfdc-kiro-purpose")
    context = managed_execution_context(request) if managed_only() else None
    if purpose not in _PURPOSES:
        raise HTTPException(422, "Invalid Kiro session purpose.")
    if context and context.purpose != purpose:
        raise HTTPException(422, 'The execution purpose does not match.')
    if context and context.remote and getattr(context, "source_acquisition", None) != "MCP_ONLY":
        require_figma_scope(context.execution_scope, **getattr(context, "figma_authorization", {}))
    if context and context.mode != "headless":
        raise SessionError("KIRO_MODE_INVALID", "Kiro 실행은 API 키 기반 헤드리스 모드만 지원합니다.")
    if len(sessions) >= MAX_SESSIONS:
        raise SessionError("KIRO_BUSY", "다른 Kiro 작업이 진행 중입니다. 잠시 뒤 다시 시도해 주세요.")
    try:
        release = session_prompt_release(request, options.promptReleaseId) or await asyncio.to_thread(runtime_release, options.promptReleaseId)
    except RegistryError as error:
        raise SessionError("KIRO_PROMPT_RELEASE_UNAVAILABLE", str(error)) from None
    except sqlite3.Error:
        raise SessionError("KIRO_PROMPT_RELEASE_UNAVAILABLE", "프롬프트 릴리스를 불러올 수 없습니다.") from None
    session_id = uuid4().hex
    session = HeadlessKiroSession(read_key(), purpose=purpose)
    try:
        if context and context.remote and getattr(context, "source_acquisition", None) != "MCP_ONLY":
            require_figma_scope(context.execution_scope, **getattr(context, "figma_authorization", {}))
    except BaseException:
        await session.close()
        raise
    if len(sessions) >= MAX_SESSIONS:
        await session.close()
        raise SessionError("KIRO_BUSY", "다른 Kiro 작업이 진행 중입니다. 잠시 뒤 다시 시도해 주세요.")
    session.prompt_release = release
    session.managed_execution_context = context
    if managed_only():
        session.managed_prompt_token = require_managed_context(request)
        session.purpose = purpose
        if context:
            context.bind(session)
    sessions[session_id] = session

    try:
        require_session_owner(request, session)
        if context and context.remote and getattr(context, "source_acquisition", None) != "MCP_ONLY":
            require_figma_scope(context.execution_scope, **getattr(context, "figma_authorization", {}))
    except BaseException:
        sessions.pop(session_id, None)
        await session.close()
        raise
    return {"id": session_id, "executionMode": session.execution_mode, "promptReleaseId": release.id,
            "imageInputContractVersion": 1, "screenPromptContextEncodingVersion": SCREEN_PROMPT_CONTEXT_CAPABILITY_VERSION}


@router.get("/{session_id}/events")
async def session_events(request: Request, session_id: str):
    # Retain the scoped empty response for older server callers. Key-only
    # headless execution has no interactive authorization or progress events.
    get_session(request, session_id)
    return {"events": []}


@router.post("/{session_id}/prompt")
async def prompt_session(request: Request, session_id: str):
    session = get_session(request, session_id)
    prompt = await read_request(request, Prompt)
    if managed_only() and (prompt.fields is None or prompt.text is not None or prompt.operation not in OPERATIONS
                           or not prompt.operation.startswith(session.purpose + ".")):
        raise SessionError("KIRO_TASK_INVALID", "Use the managed job's structured task and immutable prompt template.")
    context = getattr(session, 'managed_execution_context', None)
    if context and context.remote and prompt.fields and prompt.fields.get('fileKey', context.file_key) != context.file_key:
        raise SessionError('KIRO_TASK_INVALID', 'Use the execution attempt\'s approved Figma file.')
    execution_mode = session.execution_mode
    release = session.prompt_release
    owner = trace_owner(request.headers) if prompt.fields is not None and prompt.operation in PROMPTS else None
    managed_capture = prompt.fields is not None and prompt.operation in OPERATIONS and managed_trace_enabled(request)
    projection_started = monotonic()
    try:
        if execution_mode == "headless":
            if prompt.operation not in _HEADLESS_OPERATIONS[session.purpose]:
                raise ValueError("Unsupported operation for the headless session.")
            _validate_headless_prompt(prompt)
        image = validate_image_input(prompt.png, prompt.imageBinding, operation=prompt.operation,
                                     fields=prompt.fields, context=context)
        prepared = await prepare_screen_fields(prompt.operation, prompt.fields)
        fields = prepared.fields
        projection_ms = max(0, round((monotonic() - projection_started) * 1000))
        if fields is not prompt.fields:
            prompt = prompt.model_copy(update={"fields": fields})
        prompt._screen_prompt_context = prepared.prompt_context
        build_started = monotonic()
        with capture_prompt_run(prompt.operation) if owner or managed_capture else nullcontext() as prompt_run:
            text = prompt.build(templates=release.templates)
        build_ms = max(0, round((monotonic() - build_started) * 1000))
    except (ValueError, UnicodeError, TypeError, KeyError):
        raise SessionError("KIRO_TASK_INVALID", "Kiro 작업 요청의 원본 데이터 형식이 올바르지 않습니다.") from None
    payload = text.encode()
    if prepared.prompt_context is not None:
        logger.info("Screen prompt context %s %s %s", prompt.operation,
                    (fields.get('evidence') if prompt.operation == 'screen.repair' else fields).get('nodeId'),
                    json.dumps(prepared.prompt_context.receipt, separators=(',', ':')))
    operation = prompt.operation or "prompt"
    logger.info("Kiro %s started (%s, %s)", operation, session_id[:8], execution_mode)
    started = monotonic()
    result = await get_session(request, session_id).call("prompt", {
        "text": text, "png": None, **({"image": image} if image is not None else {})})
    completed_session = get_session(request, session_id)
    receipt = getattr(completed_session, "provider_receipt", None)
    if not isinstance(receipt, dict):
        receipt = provider_receipt("kiro")
    if image is not None:
        delivery = receipt.get("imageInput")
        if (not isinstance(delivery, dict) or delivery.get("status") != "read-verified"
                or delivery.get("reference") != image.receipt() or delivery.get("completedReads") != 1):
            raise SessionError("KIRO_HEADLESS_INCOMPLETE", "원본 PNG 입력의 전달 완료 기록을 확인하지 못했습니다.")
    receipt = {**receipt, 'gatewayPerformance': {'schemaVersion': 1, 'projectionMs': projection_ms,
        'promptBuildMs': build_ms, 'sessionCallMs': max(0, round((monotonic() - started) * 1000))}}
    if prepared.prompt_context is not None:
        receipt['sourcePromptContext'] = dict(prepared.prompt_context.receipt)
    if prompt_run is not None and owner is not None:
        request.app.state.prompt_run_values.save(owner, prompt_run, started=started, release=release, mode=execution_mode)
    if prompt_run is not None and managed_capture:
        record_managed_trace(request, prompt_run, release, text, execution_mode)
    logger.info("Kiro %s finished (%s, %s)", operation, session_id[:8], result.get("stopReason"))
    image_digest = hashlib.sha256(payload + b"\0image/png\0" + json.dumps(image.receipt(), sort_keys=True,
        separators=(",", ":")).encode() + b"\0" + image.content).hexdigest() if image is not None else None
    return {**result, "providerReceipt": receipt, "promptDigest": hashlib.sha256(payload).hexdigest(), "promptBytes": len(payload),
            **({"inputDigest": image_digest, "imageBytes": image.binding.bytes} if image is not None else {}),
            "executionMode": execution_mode, "promptReleaseId": release.id,
            **({"promptVersion": release.versions[operation]} if prompt.fields is not None else {})}


@router.delete("/{session_id}", status_code=204)
async def end_session(request: Request, session_id: str):
    sessions = authorize(request)
    if session := sessions.get(session_id):
        require_session_owner(request, session)
        sessions.pop(session_id, None)
        await session.close()

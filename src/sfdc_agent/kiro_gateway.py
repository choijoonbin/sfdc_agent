"""Local model sessions for Screen and Story conversion."""

import asyncio
import base64
import hashlib
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from time import monotonic
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .kiro_headless import HeadlessKiroSession, IDLE_SECONDS, SessionError, read_key
from .kiro_screen_projection import project_screen_fields
from .prompt_registry import PROMPTS, RegistryError, runtime_release
from .prompt_run_values import PromptRunValues, capture_prompt_run, trace_owner
from .providers import _kiro_environment

router = APIRouter(prefix="/internal/kiro/sessions")
logger = logging.getLogger("uvicorn.error")
REQUEST_LIMIT = 18 * 1024 * 1024
RESPONSE_LIMIT = 48 * 1024 * 1024
MAX_SESSIONS = 12
MAX_IDLE_PNG_SESSIONS = 4
FIGMA_PNG_PURPOSE = "figma-png"
OPERATIONS = {
    "screen.analyze", "screen.analyze.local", "screen.capture", "screen.discovery", "screen.metadata",
    "screen.generate", "screen.repair", "screen.plan", "screen.plan.repair", "screen.review-plan",
    "story.classification", "story.analysis", "story.generate",
}
_MODES = {"screen": "SFDC_AGENT_SCREEN_KIRO_MODE", "story": "SFDC_AGENT_STORY_KIRO_MODE"}
_HEADLESS_OPERATIONS = {
    "screen": {"screen.analyze.local", "screen.review-plan", "screen.generate", "screen.repair",
               "screen.plan", "screen.plan.repair"},
    "story": {"story.classification", "story.analysis", "story.generate"},
}


def kiro_mode(purpose: str) -> str:
    return os.environ.get(_MODES[purpose], "acp")


def _validate_headless_prompt(prompt):
    if prompt.fields is None or prompt.png is not None:
        raise ValueError("Headless tasks require structured text evidence.")
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
    promptReleaseId: str | None = Field(default=None, pattern=r"^prompts-(?:v1|[a-f0-9]{32})$")

    @model_validator(mode="after")
    def restrict_figma(self):
        if self.readFigma and not self.fileKey:
            raise ValueError("Figma MCP requires a file key.")
        return self


class Prompt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str | None = None
    text: str | None = Field(default=None, min_length=1, max_length=8 * 1024 * 1024)
    fields: dict | None = None
    png: str | None = None

    def build(self, *, templates=None) -> str:
        if self.operation is not None and self.operation not in OPERATIONS:
            raise ValueError("Unsupported Kiro operation.")
        if self.fields is None and self.text:
            text = self.text
        elif self.text is None and self.fields is not None and self.operation:
            if self.operation.startswith("screen."):
                from .screen_tasks import build_screen_task
                text = build_screen_task(self.operation, self.fields, templates=templates)
            else:
                from .story_tasks import build_story_task
                text = build_story_task(self.operation, self.fields, templates=templates)
        else:
            raise ValueError("Send a prompt or structured task fields.")
        if self.png is not None:
            image = base64.b64decode(self.png, validate=True)
            if not image.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("Expected PNG evidence.")
        if not text or len(text.encode()) > 8 * 1024 * 1024:
            raise ValueError("Kiro prompt exceeds the supported size.")
        return text


class KiroSession:
    execution_mode = "acp"

    def __init__(self, process):
        self.process = process
        self.pending = {}
        self.events = []
        self.updated = asyncio.Event()
        self.counter = 0
        self.expiry = 0.0
        self.figma_png_file_key = None
        self.figma_connected = False
        self.png_prompt_active = False
        self.touch()
        self.closed = False
        self.reader = asyncio.create_task(self.read())

    @classmethod
    async def start(cls):
        environment = {key: value for key, value in os.environ.items() if key in _kiro_environment
                       or key.startswith(("LC_", "XDG_"))}
        try:
            process = await asyncio.create_subprocess_exec(
                "node", str(Path(__file__).with_name("kiro_session.mjs")), env=environment,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=RESPONSE_LIMIT,
            )
        except OSError:
            raise SessionError("KIRO_UNAVAILABLE", "Kiro 실행 환경을 시작할 수 없습니다.") from None
        return cls(process)

    def touch(self):
        self.expiry = asyncio.get_running_loop().time() + IDLE_SECONDS

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if "event" in message:
                    event = message["event"]
                    if event.get("stage") == "figma-connected":
                        self.figma_connected = True
                    elif (event.get("type") in {"figma-auth", "figma-auth-required"} or
                            event.get("stage") in {"figma-connecting", "figma-auth-required", "figma-unavailable"}):
                        self.figma_connected = False
                    self.events.append(event)
                    self.events = self.events[-128:]
                    self.updated.set()
                    continue
                future = self.pending.get(message.get("id"))
                if future is None or future.done():
                    continue
                if error := message.get("error"):
                    future.set_exception(SessionError(error["code"], error["message"]))
                else:
                    future.set_result(message.get("result"))
        except (ValueError, KeyError, OSError):
            logger.warning("Kiro session returned an invalid transport response")
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(SessionError("KIRO_EXITED", "Kiro 연결이 종료됐습니다. 다시 실행해 주세요."))
            self.updated.set()

    async def call(self, method, params=None, timeout=610):
        if self.closed or self.reader.done():
            raise SessionError("KIRO_CLOSED", "Kiro 변환 연결이 종료됐습니다.")
        self.touch()
        self.counter += 1
        request_id = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            request = json.dumps({"id": request_id, "method": method, "params": params}, ensure_ascii=False)
            self.process.stdin.write(request.encode() + b"\n")
            await self.process.stdin.drain()
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            await self.close()
            raise SessionError("KIRO_TIMEOUT", "Kiro 응답 시간이 초과됐습니다. 다시 실행해 주세요.") from None
        except (BrokenPipeError, ConnectionResetError):
            raise SessionError("KIRO_EXITED", "Kiro 연결이 종료됐습니다. 다시 실행해 주세요.") from None
        finally:
            self.pending.pop(request_id, None)
            self.touch()
            if future.done() and not future.cancelled():
                future.exception()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.process.returncode is None:
                self.process.stdin.close()
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except TimeoutError:
                    if self.process.returncode is None:
                        self.process.kill()
                    await self.process.wait()
        finally:
            if not self.reader.done():
                self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)


@asynccontextmanager
async def kiro_lifespan(app):
    sessions = app.state.kiro_sessions = {}
    png_idle = app.state.kiro_png_idle = {}
    app.state.kiro_png_in_use = set()
    prompt_run_values = app.state.prompt_run_values = PromptRunValues()

    async def reap():
        while True:
            await asyncio.sleep(30)
            prompt_run_values.purge()
            await reap_kiro_sessions(app)

    reaper = asyncio.create_task(reap())
    try:
        yield
    finally:
        reaper.cancel()
        await asyncio.gather(reaper, return_exceptions=True)
        await asyncio.gather(*(session.close() for session in [*sessions.values(), *png_idle.values()]))


def session_expired(session, now):
    return session.expiry <= now or session.closed or session.reader.done()


def figma_png_file_key(session):
    key = getattr(session, "figma_png_file_key", None)
    return key if isinstance(key, str) else None


def png_session_reusable(session, now):
    return (not session_expired(session, now) and session.figma_connected
            and not session.pending and not session.png_prompt_active)


async def reap_kiro_sessions(app):
    now = asyncio.get_running_loop().time()
    sessions = app.state.kiro_sessions
    png_idle = app.state.kiro_png_idle
    expired = [session_id for session_id, session in sessions.items() if session_expired(session, now)]
    unusable = [key for key, session in png_idle.items() if not png_session_reusable(session, now)]
    # Reserve retiring file keys until the old process has stopped.
    for key in unusable:
        app.state.kiro_png_in_use.add(key)
    await asyncio.gather(*(finish_png_process(app, key, png_idle.pop(key)) for key in unusable),
                         *(close_kiro_lease(app, session_id) for session_id in expired))


async def finish_png_process(app, key, session):
    try:
        await session.close()
    finally:
        app.state.kiro_png_in_use.discard(key)


async def close_kiro_lease(app, session_id, *, reuse=False):
    session = app.state.kiro_sessions.pop(session_id, None)
    if session is None:
        return
    key = figma_png_file_key(session)
    if key is None:
        await session.close()
        return
    session.events.clear()
    session.updated.set()  # Wake pending event polls so they can reject the ended lease.
    now = asyncio.get_running_loop().time()
    png_idle = app.state.kiro_png_idle
    if reuse and len(png_idle) < MAX_IDLE_PNG_SESSIONS and png_session_reusable(session, now):
        session.touch()
        png_idle[key] = session
        app.state.kiro_png_in_use.discard(key)
    else:
        await finish_png_process(app, key, session)


def authorize(request):
    if (request.headers.get("x-sfdc-agent") != "1" or "origin" in request.headers
            or "sec-fetch-site" in request.headers):
        raise HTTPException(403, "Use the NOVA local server connection.")
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
    if session is None or session.closed or (figma_png_file_key(session) is not None and
                                             session_expired(session, asyncio.get_running_loop().time())):
        raise SessionError("KIRO_CLOSED", "Kiro 세션이 종료됐습니다. 다시 실행해 주세요.")
    return session


def error_response(_, error):
    return JSONResponse({"error": {"code": error.code, "message": str(error)}}, status_code=502)


@router.post("")
async def start_session(request: Request):
    authorize(request)
    options = await read_request(request, OpenSession)
    purpose = request.headers.get("x-sfdc-kiro-purpose")
    figma_png = purpose == FIGMA_PNG_PURPOSE
    if ((figma_png and not options.readFigma) or
            (purpose is not None and not figma_png and (purpose not in _MODES or options.readFigma))):
        raise HTTPException(422, "Invalid Kiro session purpose.")
    mode = kiro_mode(purpose) if purpose in _MODES else "acp"
    if mode not in {"acp", "headless"}:
        raise SessionError("KIRO_MODE_INVALID", "Kiro 실행 모드는 acp 또는 headless로 설정해 주세요.")
    if not figma_png:
        return await open_kiro_lease(request, options, purpose, mode)
    key = options.fileKey
    in_use = request.app.state.kiro_png_in_use
    if key in in_use:
        raise SessionError("KIRO_BUSY", "같은 Figma 파일의 PNG 작업이 진행 중입니다. 잠시 뒤 다시 시도해 주세요.")
    in_use.add(key)
    try:
        return await open_kiro_lease(request, options, purpose, mode, png_key=key)
    except BaseException:
        in_use.discard(key)
        raise


async def open_kiro_lease(request, options, purpose, mode, *, png_key=None):
    sessions = request.app.state.kiro_sessions
    if len(sessions) >= MAX_SESSIONS:
        raise SessionError("KIRO_BUSY", "다른 Kiro 작업이 진행 중입니다. 잠시 뒤 다시 시도해 주세요.")
    try:
        release = await asyncio.to_thread(runtime_release, options.promptReleaseId)
    except RegistryError as error:
        raise SessionError("KIRO_PROMPT_RELEASE_UNAVAILABLE", str(error)) from None
    except sqlite3.Error:
        raise SessionError("KIRO_PROMPT_RELEASE_UNAVAILABLE", "프롬프트 릴리스를 불러올 수 없습니다.") from None
    session_id = uuid4().hex
    if png_key is not None:
        idle = request.app.state.kiro_png_idle.pop(png_key, None)
        if idle is not None:
            if len(sessions) < MAX_SESSIONS and png_session_reusable(idle, asyncio.get_running_loop().time()):
                idle.touch()
                idle.events[:] = [{"type": "status", "stage": "figma-connected"}]
                idle.updated.set()
                idle.prompt_release = release
                sessions[session_id] = idle
                return {"id": session_id, "executionMode": idle.execution_mode, "promptReleaseId": release.id}
            await idle.close()
    session = HeadlessKiroSession(read_key(), purpose=purpose) if mode == "headless" else await KiroSession.start()
    if len(sessions) >= MAX_SESSIONS:
        await session.close()
        raise SessionError("KIRO_BUSY", "다른 Kiro 작업이 진행 중입니다. 잠시 뒤 다시 시도해 주세요.")
    session.prompt_release = release
    if png_key is not None:
        session.figma_png_file_key = png_key
    sessions[session_id] = session

    async def disconnected():
        while (await request.receive())["type"] != "http.disconnect":
            pass

    open_options = {
        "reference": {"fileKey": options.fileKey}, "readFigma": options.readFigma,
    }
    if png_key is not None:
        open_options["figmaPng"] = True
    opening = asyncio.create_task(session.call("open", open_options, timeout=65))
    disconnect = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait((opening, disconnect), return_when=asyncio.FIRST_COMPLETED)
        if disconnect in done:
            raise SessionError("KIRO_ABORTED", "Kiro 변환 작업이 중단됐습니다.")
        await opening
    except BaseException:
        sessions.pop(session_id, None)
        await session.close()
        raise
    finally:
        for task in (opening, disconnect):
            if not task.done():
                task.cancel()
        await asyncio.gather(opening, disconnect, return_exceptions=True)
    return {"id": session_id, "executionMode": session.execution_mode, "promptReleaseId": release.id}


@router.get("/{session_id}/events")
async def session_events(request: Request, session_id: str):
    session = get_session(request, session_id)
    if not session.events and not session.reader.done():
        try:
            await asyncio.wait_for(session.updated.wait(), 10)
        except TimeoutError:
            pass
    get_session(request, session_id)  # A poll from a released lease must not consume the next lease's events.
    session.updated.clear()
    events, session.events = session.events, []
    if not events and session.reader.done():
        raise SessionError("KIRO_EXITED", "Kiro 연결이 종료됐습니다. 다시 실행해 주세요.")
    return {"events": events}


@router.post("/{session_id}/wait")
async def wait_for_figma(request: Request, session_id: str):
    return {"result": await call_kiro_lease(request, session_id, "wait", timeout=IDLE_SECONDS)}


@router.post("/{session_id}/authorize")
async def authorize_figma(request: Request, session_id: str):
    return {"result": await call_kiro_lease(request, session_id, "authorize", timeout=35)}


async def call_kiro_lease(request, session_id, method, params=None, *, timeout=610):
    session = get_session(request, session_id)
    png = figma_png_file_key(session) is not None
    png_prompt = png and method == "prompt"
    if png_prompt:
        if session.png_prompt_active:
            raise SessionError("KIRO_BUSY", "같은 Figma 파일의 PNG 작업이 진행 중입니다. 잠시 뒤 다시 시도해 주세요.")
        session.png_prompt_active = True
    try:
        return await session.call(method, params, timeout=timeout)
    except (SessionError, asyncio.CancelledError):
        if png:
            await close_kiro_lease(request.app, session_id)
        raise
    finally:
        if png_prompt:
            session.png_prompt_active = False


@router.post("/{session_id}/prompt")
async def prompt_session(request: Request, session_id: str):
    session = get_session(request, session_id)
    prompt = await read_request(request, Prompt)
    execution_mode = session.execution_mode
    release = session.prompt_release
    owner = trace_owner(request.headers) if prompt.fields is not None and prompt.operation in PROMPTS else None
    try:
        if figma_png_file_key(session) is not None and (
            prompt.operation not in {None, "screen.capture"} or prompt.png is not None
        ):
            raise ValueError("Figma PNG sessions only support export and screenshot prompts.")
        if execution_mode == "headless":
            if prompt.operation not in _HEADLESS_OPERATIONS[session.purpose]:
                raise ValueError("Unsupported operation for the headless session.")
            _validate_headless_prompt(prompt)
        fields = await project_screen_fields(prompt.operation, prompt.fields)
        if fields is not prompt.fields:
            prompt = prompt.model_copy(update={"fields": fields})
        with capture_prompt_run(prompt.operation) if owner else nullcontext() as prompt_run:
            text = prompt.build(templates=release.templates)
    except (ValueError, UnicodeError, TypeError, KeyError):
        raise SessionError("KIRO_TASK_INVALID", "Kiro 작업 요청의 원본 데이터 형식이 올바르지 않습니다.") from None
    payload = text.encode()
    operation = prompt.operation or "prompt"
    logger.info("Kiro %s started (%s, %s)", operation, session_id[:8], execution_mode)
    started = monotonic()
    result = await call_kiro_lease(request, session_id, "prompt", {"text": text, "png": prompt.png})
    if prompt_run is not None:
        request.app.state.prompt_run_values.save(owner, prompt_run, started=started, release=release, mode=execution_mode)
    logger.info("Kiro %s finished (%s, %s)", operation, session_id[:8], result.get("stopReason"))
    return {**result, "promptDigest": hashlib.sha256(payload).hexdigest(), "promptBytes": len(payload),
            "executionMode": execution_mode, "promptReleaseId": release.id,
            **({"promptVersion": release.versions[operation]} if prompt.fields is not None else {})}


@router.delete("/{session_id}", status_code=204)
async def end_session(request: Request, session_id: str):
    authorize(request)
    await close_kiro_lease(request.app, session_id, reuse=True)

"""Ephemeral ACP progress bound to one live Spring execution attempt."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import re
from threading import RLock
from time import monotonic, time
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .managed_prompts import _authorize, managed_context_alive, require_managed_context
from .managed_figma_scope import require_figma_scope, FigmaProviderScopeError

router = APIRouter(prefix='/internal/v1/executions', include_in_schema=False)
_CONTEXTS = {}
_TOKENS = {}
_LOCK = RLock()
_ATTEMPTS = {}
_RETAINED_ATTEMPTS = 256
MAX_ATTEMPT = 2147483647
_PHASES = {'starting', 'connecting', 'figma-connected', 'generating', 'figma-reading', 'analyzing', 'local-analyzing'}


class ExecutionScope(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tenantId: UUID
    workspaceId: UUID


class Cancellation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    executionScope: ExecutionScope
    deadlineEpochSeconds: int = Field(strict=True, gt=0)


def _scope(value):
    return (str(value.executionScope.tenantId), str(value.executionScope.workspaceId))


def _failure(code, status=409):
    return HTTPException(status, code)


def _prune_attempts():
    now = time()
    for key, value in list(_ATTEMPTS.items()):
        if value['deadline'] + 30 <= now and not any(entry.job_id == key for entry in _CONTEXTS.values()):
            _ATTEMPTS.pop(key, None)


def _attempt_stamp(job_id, attempt, scope, deadline, fingerprint=None, *, cancel=False):
    _prune_attempts()
    previous = _ATTEMPTS.get(job_id)
    if previous:
        if previous['scope'] != scope or previous['attempt'] > attempt:
            raise _failure('EXECUTION_ATTEMPT_CONFLICT')
        if previous['attempt'] == attempt:
            if previous['deadline'] != deadline:
                raise _failure('EXECUTION_ATTEMPT_CONFLICT')
            if cancel:
                previous['status'] = 'cancelled'
                return previous
            if previous['status'] == 'cancelled':
                raise _failure('EXECUTION_CANCELLED')
            if previous['fingerprint'] != fingerprint:
                raise _failure('EXECUTION_ATTEMPT_CONFLICT')
            raise _failure('EXECUTION_ALREADY_RUNNING' if previous['status'] == 'running' else 'EXECUTION_ALREADY_FINISHED')
    elif len(_ATTEMPTS) >= _RETAINED_ATTEMPTS:
        # This is not worker admission BUSY: never discard an acknowledged live cancellation.
        raise _failure('EXECUTION_STATE_UNAVAILABLE', 503)
    value = {'attempt': attempt, 'scope': scope, 'deadline': deadline, 'fingerprint': fingerprint,
             'status': 'cancelled' if cancel else 'running'}
    _ATTEMPTS[job_id] = value
    return value


def _figma_url(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    try:
        url = urlsplit(value)
        if url.scheme == 'https' and url.netloc in {'figma.com', 'www.figma.com'} and url.path.startswith('/oauth/'):
            return value
    except ValueError:
        pass
    return None


def execution_mode(payload):
    from .kiro_gateway import kiro_mode
    # Captured archive evidence supports both original CLI ACP and headless
    # sessions. Its source type restricts tools independently of model transport.
    return kiro_mode(payload.kind.lower())


@dataclass(eq=False)
class ExecutionContext:
    job_id: str
    attempt: int
    token: str | None
    purpose: str
    mode: str
    file_key: str | None
    remote: bool
    expires: float
    task: asyncio.Task | None = None
    execution_scope: tuple | None = None
    sessions: set = field(default_factory=set)
    figma_session: object | None = None
    authorization: asyncio.Task | None = None
    cleanup: asyncio.Task | None = None
    closed: bool = False
    auth_error: str | None = None
    view: dict = field(default_factory=lambda: {'status': 'running', 'phase': 'connecting'})

    def alive(self):
        return not self.closed and self.expires > monotonic() and (not self.token or managed_context_alive(self.token))

    def public_view(self):
        return {'jobId': self.job_id, 'attempt': self.attempt, **self.view}

    def record_auth_error(self, code):
        from .kiro_readiness import AUTH_ERROR_CODES
        if self.alive() and code in AUTH_ERROR_CODES:
            self.auth_error = code

    def bind(self, session, read_figma):
        self.sessions.add(session)
        if read_figma:
            self.figma_session = session
        session.managed_execution_context = self
        session.on_managed_event = self.event

    def event(self, event):
        if not self.alive() or not isinstance(event, dict):
            return
        kind = event.get('type')
        if kind in {'figma-auth', 'figma-auth-required'} and self.remote and self.mode == 'acp':
            self.view = {'status': 'needs-auth', 'phase': 'figma-auth'}
            if url := _figma_url(event.get('url')):
                self.view['authUrl'] = url
        elif kind == 'figma-tool':
            self.view = {'status': 'running', 'phase': 'figma-reading'}
        elif kind == 'status':
            phase = event.get('stage')
            if phase == 'figma-auth-required' and self.remote and self.mode == 'acp':
                self.view = {'status': 'needs-auth', 'phase': 'figma-auth'}
            elif phase in _PHASES and (phase == 'figma-connected' or self.view['status'] == 'running'):
                self.view = {'status': 'running', 'phase': phase}

    async def close(self):
        self.closed = True
        async def cleanup():
            if self.authorization:
                self.authorization.cancel()
                await asyncio.gather(self.authorization, return_exceptions=True)
            await asyncio.gather(*(session.close() for session in self.sessions), return_exceptions=True)
            self.sessions.clear()
            self.figma_session = None
        if self.cleanup is None:
            self.cleanup = asyncio.create_task(cleanup())
        cancelled = False
        while not self.cleanup.done():
            try:
                await asyncio.shield(self.cleanup)
            except asyncio.CancelledError:
                cancelled = True
        self.cleanup.result()
        if cancelled:
            raise asyncio.CancelledError


@asynccontextmanager
async def execution_progress(payload, token, timeout):
    from hashlib import sha256
    import json
    source = urlsplit(payload.sourceReference)
    match = re.match(r'^/(?:design|file)/([A-Za-z0-9]{1,160})(?:/|$)', source.path)
    remote = bool(source.scheme == 'https' and source.netloc in {'figma.com', 'www.figma.com'}
                  and match and payload.archive is None)
    file_key = match[1] if remote else None
    if remote:
        require_figma_scope(payload.executionScope)
    context = ExecutionContext(str(payload.jobId), payload.attempt, token, payload.kind.lower(),
                               execution_mode(payload) if payload.engine == 'KIRO' else 'nova',
                               file_key, remote, monotonic() + timeout, asyncio.current_task(), _scope(payload))
    fingerprint = sha256(json.dumps(payload.model_dump(mode='json', exclude_none=True), sort_keys=True,
                                    ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    key = (context.job_id, context.attempt)
    with _LOCK:
        _attempt_stamp(context.job_id, context.attempt, _scope(payload), payload.deadlineEpochSeconds, fingerprint)
        previous = [entry for entry in _CONTEXTS.values() if entry.job_id == context.job_id]
        if any(entry.attempt >= context.attempt for entry in previous):
            raise HTTPException(409, 'Execution attempt is already running.')
        for entry in previous:
            entry.closed = True
            _CONTEXTS.pop((entry.job_id, entry.attempt), None)
        _CONTEXTS[key] = context
        if token:
            _TOKENS[token] = context
    try:
        await asyncio.gather(*(_retire(entry, context.task) for entry in previous))
        yield context
    finally:
        try:
            await context.close()
        finally:
            with _LOCK:
                if _CONTEXTS.get(key) is context:
                    _CONTEXTS.pop(key, None)
                if _TOKENS.get(token) is context:
                    _TOKENS.pop(token, None)
                stamp = _ATTEMPTS.get(context.job_id)
                if stamp and stamp['attempt'] == context.attempt and stamp['status'] == 'running':
                    stamp['status'] = 'finished'


async def _retire(context, protected_task=None):
    context.closed = True
    if context.task and context.task not in (asyncio.current_task(), protected_task) and not context.task.done():
        context.task.cancel()
        await asyncio.gather(context.task, return_exceptions=True)
    await context.close()


@asynccontextmanager
async def execution_lifespan(app):
    try:
        yield
    finally:
        with _LOCK:
            active = list(_CONTEXTS.values())
        await asyncio.gather(*(_retire(entry, asyncio.current_task()) for entry in active), return_exceptions=True)


def managed_execution_context(request):
    token = require_managed_context(request)
    with _LOCK:
        context = _TOKENS.get(token)
    if context and not context.alive():
        raise HTTPException(403, 'The execution attempt ended.')
    return context


def _find(job_id, attempt):
    if not 1 <= attempt <= MAX_ATTEMPT:
        raise _failure('EXECUTION_ATTEMPT_INVALID', 422)
    with _LOCK:
        context = _CONTEXTS.get((str(job_id), attempt))
    if not context or not context.alive() or not context.remote or context.purpose != 'screen' or context.mode != 'acp':
        raise HTTPException(404, 'Live ACP execution not found.')
    require_figma_scope(context.execution_scope)
    return context


@router.post('/{job_id}/attempts/{attempt}/cancel', status_code=204)
async def cancel(request: Request, job_id: UUID, attempt: int):
    _authorize(request)
    if not 1 <= attempt <= MAX_ATTEMPT:
        raise _failure('EXECUTION_ATTEMPT_INVALID', 422)
    from .managed_execution import _decode_json
    body = bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                if len(body) + len(chunk) > 1024:
                    raise _failure('EXECUTION_CANCEL_TOO_LARGE', 413)
                body.extend(chunk)
        value = Cancellation.model_validate(_decode_json(body))
    except (ValueError, RecursionError):
        raise _failure('EXECUTION_CANCEL_INVALID', 422) from None
    except TimeoutError:
        raise _failure('EXECUTION_CANCEL_TIMED_OUT', 408) from None
    if value.deadlineEpochSeconds > time() + 3630:
        raise _failure('EXECUTION_DEADLINE_INVALID', 422)
    with _LOCK:
        current = [entry for entry in _CONTEXTS.values() if entry.job_id == str(job_id) and entry.attempt <= attempt]
        if value.deadlineEpochSeconds + 30 > time() or current:
            _attempt_stamp(str(job_id), attempt, _scope(value), value.deadlineEpochSeconds, cancel=True)
            for entry in current:
                entry.closed = True
    await asyncio.gather(*(_retire(entry, asyncio.current_task()) for entry in current))


@router.get('/{job_id}/attempts/{attempt}/progress')
async def progress(request: Request, job_id: UUID, attempt: int):
    _authorize(request)
    return _find(job_id, attempt).public_view()


@router.post('/{job_id}/attempts/{attempt}/authorize')
async def authorize(request: Request, job_id: UUID, attempt: int):
    _authorize(request)
    context = _find(job_id, attempt)
    if context.view['status'] != 'needs-auth' or context.view.get('authUrl') or not context.figma_session:
        return context.public_view()
    async def request_authorization():
        try:
            require_figma_scope(context.execution_scope)
            value = await context.figma_session.call('authorize', timeout=35)
            if context.alive() and context.view['status'] == 'needs-auth' and (url := _figma_url(value)):
                context.view = {**context.view, 'authUrl': url}
        except FigmaProviderScopeError:
            raise
        except Exception:
            raise HTTPException(502, 'Figma authorization is unavailable.') from None
    if not context.authorization or context.authorization.done():
        context.authorization = asyncio.create_task(request_authorization())
    try:
        await asyncio.shield(context.authorization)
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling():
            raise
    return _find(job_id, attempt).public_view()

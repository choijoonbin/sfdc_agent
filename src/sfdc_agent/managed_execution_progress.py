"""Ephemeral ACP progress bound to one live Spring execution attempt."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import re
from threading import RLock
from time import monotonic
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from .managed_prompts import _authorize, managed_context_alive, require_managed_context

router = APIRouter(prefix='/internal/v1/executions', include_in_schema=False)
_CONTEXTS = {}
_TOKENS = {}
_LOCK = RLock()
_PHASES = {'starting', 'connecting', 'figma-connected', 'generating', 'figma-reading', 'analyzing', 'local-analyzing'}


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
    token: str
    purpose: str
    mode: str
    file_key: str | None
    remote: bool
    expires: float
    sessions: set = field(default_factory=set)
    figma_session: object | None = None
    authorization: asyncio.Task | None = None
    closed: bool = False
    view: dict = field(default_factory=lambda: {'status': 'running', 'phase': 'connecting'})

    def alive(self):
        return not self.closed and self.expires > monotonic() and managed_context_alive(self.token)

    def public_view(self):
        return {'jobId': self.job_id, 'attempt': self.attempt, **self.view}

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
        if self.authorization:
            self.authorization.cancel()
            await asyncio.gather(self.authorization, return_exceptions=True)
        await asyncio.gather(*(session.close() for session in self.sessions), return_exceptions=True)
        self.sessions.clear()
        self.figma_session = None


@asynccontextmanager
async def execution_progress(payload, token, timeout):
    if payload.engine != 'KIRO' or not token:
        yield None
        return
    source = urlsplit(payload.sourceReference)
    match = re.match(r'^/(?:design|file)/([A-Za-z0-9]{1,160})(?:/|$)', source.path)
    remote = bool(source.scheme == 'https' and source.netloc in {'figma.com', 'www.figma.com'}
                  and match and payload.archive is None)
    file_key = match[1] if remote else None
    context = ExecutionContext(str(payload.jobId), payload.attempt, token, payload.kind.lower(), execution_mode(payload),
                               file_key, remote, monotonic() + timeout)
    key = (context.job_id, context.attempt)
    with _LOCK:
        previous = [entry for entry in _CONTEXTS.values() if entry.job_id == context.job_id]
        if any(entry.attempt >= context.attempt for entry in previous):
            raise HTTPException(409, 'Execution attempt is already running.')
        for entry in previous:
            entry.closed = True
            _CONTEXTS.pop((entry.job_id, entry.attempt), None)
        _CONTEXTS[key] = context
        _TOKENS[token] = context
    try:
        await asyncio.gather(*(entry.close() for entry in previous))
        yield context
    finally:
        with _LOCK:
            if _CONTEXTS.get(key) is context:
                _CONTEXTS.pop(key, None)
            if _TOKENS.get(token) is context:
                _TOKENS.pop(token, None)
        await context.close()


def managed_execution_context(request):
    token = require_managed_context(request)
    with _LOCK:
        context = _TOKENS.get(token)
    if context and not context.alive():
        raise HTTPException(403, 'The execution attempt ended.')
    return context


def _find(job_id, attempt):
    with _LOCK:
        context = _CONTEXTS.get((str(job_id), attempt))
    if not context or not context.alive() or not context.remote or context.purpose != 'screen' or context.mode != 'acp':
        raise HTTPException(404, 'Live ACP execution not found.')
    return context


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
            value = await context.figma_session.call('authorize', timeout=35)
            if context.alive() and context.view['status'] == 'needs-auth' and (url := _figma_url(value)):
                context.view = {**context.view, 'authUrl': url}
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

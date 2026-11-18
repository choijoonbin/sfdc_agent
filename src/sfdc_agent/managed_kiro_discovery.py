"""Service-owned read-only ACP Figma discovery, separate from conversion sessions."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import os
import tempfile
from time import monotonic, time
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.requests import ClientDisconnect

from .kiro_gateway import KiroSession, SessionError
from .managed_execution import (IncompleteExecution, _decode_json, _encode_json, _read_output,
                                _runner_configuration, _stop_process, _write_input)
from .managed_prompts import PromptBundle, _authorize, checked_bundle
from .managed_execution_progress import ExecutionScope
from .managed_figma_scope import require_figma_scope
from .screen_tasks import build_screen_task

router = APIRouter(prefix='/internal/v1/figma-discoveries', include_in_schema=False)
LIFETIME_SECONDS = 15 * 60
MAX_DISCOVERIES = 64
MAX_ACTIVE = 4
REQUEST_BYTES = 4 * 1024 * 1024
ERRORS = {
    'FIGMA_PROVIDER_SCOPE_UNAVAILABLE': 'Figma 연결을 사용할 서버 범위를 먼저 구성해주세요.',
    'FIGMA_PROVIDER_SCOPE_FORBIDDEN': '이 워크스페이스에는 해당 Figma 연결이 배정되지 않았습니다.',
    'KIRO_UNAVAILABLE': 'Kiro 실행 환경을 시작할 수 없습니다.',
    'KIRO_VERSION': 'HTTP MCP를 지원하는 Kiro CLI V3가 필요합니다.',
    'KIRO_MODE_UNAVAILABLE': 'Kiro의 Figma 읽기 전용 모드를 시작하지 못했습니다.',
    'KIRO_FIGMA_UNAVAILABLE': 'Kiro에서 Figma MCP에 연결하지 못했습니다. Figma 연결 상태를 확인해주세요.',
    'KIRO_FIGMA_AUTH': 'Figma 로그인 주소를 받지 못했습니다. 다시 실행해주세요.',
    'KIRO_TIMEOUT': 'Kiro 목록 조회 시간이 초과됐습니다. 다시 실행해주세요.',
    'KIRO_EXITED': 'Kiro 연결이 종료됐습니다. 다시 실행해주세요.',
    'KIRO_CLOSED': 'Kiro 연결이 종료됐습니다. 다시 실행해주세요.',
    'KIRO_CANCELLED': 'Kiro 목록 조회를 취소했습니다.',
    'FIGMA_INVALID_INPUT': 'Figma 기획서 링크를 확인해주세요.',
    'FIGMA_DISCOVERY_TOO_LARGE': '화면 목록이 너무 큽니다. 변환할 화면 링크를 직접 입력해주세요.',
    'FIGMA_RATE_LIMITED': 'Figma MCP 조회 한도에 도달했습니다. 잠시 후 다시 시도해주세요.',
    'KIRO_FIGMA_METADATA_UNAVAILABLE': 'Figma MCP가 선택한 기획서의 목록을 반환하지 않았습니다.',
    'KIRO_FIGMA_METADATA_INVALID': 'Figma MCP가 반환한 기획서 목록을 확인할 수 없습니다.',
    'KIRO_DISCOVERY_FAILED': 'Kiro 목록 조회를 완료하지 못했습니다. 연결 상태를 확인해주세요.',
}
PHASES = {'starting', 'connecting', 'figma-auth-required', 'figma-connected', 'generating', 'figma-reading'}


class Reference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    fileKey: str = Field(pattern=r'^[A-Za-z0-9]{1,160}$')
    nodeId: str | None = Field(default=None, pattern=r'^\d+[:-]\d+$', max_length=40)
    pageId: str | None = Field(default=None, pattern=r'^\d+[:-]\d+$', max_length=40)
    selectedPageId: str | None = Field(default=None, pattern=r'^\d+[:-]\d+$', max_length=40)
    selectedNodeId: str | None = Field(default=None, pattern=r'^\d+[:-]\d+$', max_length=40)

    @field_validator('nodeId', 'pageId', 'selectedPageId', 'selectedNodeId')
    @classmethod
    def normalize_node(cls, value):
        return value.replace('-', ':') if value else value


class DiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: UUID
    executionScope: ExecutionScope
    reference: Reference
    promptBundle: PromptBundle
    timeoutSeconds: int = Field(strict=True, ge=30, le=LIFETIME_SECONDS)
    deadlineEpochSeconds: int = Field(strict=True, gt=0)


def auth_url(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    try:
        url = urlsplit(value)
        if (url.scheme == 'https' and url.netloc in {'figma.com', 'www.figma.com'}
                and not url.username and not url.password and url.path.startswith('/oauth/')):
            return value
    except ValueError:
        pass
    return None


async def project_discovery(reference, result):
    binary, execution_runner, _ = _runner_configuration()
    runner = execution_runner.with_name('managed-kiro-discovery.mjs')
    if not runner.is_file() or runner.is_symlink():
        raise IncompleteExecution
    encoded = _encode_json({'reference': reference, 'promptResult': result})
    if len(encoded) > 24 * 1024 * 1024:
        raise IncompleteExecution
    with tempfile.TemporaryDirectory(prefix='sfdc-figma-discovery-') as scratch:
        environment = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'LC_CTYPE') if key in os.environ}
        environment.update({'TMPDIR': scratch, 'TEMP': scratch, 'TMP': scratch})
        process = await asyncio.create_subprocess_exec(binary, str(runner), cwd=scratch, env=environment,
            start_new_session=os.name == 'posix', stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        tasks = (asyncio.create_task(_write_input(process, encoded)), asyncio.create_task(_read_output(process.stdout)))
        finished = False
        try:
            async with asyncio.timeout(30):
                _, output = await asyncio.gather(*tasks)
                code = await process.wait()
            finished = True
            if code or len(output) > 4 * 1024 * 1024:
                raise IncompleteExecution
        finally:
            if not finished:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await _stop_process(process)
    value = _decode_json(output)
    if not isinstance(value, dict) or set(value) not in ({'result'}, {'error'}):
        raise IncompleteExecution
    if 'error' in value:
        code = value['error'].get('code') if isinstance(value['error'], dict) else None
        raise SessionError(code if code in ERRORS else 'KIRO_DISCOVERY_FAILED', '')
    result = value['result']
    if not isinstance(result, dict) or not (isinstance(result.get('pages'), list) or isinstance(result.get('page'), dict)):
        raise IncompleteExecution
    return result


@dataclass
class Discovery:
    payload: DiscoveryRequest
    fingerprint: str
    expires: float
    checkpoint: dict
    view: dict
    task: asyncio.Task | None = None
    session: KiroSession | None = None
    authorization: asyncio.Task | None = None

    def public_view(self):
        return {**self.view, 'promptCheckpoint': self.checkpoint}


class Discoveries:
    def __init__(self):
        self.jobs = {}
        self.lock = asyncio.Lock()

    async def purge(self):
        expired = [key for key, job in self.jobs.items() if job.expires <= monotonic()]
        for key in expired:
            job = self.jobs.pop(key, None)
            if job:
                await self.close_job(job)

    async def close_job(self, job):
        tasks = [task for task in (job.task, job.authorization) if task is not None and task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if job.session:
            await job.session.close()

    async def close(self):
        jobs, self.jobs = list(self.jobs.values()), {}
        await asyncio.gather(*(self.close_job(job) for job in jobs), return_exceptions=True)

    async def start(self, payload):
        require_figma_scope(payload.executionScope)
        remaining = payload.deadlineEpochSeconds - time()
        if remaining <= 0:
            raise HTTPException(410, 'Discovery deadline expired.')
        if remaining > LIFETIME_SECONDS:
            raise HTTPException(422, 'Discovery deadline exceeds the supported lifetime.')
        release = checked_bundle(payload.promptBundle)
        fields = {'fileKey': payload.reference.fileKey, 'nodeId': payload.reference.pageId}
        text = build_screen_task('screen.discovery', fields, templates=release.templates)
        prompt = text.encode()
        if len(prompt) > 1024 * 1024:
            raise HTTPException(422, 'Discovery prompt is too large.')
        normalized = payload.model_dump(mode='json', exclude_none=True)
        fingerprint = sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        async with self.lock:
            await self.purge()
            remaining = payload.deadlineEpochSeconds - time()
            if remaining <= 0:
                raise HTTPException(410, 'Discovery deadline expired.')
            key = str(payload.id)
            if existing := self.jobs.get(key):
                if existing.fingerprint != fingerprint:
                    raise HTTPException(409, 'Discovery id belongs to a different request.')
                return existing.public_view()
            if len(self.jobs) >= MAX_DISCOVERIES or sum(job.view['status'] in {'running', 'needs-auth'} for job in self.jobs.values()) >= MAX_ACTIVE:
                raise HTTPException(503, 'Kiro discovery is busy.', headers={'Retry-After': '2'})
            checkpoint = {'releaseId': release.id, 'bundleSha256': payload.promptBundle.sha256,
                'templateSha256': sha256(release.templates['screen.discovery'].encode()).hexdigest(),
                'promptDigest': sha256(prompt).hexdigest(), 'promptBytes': len(prompt), 'operation': 'screen.discovery'}
            job = Discovery(payload, fingerprint, monotonic() + remaining, checkpoint,
                {'id': key, 'status': 'running', 'phase': 'connecting'})
            self.jobs[key] = job
            job.task = asyncio.create_task(self.run(job, text))
            return job.public_view()

    async def get(self, key):
        await self.purge()
        job = self.jobs.get(str(key))
        if not job:
            raise HTTPException(404, 'Discovery not found.')
        if job.view['status'] in {'running', 'needs-auth'}:
            require_figma_scope(job.payload.executionScope)
        return job

    def event(self, job, event):
        if job.view['status'] not in {'running', 'needs-auth'} or not isinstance(event, dict):
            return
        key = str(job.payload.id)
        if event.get('type') in {'figma-auth', 'figma-auth-required'}:
            job.view = {'id': key, 'status': 'needs-auth', 'phase': 'figma-auth'}
            if url := auth_url(event.get('url')):
                job.view['authUrl'] = url
        elif event.get('type') == 'figma-tool':
            job.view = {'id': key, 'status': 'running', 'phase': 'figma-reading'}
        elif event.get('type') == 'status' and event.get('stage') in PHASES:
            phase = event['stage']
            if phase == 'figma-auth-required':
                job.view = {'id': key, 'status': 'needs-auth', 'phase': 'figma-auth'}
            elif phase == 'figma-connected' or job.view['status'] == 'running':
                job.view = {'id': key, 'status': 'running', 'phase': phase}

    async def watch_events(self, job):
        session = job.session
        while True:
            events, session.events = session.events, []
            session.updated.clear()
            for event in events:
                self.event(job, event)
            await session.updated.wait()

    async def run(self, job, text):
        watcher = None
        try:
            async with asyncio.timeout(min(job.payload.timeoutSeconds, max(0, job.payload.deadlineEpochSeconds - time()))):
                require_figma_scope(job.payload.executionScope)
                opening = asyncio.create_task(KiroSession.start())
                try:
                    job.session = await asyncio.shield(opening)
                except asyncio.CancelledError:
                    # Process creation can finish just as cancellation arrives. Always
                    # take ownership of that child before closing the discovery.
                    job.session = await opening
                    raise
                watcher = asyncio.create_task(self.watch_events(job))
                require_figma_scope(job.payload.executionScope)
                await job.session.call('open', {'reference': {'fileKey': job.payload.reference.fileKey}, 'readFigma': True}, timeout=65)
                require_figma_scope(job.payload.executionScope)
                result = await job.session.call('prompt', {'text': text}, timeout=job.payload.timeoutSeconds)
                require_figma_scope(job.payload.executionScope)
                result = await project_discovery(job.payload.reference.model_dump(exclude_none=True), result)
                job.view = {'id': str(job.payload.id), 'status': 'complete', 'result': result}
        except asyncio.CancelledError:
            job.view = {'id': str(job.payload.id), 'status': 'failed',
                        'error': {'code': 'KIRO_CANCELLED', 'message': ERRORS['KIRO_CANCELLED']}}
            raise
        except Exception as error:
            code = 'KIRO_TIMEOUT' if isinstance(error, TimeoutError) else getattr(error, 'code', None)
            code = code if code in ERRORS else 'KIRO_DISCOVERY_FAILED'
            job.view = {'id': str(job.payload.id), 'status': 'failed', 'error': {'code': code, 'message': ERRORS[code]}}
        finally:
            if watcher:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            if job.authorization:
                job.authorization.cancel()
                await asyncio.gather(job.authorization, return_exceptions=True)
            if job.session:
                await job.session.close()
                job.session = None

    async def authorize(self, job):
        require_figma_scope(job.payload.executionScope)
        if job.view['status'] != 'needs-auth' or job.view.get('authUrl') or not job.session:
            return job.public_view()
        async def request_authorization():
            try:
                require_figma_scope(job.payload.executionScope)
                value = await job.session.call('authorize', timeout=35)
                if job.view['status'] == 'needs-auth' and (url := auth_url(value)):
                    job.view = {**job.view, 'authUrl': url}
            except SessionError as error:
                # A failed authorization attempt must not reveal arbitrary provider text.
                code = error.code if error.code in ERRORS else 'KIRO_FIGMA_AUTH'
                if job.view['status'] == 'needs-auth':
                    job.view['error'] = {'code': code, 'message': ERRORS[code]}
        if not job.authorization or job.authorization.done():
            job.authorization = asyncio.create_task(request_authorization())
        try:
            await asyncio.shield(job.authorization)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            # The discovery may finish or be explicitly deleted while an OAuth
            # URL request is still in flight. Its terminal view remains valid.
        return job.public_view()


@asynccontextmanager
async def discovery_lifespan(app):
    manager = app.state.figma_discoveries = Discoveries()
    async def reap():
        while True:
            await asyncio.sleep(10)
            await manager.purge()
    reaper = asyncio.create_task(reap())
    try:
        yield
    finally:
        reaper.cancel()
        await asyncio.gather(reaper, return_exceptions=True)
        await manager.close()


async def read_payload(request):
    body = bytearray()
    try:
        async with asyncio.timeout(15):
            async for chunk in request.stream():
                if len(body) + len(chunk) > REQUEST_BYTES:
                    raise HTTPException(413, 'Discovery input is too large.')
                body.extend(chunk)
        payload = DiscoveryRequest.model_validate(_decode_json(body))
        checked_bundle(payload.promptBundle)
        return payload
    except ClientDisconnect:
        raise HTTPException(499, 'Discovery request disconnected.') from None
    except TimeoutError:
        raise HTTPException(408, 'Discovery input timed out.') from None
    except (ValueError, TypeError, RecursionError):
        raise HTTPException(422, 'Invalid discovery request.') from None


@router.post('')
async def create(request: Request):
    _authorize(request)
    return await request.app.state.figma_discoveries.start(await read_payload(request))


@router.get('/{discovery_id}')
async def read(request: Request, discovery_id: UUID):
    _authorize(request)
    return (await request.app.state.figma_discoveries.get(discovery_id)).public_view()


@router.post('/{discovery_id}/authorize')
async def authorize(request: Request, discovery_id: UUID):
    _authorize(request)
    manager = request.app.state.figma_discoveries
    return await manager.authorize(await manager.get(discovery_id))


@router.delete('/{discovery_id}', status_code=204)
async def delete(request: Request, discovery_id: UUID):
    _authorize(request)
    manager = request.app.state.figma_discoveries
    if job := manager.jobs.pop(str(discovery_id), None):
        await manager.close_job(job)

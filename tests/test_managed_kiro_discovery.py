import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
from time import monotonic, time
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException

from sfdc_agent.api import app
from sfdc_agent.kiro_gateway import SessionError
from sfdc_agent.managed_kiro_discovery import (
    Discoveries, DiscoveryRequest, auth_url, discovery_lifespan, project_discovery,
)
from sfdc_agent.managed_prompts import template_digest
from sfdc_agent.prompt_templates import load_baseline_templates


BASE = '/internal/v1/figma-discoveries'
TOKEN = 'discovery-service-fixture'
AUTH = 'https://www.figma.com/oauth/mcp?state=expected-protocol-state'


class FakeACP:
    def __init__(self, needs_auth=False):
        self.events = []
        self.updated = asyncio.Event()
        self.gate = asyncio.Event()
        self.needs_auth = needs_auth
        self.calls = []
        self.closed = False

    def emit(self, event):
        self.events.append(event)
        self.updated.set()

    async def call(self, method, params=None, timeout=None):
        self.calls.append((method, params))
        if method == 'open':
            if self.needs_auth:
                self.emit({'type': 'figma-auth-required'})
            return None
        if method == 'authorize':
            self.emit({'type': 'figma-auth', 'url': AUTH})
            return AUTH
        if method == 'prompt':
            await self.gate.wait()
            return {'text': 'Only MCP evidence is projected', 'toolCalls': [{
                'name': 'get_metadata', 'status': 'completed', 'input': {'fileKey': 'RealFile'},
                'output': {'content': [{'text': '<document><canvas id="1:2" name="실제 페이지" /></document>'}]},
            }]}
        raise AssertionError(method)

    async def close(self):
        self.closed = True


class ManagedKiroDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scope = {'tenantId': 'aa000000-0000-4000-8000-000000000001', 'workspaceId': 'ad000000-0000-4000-8000-000000000001'}
        self.enterContext(patch.dict(os.environ, {'SFDC_MANAGED_FIGMA_SCOPE': self.scope['tenantId'] + '/' + self.scope['workspaceId']}))
        templates = load_baseline_templates()
        self.raw = {'id': str(uuid4()), 'executionScope': self.scope, 'reference': {'fileKey': 'RealFile', 'nodeId': '1:2', 'selectedPageId': '1:2'},
                    'promptBundle': {'releaseId': 'prompts-v1', 'sha256': template_digest(templates), 'templates': templates},
                    'timeoutSeconds': 30, 'deadlineEpochSeconds': int(time()) + 60}
        self.manager = Discoveries()
        self.addAsyncCleanup(self.manager.close)

    def payload(self, **changes):
        return DiscoveryRequest.model_validate({**self.raw, **changes})

    async def wait_for(self, predicate):
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(0.005)

    async def test_uses_pinned_prompt_actual_acp_and_original_metadata_projection(self):
        session = FakeACP(needs_auth=True)
        runner = Path(__file__).resolve().parents[2] / 'SFDC_Ui/nextbss/server/managed-execution.mjs'
        self.enterContext(patch.dict(os.environ, {'SFDC_MANAGED_EXECUTION_RUNNER': str(runner),
                                                 'SFDC_AGENT_EXECUTION_TOKEN': 'not-forwarded-to-projector'}))
        with patch('sfdc_agent.managed_kiro_discovery.KiroSession.start', new=AsyncMock(return_value=session)):
            view = await self.manager.start(self.payload())
            self.assertEqual(view['status'], 'running')
            job = await self.manager.get(self.raw['id'])
            await self.wait_for(lambda: job.view['status'] == 'needs-auth')
            self.assertEqual((await self.manager.authorize(job))['authUrl'], AUTH)
            self.assertEqual(session.calls[0], ('open', {'reference': {'fileKey': 'RealFile'}, 'readFigma': True}))
            self.assertNotIn('headless', json.dumps(session.calls))
            prompt = next(params['text'] for method, params in session.calls if method == 'prompt')
            self.assertEqual(view['promptCheckpoint']['promptDigest'], sha256(prompt.encode()).hexdigest())
            self.assertEqual(view['promptCheckpoint']['bundleSha256'], self.raw['promptBundle']['sha256'])
            self.assertNotIn('pageId', self.payload().reference.model_dump(exclude_none=True))
            session.emit({'type': 'status', 'stage': 'figma-connected'})
            session.gate.set()
            await job.task
            self.assertEqual(job.view['status'], 'complete', job.view)
            self.assertEqual(job.view['result']['pages'], [{'id': '1:2', 'name': '실제 페이지'}])
            self.assertEqual(job.view['result']['selectedPageId'], '1:2')
            self.assertEqual(job.view['result']['source']['provider'], 'kiro')
            self.assertTrue(session.closed)

    async def test_idempotence_conflict_bound_and_delete_cleanup(self):
        session = FakeACP()
        start = AsyncMock(return_value=session)
        with patch('sfdc_agent.managed_kiro_discovery.KiroSession.start', new=start), patch('sfdc_agent.managed_kiro_discovery.MAX_ACTIVE', 1):
            first = await self.manager.start(self.payload())
            second = await self.manager.start(self.payload())
            self.assertEqual(first, second)
            await self.wait_for(lambda: bool(session.calls))
            self.assertEqual(start.await_count, 1)
            with self.assertRaises(HTTPException) as conflict:
                await self.manager.start(self.payload(timeoutSeconds=31))
            self.assertEqual(conflict.exception.status_code, 409)
            with self.assertRaises(HTTPException) as busy:
                await self.manager.start(self.payload(id=str(uuid4())))
            self.assertEqual(busy.exception.status_code, 503)
            job = await self.manager.get(self.raw['id'])
            await self.manager.close_job(job)
            self.assertTrue(session.closed)
            self.assertTrue(job.task.cancelled())

    async def test_expiry_and_lifespan_cancel_pending_acp(self):
        session = FakeACP()
        with patch('sfdc_agent.managed_kiro_discovery.KiroSession.start', new=AsyncMock(return_value=session)):
            await self.manager.start(self.payload())
            await self.wait_for(lambda: bool(session.calls))
            job = self.manager.jobs[self.raw['id']]
            job.expires = monotonic() - 1
            with self.assertRaises(HTTPException) as missing:
                await self.manager.get(self.raw['id'])
            self.assertEqual(missing.exception.status_code, 404)
            self.assertTrue(session.closed)
            with patch('sfdc_agent.managed_kiro_discovery.KiroSession.start', new=AsyncMock(return_value=FakeACP())):
                async with discovery_lifespan(app):
                    manager = app.state.figma_discoveries
                    await manager.start(self.payload())
                    job = manager.jobs[self.raw['id']]
                    await asyncio.sleep(0)
                self.assertTrue(job.task.cancelled())
                self.assertFalse(manager.jobs)

    async def test_cancellation_takes_ownership_of_a_child_created_during_process_start(self):
        session = FakeACP()
        started, release = asyncio.Event(), asyncio.Event()
        async def opening():
            started.set()
            await release.wait()
            return session
        with patch('sfdc_agent.managed_kiro_discovery.KiroSession.start', new=opening):
            await self.manager.start(self.payload())
            job = await self.manager.get(self.raw['id'])
            await started.wait()
            closing = asyncio.create_task(self.manager.close_job(job))
            await asyncio.sleep(0)
            release.set()
            await closing
            self.assertTrue(session.closed)
            self.assertEqual(job.view['error']['code'], 'KIRO_CANCELLED')

    async def test_timeout_covers_auth_wait_and_authorization_retries_share_one_rpc(self):
        session = FakeACP(needs_auth=True)
        authorization_started, authorization_release = asyncio.Event(), asyncio.Event()
        original_call = session.call
        async def call(method, params=None, timeout=None):
            if method == 'authorize':
                session.calls.append((method, params))
                authorization_started.set()
                await authorization_release.wait()
                return AUTH
            return await original_call(method, params, timeout)
        session.call = call
        with patch('sfdc_agent.managed_kiro_discovery.KiroSession.start', new=AsyncMock(return_value=session)):
            await self.manager.start(self.payload())
            job = await self.manager.get(self.raw['id'])
            await self.wait_for(lambda: job.view['status'] == 'needs-auth')
            one = asyncio.create_task(self.manager.authorize(job))
            two = asyncio.create_task(self.manager.authorize(job))
            await authorization_started.wait()
            authorization_release.set()
            views = await asyncio.gather(one, two)
            self.assertEqual(sum(method == 'authorize' for method, _ in session.calls), 1)
            self.assertTrue(all(view['authUrl'] == AUTH for view in views))
            await self.manager.close()
        timed = FakeACP(needs_auth=True)
        with patch('sfdc_agent.managed_kiro_discovery.KiroSession.start', new=AsyncMock(return_value=timed)):
            # Keep the public policy minimum30 while the immutable deadline is
            # about to expire. A restart cannot extend that deadline.
            deadline = int(time()) + 1
            with patch('sfdc_agent.managed_kiro_discovery.time', return_value=deadline - 0.01):
                await self.manager.start(self.payload(deadlineEpochSeconds=deadline))
                job = await self.manager.get(self.raw['id'])
                await job.task
            self.assertEqual(job.view['error']['code'], 'KIRO_TIMEOUT')
            self.assertTrue(timed.closed)

    async def test_immutable_deadline_and_160_character_file_key_survive_idempotent_retry(self):
        session = FakeACP()
        key = 'A' * 160
        with patch('sfdc_agent.managed_kiro_discovery.KiroSession.start', new=AsyncMock(return_value=session)):
            request = self.payload(reference={'fileKey': key})
            await self.manager.start(request)
            await self.wait_for(lambda: bool(session.calls))
            self.assertEqual(session.calls[0][1]['reference']['fileKey'], key)
            self.assertIn(key, next(params['text'] for method, params in session.calls if method == 'prompt'))
            await self.manager.start(request)
            with self.assertRaises(HTTPException) as changed:
                await self.manager.start(request.model_copy(update={'deadlineEpochSeconds': self.raw['deadlineEpochSeconds'] + 1}))
            self.assertEqual(changed.exception.status_code, 409)
            with self.assertRaises(HTTPException) as expired:
                await self.manager.start(request.model_copy(update={'deadlineEpochSeconds': int(time()) - 1}))
            self.assertEqual(expired.exception.status_code, 410)
            with self.assertRaises(HTTPException) as unbounded:
                await self.manager.start(request.model_copy(update={'deadlineEpochSeconds': int(time()) + 902}))
            self.assertEqual(unbounded.exception.status_code, 422)

    async def test_completion_while_authorize_is_pending_returns_terminal_view(self):
        session = FakeACP(needs_auth=True)
        authorization_started = asyncio.Event()
        original_call = session.call
        async def call(method, params=None, timeout=None):
            if method == 'authorize':
                authorization_started.set()
                await asyncio.Future()
            return await original_call(method, params, timeout)
        session.call = call
        with patch('sfdc_agent.managed_kiro_discovery.KiroSession.start', new=AsyncMock(return_value=session)), \
             patch('sfdc_agent.managed_kiro_discovery.project_discovery', new=AsyncMock(return_value={'pages': []})):
            await self.manager.start(self.payload())
            job = await self.manager.get(self.raw['id'])
            await self.wait_for(lambda: job.view['status'] == 'needs-auth')
            authorizing = asyncio.create_task(self.manager.authorize(job))
            await authorization_started.wait()
            session.gate.set()
            await job.task
            self.assertEqual((await authorizing)['status'], 'complete')
            self.assertTrue(session.closed)

    async def test_sanitizes_events_errors_and_rejects_unsafe_auth_links(self):
        session = FakeACP()
        with patch('sfdc_agent.managed_kiro_discovery.KiroSession.start', new=AsyncMock(return_value=session)):
            await self.manager.start(self.payload())
            job = await self.manager.get(self.raw['id'])
            await self.wait_for(lambda: bool(session.calls))
            for url in ['https://figma.com.evil.invalid/oauth/mcp?token=private', 'http://figma.com/oauth/mcp',
                        'https://figma.com@evil.invalid/oauth/mcp', 'https://figma.com:443/oauth/mcp',
                        'https://figma.com/oauth-evil', 'https://figma.com/oauth/mcp\r\nsecret']:
                self.assertIsNone(auth_url(url), url)
                self.manager.event(job, {'type': 'figma-auth', 'url': url})
                self.assertNotIn('authUrl', job.view)
            self.manager.event(job, {'type': 'status', 'stage': '/private/path?token=secret'})
            self.assertNotIn('secret', json.dumps(job.public_view()))
        await self.manager.close()
        with patch('sfdc_agent.managed_kiro_discovery.KiroSession.start', new=AsyncMock(side_effect=SessionError('KIRO_EXITED', 'raw-password-private-path'))):
            await self.manager.start(self.payload())
            job = await self.manager.get(self.raw['id'])
            await job.task
            self.assertEqual(job.view['error']['code'], 'KIRO_EXITED')
            self.assertNotIn('raw-password', json.dumps(job.public_view()))

    async def request(self, method, path, body=b'{}', token=TOKEN, extra=(), disconnected=False):
        headers = [(b'host', b'127.0.0.1:8765'), (b'content-type', b'application/json')]
        if token is not None:
            headers.append((b'authorization', ('Bearer ' + token).encode()))
        headers.extend(extra)
        messages = []
        received = False
        async def receive():
            nonlocal received
            if received:
                await asyncio.Future()
            received = True
            if disconnected:
                return {'type': 'http.disconnect'}
            return {'type': 'http.request', 'body': body, 'more_body': False}
        async def send(message):
            messages.append(message)
        await app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'scheme': 'http',
                   'method': method, 'path': path, 'root_path': '', 'query_string': b'', 'headers': headers,
                   'server': ('127.0.0.1', 8765), 'client': ('127.0.0.1', 40000)}, receive, send)
        raw = b''.join(message.get('body', b'') for message in messages)
        return messages[0]['status'], json.loads(raw) if raw else None, received

    async def test_service_boundary_validates_before_body_and_never_opens_legacy_registry(self):
        self.enterContext(patch.dict(os.environ, {'SFDC_AGENT_EXECUTION_TOKEN': TOKEN, 'SFDC_AGENT_MANAGED_ONLY': '1'}))
        async with discovery_lifespan(app):
            for method, path in [('POST', BASE), ('GET', BASE + '/' + self.raw['id']),
                                 ('POST', BASE + '/' + self.raw['id'] + '/authorize'), ('DELETE', BASE + '/' + self.raw['id'])]:
                status, _, received = await self.request(method, path, body=b'not json', token=None)
                self.assertEqual(status, 401)
                self.assertFalse(received)
            self.assertEqual((await self.request('POST', BASE, extra=((b'origin', b'https://browser.invalid'),)))[0], 403)
            changed = {**self.raw, 'promptBundle': {**self.raw['promptBundle'], 'sha256': '0' * 64}}
            with patch('sfdc_agent.prompt_registry.runtime_release', side_effect=AssertionError('No SQLite')):
                self.assertEqual((await self.request('POST', BASE, json.dumps(changed).encode()))[0], 422)
            self.assertEqual((await self.request('POST', BASE, json.dumps({**self.raw, 'timeoutSeconds': True}).encode()))[0], 422)
            self.assertEqual((await self.request('POST', BASE, json.dumps({**self.raw, 'sessionId': 'not-allowed'}).encode()))[0], 422)
            self.assertEqual((await self.request('DELETE', BASE + '/' + self.raw['id']))[0], 204)
            self.assertEqual((await self.request('POST', BASE, disconnected=True))[0], 499)
            self.assertFalse(app.state.figma_discoveries.jobs)

    async def test_service_create_poll_conflict_and_delete_use_only_dedicated_lifecycle(self):
        session = FakeACP(needs_auth=True)
        self.enterContext(patch.dict(os.environ, {'SFDC_AGENT_EXECUTION_TOKEN': TOKEN, 'SFDC_AGENT_MANAGED_ONLY': '1'}))
        self.enterContext(patch('sfdc_agent.managed_kiro_discovery.KiroSession.start', new=AsyncMock(return_value=session)))
        async with discovery_lifespan(app):
            status, view, _ = await self.request('POST', BASE, json.dumps(self.raw).encode())
            self.assertEqual(status, 200)
            self.assertEqual(view['status'], 'running')
            await self.wait_for(lambda: bool(session.calls))
            status, view, _ = await self.request('GET', BASE + '/' + self.raw['id'])
            self.assertEqual(status, 200)
            self.assertEqual(view['id'], self.raw['id'])
            self.assertNotIn('sessionId', json.dumps(view))
            changed = {**self.raw, 'reference': {'fileKey': 'AnotherFile'}}
            self.assertEqual((await self.request('POST', BASE, json.dumps(changed).encode()))[0], 409)
            self.assertEqual((await self.request('DELETE', BASE + '/' + self.raw['id']))[0], 204)
            self.assertTrue(session.closed)
            self.assertEqual((await self.request('GET', BASE + '/' + self.raw['id']))[0], 404)

    async def test_all_13_maximum_legal_quote_escaped_templates_fit_the_bounded_request(self):
        templates = {name: body + '"' * (128 * 1024 - len(body.encode()))
                     for name, body in load_baseline_templates().items()}
        self.assertTrue(all(len(body.encode()) == 128 * 1024 for body in templates.values()))
        bundle = {'releaseId': 'prompts-v1', 'sha256': template_digest(templates), 'templates': templates}
        body = json.dumps({**self.raw, 'promptBundle': bundle}, ensure_ascii=False).encode()
        self.assertGreater(len(body), 2 * 1024 * 1024)
        self.assertLess(len(body), 4 * 1024 * 1024)
        session = FakeACP()
        self.enterContext(patch.dict(os.environ, {'SFDC_AGENT_EXECUTION_TOKEN': TOKEN, 'SFDC_AGENT_MANAGED_ONLY': '1'}))
        self.enterContext(patch('sfdc_agent.managed_kiro_discovery.KiroSession.start', new=AsyncMock(return_value=session)))
        async with discovery_lifespan(app):
            status, view, _ = await self.request('POST', BASE, body)
            self.assertEqual(status, 200, view)
            self.assertEqual(view['promptCheckpoint']['bundleSha256'], bundle['sha256'])
            self.assertEqual(view['status'], 'running')
            over_limit = b' ' * (4 * 1024 * 1024 + 1)
            status, _, read = await self.request('POST', BASE, over_limit, token=None)
            self.assertEqual(status, 401)
            self.assertFalse(read)
            self.assertEqual((await self.request('POST', BASE, over_limit))[0], 413)

    async def test_page_metadata_is_selected_by_exact_pinned_file_and_page(self):
        reference = {'fileKey': 'RealFile', 'pageId': '1:2', 'selectedNodeId': '1:3'}
        result = {'toolCalls': [{'name': 'get_metadata', 'status': 'completed',
            'input': {'fileKey': 'RealFile', 'nodeId': '1:2'},
            'output': {'text': '<canvas id="1:2" name="페이지"><frame id="1:3" name="원래 화면" width="800" height="600" /></canvas>'}}]}
        value = await project_discovery(reference, result)
        self.assertEqual(value['selectedScreenId'], '1:3')
        self.assertEqual(value['page']['screens'][0]['name'], '원래 화면')
        result['toolCalls'][0]['input']['fileKey'] = 'OtherFile'
        with self.assertRaises(SessionError) as missing:
            await project_discovery(reference, result)
        self.assertEqual(missing.exception.code, 'KIRO_FIGMA_METADATA_UNAVAILABLE')

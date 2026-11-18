import asyncio
import json
import os
from time import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from starlette.requests import Request

from sfdc_agent.api import app
from sfdc_agent.kiro_gateway import KiroSession, kiro_lifespan
from sfdc_agent.managed_execution import ExecutionRequest, _check_kiro_configuration
from sfdc_agent.managed_execution_progress import execution_mode, execution_progress
from sfdc_agent.managed_prompts import PromptBundle, execution_prompts, runner_token, template_digest
from sfdc_agent.prompt_templates import load_baseline_templates
from tests import test_kiro_gateway as gateway_fixtures
from tests.test_kiro_gateway import StubSession


BASE = '/internal/kiro/sessions'
SERVICE = ((b'authorization', b'Bearer progress-service-fixture'),)


class ManagedExecutionProgressTests(unittest.IsolatedAsyncioTestCase):
    request = gateway_fixtures.GatewayTests.request

    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'SFDC_AGENT_MANAGED_ONLY': '1',
            'SFDC_AGENT_EXECUTION_TOKEN': 'progress-service-fixture', 'SFDC_AGENT_SCREEN_KIRO_MODE': 'acp',
            'SFDC_AGENT_STORY_KIRO_MODE': 'acp', 'SFDC_MANAGED_AGENT_ORIGIN': 'http://127.0.0.1:8765',
            'KIRO_API_KEY': ''}))
        templates = load_baseline_templates()
        self.bundle = PromptBundle(releaseId='prompts-v1', sha256=template_digest(templates), templates=templates)
        self.payload = ExecutionRequest.model_validate({'jobId': str(uuid4()), 'attempt': 1, 'kind': 'SCREEN',
            'executionScope': {'tenantId': str(uuid4()), 'workspaceId': str(uuid4())},
            'timeoutSeconds': 90, 'deadlineEpochSeconds': int(time()) + 91,
            'engine': 'KIRO', 'sourceReference': 'https://www.figma.com/design/ApprovedFile?node-id=1-2',
            'designReleaseId': 'nextbss-1.9-' + 'a' * 64, 'promptReleaseId': 'prompts-v1',
            'promptBundle': self.bundle.model_dump()})
        self.progress = '/internal/v1/executions/' + str(self.payload.jobId) + '/attempts/1'
        self.enterContext(patch.dict(os.environ, {'SFDC_MANAGED_FIGMA_SCOPE': str(self.payload.executionScope.tenantId) + '/' + str(self.payload.executionScope.workspaceId)}))

    def headers(self):
        return ((b'x-sfdc-managed-prompt', runner_token().encode()), (b'x-sfdc-kiro-purpose', b'screen'))

    async def test_actual_bound_session_events_and_authorize_are_private_attempt_scoped(self):
        session = StubSession()
        async with kiro_lifespan(app):
          with execution_prompts(self.bundle):
            async with execution_progress(self.payload, runner_token(), 90) as context:
                with patch('sfdc_agent.kiro_gateway.KiroSession.start', new=AsyncMock(return_value=session)):
                    status, opened = await self.request('POST', BASE, {'fileKey': 'ApprovedFile', 'readFigma': True,
                        'promptReleaseId': 'prompts-v1'}, extra_headers=self.headers())
                self.assertEqual(status, 200, opened)
                self.assertEqual(session.calls[0], ('open', {'reference': {'fileKey': 'ApprovedFile'}, 'readFigma': True}))
                self.assertEqual(context.figma_session, session)
                session.on_managed_event({'type': 'figma-auth-required'})
                self.assertEqual((await self.request('GET', self.progress + '/progress'))[0], 401)
                self.assertEqual((await self.request('GET', self.progress.replace('/attempts/1', '/attempts/2') + '/progress', extra_headers=SERVICE))[0], 404)
                status, view = await self.request('GET', self.progress + '/progress', extra_headers=SERVICE)
                self.assertEqual(status, 200)
                self.assertEqual(view, {'jobId': str(self.payload.jobId), 'attempt': 1, 'status': 'needs-auth', 'phase': 'figma-auth'})
                for _ in range(2):
                    status, view = await self.request('POST', self.progress + '/authorize', {}, extra_headers=SERVICE)
                    self.assertEqual(status, 200)
                    self.assertEqual(view['authUrl'], 'https://www.figma.com/oauth/authorize')
                self.assertEqual(sum(method == 'authorize' for method, _ in session.calls), 1)
                session.on_managed_event({'type': 'status', 'stage': 'figma-connected'})
                self.assertEqual((await self.request('GET', self.progress + '/progress', extra_headers=SERVICE))[1]['status'], 'running')
                self.assertNotIn('authUrl', context.public_view())
            self.assertTrue(session.closed)
            session.on_managed_event({'type': 'figma-auth', 'url': 'https://figma.com/oauth/mcp?state=late'})
            self.assertEqual((await self.request('GET', self.progress + '/progress', extra_headers=SERVICE))[0], 404)

    async def test_wrong_file_unbound_context_raw_prompt_and_browser_calls_remain_blocked(self):
        session = StubSession()
        async with kiro_lifespan(app):
          with execution_prompts(self.bundle):
            input = {'fileKey': 'ApprovedFile', 'readFigma': True, 'promptReleaseId': 'prompts-v1'}
            self.assertEqual((await self.request('POST', BASE, input, extra_headers=self.headers()))[0], 422)
            async with execution_progress(self.payload, runner_token(), 90):
                launch = AsyncMock(return_value=session)
                with patch('sfdc_agent.kiro_gateway.KiroSession.start', new=launch):
                    self.assertEqual((await self.request('POST', BASE, {**input, 'fileKey': 'OtherFile'}, extra_headers=self.headers()))[0], 422)
                    launch.assert_not_called()
                    status, opened = await self.request('POST', BASE, input, extra_headers=self.headers())
                self.assertEqual(status, 200)
                path = BASE + '/' + opened['id'] + '/prompt'
                for invalid in ({'text': 'arbitrary raw prompt'}, {'operation': 'screen.discovery', 'fields': {'fileKey': 'OtherFile'}},
                                {'operation': 'story.analysis', 'fields': {}}):
                    self.assertEqual((await self.request('POST', path, invalid, extra_headers=self.headers()))[0], 502)
                self.assertEqual(sum(method == 'prompt' for method, _ in session.calls), 0)
                self.assertEqual((await self.request('GET', self.progress + '/progress', extra_headers=SERVICE + ((b'origin', b'https://browser.invalid'),)))[0], 403)
                session.on_managed_event({'type': 'figma-auth', 'url': 'https://figma.com.evil.invalid/oauth/token'})
                self.assertNotIn('authUrl', session.managed_execution_context.public_view())

    async def test_new_attempt_retires_old_sessions_tokens_and_progress(self):
        session = StubSession()
        async with kiro_lifespan(app):
          with execution_prompts(self.bundle):
            old_headers = self.headers()
            async with execution_progress(self.payload, runner_token(), 90) as old:
                old.bind(session, True)
                with execution_prompts(self.bundle):
                    next_attempt = self.payload.model_copy(update={'attempt': 2})
                    async with execution_progress(next_attempt, runner_token(), 90):
                        self.assertFalse(old.alive())
                        self.assertTrue(session.closed)
                        self.assertEqual((await self.request('GET', self.progress + '/progress', extra_headers=SERVICE))[0], 404)
                        self.assertEqual((await self.request('POST', BASE, {'fileKey': 'ApprovedFile', 'readFigma': True}, extra_headers=old_headers))[0], 403)
                        with execution_prompts(self.bundle):
                            with self.assertRaises(HTTPException) as stale:
                                async with execution_progress(self.payload, runner_token(), 90):
                                    self.fail('Older attempt must not run')
                            self.assertEqual(stale.exception.status_code, 409)

    async def test_cli_acp_needs_no_headless_key_for_remote_or_captured_local_evidence(self):
        request = Request({'type': 'http', 'headers': [], 'server': ('127.0.0.1', 8765)})
        _check_kiro_configuration(request, self.payload)
        story = self.payload.model_copy(update={'kind': 'STORY'})
        _check_kiro_configuration(request, story)
        with patch.dict(os.environ, {'SFDC_AGENT_SCREEN_KIRO_MODE': 'headless'}):
            with self.assertRaises(HTTPException) as missing:
                _check_kiro_configuration(request, self.payload)
            self.assertEqual(missing.exception.status_code, 503)
        archive = {**self.payload.executionScope.model_dump(mode='json'), 'fileKey': 'ApprovedFile', 'sha256': 'b' * 64}
        local = ExecutionRequest.model_validate({**self.payload.model_dump(mode='json'), 'archive': archive})
        self.assertEqual(execution_mode(local), 'acp')
        _check_kiro_configuration(request, local)
        with patch.dict(os.environ, {'SFDC_AGENT_SCREEN_KIRO_MODE': 'headless'}):
            self.assertEqual(execution_mode(local), 'headless')
            with self.assertRaises(HTTPException):
                _check_kiro_configuration(request, local)

    async def test_archive_context_never_grants_remote_mcp_even_when_global_mode_is_acp(self):
        archive = {**self.payload.executionScope.model_dump(mode='json'), 'fileKey': 'ApprovedFile', 'sha256': 'b' * 64}
        local = ExecutionRequest.model_validate({**self.payload.model_dump(mode='json'), 'archive': archive})
        async with kiro_lifespan(app):
          with execution_prompts(self.bundle):
            async with execution_progress(local, runner_token(), 90) as context:
                self.assertEqual(context.mode, 'acp')
                self.assertFalse(context.remote)
                self.assertEqual((await self.request('GET', self.progress + '/progress', extra_headers=SERVICE))[0], 404)
                self.assertEqual((await self.request('POST', BASE, {'fileKey': 'ApprovedFile', 'readFigma': True}, extra_headers=self.headers()))[0], 422)
                session = StubSession()
                with patch('sfdc_agent.kiro_gateway.KiroSession.start', new=AsyncMock(return_value=session)):
                    status, opened = await self.request('POST', BASE, {'fileKey': 'ApprovedFile', 'readFigma': False,
                        'promptReleaseId': 'prompts-v1'}, extra_headers=self.headers())
                self.assertEqual(status, 200, opened)
                self.assertEqual(opened['executionMode'], 'acp')
                self.assertEqual(session.calls[0][1]['readFigma'], False)

    async def test_real_python_acp_transport_projects_only_actual_allowlisted_events(self):
        events = [{'type': 'status', 'stage': '/private/secret'},
                  {'type': 'figma-auth', 'url': 'https://figma.com/oauth/mcp?state=actual-protocol'},
                  {'type': 'figma-auth', 'url': 'https://attacker.invalid/oauth/mcp?token=secret'}]
        queue = asyncio.Queue()
        for event in events:
            queue.put_nowait((json.dumps({'event': event}) + '\n').encode())
        queue.put_nowait(b'')
        process = SimpleNamespace(stdout=SimpleNamespace(readline=queue.get), returncode=0)
        with execution_prompts(self.bundle):
            async with execution_progress(self.payload, runner_token(), 90) as context:
                session = KiroSession(process)
                context.bind(session, True)
                await session.reader
                self.assertEqual(context.view, {'status': 'needs-auth', 'phase': 'figma-auth'})
                self.assertNotIn('secret', json.dumps(context.public_view()))
                self.assertEqual(session.events, events, 'Original ACP event protocol remains available to its Node owner')

    async def test_managed_160_character_file_pin_reaches_capture_without_widening_legacy_open(self):
        file_key = 'A' * 160
        payload = self.payload.model_copy(update={'sourceReference': 'https://www.figma.com/design/' + file_key})
        session = StubSession()
        async with kiro_lifespan(app):
          with execution_prompts(self.bundle):
            async with execution_progress(payload, runner_token(), 90):
                with patch('sfdc_agent.kiro_gateway.KiroSession.start', new=AsyncMock(return_value=session)):
                    status, opened = await self.request('POST', BASE, {'fileKey': file_key, 'readFigma': True,
                        'promptReleaseId': 'prompts-v1'}, extra_headers=self.headers())
                self.assertEqual(status, 200)
                status, _ = await self.request('POST', BASE + '/' + opened['id'] + '/prompt',
                    {'operation': 'screen.capture', 'fields': {'fileKey': file_key, 'nodeId': '1:2', 'maxDimension': 100}},
                    extra_headers=self.headers())
                self.assertEqual(status, 200)
                self.assertIn(file_key, session.calls[-1][1]['text'])
        from sfdc_agent.kiro_gateway import OpenSession
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            OpenSession(fileKey=file_key, readFigma=True)

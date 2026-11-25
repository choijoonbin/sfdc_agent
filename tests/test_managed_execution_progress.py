import os
from time import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from starlette.requests import Request

from sfdc_agent.api import app
from sfdc_agent.kiro_gateway import kiro_lifespan
from sfdc_agent.kiro_headless import SessionError
from sfdc_agent.managed_execution import ExecutionRequest, _check_kiro_configuration
from sfdc_agent.managed_execution_progress import execution_mode, execution_progress
from sfdc_agent.managed_prompts import PromptBundle, execution_prompts, runner_token, template_digest
from sfdc_agent.prompt_templates import load_baseline_templates
from tests import test_kiro_gateway as gateway_fixtures
from tests.test_kiro_gateway import StubSession


BASE = '/internal/kiro/sessions'
SERVICE = ((b'authorization', b'Bearer progress-service-fixture'),)
KEY = 'ksk_progress_synthetic_fixture'


class ManagedExecutionProgressTests(unittest.IsolatedAsyncioTestCase):
    request = gateway_fixtures.GatewayTests.request

    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            'SFDC_AGENT_MANAGED_ONLY': '1',
            'SFDC_AGENT_EXECUTION_TOKEN': 'progress-service-fixture',
            'SFDC_MANAGED_AGENT_ORIGIN': 'http://127.0.0.1:8766',
            'KIRO_API_KEY': KEY,
        }, clear=True))
        templates = load_baseline_templates()
        self.bundle = PromptBundle(releaseId='prompts-v1', sha256=template_digest(templates), templates=templates)
        self.payload = ExecutionRequest.model_validate({
            'jobId': str(uuid4()), 'attempt': 1, 'kind': 'SCREEN',
            'executionScope': {'tenantId': str(uuid4()), 'workspaceId': str(uuid4())},
            'timeoutSeconds': 90, 'deadlineEpochSeconds': int(time()) + 91,
            'engine': 'KIRO', 'sourceReference': 'https://www.figma.com/design/ApprovedFile?node-id=1-2',
            'designReleaseId': 'nextbss-1.9-' + 'a' * 64, 'promptReleaseId': 'prompts-v1',
            'promptBundle': self.bundle.model_dump(),
        })
        allocation = str(self.payload.executionScope.tenantId) + '/' + str(self.payload.executionScope.workspaceId)
        self.enterContext(patch.dict(os.environ, {'SFDC_MANAGED_FIGMA_SCOPE': allocation}))

    def headers(self):
        return ((b'x-sfdc-managed-prompt', runner_token().encode()), (b'x-sfdc-kiro-purpose', b'screen'))

    async def test_bound_session_is_key_only_and_has_no_authorization_routes(self):
        session = StubSession()
        async with kiro_lifespan(app):
            with execution_prompts(self.bundle):
                async with execution_progress(self.payload, runner_token(), 90) as context:
                    with patch('sfdc_agent.kiro_gateway.read_key', return_value=KEY), patch(
                            'sfdc_agent.kiro_gateway.HeadlessKiroSession', return_value=session):
                        status, opened = await self.request('POST', BASE, {
                            'fileKey': 'ApprovedFile', 'readFigma': False, 'promptReleaseId': 'prompts-v1',
                        }, extra_headers=self.headers())
                    self.assertEqual(status, 200, opened)
                    self.assertEqual(opened['executionMode'], 'headless')
                    self.assertIn(session, context.sessions)
                    self.assertEqual(session.calls, [])
                    path = BASE + '/' + opened['id']
                    self.assertEqual((await self.request('GET', path + '/events', extra_headers=self.headers()))[0], 200)
                    self.assertEqual((await self.request('POST', path + '/authorize', {}, extra_headers=self.headers()))[0], 404)
                    self.assertEqual((await self.request('GET',
                        '/internal/v1/executions/' + str(self.payload.jobId) + '/attempts/1/progress',
                        extra_headers=SERVICE))[0], 404)
            self.assertTrue(session.closed)

    async def test_managed_session_rejects_figma_mcp_raw_text_and_wrong_source(self):
        session = StubSession()
        async with kiro_lifespan(app):
            with execution_prompts(self.bundle):
                async with execution_progress(self.payload, runner_token(), 90):
                    with patch('sfdc_agent.kiro_gateway.read_key', return_value=KEY), patch(
                            'sfdc_agent.kiro_gateway.HeadlessKiroSession', return_value=session):
                        self.assertEqual((await self.request('POST', BASE, {
                            'fileKey': 'ApprovedFile', 'readFigma': True,
                        }, extra_headers=self.headers()))[0], 422)
                        status, opened = await self.request('POST', BASE, {
                            'fileKey': 'ApprovedFile', 'readFigma': False,
                            'promptReleaseId': self.bundle.releaseId,
                        }, extra_headers=self.headers())
                    self.assertEqual(status, 200)
                    path = BASE + '/' + opened['id'] + '/prompt'
                    for invalid in (
                        {'text': 'arbitrary raw prompt'},
                        {'operation': 'screen.review-plan', 'fields': {
                            'fileKey': 'OtherFile', 'sourceContext': {'nodes': []},
                        }},
                        {'operation': 'story.analysis', 'fields': {}},
                    ):
                        self.assertEqual((await self.request('POST', path, invalid,
                            extra_headers=self.headers()))[0], 502)
                    self.assertEqual(sum(method == 'prompt' for method, _ in session.calls), 0)

    async def test_new_attempt_retires_old_sessions_and_tokens(self):
        session = StubSession()
        async with kiro_lifespan(app):
            with execution_prompts(self.bundle):
                old_token = runner_token()
                async with execution_progress(self.payload, old_token, 90) as old:
                    old.bind(session)
                    with execution_prompts(self.bundle):
                        next_attempt = self.payload.model_copy(update={'attempt': 2})
                        async with execution_progress(next_attempt, runner_token(), 90):
                            self.assertFalse(old.alive())
                            self.assertTrue(session.closed)
                            self.assertEqual((await self.request('POST', BASE, {
                                'fileKey': 'ApprovedFile', 'readFigma': False,
                            }, extra_headers=((b'x-sfdc-managed-prompt', old_token.encode()),
                                             (b'x-sfdc-kiro-purpose', b'screen'))))[0], 403)
                            with execution_prompts(self.bundle):
                                with self.assertRaises(HTTPException) as stale:
                                    async with execution_progress(self.payload, runner_token(), 90):
                                        self.fail('Older attempt must not run')
                                self.assertEqual(stale.exception.status_code, 409)

    async def test_all_kiro_execution_requires_the_key_only_configuration(self):
        request = Request({'type': 'http', 'headers': [], 'server': ('127.0.0.1', 8766)})
        self.assertEqual(execution_mode(self.payload), 'headless')
        with patch('sfdc_agent.kiro_headless.read_key', return_value=KEY):
            _check_kiro_configuration(request, self.payload)
            story = self.payload.model_copy(update={'kind': 'STORY'})
            _check_kiro_configuration(request, story)
        with patch('sfdc_agent.kiro_headless.read_key', side_effect=SessionError('KIRO_KEY_INVALID', 'invalid')):
            with self.assertRaises(HTTPException) as missing:
                _check_kiro_configuration(request, self.payload)
        self.assertEqual(missing.exception.status_code, 502)
        self.assertEqual(missing.exception.detail, 'KIRO_KEY_INVALID')

        archive = {**self.payload.executionScope.model_dump(mode='json'),
                   'fileKey': 'ApprovedFile', 'sha256': 'b' * 64}
        local = ExecutionRequest.model_validate({**self.payload.model_dump(mode='json'), 'archive': archive})
        self.assertEqual(execution_mode(local), 'headless')
        with patch('sfdc_agent.kiro_headless.read_key', return_value=KEY):
            _check_kiro_configuration(request, local)


if __name__ == '__main__':
    unittest.main()

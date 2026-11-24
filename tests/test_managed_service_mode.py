import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile
from time import time
import unittest
from unittest.mock import patch

from starlette.middleware.trustedhost import TrustedHostMiddleware

from sfdc_agent.api import app
from sfdc_agent.kiro_gateway import kiro_lifespan
from sfdc_agent.managed_prompts import PromptBundle, execution_prompts, runner_token, template_digest
from sfdc_agent.prompt_templates import load_baseline_templates
from sfdc_agent.service_mode import trusted_hosts
from tests.test_kiro_gateway import StubSession
from tests.test_managed_execution import ANALYSIS_SCRIPT

BASE = '/internal/kiro/sessions'


class ManagedServiceBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'SFDC_AGENT_MANAGED_ONLY': '1', 'SFDC_AGENT_EXECUTION_TOKEN': 'service-only-test-token',
                                                 'SFDC_MANAGED_FIGMA_SCOPE': 'aa000000-0000-4000-8000-000000000001/ad000000-0000-4000-8000-000000000001',
                                                 'KIRO_API_KEY': 'ksk_service_synthetic_fixture'}))
        templates = load_baseline_templates()
        self.bundle = PromptBundle(releaseId='prompts-v1', sha256=template_digest(templates), templates=templates)

    async def request(self, method, path, payload=None, *, token=None, bearer=None, extra=()):
        headers = [(b'host', b'127.0.0.1:8765'), (b'x-sfdc-agent', b'1')]
        if token:
            headers.append((b'x-sfdc-managed-prompt', token.encode()))
        if bearer:
            headers.append((b'authorization', ('Bearer ' + bearer).encode()))
        headers.extend(extra)
        messages = []
        sent = False
        async def receive():
            nonlocal sent
            if sent:
                await asyncio.Future()
            sent = True
            return {'type': 'http.request', 'body': json.dumps(payload or {}).encode(), 'more_body': False}
        async def send(message):
            messages.append(message)
        await app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'scheme': 'http',
                   'method': method, 'path': path, 'root_path': '', 'query_string': b'', 'headers': headers,
                   'server': ('127.0.0.1', 8765), 'client': ('127.0.0.1', 40000)}, receive, send)
        body = b''.join(message.get('body', b'') for message in messages)
        return messages[0]['status'], json.loads(body) if body else None

    async def test_hides_legacy_sqlite_routes_and_health_does_not_open_a_global_registry(self):
        with patch('sfdc_agent.api.runtime_release', side_effect=AssertionError('Global SQLite must stay unopened')), patch('sfdc_agent.api.store_for_project') as store:
            for path in ('/runs', '/runs/' + 'a' * 32, '/runs/' + 'a' * 32 + '/comparison', '/internal/prompts',
                         '/internal/prompts/active', '/internal/prompts/drafts', '/internal/kiro/design-standards/candidate', '/docs', '/openapi.json'):
                for method in ('GET', 'POST'):
                    self.assertEqual((await self.request(method, path, bearer='service-only-test-token'))[0], 404, path)
            status, health = await self.request('GET', '/health')
            self.assertEqual(status, 200)
            self.assertEqual(health['mode'], 'managed')
            self.assertNotIn('promptReleaseId', health)
            store.assert_not_called()
        self.assertEqual((await self.request('GET', '/internal/v1/prompts/schema'))[0], 401)
        self.assertEqual((await self.request('GET', '/internal/v1/prompts/schema', bearer='service-only-test-token'))[0], 200)

    async def test_every_session_action_is_bound_to_one_current_execution_token(self):
        session = StubSession()
        create = {'promptReleaseId': self.bundle.releaseId, 'readFigma': False}
        purpose = ((b'x-sfdc-kiro-purpose', b'screen'),)
        fields = {'source': {'fileKey': 'Fixture', 'nodeId': '1:2'}, 'sourceDigest': 'a' * 64, 'referenceContext': 'Fixture source'}
        async with kiro_lifespan(app):
            self.assertEqual((await self.request('POST', BASE, create, bearer='service-only-test-token', extra=purpose))[0], 403)
            with execution_prompts(self.bundle):
                owner = runner_token()
                with patch('sfdc_agent.kiro_gateway.read_key', return_value='ksk_service_synthetic_fixture'), patch(
                        'sfdc_agent.kiro_gateway.HeadlessKiroSession', return_value=session) as start:
                    self.assertEqual((await self.request('POST', BASE, create, token=owner))[0], 422)
                    self.assertEqual((await self.request('POST', BASE, {**create, 'promptReleaseId': 'prompts-' + 'a' * 32}, token=owner, extra=purpose))[0], 403)
                    status, opened = await self.request('POST', BASE, create, token=owner, extra=purpose)
                    self.assertEqual(status, 200)
                    start.assert_called_once_with('ksk_service_synthetic_fixture', purpose='screen')
                path = BASE + '/' + opened['id']
                with execution_prompts(self.bundle):
                    other = runner_token()
                    for method, suffix, payload in (
                        ('GET', '/events', None),
                        ('POST', '/prompt', {'operation': 'screen.plan', 'fields': fields}), ('DELETE', '', None),
                    ):
                        self.assertEqual((await self.request(method, path + suffix, payload, token=other))[0], 403, suffix)
                    self.assertFalse(session.closed)
                self.assertEqual(session.calls, [])
                self.assertEqual((await self.request('GET', path + '/events', token=owner))[0], 200)
                self.assertEqual((await self.request('POST', path + '/prompt', {'text': 'Unpinned text'}, token=owner))[0], 502)
                self.assertEqual((await self.request('POST', path + '/prompt', {'operation': 'story.classification', 'fields': {'evidence': '{}'}}, token=owner))[0], 502)
                self.assertEqual((await self.request('POST', path + '/prompt', {'operation': 'screen.plan', 'fields': fields}, token=owner))[0], 200)
                self.assertEqual((await self.request('POST', path + '/wait', token=owner))[0], 404)
                self.assertEqual((await self.request('POST', path + '/authorize', token=owner))[0], 404)
                self.assertEqual((await self.request('GET', path + '/events', token=owner, extra=((b'x-sfdc-managed-prompt', owner.encode()),)))[0], 403)
            for method, suffix in (('GET', '/events'), ('POST', '/prompt'), ('DELETE', '')):
                self.assertEqual((await self.request(method, path + suffix, token=owner))[0], 403)
            self.assertFalse(session.closed)
        self.assertTrue(session.closed)

    async def test_own_job_can_close_its_session_without_exposing_the_service_token_to_a_child(self):
        session = StubSession()
        with execution_prompts(self.bundle):
            async with kiro_lifespan(app):
                token = runner_token()
                with patch('sfdc_agent.kiro_gateway.read_key', return_value='ksk_service_synthetic_fixture'), patch(
                        'sfdc_agent.kiro_gateway.HeadlessKiroSession', return_value=session):
                    status, opened = await self.request('POST', BASE, {'promptReleaseId': self.bundle.releaseId}, token=token,
                                                       extra=((b'x-sfdc-kiro-purpose', b'screen'),))
                    self.assertEqual(status, 200)
                self.assertEqual((await self.request('DELETE', BASE + '/' + opened['id'], token=token))[0], 204)
                self.assertTrue(session.closed)
        with tempfile.TemporaryDirectory() as temporary:
            runner = Path(temporary) / 'runner.mjs'
            runner.write_text(ANALYSIS_SCRIPT.replace('receivedFigmaToken:', 'hasJobToken:Boolean(process.env.SFDC_MANAGED_PROMPT_CONTEXT),receivedFigmaToken:'))
            (Path(temporary) / 'ingress').mkdir()
            (Path(temporary) / 'managed-runtime.json').write_text(json.dumps({'sha256': 'b' * 64}))
            payload = {'jobId': '8b9ed4d2-ffb7-4fd5-9e14-1d4b1ec8b42d', 'attempt': 1, 'kind': 'SCREEN', 'engine': 'NOVA',
                       'executionScope': {'tenantId':'aa000000-0000-4000-8000-000000000001','workspaceId':'ad000000-0000-4000-8000-000000000001'},
                       'timeoutSeconds':10,'deadlineEpochSeconds':int(time())+11,
                       'sourceReference': 'https://www.figma.com/design/FrameDocument', 'designReleaseId': 'standard',
                       'promptReleaseId': self.bundle.releaseId, 'promptBundle': self.bundle.model_dump()}
            with patch.dict(os.environ, {'SFDC_MANAGED_EXECUTION_RUNNER': str(runner), 'SFDC_MANAGED_EXECUTION_NODE': shutil.which('node'),
                                         'SFDC_MANAGED_EXECUTION_TIMEOUT_SECONDS': '10', 'SFDC_MANAGED_RELEASE_INGRESS': str(Path(temporary)/'ingress')}), \
                 patch('sfdc_agent.managed_execution.engine_root', return_value=Path(temporary)):
                status, reply = await self.request('POST', '/internal/v1/executions', payload, bearer='service-only-test-token')
                self.assertEqual(status, 200)
                self.assertFalse(reply['checkpoint']['receivedServiceToken'])
                self.assertTrue(reply['checkpoint']['hasJobToken'])

    async def test_imported_nova_prompt_pin_opens_only_its_managed_kiro_context(self):
        release_id = 'prompts-nova-' + 'c' * 64
        bundle = self.bundle.model_copy(update={'releaseId': release_id})
        session = StubSession()
        async with kiro_lifespan(app):
            with execution_prompts(bundle):
                token = runner_token()
                with patch('sfdc_agent.kiro_gateway.read_key', return_value='ksk_service_synthetic_fixture'), patch(
                    'sfdc_agent.kiro_gateway.HeadlessKiroSession', return_value=session) as start, patch(
                    'sfdc_agent.kiro_gateway.runtime_release', side_effect=AssertionError('Must use the scoped bundle')
                ):
                    status, opened = await self.request('POST', BASE, {'promptReleaseId': release_id}, token=token,
                                                       extra=((b'x-sfdc-kiro-purpose', b'screen'),))
                    self.assertEqual(status, 200)
                    self.assertEqual(opened['promptReleaseId'], release_id)
                    start.assert_called_once_with('ksk_service_synthetic_fixture', purpose='screen')
                    status, _ = await self.request('POST', BASE, {'promptReleaseId': 'prompts-nova-' + 'd' * 64}, token=token,
                                                   extra=((b'x-sfdc-kiro-purpose', b'screen'),))
                    self.assertEqual(status, 403)
                    self.assertEqual((await self.request('DELETE', BASE + '/' + opened['id'], token=token))[0], 204)
        self.assertTrue(session.closed)

    async def test_explicit_trusted_hosts_retain_local_defaults_and_reject_wildcards(self):
        with patch.dict(os.environ, {'SFDC_AGENT_TRUSTED_HOSTS': 'agent.internal,agent.service.local'}):
            self.assertEqual(trusted_hosts(), ['localhost', '127.0.0.1', 'agent.internal', 'agent.service.local'])
            async def target(scope, receive, send):
                await send({'type': 'http.response.start', 'status': 204, 'headers': []})
                await send({'type': 'http.response.body', 'body': b''})
            middleware = TrustedHostMiddleware(target, allowed_hosts=trusted_hosts())
            for host, expected in ((b'agent.internal:8765', 204), (b'attacker.internal', 400)):
                output = []
                async def send(message): output.append(message)
                await middleware({'type': 'http', 'headers': [(b'host', host)]}, None, send)
                self.assertEqual(output[0]['status'], expected)
        for invalid in ('*', '*.internal', 'https://agent.internal', 'agent.internal:8765'):
            with patch.dict(os.environ, {'SFDC_AGENT_TRUSTED_HOSTS': invalid}), self.assertRaises(ValueError):
                trusted_hosts()


if __name__ == '__main__':
    unittest.main()

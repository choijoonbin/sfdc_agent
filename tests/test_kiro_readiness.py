import json
import os
from time import monotonic
import unittest
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from sfdc_agent.api import app
from sfdc_agent import kiro_readiness
from sfdc_agent.kiro_gateway import error_response
from sfdc_agent.kiro_headless import HeadlessKiroSession, SessionError
from sfdc_agent.managed_execution_progress import ExecutionContext


KEY = 'ksk_synthetic_readiness_fixture'
OTHER_KEY = 'ksk_synthetic_other_fixture'


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            'SFDC_AGENT_SCREEN_KIRO_MODE': 'headless', 'SFDC_AGENT_STORY_KIRO_MODE': 'headless',
            'KIRO_API_KEY': KEY,
        }, clear=True))
        self.enterContext(patch('sfdc_agent.kiro_readiness._EVIDENCE', None))
        self.enterContext(patch('sfdc_agent.kiro_readiness.shutil.which', return_value='/synthetic/kiro-cli'))

    def screen(self):
        return kiro_readiness.readiness_snapshot()['operations'][0]

    def test_configuration_is_unknown_without_a_model_response(self):
        with patch('sfdc_agent.kiro_headless.asyncio.create_subprocess_exec', side_effect=AssertionError('No child')):
            state = self.screen()
        self.assertTrue(state['configured'])
        self.assertEqual(state['authentication'], 'UNKNOWN')
        self.assertIsNone(state['ready'])
        self.assertIsNone(state['checkedAt'])
        self.assertEqual(state['reasonCode'], 'KIRO_AUTH_NOT_VERIFIED')

    def test_success_is_credential_bound_and_expiry_is_unknown_not_failure(self):
        with patch('sfdc_agent.kiro_readiness.monotonic', return_value=100):
            kiro_readiness.record_model_success(KEY)
        with patch('sfdc_agent.kiro_readiness.monotonic', return_value=399.9):
            state = self.screen()
        self.assertEqual(state['authentication'], 'VERIFIED')
        self.assertTrue(state['ready'])
        checked_at = state['checkedAt']
        with patch('sfdc_agent.kiro_readiness.monotonic', return_value=400):
            expired = self.screen()
        self.assertEqual(expired['authentication'], 'UNKNOWN')
        self.assertIsNone(expired['ready'])
        self.assertEqual(expired['reasonCode'], 'KIRO_AUTH_VERIFICATION_EXPIRED')
        self.assertEqual(expired['checkedAt'], checked_at)
        with patch.dict(os.environ, {'KIRO_API_KEY': OTHER_KEY}):
            changed = self.screen()
        self.assertEqual(changed['authentication'], 'UNKNOWN')
        self.assertIsNone(changed['checkedAt'])
        self.assertEqual(changed['reasonCode'], 'KIRO_AUTH_NOT_VERIFIED')

    def test_cache_is_single_record_and_never_exposes_credential_material(self):
        for index in range(100):
            kiro_readiness.record_model_success(KEY + str(index))
        kiro_readiness.record_model_success(KEY)
        self.assertIsInstance(kiro_readiness._EVIDENCE, kiro_readiness._Evidence)
        body = json.dumps(kiro_readiness.readiness_snapshot())
        self.assertNotIn(KEY, body)
        self.assertNotIn(kiro_readiness._EVIDENCE.credential.hex(), body)
        self.assertNotIn('credential', body)
        self.assertNotIn('fingerprint', body)

    def test_only_allowlisted_observed_authentication_failure_changes_evidence(self):
        kiro_readiness.record_auth_failure(KEY, 'KIRO_TIMEOUT')
        self.assertEqual(self.screen()['authentication'], 'UNKNOWN')
        kiro_readiness.record_auth_failure(KEY, 'KIRO_KEY_AUTH_REQUIRED')
        state = self.screen()
        self.assertTrue(state['configured'])
        self.assertEqual(state['authentication'], 'FAILED')
        self.assertFalse(state['ready'])
        self.assertEqual(state['reasonCode'], 'KIRO_KEY_AUTH_REQUIRED')

    def test_configuration_problems_are_not_claimed_as_provider_authentication_failures(self):
        for key in ('', 'invalid synthetic fixture'):
            with self.subTest(key_kind='empty' if not key else 'malformed'), patch.dict(os.environ, {'KIRO_API_KEY': key}):
                state = self.screen()
            self.assertFalse(state['configured'])
            self.assertFalse(state['ready'])
            self.assertEqual(state['authentication'], 'UNKNOWN')
            self.assertEqual(state['reasonCode'], 'KIRO_KEY_INVALID')
        with patch('sfdc_agent.kiro_readiness.shutil.which', return_value=None):
            self.assertEqual(self.screen()['reasonCode'], 'KIRO_UNAVAILABLE')
        with patch.dict(os.environ, {'SFDC_AGENT_SCREEN_KIRO_MODE': 'unsupported'}):
            state = self.screen()
            self.assertIsNone(state['executionMode'])
            self.assertEqual(state['reasonCode'], 'KIRO_MODE_INVALID')

    def test_acp_development_configuration_does_not_claim_cli_login_or_read_a_key(self):
        with patch.dict(os.environ, {'SFDC_AGENT_SCREEN_KIRO_MODE': 'acp', 'SFDC_AGENT_STORY_KIRO_MODE': 'acp'}), patch(
            'sfdc_agent.kiro_headless.read_key', side_effect=AssertionError('ACP must not read key')):
            state = self.screen()
        self.assertTrue(state['configured'])
        self.assertEqual(state['authentication'], 'UNKNOWN')
        self.assertEqual(state['reasonCode'], 'KIRO_CLI_AUTH_NOT_CHECKED')


class ReadinessRouteTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, *, headers=(), path='/internal/v1/kiro/readiness'):
        messages = []
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(message):
            messages.append(message)
        await app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'scheme': 'http',
                   'method': 'GET', 'path': path, 'root_path': '', 'query_string': b'',
                   'headers': [(b'host', b'127.0.0.1'), *headers],
                   'server': ('127.0.0.1', 8766), 'client': ('127.0.0.1', 40000)}, receive, send)
        return messages[0]['status'], json.loads(b''.join(x.get('body', b'') for x in messages))

    async def test_service_auth_precedes_readiness_and_get_starts_no_cli_or_provider(self):
        bearer = (b'authorization', b'Bearer synthetic-service-token')
        with patch.dict(os.environ, {'SFDC_AGENT_MANAGED_ONLY': '1', 'SFDC_AGENT_EXECUTION_TOKEN': 'synthetic-service-token'}, clear=True), patch(
                'sfdc_agent.kiro_readiness.readiness_snapshot', return_value={'provider': 'kiro'}) as snapshot, patch(
                'sfdc_agent.kiro_headless.asyncio.create_subprocess_exec', side_effect=AssertionError('No subprocess')):
            self.assertEqual((await self.request())[0], 401)
            self.assertEqual((await self.request(headers=[(b'authorization', b'Bearer wrong')]))[0], 401)
            self.assertEqual((await self.request(headers=[bearer, bearer]))[0], 401)
            self.assertEqual((await self.request(headers=[bearer, (b'origin', b'https://example.invalid')]))[0], 403)
            self.assertEqual((await self.request(headers=[bearer, (b'sec-fetch-site', b'same-origin')]))[0], 403)
            snapshot.assert_not_called()
            self.assertEqual(await self.request(headers=[bearer]), (200, {'provider': 'kiro'}))
            snapshot.assert_called_once()
            health = (await self.request(path='/health'))[1]
            self.assertEqual(health['status'], 'ready')
            self.assertNotIn('authentication', health)


class HeadlessEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch('sfdc_agent.kiro_readiness._EVIDENCE', None))
        self.enterContext(patch.dict(os.environ, {'KIRO_API_KEY': KEY}, clear=True))
        self.enterContext(patch('sfdc_agent.kiro_readiness.shutil.which', return_value='/synthetic/kiro-cli'))

    def state(self):
        return kiro_readiness._authentication(KEY)

    async def test_only_completed_model_response_verifies_the_key(self):
        response = json.dumps({'type': 'runFinished', 'data': {'status': 'success', 'stopReason': 'end_turn',
                               'finalText': '{}', 'finalTextTruncated': False}}).encode() + b'\n'
        session = HeadlessKiroSession(KEY)
        async def execute(binary, args, *rest, **kwargs):
            self.assertEqual(self.state()['authentication'], 'UNKNOWN')
            if args[0] == 'whoami':
                return 0, b'Authenticated with API key\n'
            self.assertEqual(args[0], 'chat')
            return 0, response
        with patch('sfdc_agent.kiro_headless.shutil.which', return_value='/synthetic/kiro-cli'), patch(
                'sfdc_agent.kiro_headless._execute', side_effect=execute) as run:
            try:
                await session.call('prompt', {'text': 'synthetic fixture'}, timeout=5)
            finally:
                await session.close()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(self.state()['authentication'], 'VERIFIED')

    async def test_auth_check_failure_is_failed_but_unclassified_model_failure_is_not(self):
        for identity, chat, expected in ((b'Not logged in', None, 'FAILED'),
                                         (b'Authenticated with API key', (1, b'private fixture failure'), 'UNKNOWN')):
            with self.subTest(expected=expected), patch('sfdc_agent.kiro_readiness._EVIDENCE', None), patch(
                    'sfdc_agent.kiro_headless.shutil.which', return_value='/synthetic/kiro-cli'), patch(
                    'sfdc_agent.kiro_headless._execute', side_effect=[(0, identity)] + ([chat] if chat else [])) as run:
                session = HeadlessKiroSession(KEY)
                try:
                    with self.assertRaises(SessionError):
                        await session.call('prompt', {'text': 'synthetic fixture'}, timeout=5)
                finally:
                    await session.close()
                self.assertEqual(self.state()['authentication'], expected)
                self.assertEqual(run.call_count, 2 if chat else 1)


class ContextErrorTests(unittest.TestCase):
    def context(self):
        return ExecutionContext('fixture-job', 1, None, 'screen', 'headless', None, False, monotonic() + 30)

    def test_unknown_codes_and_closed_or_expired_attempts_cannot_record_an_auth_error(self):
        context = self.context()
        context.record_auth_error('KIRO_TIMEOUT')
        self.assertIsNone(context.auth_error)
        context.closed = True
        context.record_auth_error('KIRO_KEY_AUTH_REQUIRED')
        self.assertIsNone(context.auth_error)
        context.closed = False
        context.expires = monotonic() - 1
        context.record_auth_error('KIRO_LOGIN_REQUIRED')
        self.assertIsNone(context.auth_error)

    def test_gateway_only_records_the_current_live_session_owner(self):
        own = self.context()
        foreign = self.context()
        session = type('Session', (), {'managed_execution_context': foreign})()
        request = Request({'type': 'http', 'headers': [], 'path_params': {'session_id': 'foreign'}, 'app': app})
        error = SessionError('KIRO_LOGIN_REQUIRED', 'synthetic private message')
        with patch('sfdc_agent.kiro_gateway.managed_only', return_value=True), patch(
                'sfdc_agent.kiro_gateway.managed_execution_context', return_value=own), patch.object(
                app.state, 'kiro_sessions', {'foreign': session}, create=True), patch(
                'sfdc_agent.kiro_gateway.require_session_owner') as require_owner:
            error_response(request, error)
            self.assertIsNone(own.auth_error)
            require_owner.assert_not_called()
            session.managed_execution_context = own
            error_response(request, error)
            self.assertEqual(own.auth_error, 'KIRO_LOGIN_REQUIRED')
            require_owner.assert_called_once_with(request, session)

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from screen_prompt_context_fixtures import screen_fields
from sfdc_agent.api import app
from sfdc_agent.config import engine_root
from sfdc_agent.kiro_gateway import kiro_lifespan, prompt_session
from sfdc_agent.kiro_headless import SessionError
from sfdc_agent.kiro_screen_projection import prepare_screen_fields
from sfdc_agent.managed_execution import _check_component_library_configuration
from sfdc_agent.prompt_registry import BASELINE_RELEASE, runtime_release
from sfdc_agent.screen_tasks import build_screen_task
from sfdc_agent.screen_prompt_context import SCREEN_PROMPT_CONTEXT_CAPABILITY_VERSION


def request(payload):
    sent = False
    async def receive():
        nonlocal sent
        if sent:
            await asyncio.Future()
        sent = True
        return {'type': 'http.request', 'body': json.dumps(payload).encode(), 'more_body': False}
    return Request({'type': 'http', 'method': 'POST', 'app': app, 'headers': [
        (b'x-sfdc-agent', b'1'), (b'x-sfdc-prompt-trace-owner', b'a' * 64)]}, receive=receive)


class SessionFixture:
    execution_mode = 'headless'
    purpose = 'screen'
    expiry = float('inf')
    closed = False

    def __init__(self):
        self.prompt_release = runtime_release(BASELINE_RELEASE)
        self.provider_receipt = {'schemaVersion': 1, 'provider': 'synthetic'}
        self.calls = []

    async def call(self, method, params):
        self.calls.append(params)
        return {'text': '{}', 'stopReason': 'end_turn', 'toolCalls': []}

    async def close(self):
        self.closed = True


class ScreenPromptGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_capability_matches_decoder_support_and_rejects_other_versions(self):
        payload = SimpleNamespace(componentLibrary=None, sourceAcquisition=None, engine='KIRO', kind='SCREEN',
            _screen_contract=None)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'nextbss/engine/managed-runtime-capabilities.json'
            path.parent.mkdir(parents=True)
            capability = {'schemaVersion': 1, 'generationFamilies': []}
            for version in (1, SCREEN_PROMPT_CONTEXT_CAPABILITY_VERSION, 3, True):
                with self.subTest(version=version):
                    path.write_text(json.dumps({**capability, 'screenPromptContextEncodingVersion': version}))
                    with patch('sfdc_agent.managed_execution.engine_root', return_value=root):
                        if type(version) is int and version == SCREEN_PROMPT_CONTEXT_CAPABILITY_VERSION:
                            _check_component_library_configuration(payload)
                        else:
                            with self.assertRaises(HTTPException):
                                _check_component_library_configuration(payload)

    async def test_partial_mcp_keeps_captured_text_and_does_not_encode_incidental_metadata(self):
        fields = screen_fields()
        fields.update(local=False, sourceKind='figma-mcp', capturedContext=[
            {'nodeId': '31:1', 'text': 'Synthetic captured MCP design text'}])
        for context in [{'provider': 'figma-mcp', 'toolEvidence': []},
                        {'nodes': fields['sourceContext']['nodes'][:1], 'evidenceCoverage': {'completeAuthoredScene': False}}]:
            candidate = {**fields, 'sourceContext': context}
            with self.subTest(context=list(context)), patch(
                    'sfdc_agent.kiro_screen_projection.asyncio.create_subprocess_exec') as child:
                prepared = await prepare_screen_fields('screen.generate', candidate)
                child.assert_not_called()
                self.assertIs(prepared.fields, candidate)
                self.assertIsNone(prepared.prompt_context)
                text = build_screen_task('screen.generate', prepared.fields)
                self.assertIn('Synthetic captured MCP design text', text)

    async def test_gateway_keeps_source_and_trace_facts_and_returns_only_bounded_encoding_metrics(self):
        fields = screen_fields()
        original = deepcopy(fields)
        session = SessionFixture()
        async with kiro_lifespan(app):
            app.state.kiro_sessions['synthetic-session'] = session
            with patch('sfdc_agent.kiro_gateway.managed_only', return_value=False), patch(
                    'sfdc_agent.kiro_screen_projection.engine_root', return_value=engine_root()), \
                    self.assertLogs('uvicorn.error', level='INFO') as captured:
                reply = await prompt_session(request({'operation': 'screen.generate', 'fields': fields}), 'synthetic-session')
                repair = {'local': True, 'diagnostic': 'Synthetic finding', 'previousDraft': '{}', 'evidence': fields}
                repaired = await prompt_session(request({'operation': 'screen.repair', 'fields': repair}), 'synthetic-session')
                self.assertEqual(fields, original)
                self.assertEqual(reply['providerReceipt']['sourcePromptContext'], repaired['providerReceipt']['sourcePromptContext'])
                receipt = reply['providerReceipt']['sourcePromptContext']
                self.assertGreater(receipt['sourceBytes'], 512 * 1024)
                self.assertLessEqual(receipt['promptBytes'], 512 * 1024)
                trace = app.state.prompt_run_values.get('a' * 64, 'screen.generate', 'source_context')
                value = trace['values'][0]['value']
                self.assertTrue(value in session.calls[0]['text'], 'Trace substitution must match the actual model payload.')
                self.assertEqual(json.loads(value)['encoding'], 'screen-record-tables-v1')
                self.assertEqual(json.loads(value)['sourceSha256'], receipt['sourceSha256'])
                self.assertEqual(set(receipt), {'schemaVersion', 'encoding', 'sourceBytes', 'promptBytes',
                                               'sourceSha256', 'promptSha256'})
                for line in captured.output:
                    self.assertNotIn('원문 조건', line)
                    self.assertNotIn('표시 0', line)
                with self.assertRaises(SessionError) as rejected:
                    await prompt_session(request({'operation': 'screen.generate', 'fields': {
                        **fields, 'sourcePromptContext': {'forged': True}}}), 'synthetic-session')
                self.assertEqual(rejected.exception.code, 'KIRO_TASK_INVALID')
                self.assertEqual(len(session.calls), 2)

    async def test_irreducible_context_stops_before_the_provider(self):
        fields = screen_fields()
        fields['sourceContext'] = {'nodes': fields['sourceContext']['nodes'][:1], 'description': 'x' * (513 * 1024)}
        session = SessionFixture()
        async with kiro_lifespan(app):
            app.state.kiro_sessions['synthetic-session'] = session
            with patch('sfdc_agent.kiro_gateway.managed_only', return_value=False), patch(
                    'sfdc_agent.kiro_screen_projection.engine_root', return_value=engine_root()):
                with self.assertRaises(SessionError):
                    await prompt_session(request({'operation': 'screen.generate', 'fields': fields}), 'synthetic-session')
        self.assertEqual(session.calls, [])


class ScreenPromptCapabilityTests(unittest.TestCase):
    def test_ai_screen_rejects_mixed_worker_versions_including_legacy_requests(self):
        payload = SimpleNamespace(kind='SCREEN', engine='KIRO', componentLibrary=None,
                                  sourceAcquisition=None, _screen_contract=None)
        with tempfile.TemporaryDirectory() as directory, patch(
                'sfdc_agent.managed_execution.engine_root', return_value=Path(directory)):
            path = Path(directory) / 'nextbss/engine/managed-runtime-capabilities.json'
            path.parent.mkdir(parents=True)
            for version in [None, True, False, '1', 0, 1, 3]:
                path.write_text(json.dumps({'schemaVersion': 1, 'generationFamilies': [],
                                           'screenPromptContextEncodingVersion': version}))
                with self.subTest(version=version), self.assertRaises(HTTPException) as denied:
                    _check_component_library_configuration(payload)
                self.assertEqual(denied.exception.status_code, 409)
                self.assertEqual(denied.exception.detail, 'RELEASE_RUNTIME_INCOMPATIBLE')
            path.write_text(json.dumps({'schemaVersion': 1, 'generationFamilies': [],
                                       'screenPromptContextEncodingVersion': 2}))
            _check_component_library_configuration(payload)
            path.unlink()
            for kind, engine in [('SCREEN', 'NOVA'), ('STORY', 'KIRO')]:
                _check_component_library_configuration(SimpleNamespace(kind=kind, engine=engine,
                    componentLibrary=None, sourceAcquisition=None, _screen_contract=None))


if __name__ == '__main__':
    unittest.main()

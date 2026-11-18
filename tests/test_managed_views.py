import base64
from hashlib import sha256
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request
from sfdc_agent.managed_views import Projection, project, original_view


class OriginalAnalysisViewsTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, kind, document, digest=None):
        runner = Path(__file__).resolve().parents[2] / 'SFDC_Ui' / 'nextbss/server/managed-execution.mjs'
        if not runner.is_file():
            self.skipTest('The sibling managed UI runtime is not installed')
        raw = json.dumps(document).encode()
        payload = Projection(kind=kind, sha256=digest or sha256(raw).hexdigest(), raw=base64.b64encode(raw).decode())
        with patch.dict(os.environ, {'SFDC_MANAGED_EXECUTION_RUNNER': str(runner),
                                     'SFDC_AGENT_EXECUTION_TOKEN': 'must-stay-outside-child'}):
            return await project(payload)

    async def test_screen_view_keeps_original_review_fields_and_drops_private_input(self):
        value = await self.invoke('SCREEN', {
            'schemaVersion': 1,
            'analysis': {'id': '6ad9cc7e-eaf5-4027-b8d4-993a48199d71', 'digest': 'a' * 64,
                         'source': {'nodeId': '12:34'}, 'mappings': [{'nodeId': '12:35', 'component': 'text-field'}],
                         'issues': [], 'password': 'private-value'},
            'input': {'secret': 'private-input'}, 'timing': {'durationMs': 123},
        })
        self.assertEqual(value['mappings'][0]['component'], 'text-field')
        self.assertEqual(value['digest'], 'a' * 64)
        self.assertEqual(value['timing']['durationMs'], 123)
        self.assertNotIn('password', value)
        self.assertNotIn('input', value)
        self.assertNotIn('private-input', json.dumps(value))

    async def test_story_view_uses_original_public_provenance_without_exposing_models(self):
        value = await self.invoke('STORY', {
            'id': '6ad9cc7e-eaf5-4027-b8d4-993a48199d72', 'planVersion': 1, 'analysisMethod': 'source',
            'source': {'fileKey': 'OriginalFile'}, 'board': {'id': '12:34'},
            'workflows': [{'id': 'flow', 'title': '원래 업무', 'analysisMethod': 'source', 'cases': [], 'links': [],
                'screens': [{'id': '12:35', 'title': '원래 화면', 'descriptions': [], 'model': {'sourceMode': 'measured'}}],
                'plan': {'screens': [], 'scenarios': [{'id': 'base', 'title': '기본 시나리오', 'fixture': {'private': True}, 'evidence': []}]}}],
        })
        workflow = value['workflows'][0]
        self.assertNotIn('plan', workflow)
        self.assertNotIn('model', workflow['screens'][0])
        self.assertEqual(workflow['screens'][0]['sourceMode'], 'measured')
        self.assertEqual(workflow['scenarios'][0]['title'], '기본 시나리오')
        self.assertNotIn('fixture', workflow['scenarios'][0])
        self.assertTrue(value['board']['previewUrl'].endswith('/12:34.svg'))

    async def test_projection_rejects_changed_raw_digest(self):
        with self.assertRaises(HTTPException) as caught:
            await self.invoke('SCREEN', {'analysis': {}}, digest='0' * 64)
        self.assertEqual(caught.exception.status_code, 422)

    async def test_service_auth_rejects_before_reading_private_projection_body(self):
        consumed = False
        async def receive():
            nonlocal consumed
            consumed = True
            return {'type': 'http.request', 'body': b'private-analysis', 'more_body': False}
        request = Request({'type': 'http', 'method': 'POST', 'path': '/internal/v1/analysis-views', 'headers': []}, receive)
        with patch.dict(os.environ, {'SFDC_AGENT_EXECUTION_TOKEN': 'projection-service-token'}):
            with self.assertRaises(HTTPException) as caught:
                await original_view(request)
        self.assertEqual(caught.exception.status_code, 401)
        self.assertFalse(consumed)

    async def test_authenticated_invalid_body_is_bounded_and_releases_worker_slot(self):
        async def receive():
            return {'type': 'http.request', 'body': b'{"kind":"SCREEN","raw":"invalid"}', 'more_body': False}
        request = Request({'type': 'http', 'method': 'POST', 'path': '/internal/v1/analysis-views',
                           'headers': [(b'authorization', b'Bearer projection-service-token')]}, receive)
        with patch.dict(os.environ, {'SFDC_AGENT_EXECUTION_TOKEN': 'projection-service-token'}):
            for _ in range(3):
                with self.assertRaises(HTTPException) as caught:
                    await original_view(request)
                self.assertEqual(caught.exception.status_code, 422)
                # Reuse requests only with fresh body state, matching an actual retry.
                request = Request(request.scope, receive)

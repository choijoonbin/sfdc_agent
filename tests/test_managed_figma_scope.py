import json
import os
from pathlib import Path
from time import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sfdc_agent.api import app
from sfdc_agent.managed_figma_scope import FigmaProviderScopeError, require_figma_scope
from sfdc_agent.managed_execution import ExecutionRequest
from sfdc_agent.managed_execution_progress import execution_progress
from sfdc_agent.managed_prompts import PromptBundle, execution_prompts, runner_token, template_digest
from sfdc_agent.prompt_templates import load_baseline_templates
from sfdc_agent.kiro_gateway import kiro_lifespan
from tests import test_managed_execution as execution_fixtures
from tests import test_kiro_gateway as gateway_fixtures


A = execution_fixtures.EXECUTION['executionScope']
B = {'tenantId': 'bb000000-0000-4000-8000-000000000001', 'workspaceId': 'bd000000-0000-4000-8000-000000000001'}
ALLOCATION = A['tenantId'] + '/' + A['workspaceId']
FORBIDDEN = {'code': 'FIGMA_PROVIDER_SCOPE_FORBIDDEN'}


class ManagedFigmaScopeTests(unittest.IsolatedAsyncioTestCase):
    request = execution_fixtures.ManagedExecutionTests.request
    write_runner = execution_fixtures.ManagedExecutionTests.write_runner

    def setUp(self):
        execution_fixtures.ManagedExecutionTests.setUp(self)
        templates = load_baseline_templates()
        self.bundle = PromptBundle(releaseId='prompts-v1', sha256=template_digest(templates), templates=templates)
        self.remote = execution_fixtures.EXECUTION['sourceReference']

    def test_only_a_canonical_trusted_pair_allocates_the_provider_credential(self):
        for invalid in ('', 'tenant/workspace', ALLOCATION + '/', ' ' + ALLOCATION, ALLOCATION.upper()):
            with patch.dict(os.environ, {'SFDC_MANAGED_FIGMA_SCOPE': invalid}), self.assertRaises(FigmaProviderScopeError) as failure:
                require_figma_scope(SimpleNamespace(**A))
            self.assertEqual(failure.exception.status_code, 503)
            self.assertEqual(failure.exception.code, 'FIGMA_PROVIDER_SCOPE_UNAVAILABLE')
        require_figma_scope(SimpleNamespace(**A))
        with self.assertRaises(FigmaProviderScopeError) as failure:
            require_figma_scope(SimpleNamespace(**B))
        self.assertEqual(failure.exception.status_code, 403)

    async def test_all_remote_entrypoints_block_foreign_scope_before_any_child(self):
        paths = [('/internal/v1/executions', {**execution_fixtures.EXECUTION, 'executionScope': B}),
                 ('/internal/v1/source-discovery', {'sourceReference': self.remote, 'executionScope': B}),
                 ('/internal/v1/source-reference', {'reference': self.remote + '?node-id=1-2&version-id=Fixed', 'executionScope': B})]
        with patch('sfdc_agent.managed_execution._run_process', new=AsyncMock()) as execution, \
                patch('sfdc_agent.managed_source_reference._run_process', new=AsyncMock()) as reference:
            for path, payload in paths:
                with self.subTest(path=path):
                    status, body, _ = await self.request(payload, path=path)
                    self.assertEqual((status, body), (403, FORBIDDEN))
            execution.assert_not_awaited()
            reference.assert_not_awaited()
            with patch.dict(os.environ, {'SFDC_MANAGED_FIGMA_SCOPE': ''}):
                for path, payload in paths:
                    status, body, _ = await self.request(payload, path=path)
                    self.assertEqual((status, body), (503, {'code': 'FIGMA_PROVIDER_SCOPE_UNAVAILABLE'}))

    async def test_allocated_rest_paths_forward_only_their_trusted_credential(self):
        self.write_runner(execution_fixtures.ANALYSIS_SCRIPT)
        status, result, _ = await self.request(execution_fixtures.EXECUTION)
        self.assertEqual(status, 200)
        self.assertTrue(result['checkpoint']['receivedFigmaToken'])
        with patch.dict(os.environ, {'FIGMA_PLAN_ACCESS_TOKEN': 'explicit-plan-fixture'}):
            status, planned, _ = await self.request({**execution_fixtures.EXECUTION, 'jobId': str(uuid4())})
        self.assertEqual(status, 200)
        self.assertTrue(planned['checkpoint']['receivedPlanToken'])
        root = Path(self.directory)
        for name in ('managed-source-discovery.mjs', 'managed-source-reference.mjs'):
            file = root / 'nextbss/server' / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('trusted fixture')
        process = AsyncMock(return_value=b'{"result":{"pages":[]}}')
        with patch('sfdc_agent.managed_execution._run_process', process):
            self.assertEqual((await self.request({'sourceReference': self.remote, 'executionScope': A}, path='/internal/v1/source-discovery'))[0], 200)
        self.assertEqual(process.call_args.args[5], {'FIGMA_ACCESS_TOKEN': 'explicit-figma-test-token'})
        with patch('sfdc_agent.managed_source_reference.engine_root', return_value=root), \
                patch('sfdc_agent.managed_source_reference._run_process', new=AsyncMock(return_value=b'{"result":{"provenance":"OFFICIAL_FIGMA_EXPORT"}}')), \
                patch.dict(os.environ, {'FIGMA_PLAN_ACCESS_TOKEN': 'explicit-plan-fixture'}):
            self.assertEqual((await self.request({'reference': self.remote + '?node-id=1-2&version-id=Fixed', 'executionScope': A}, path='/internal/v1/source-reference'))[0], 200)

    async def test_local_and_remote_owned_archives_do_not_receive_global_figma_tokens(self):
        source = execution_fixtures.ANALYSIS_SCRIPT.replace("source:{fileKey:new URL(request.sourceReference).pathname.split('/')[2]}",
            "source:{fileKey:request.archive.fileKey,archiveSha256:request.archive.sha256}")
        self.write_runner(source)
        for reference, key in [(self.remote, 'FrameDocument'), ('figma-local://local' + 'a'*32 + '/source?node-id=1-2&page-id=1-1', 'local'+'a'*32)]:
            payload = {**execution_fixtures.EXECUTION, 'sourceReference': reference, 'executionScope': B,
                       'archive': {**B, 'fileKey': key, 'sha256': 'a'*64}}
            with patch.dict(os.environ, {'FIGMA_PLAN_ACCESS_TOKEN': 'explicit-plan-fixture'}):
                status, result, _ = await self.request(payload)
            self.assertEqual(status, 200, result)
            self.assertFalse(result['checkpoint']['receivedFigmaToken'])
            self.assertFalse(result['checkpoint']['receivedPlanToken'])

    async def test_conversion_gateway_rechecks_allocation_and_keeps_local_model_sessions_usable(self):
        self.enterContext(patch.dict(os.environ, {'SFDC_AGENT_MANAGED_ONLY': '1',
                                                  'KIRO_API_KEY': 'ksk_scope_synthetic_fixture'}))
        call = gateway_fixtures.GatewayTests.request
        async with kiro_lifespan(app):
            for archive in (None, {**B, 'fileKey': 'FrameDocument', 'sha256': 'a'*64}):
                scope = A if archive is None else B
                payload = ExecutionRequest.model_validate({**execution_fixtures.EXECUTION, 'jobId': str(uuid4()), 'engine': 'KIRO',
                    'executionScope': scope, 'timeoutSeconds': 90, 'deadlineEpochSeconds': int(time())+91,
                    'promptBundle': self.bundle.model_dump(), **({'archive': archive} if archive else {})})
                session = gateway_fixtures.StubSession()
                with execution_prompts(self.bundle):
                    async with execution_progress(payload, runner_token(), 90) as context:
                        headers = ((b'x-sfdc-managed-prompt', runner_token().encode()), (b'x-sfdc-kiro-purpose', b'screen'))
                        with patch('sfdc_agent.kiro_gateway.read_key', return_value='ksk_scope_synthetic_fixture'), patch(
                                'sfdc_agent.kiro_gateway.HeadlessKiroSession', return_value=session):
                            status, result = await call(self, 'POST', '/internal/kiro/sessions', {'fileKey': 'FrameDocument',
                                'readFigma': False, 'promptReleaseId': 'prompts-v1'}, extra_headers=headers)
                        self.assertEqual(status, 200, result)
                        path = '/internal/kiro/sessions/' + result['id']
                        with patch.dict(os.environ, {'SFDC_MANAGED_FIGMA_SCOPE': ''}):
                            status, body = await call(self, 'GET', path + '/events', extra_headers=headers)
                            self.assertEqual(status, 200 if archive else 503)
                            if not archive:
                                self.assertEqual(body, {'code': 'FIGMA_PROVIDER_SCOPE_UNAVAILABLE'})
                        self.assertEqual((await call(self, 'DELETE', path, extra_headers=headers))[0], 204)
                        self.assertTrue(session.closed)

    async def test_configuration_revoked_during_rest_execution_cannot_publish_the_finished_output(self):
        self.write_runner(execution_fixtures.ANALYSIS_SCRIPT)
        from sfdc_agent.managed_execution import _run_process
        async def run(*args, **kwargs):
            output = await _run_process(*args, **kwargs)
            os.environ['SFDC_MANAGED_FIGMA_SCOPE'] = B['tenantId'] + '/' + B['workspaceId']
            return output
        with patch('sfdc_agent.managed_execution._run_process', side_effect=run):
            status, body, _ = await self.request(execution_fixtures.EXECUTION)
        self.assertEqual((status, body), (403, FORBIDDEN))
        self.assertFalse(list((Path(self.directory) / 'ingress').rglob('manifest.json')))

    async def test_configuration_revoked_while_starting_key_session_closes_it_before_opening(self):
        self.enterContext(patch.dict(os.environ, {'SFDC_AGENT_MANAGED_ONLY': '1',
                                                  'KIRO_API_KEY': 'ksk_scope_synthetic_fixture'}))
        payload = ExecutionRequest.model_validate({**execution_fixtures.EXECUTION, 'jobId': str(uuid4()), 'engine': 'KIRO',
            'timeoutSeconds': 90, 'deadlineEpochSeconds': int(time())+91, 'promptBundle': self.bundle.model_dump()})
        session = gateway_fixtures.StubSession()
        def start(_key, *, purpose):
            os.environ['SFDC_MANAGED_FIGMA_SCOPE'] = B['tenantId'] + '/' + B['workspaceId']
            return session
        async with kiro_lifespan(app):
            with execution_prompts(self.bundle):
                async with execution_progress(payload, runner_token(), 90):
                    headers = ((b'x-sfdc-managed-prompt', runner_token().encode()), (b'x-sfdc-kiro-purpose', b'screen'))
                    with patch('sfdc_agent.kiro_gateway.read_key', return_value='ksk_scope_synthetic_fixture'), patch(
                            'sfdc_agent.kiro_gateway.HeadlessKiroSession', side_effect=start):
                        status, body = await gateway_fixtures.GatewayTests.request(self, 'POST', '/internal/kiro/sessions',
                            {'fileKey': 'FrameDocument', 'readFigma': False, 'promptReleaseId': 'prompts-v1'}, extra_headers=headers)
                    self.assertEqual((status, body), (403, FORBIDDEN))
                    self.assertEqual(session.calls, [])
                    self.assertTrue(session.closed)

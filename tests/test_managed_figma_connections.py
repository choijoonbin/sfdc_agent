from copy import deepcopy
from hashlib import sha256
import hmac
import json
import os
from time import time
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
import unittest

from pydantic import ValidationError
from sfdc_agent.managed_figma_connections import FigmaConnectionProof, arguments, request_context
from sfdc_agent.managed_figma_scope import FigmaProviderScopeError, require_figma_scope
from sfdc_agent.managed_contracts import ExecutionRequest
from sfdc_agent.server_runtime import _organization_scope_request
from sfdc_agent.server_runtime import ServerCapabilityBoundary
from tests import test_managed_execution as fixtures

KEY = 'organization-test-service-secret'
OWNER = fixtures.EXECUTION['executionScope']
SHARED = {**OWNER, 'workspaceId': 'ad000000-0000-4000-8000-000000000002', 'actorUserId': 'ae000000-0000-4000-8000-000000000001'}


def signed(payload, purpose, *, seconds=120):
    now = int(time())
    value = {'schemaVersion': 1, 'connectionId': 'af000000-0000-4000-8000-000000000001', 'connectionVersion': 2, 'bindingVersion': 1,
             'tenantId': SHARED['tenantId'], 'workspaceId': SHARED['workspaceId'], 'runtimeProfile': 'PROCESS_DEFAULT', 'purpose': purpose,
             'contextSha256': request_context(payload, purpose), 'issuedAtEpochSeconds': now,
             'expiresAtEpochSeconds': payload['deadlineEpochSeconds'] + 30 if purpose == 'EXECUTION' else now + seconds}
    framed = '\n'.join(['sfdc-figma-connection-v1', value['connectionId'], str(value['connectionVersion']), str(value['bindingVersion']),
                        value['tenantId'], value['workspaceId'], value['runtimeProfile'], value['purpose'], value['contextSha256'],
                        str(value['issuedAtEpochSeconds']), str(value['expiresAtEpochSeconds'])])
    value['signature'] = hmac.new(KEY.encode(), framed.encode(), 'sha256').hexdigest()
    return value


class FigmaConnectionProofTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'SFDC_AGENT_EXECUTION_TOKEN': KEY,
            'SFDC_MANAGED_FIGMA_SCOPE': OWNER['tenantId'] + '/' + OWNER['workspaceId']}))

    def test_each_remote_purpose_requires_the_exact_scope_context_and_signature(self):
        for purpose, payload in [
            ('EXECUTION', {**fixtures.EXECUTION, 'executionScope': SHARED, 'deadlineEpochSeconds': int(time()) + 900}),
            ('DISCOVERY', {'executionScope': SHARED, 'sourceReference': fixtures.EXECUTION['sourceReference'], 'pageId': '1:1'}),
            ('REFERENCE', {'executionScope': SHARED, 'reference': fixtures.EXECUTION['sourceReference'] + '?node-id=1-2&version-id=123'}),
            ('DESIGN_BUILD', {'executionScope': SHARED, 'buildId': str(uuid4()), 'baseManifestSha256': 'a'*64,
                              'sources': {'guide': {'fileKey': 'Fixture', 'revision': '123', 'nodeId': '1:2'}}}),
        ]:
            payload['figmaConnection'] = signed(payload, purpose, seconds=930)
            require_figma_scope(SimpleNamespace(**SHARED), **arguments(payload, purpose))
            for field, changed in [('workspaceId', OWNER['workspaceId']), ('tenantId', str(uuid4())), ('connectionVersion', 3),
                                   ('bindingVersion', 2), ('purpose', 'REFERENCE' if purpose != 'REFERENCE' else 'DISCOVERY'),
                                   ('contextSha256', 'b'*64), ('signature', '0'*64)]:
                altered = deepcopy(payload); altered['figmaConnection'][field] = changed
                with self.subTest(purpose=purpose, field=field), self.assertRaises(FigmaProviderScopeError):
                    require_figma_scope(SimpleNamespace(**SHARED), **arguments(altered, purpose))

    def test_execution_lease_covers_900_seconds_and_cannot_be_extended_by_the_caller(self):
        now = int(time())
        payload = {**fixtures.EXECUTION, 'executionScope': SHARED, 'deadlineEpochSeconds': now+900}
        payload['figmaConnection'] = signed(payload, 'EXECUTION')
        with patch('sfdc_agent.managed_figma_connections.time', return_value=now+901):
            require_figma_scope(SimpleNamespace(**SHARED), **arguments(payload, 'EXECUTION'))
        payload['deadlineEpochSeconds'] += 1
        with self.assertRaises(FigmaProviderScopeError):
            require_figma_scope(SimpleNamespace(**SHARED), **arguments(payload, 'EXECUTION'))
        payload['deadlineEpochSeconds'] -= 1
        with patch('sfdc_agent.managed_figma_connections.time', return_value=now+931), self.assertRaises(FigmaProviderScopeError):
            require_figma_scope(SimpleNamespace(**SHARED), **arguments(payload, 'EXECUTION'))

    def test_verifier_matches_the_java_issuer_known_vector(self):
        proof={'schemaVersion':1,'connectionId':'af000000-0000-4000-8000-000000000001','connectionVersion':2,'bindingVersion':1,
               'tenantId':OWNER['tenantId'],'workspaceId':SHARED['workspaceId'],'runtimeProfile':'PROCESS_DEFAULT','purpose':'DISCOVERY',
               'contextSha256':'a'*64,'issuedAtEpochSeconds':1800000000,'expiresAtEpochSeconds':1800000120,
               'signature':'a0e88e31319f1e8cc3f25ae543617869d49e3949c9c2765ce15fea1d1c5b7c6c'}
        with patch('sfdc_agent.managed_figma_connections.time',return_value=1800000060):
            require_figma_scope(SimpleNamespace(**SHARED),proof=proof,purpose='DISCOVERY',context='a'*64)

    def test_forged_proof_never_falls_back_to_legacy_and_unknown_fields_are_forbidden(self):
        payload = {'sourceReference': fixtures.EXECUTION['sourceReference'], 'executionScope': SHARED}
        proof = signed(payload, 'DISCOVERY')
        with self.assertRaises(FigmaProviderScopeError):
            require_figma_scope(SimpleNamespace(**OWNER), proof=proof, purpose='DISCOVERY', context=proof['contextSha256'])
        for field, value in [('token', 'synthetic'), ('connectionVersion', True), ('schemaVersion', 2), ('bindingVersion', 0)]:
            with self.subTest(field=field), self.assertRaises(ValidationError):
                FigmaConnectionProof.model_validate({**proof, field: value})

    def test_server_cross_workspace_rest_requires_proof_and_offline_paths_remain_same_tenant(self):
        payload = {'executionScope': SHARED, 'sourceReference': fixtures.EXECUTION['sourceReference']}
        self.assertFalse(_organization_scope_request('/internal/v1/source-discovery', payload, OWNER))
        payload['figmaConnection'] = signed(payload, 'DISCOVERY')
        self.assertTrue(_organization_scope_request('/internal/v1/source-discovery', payload, OWNER))
        self.assertFalse(_organization_scope_request('/internal/v1/source-reference', payload, OWNER))
        self.assertTrue(_organization_scope_request('/internal/v1/executions/x/attempts/1/cancel', {'executionScope': SHARED}, OWNER))
        foreign = {**SHARED, 'tenantId': str(uuid4())}
        self.assertFalse(_organization_scope_request('/internal/v1/source-revisions', {'executionScope': foreign}, OWNER))
        self.assertFalse(_organization_scope_request('/internal/v1/unrelated', {'executionScope': SHARED}, OWNER))

    def test_rest_proof_cannot_authorize_personal_mcp_or_local_acquisition(self):
        payload = {**fixtures.EXECUTION, 'executionScope': SHARED, 'engine': 'KIRO', 'sourceAcquisition': 'MCP_ONLY',
                   'deadlineEpochSeconds': int(time())+10}
        payload['figmaConnection'] = signed(payload, 'EXECUTION')
        with self.assertRaises(ValidationError):
            ExecutionRequest.model_validate(payload)


class SharedFigmaExecutionTests(unittest.IsolatedAsyncioTestCase):
    request = fixtures.ManagedExecutionTests.request
    write_runner = fixtures.ManagedExecutionTests.write_runner

    def setUp(self):
        fixtures.ManagedExecutionTests.setUp(self)
        self.enterContext(patch.dict(os.environ, {'SFDC_AGENT_EXECUTION_TOKEN': KEY}))

    async def test_shared_execution_does_not_forward_the_authorization_to_node_or_checkpoint(self):
        self.write_runner(fixtures.ANALYSIS_SCRIPT.replace("...request.executionScope", "tenantId:request.executionScope.tenantId,workspaceId:request.executionScope.workspaceId"))
        payload = {**fixtures.EXECUTION, 'executionScope': SHARED, 'deadlineEpochSeconds': int(time())+10}
        payload['figmaConnection'] = signed(payload, 'EXECUTION')
        status, result, _ = await self.request(payload, authorization='Bearer '+KEY, fresh=False)
        self.assertEqual(status, 200, result)
        self.assertTrue(result['checkpoint']['receivedFigmaToken'])
        self.assertFalse(result['checkpoint']['receivedServiceToken'])
        self.assertNotIn('figmaConnection', result['checkpoint']['request'])
        self.assertNotIn(payload['figmaConnection']['signature'], json.dumps(result))

    async def test_standalone_server_accepts_only_the_signed_same_owner_remote_request(self):
        self.write_runner(fixtures.ANALYSIS_SCRIPT.replace("...request.executionScope", "tenantId:request.executionScope.tenantId,workspaceId:request.executionScope.workspaceId"))
        contract={'worker':{'scope':OWNER},'capabilities':{'figmaRest':True,'kiroHeadless':True,'visualCapture':True}}
        payload={**fixtures.EXECUTION,'executionScope':SHARED,'deadlineEpochSeconds':int(time())+10}
        with patch.object(fixtures,'app',ServerCapabilityBoundary(fixtures.app,contract)):
            self.assertEqual((await self.request(payload,authorization='Bearer '+KEY,fresh=False))[0],403)
            payload['figmaConnection']=signed(payload,'EXECUTION')
            self.assertEqual((await self.request(payload,authorization='Bearer '+KEY,fresh=False))[0],200)


if __name__ == '__main__':
    unittest.main()

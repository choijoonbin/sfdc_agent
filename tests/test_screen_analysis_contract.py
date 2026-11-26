import base64
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
from time import time
from uuid import uuid4
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from sfdc_agent.managed_execution import ExecutionRequest, _requires_remote_figma, _check_component_library_configuration


def checkpoint(engine='NOVA', acquisition='REST_FIRST', actual='REST', archive=None):
    contract={'schemaVersion':1,'analysisEngine':engine,'sourceAcquisition':acquisition,'actualAcquisition':actual,
              'sourceDigest':'a'*64,'designReleaseId':'nextbss-1.9-'+'b'*64,'promptReleaseId':'prompts-v1','generatorSourceSha256':'c'*64}
    captured={'schemaVersion':1,'sourceAcquisition':acquisition,'actualAcquisition':actual,'sourceDigest':'a'*64,
              'sourceStorage':'mcp-evidence' if actual=='MCP' else 'nova-plan'}
    if actual!='MCP':
        contract['sceneSha256']='d'*64;captured['sceneSha256']='d'*64
    engines=['KIRO'] if actual=='MCP' else ['NOVA','KIRO']
    analysis={'id':str(uuid4()),'digest':'e'*64,'sourceDigest':'f'*64,'analysisMethod':engine.lower(),'contract':contract,
              'allowedGenerationEngines':engines,'generationOptions':[{'engine':value,'available':value in engines} for value in ('NOVA','KIRO')],
              'source':{'local':actual=='LOCAL_FILE','version':'local-'+archive if archive else 'fixture-version'}}
    source_input={'screenInput':captured}
    if actual=='MCP':source_input['mcpSource']={'snapshotId':'a'*64}
    else:source_input['plan']='opaque-protocol-fixture'
    return {'analysis':analysis,'input':source_input}


def encoded(record):
    raw=json.dumps(record,ensure_ascii=False,separators=(',',':')).encode()
    return {'id':record['analysis']['id'],'digest':sha256(raw).hexdigest(),'bytes':base64.b64encode(raw).decode()}


def request(record, engine='KIRO', acquisition='REST_FIRST', archive=None, measured=False):
    tenant,workspace=str(uuid4()),str(uuid4())
    payload={'jobId':str(uuid4()),'attempt':1,'executionScope':{'tenantId':tenant,'workspaceId':workspace,'actorUserId':str(uuid4())},
             'timeoutSeconds':90,'deadlineEpochSeconds':int(time())+91,'kind':'SCREEN','engine':engine,'operation':'GENERATE',
             'sourceReference':'https://www.figma.com/design/Fixture?node-id=1-2&page-id=1-1',
             'designReleaseId':'nextbss-1.9-'+'b'*64,'promptReleaseId':'prompts-v1','analysisJobId':str(uuid4()),
             'analysis':encoded(record),'sourceAcquisition':acquisition}
    if archive:
        payload['archive']={'tenantId':tenant,'workspaceId':workspace,'fileKey':'Fixture','sha256':archive}
        if measured:payload.update(inputMode='MEASURED',sourceVersion='local-'+archive)
        else:payload['sourceReference']='figma-local://Fixture/source?node-id=1-2&page-id=1-1'
    return payload


class ScreenAnalysisContractTests(unittest.TestCase):
    def test_cross_engine_generation_uses_the_exact_source_and_does_not_require_remote_credentials(self):
        record=checkpoint()
        payload=ExecutionRequest.model_validate(request(record))
        self.assertEqual(payload._screen_contract['contract']['analysisEngine'],'NOVA')
        self.assertEqual(payload.engine,'KIRO')
        self.assertFalse(_requires_remote_figma(payload))
        self.assertNotIn('_screen_contract',payload.model_dump())
        self.assertEqual(payload.analysis,encoded(record))

    def test_api_only_analysis_uses_rest_with_kiro(self):
        payload=request(checkpoint(engine='KIRO',acquisition='REST_ONLY'),acquisition='REST_ONLY')
        payload.pop('analysis');payload.pop('analysisJobId');payload['operation']='ANALYZE'
        checked=ExecutionRequest.model_validate(payload)
        self.assertTrue(_requires_remote_figma(checked))

    def test_analysis_presets_reject_ambiguous_rest_fallback_but_generation_keeps_its_original_policy(self):
        for engine,acquisition in (('NOVA','REST_FIRST'),('KIRO','REST_ONLY'),('KIRO','MCP_ONLY')):
            payload=request(checkpoint(engine=engine,acquisition=acquisition),engine=engine,acquisition=acquisition)
            payload.pop('analysis');payload.pop('analysisJobId');payload['operation']='ANALYZE'
            with self.subTest(engine=engine,acquisition=acquisition):ExecutionRequest.model_validate(payload)
        for engine,acquisition in (('KIRO','REST_FIRST'),('NOVA','REST_ONLY'),('NOVA','MCP_ONLY')):
            payload=request(checkpoint(engine=engine,acquisition=acquisition),engine=engine,acquisition=acquisition)
            payload.pop('analysis');payload.pop('analysisJobId');payload['operation']='ANALYZE'
            with self.subTest(engine=engine,acquisition=acquisition),self.assertRaises(ValidationError):
                ExecutionRequest.model_validate(payload)
        cross_generation=ExecutionRequest.model_validate(request(checkpoint(),engine='KIRO',acquisition='REST_FIRST'))
        self.assertEqual(cross_generation.sourceAcquisition,'REST_FIRST')
        self.assertEqual(cross_generation.engine,'KIRO')

    def test_mcp_cannot_enable_a_deterministic_generator_or_a_fabricated_scene_hash(self):
        record=checkpoint(engine='KIRO',acquisition='MCP_ONLY',actual='MCP')
        self.assertIsNotNone(ExecutionRequest.model_validate(request(record,acquisition='MCP_ONLY'))._screen_contract)
        different_receipt=deepcopy(record);different_receipt['input']['mcpSource']['snapshotId']='0'*64
        with self.assertRaises(ValidationError):ExecutionRequest.model_validate(request(different_receipt,acquisition='MCP_ONLY'))
        with self.assertRaises(ValidationError):ExecutionRequest.model_validate(request(record,engine='NOVA',acquisition='MCP_ONLY'))
        record['analysis']['allowedGenerationEngines'].append('NOVA')
        record['analysis']['generationOptions'][0]['available']=True
        with self.assertRaises(ValidationError):ExecutionRequest.model_validate(request(record,engine='NOVA',acquisition='MCP_ONLY'))

    def test_rehashed_release_source_and_capability_changes_are_rejected(self):
        record=checkpoint()
        for field in ('promptReleaseId','designReleaseId','sourceDigest','analysisEngine','sourceAcquisition'):
            altered=deepcopy(record);altered['analysis']['contract'][field]='different'
            with self.subTest(field=field),self.assertRaises(ValidationError):
                ExecutionRequest.model_validate(request(altered))
        payload=request(record);payload['analysis']['digest']='0'*64
        with self.assertRaises(ValidationError):ExecutionRequest.model_validate(payload)

    def test_local_measured_source_keeps_its_origin_while_selecting_ai_generation(self):
        archive='a'*64
        record=checkpoint(acquisition='LOCAL_FILE',actual='LOCAL_FILE',archive=archive)
        payload=ExecutionRequest.model_validate(request(record,acquisition='LOCAL_FILE',archive=archive,measured=True))
        self.assertEqual(payload.inputMode,'MEASURED')
        self.assertEqual(payload.engine,'KIRO')
        self.assertFalse(_requires_remote_figma(payload))

    def test_unknown_contract_version_does_not_become_a_legacy_request(self):
        record=checkpoint();record['analysis']['contract']['schemaVersion']=2
        with self.assertRaises(ValidationError):ExecutionRequest.model_validate(request(record))

    def test_saved_contract_is_rejected_before_worker_work_when_runtime_does_not_support_it(self):
        payload=ExecutionRequest.model_validate(request(checkpoint()))
        with tempfile.TemporaryDirectory() as directory,patch('sfdc_agent.managed_execution.engine_root',return_value=Path(directory)):
            path=Path(directory)/'nextbss/engine/managed-runtime-capabilities.json';path.parent.mkdir(parents=True)
            caps={'schemaVersion':1,'generationFamilies':[],'sourceAcquisitionVersion':1,'sourceAcquisitions':['REST_FIRST']}
            path.write_text(json.dumps(caps))
            with self.assertRaises(Exception) as failure:_check_component_library_configuration(payload)
            self.assertEqual(failure.exception.detail,'RELEASE_RUNTIME_INCOMPATIBLE')
            caps['screenAnalysisContractVersion']=1;path.write_text(json.dumps(caps))
            _check_component_library_configuration(payload)


if __name__=='__main__':unittest.main()

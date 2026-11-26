import unittest
from copy import deepcopy
from time import time
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException

from sfdc_agent.managed_execution import ExecutionRequest, _requires_remote_figma, _check_component_library_configuration
from tests.test_managed_execution import EXECUTION


class SourceAcquisitionTests(unittest.TestCase):
    def request(self):
        return {**deepcopy(EXECUTION), 'deadlineEpochSeconds': int(time()) + 60}

    def test_rest_first_candidate_retains_remote_authorization_and_credentials(self):
        request = self.request()
        request.update(sourceAcquisition='REST_FIRST', sourceReference='HTTPS://www.figma.com/design/FrameDocument')
        request['archive'] = {**request['executionScope'], 'fileKey': 'FrameDocument', 'sha256': 'a' * 64}
        self.assertTrue(_requires_remote_figma(ExecutionRequest.model_validate(request)))
        request['archive']['workspaceId'] = 'ad000000-0000-4000-8000-000000000002'
        with self.assertRaises(ValueError):
            ExecutionRequest.model_validate(request)

    def test_mcp_requires_signed_actor_and_never_uses_rest_credentials_or_archive(self):
        request = self.request()
        request.update(engine='KIRO', sourceAcquisition='MCP_ONLY')
        with self.assertRaises(ValueError):
            ExecutionRequest.model_validate(request)
        request['executionScope']['actorUserId'] = 'ab000000-0000-4000-8000-000000000001'
        self.assertFalse(_requires_remote_figma(ExecutionRequest.model_validate(request)))
        request['archive'] = {key: value for key, value in request['executionScope'].items() if key != 'actorUserId'}
        request['archive'].update(fileKey='FrameDocument', sha256='a' * 64)
        with self.assertRaises(ValueError):
            ExecutionRequest.model_validate(request)

    def test_local_file_policy_cannot_be_used_for_an_ordinary_remote_url(self):
        request = self.request()
        request.update(sourceAcquisition='LOCAL_FILE')
        request['archive'] = {**request['executionScope'], 'fileKey': 'FrameDocument', 'sha256': 'a' * 64}
        with self.assertRaises(ValueError):
            ExecutionRequest.model_validate(request)

    def test_old_worker_without_source_policy_capability_is_rejected_before_model_work(self):
        request = ExecutionRequest.model_validate({**self.request(), 'sourceAcquisition': 'REST_FIRST'})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'nextbss/engine/managed-runtime-capabilities.json'
            path.parent.mkdir(parents=True)
            capability = {'schemaVersion': 1, 'generationFamilies': []}
            path.write_text(json.dumps(capability))
            with patch('sfdc_agent.managed_execution.engine_root', return_value=Path(directory)):
                with self.assertRaises(HTTPException) as error:
                    _check_component_library_configuration(request)
                self.assertEqual(error.exception.detail, 'RELEASE_RUNTIME_INCOMPATIBLE')
                capability.update(sourceAcquisitionVersion=1, sourceAcquisitions=['REST_FIRST'])
                path.write_text(json.dumps(capability))
                _check_component_library_configuration(request)

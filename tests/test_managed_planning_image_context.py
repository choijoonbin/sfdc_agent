"""Synthetic wrapper/render image-boundary regressions; no provider or DB calls."""
from copy import deepcopy
from hashlib import sha256
import json
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from sfdc_agent.image_input import validate_image_input
from sfdc_agent.managed_contracts import ExecutionRequest
from sfdc_agent.managed_execution_progress import execution_progress
from test_screen_analysis_contract import checkpoint, request
from image_input_fixtures import image_payload


def fixture(*, planning=True, evidence=True):
    record = checkpoint()
    source = {'fileKey': 'FixtureFile', 'nodeId': '1:3', 'version': '123456789', 'revisionVerified': True}
    record['analysis'].update(source=source, dimensions={'width': 2, 'height': 2})
    if planning:
        selection = {'schemaVersion': 1, 'requestedNodeId': '1:2', 'planningRootId': '1:2',
                     'renderNodeId': '1:3', 'sourceVersion': source['version'],
                     'planningSourceSha256': '1' * 64, 'renderSourceSha256': '2' * 64, 'evidenceSha256': '3' * 64}
        digest = sha256(json.dumps(selection, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        record['analysis']['planningSelection'] = selection
        record['analysis']['contract']['planningSelectionSha256'] = digest
        record['input']['screenInput']['planningSelectionSha256'] = digest
    if evidence:
        record['analysis']['sourceEvidence'] = {'schemaVersion': 2,
            'rawSnapshotSha256': record['analysis']['contract']['sourceDigest'],
            'sceneSha256': record['analysis']['contract']['sceneSha256'],
            'identity': {'fileKey': source['fileKey'], 'nodeId': source['nodeId'],
                         'actualRevision': source['version'], 'requestedRevision': source['version']}}
    body = image_payload(source_version=source['version'])
    body['fields']['nodeId'] = body['imageBinding']['nodeId'] = source['nodeId']
    body['fields']['reviewedAnalysis']['source'] = deepcopy(source)
    body['fields']['reviewedAnalysis']['sourceDigest'] = record['analysis']['sourceDigest']
    body['imageBinding']['analysisSourceDigest'] = record['analysis']['sourceDigest']
    return record, body


def payload(record, *, node='1-2'):
    value = request(record)
    value['sourceReference'] = 'https://www.figma.com/design/FixtureFile?node-id=' + node + '&version-id=123456789'
    return value


def validate(body, context=None):
    return validate_image_input(body['png'], body['imageBinding'], operation=body['operation'], fields=body['fields'], context=context)


class ManagedPlanningImageContextTests(unittest.IsolatedAsyncioTestCase):
    async def context(self, checked):
        # A saved generation must use its checkpoint; tests cannot read Figma.
        return execution_progress(checked, None, 60)

    async def test_receipt_bound_child_uses_private_render_context_without_mutating_wrapper_url(self):
        record, body = fixture(); checked = ExecutionRequest.model_validate(payload(record)); before = checked.sourceReference
        self.assertEqual(checked._screen_contract['planningImageSource']['nodeId'], '1:3')
        with patch('sfdc_agent.managed_execution_progress.require_figma_scope', side_effect=AssertionError('no source API')):
            async with execution_progress(checked, None, 60) as context:
                self.assertEqual((context.file_key, context.node_id, context.remote), ('FixtureFile', '1:3', False))
                self.assertEqual(validate(body, context).binding.nodeId, '1:3')
        self.assertEqual(checked.sourceReference, before)
        self.assertNotIn('_screen_contract', checked.model_dump())
        self.assertNotIn('planningImageSource', checked.model_dump())

    async def test_source_evidence_and_receipt_mismatches_never_authorize_an_alias(self):
        mutations = [
            lambda r: r['analysis']['sourceEvidence']['identity'].update(fileKey='OtherFile'),
            lambda r: r['analysis']['sourceEvidence']['identity'].update(nodeId='9:9'),
            lambda r: r['analysis']['sourceEvidence']['identity'].update(actualRevision='other'),
            lambda r: r['analysis']['sourceEvidence']['identity'].update(requestedRevision='other'),
            lambda r: r['analysis']['sourceEvidence'].update(rawSnapshotSha256='0' * 64),
            lambda r: r['analysis']['sourceEvidence'].update(sceneSha256='0' * 64),
            lambda r: r['analysis']['planningSelection'].update(renderNodeId='9:9'),
            lambda r: r['analysis']['planningSelection'].update(requestedNodeId='9:9'),
            lambda r: r['analysis']['planningSelection'].update(evidenceSha256='0' * 64),
            lambda r: r['input']['screenInput'].update(planningSelectionSha256='0' * 64),
        ]
        for i, mutate in enumerate(mutations):
            record, _ = fixture(); mutate(record)
            with self.subTest(mutation=i), self.assertRaises(ValidationError): ExecutionRequest.model_validate(payload(record))
        record, _ = fixture(); wrong = payload(record); wrong['sourceReference'] = wrong['sourceReference'].replace('version-id=123456789', 'version-id=987654321')
        with self.assertRaises(ValidationError): ExecutionRequest.model_validate(wrong)

    async def test_model_fields_cannot_promote_a_legacy_wrapper_without_private_source_evidence(self):
        record, body = fixture(evidence=False); checked = ExecutionRequest.model_validate(payload(record))
        body['fields']['reviewedAnalysis']['planningSelection'] = {'renderNodeId': '1:3', 'requestedNodeId': '1:2'}
        self.assertNotIn('planningImageSource', checked._screen_contract)
        async with execution_progress(checked, None, 60) as context:
            self.assertEqual(context.node_id, '1:2')
            with self.assertRaises(ValueError): validate(body, context)

    async def test_unsupported_old_evidence_does_not_receive_new_child_authority(self):
        record, body = fixture(); record['analysis']['sourceEvidence']['schemaVersion'] = 1
        checked = ExecutionRequest.model_validate(payload(record)); self.assertNotIn('planningImageSource', checked._screen_contract)
        async with execution_progress(checked, None, 60) as context:
            with self.assertRaises(ValueError): validate(body, context)

    async def test_png_node_revision_dimension_digest_and_scale_guards_remain_exact(self):
        record, body = fixture(); checked = ExecutionRequest.model_validate(payload(record))
        mutations = [
            lambda b: b['imageBinding'].update(fileKey='OtherFile'),
            lambda b: b['imageBinding'].update(nodeId='9:9'),
            lambda b: b['imageBinding'].update(sourceVersion='other'),
            lambda b: b['imageBinding'].update(analysisSourceDigest='0' * 64),
            lambda b: b['imageBinding'].update(sha256='0' * 64),
            lambda b: b['imageBinding'].update(scale=2),
            lambda b: b['fields']['layoutDimensions'].update(width=3),
            lambda b: b['fields']['reviewedAnalysis']['dimensions'].update(height=3),
        ]
        async with execution_progress(checked, None, 60) as context:
            for i, mutate in enumerate(mutations):
                changed = deepcopy(body); mutate(changed)
                with self.subTest(mutation=i), self.assertRaises(ValueError): validate(changed, context)

    async def test_self_consistent_foreign_model_fields_cannot_change_trusted_file_or_node(self):
        record, body = fixture(); checked = ExecutionRequest.model_validate(payload(record))
        async with execution_progress(checked, None, 60) as context:
            for field, other in [('fileKey', 'OtherFile'), ('nodeId', '9:9')]:
                changed = deepcopy(body); changed['imageBinding'][field] = other
                changed['fields'][field] = changed['fields']['reviewedAnalysis']['source'][field] = other
                self.assertIsNotNone(validate(changed))  # Only internally consistent, never owned by this attempt.
                with self.subTest(field=field), self.assertRaises(ValueError): validate(changed, context)

    async def test_existing_direct_render_url_stays_unchanged(self):
        record, body = fixture(planning=False); checked = ExecutionRequest.model_validate(payload(record, node='1-3'))
        self.assertNotIn('planningImageSource', checked._screen_contract)
        async with execution_progress(checked, None, 60) as context:
            self.assertEqual(context.node_id, '1:3'); self.assertIsNotNone(validate(body, context))

    async def test_existing_direct_mcp_render_url_preserves_unverified_revision_and_scale(self):
        record = checkpoint(engine='KIRO', acquisition='MCP_ONLY', actual='MCP')
        record['analysis']['source'] = {'fileKey': 'FixtureFile', 'nodeId': '12:34', 'snapshotId': 'a' * 64, 'revisionVerified': False}
        value = request(record, acquisition='MCP_ONLY'); value['sourceReference'] = 'https://www.figma.com/design/FixtureFile?node-id=12-34'
        checked = ExecutionRequest.model_validate(value)
        body = image_payload(source_version='mcp-snapshot:' + 'a' * 64, revision=False, scale_verified=False)
        body['fields']['reviewedAnalysis']['source'].pop('version'); body['fields']['reviewedAnalysis']['source']['snapshotId'] = 'a' * 64
        body['fields']['reviewedAnalysis']['sourceDigest'] = body['imageBinding']['analysisSourceDigest'] = record['analysis']['sourceDigest']
        async with execution_progress(checked, None, 60) as context:
            self.assertEqual(context.node_id, '12:34'); result = validate(body, context)
            self.assertFalse(result.binding.revisionVerified); self.assertFalse(result.binding.scaleVerified)

    async def test_http_private_fact_or_generate_render_override_is_rejected(self):
        record, _ = fixture()
        for extra in ({'_screen_contract': {'planningImageSource': {'nodeId': '9:9'}}}, {'renderNodeId': '1:3'}):
            with self.subTest(extra=list(extra)), self.assertRaises(ValidationError): ExecutionRequest.model_validate({**payload(record), **extra})


if __name__ == '__main__': unittest.main()

from copy import deepcopy
import json
import os
import tempfile
from unittest.mock import patch
from fastapi import HTTPException
from pathlib import Path
import unittest

from pydantic import ValidationError
from sfdc_agent.logical_scope_contract import validate_logical_scope, validate_scope_review
from sfdc_agent.managed_contracts import ExecutionRequest
from tests.test_screen_analysis_contract import checkpoint, request


def scope_record():
    record = checkpoint()
    scope = {'schemaVersion': 1, 'scopeDigest': '6' * 64,
             'source': {'fileKey': 'Fixture', 'version': 'fixture-version'}, 'baseMemberId': '17:1',
             'members': [], 'edges': [], 'gaps': [],
             'collection': {'complete': True, 'truncated': False, 'outlineMemberCount': 3,
                            'capturedMemberCount': 2, 'omittedMemberCount': 0, 'maxMembers': 24, 'maxOutlineMembers': 80}}
    for index, role in enumerate(('base', 'popup', 'candidate')):
        captured = index < 2
        scope['members'].append({'id': f'17:{index + 1}', 'sourceNodeId': f'17:{index + 1}', 'renderNodeId': f'17:{index + 1}',
            'label': f'일반적인 화면 {index}', 'role': role, 'status': 'source-evidence' if captured else 'review-required',
            'captured': captured, 'eligible': captured, 'renderNodeType': 'FRAME',
            **({'sourceSha256': str(index + 1) * 64} if captured else {'outlineSha256': '3' * 64}),
            'evidence': [{'kind': 'prototype' if index == 1 else 'source-node', 'nodeId': f'17:{index + 1}'}]})
    record['analysis']['logicalScope'] = scope
    record['analysis']['source'].update(fileKey='Fixture', nodeId='17:1')
    record['analysis']['contract']['logicalScopeSha256'] = scope['scopeDigest']
    record['input']['screenInput']['logicalScopeSha256'] = scope['scopeDigest']
    return record


def reviewed(scope):
    return {'schemaVersion': 1, 'scopeDigest': scope['scopeDigest'], 'decisions': [
        {'memberId': member['id'], 'included': member['role'] in ('base', 'popup'), 'role': member['role']}
        for member in scope['members']]}


class LogicalScopeContractTests(unittest.TestCase):
    def setUp(self):
        self.record = scope_record()
        self.scope = self.record['analysis']['logicalScope']
        self.review = reviewed(self.scope)

    def test_frozen_graph_and_full_review_are_preserved_and_normalized(self):
        scope = validate_logical_scope(self.record['analysis'], self.record['input']['screenInput'])
        self.review['decisions'].reverse()
        normalized = validate_scope_review(scope, self.review)
        self.assertEqual([item['memberId'] for item in normalized['decisions']], ['17:1', '17:2', '17:3'])
        self.assertEqual(scope['members'][2]['status'], 'review-required')
        self.assertNotIn('sourceSha256', scope['members'][2])

    def test_source_hash_pair_revision_and_capture_status_cannot_be_split(self):
        mutations = [lambda r: r['input']['screenInput'].pop('logicalScopeSha256'),
                     lambda r: r['analysis']['contract'].update(logicalScopeSha256='7' * 64),
                     lambda r: r['analysis']['source'].update(version='other-revision'),
                     lambda r: r['analysis']['source'].update(fileKey='DifferentFile'),
                     lambda r: r['analysis']['logicalScope']['members'][2].update(eligible=True),
                     lambda r: r['analysis']['logicalScope']['members'][2].update(sourceSha256='4' * 64),
                     lambda r: r['analysis']['logicalScope']['collection'].update(capturedMemberCount=3)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                altered = deepcopy(self.record)
                mutate(altered)
                with self.assertRaises(ValueError):
                    validate_logical_scope(altered['analysis'], altered['input']['screenInput'])

    def test_every_member_must_be_reviewed_once_and_base_is_immutable(self):
        mutations = [lambda r: r['decisions'].pop(),
                     lambda r: r['decisions'][0].update(included=False),
                     lambda r: r['decisions'][0].update(role='state'),
                     lambda r: r['decisions'][1].update(role='state'),
                     lambda r: r['decisions'][2].update(memberId='17:2'),
                     lambda r: r['decisions'][2].update(memberId='unknown'),
                     lambda r: r['decisions'][2].update(included=True, role='state'),
                     lambda r: r['decisions'][2].update(role='base')]
        for mutate in mutations:
            altered = deepcopy(self.review)
            mutate(altered)
            with self.subTest(review=altered), self.assertRaises(ValueError):
                validate_scope_review(self.scope, altered)

    def test_user_can_classify_captured_candidate_without_creating_source_evidence(self):
        member = self.scope['members'][2]
        member.update(captured=True, eligible=True, sourceSha256='4' * 64)
        self.scope['collection']['capturedMemberCount'] = 3
        review = reviewed(self.scope)
        review['decisions'][2].update(included=True, role='state')
        result = validate_scope_review(self.scope, review)
        self.assertTrue(result['decisions'][2]['included'])
        self.assertEqual(member['role'], 'candidate')
        self.assertEqual(member['status'], 'review-required')

    def test_missing_changed_and_forged_review_fields_are_rejected(self):
        for review in (None, {}, {**self.review, 'scopeDigest': '0' * 64}, {**self.review, 'authorized': True}):
            with self.subTest(review=review), self.assertRaises(ValueError):
                validate_scope_review(self.scope, review)
        changed = deepcopy(self.review)
        changed['decisions'][0]['included'] = 1
        with self.assertRaises(ValueError):
            validate_scope_review(self.scope, changed)

    def test_private_sources_and_over_limit_outlines_cannot_enter_public_receipt(self):
        for value in ({'sourceSnapshot': {}}, {'authorization': 'forbidden'}, {'source': {'id': '1:1', 'type': 'FRAME', 'children': []}}):
            altered = deepcopy(self.record)
            altered['analysis']['logicalScope']['gaps'].append({'code': 'UNRESOLVED', **value})
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_logical_scope(altered['analysis'], altered['input']['screenInput'])
        altered = deepcopy(self.record)
        altered['analysis']['logicalScope']['members'] *= 28
        with self.assertRaises(ValueError):
            validate_logical_scope(altered['analysis'], altered['input']['screenInput'])

    def test_request_requires_scope_review_and_forwards_only_normalized_decisions(self):
        payload = request(self.record)
        with self.assertRaises(ValidationError):
            ExecutionRequest.model_validate(payload)
        payload['scopeReview'] = self.review
        validated = ExecutionRequest.model_validate(payload)
        self.assertEqual(validated.model_dump(exclude_none=True)['scopeReview'], self.review)
        self.assertEqual(validated._screen_contract['logicalScope'], self.scope)
        payload['operation'] = 'ANALYZE'
        with self.assertRaises(ValidationError):
            ExecutionRequest.model_validate(payload)

    def test_legacy_checkpoint_does_not_silently_gain_scope(self):
        legacy = checkpoint()
        validated = ExecutionRequest.model_validate(request(legacy))
        self.assertIsNone(validated.scopeReview)
        with self.assertRaises(ValidationError):
            ExecutionRequest.model_validate({**request(legacy), 'scopeReview': self.review})
        orphaned = deepcopy(self.record)
        orphaned['analysis'].pop('logicalScope')
        with self.assertRaises(ValueError):
            validate_logical_scope(orphaned['analysis'], orphaned['input']['screenInput'])

    def test_scope_markers_cannot_downgrade_to_a_legacy_checkpoint(self):
        for marker in ('scope', 'capture', 'review'):
            changed = deepcopy(self.record)
            for key in ('contract', 'allowedGenerationEngines', 'generationOptions'):
                changed['analysis'].pop(key)
            if marker != 'scope':
                changed['analysis'].pop('logicalScope')
            if marker != 'capture':
                changed['input']['screenInput'].pop('logicalScopeSha256')
            if marker == 'review':
                changed['scopeReview'] = self.review
            with self.subTest(marker=marker), self.assertRaises(ValidationError):
                ExecutionRequest.model_validate(request(changed))

    @unittest.skipUnless(os.environ.get('SFDC_LOGICAL_SCOPE_FIXTURE'), 'The shared source-neutral golden fixture was not provided.')
    def test_common_overlay_and_typed_reference_golden_wire_contract(self):
        path = Path(os.environ['SFDC_LOGICAL_SCOPE_FIXTURE'])
        self.assertTrue(path.is_absolute() and path.stat().st_size <= 128 * 1024)
        golden = json.loads(path.read_text())
        scope = golden['logicalScope']
        analysis = {'logicalScope': scope, 'contract': {'logicalScopeSha256': scope['scopeDigest']}, 'source': scope['source']}
        self.assertEqual(validate_logical_scope(analysis, {'logicalScopeSha256': scope['scopeDigest']}), scope)
        self.assertEqual(validate_scope_review(scope, golden['scopeReview']), golden['scopeReview'])
        self.assertTrue(any(edge['action'] == 'OVERLAY' and edge['executable'] for edge in scope['edges']))
        self.assertTrue(any(edge['action'] == 'REFERENCE' and not edge['executable'] for edge in scope['edges']))
        changed = deepcopy(analysis)
        edge = next(edge for edge in changed['logicalScope']['edges'] if edge['action'] == 'REFERENCE')
        edge.update(executable=True, status='source-evidence')
        with self.assertRaises(ValueError):
            validate_logical_scope(changed, {'logicalScopeSha256': scope['scopeDigest']})

    def test_saved_logical_scope_requires_manifest_v3_before_generation_starts(self):
        from sfdc_agent.managed_execution import _check_component_library_configuration
        payload = ExecutionRequest.model_validate({**request(self.record), 'scopeReview': self.review})
        capabilities = {'schemaVersion': 1, 'generationFamilies': [], 'sourceAcquisitionVersion': 1,
                        'sourceAcquisitions': ['REST_FIRST'], 'screenAnalysisContractVersion': 1,
                        'screenPromptContextEncodingVersion': 2,
                        'logicalScopeContractVersion': 1, 'deploymentManifestVersions': [1, 2]}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'SFDC_MANAGED_ENGINE_ROOT': directory}):
            path = Path(directory) / 'nextbss/engine/managed-runtime-capabilities.json'
            path.parent.mkdir(parents=True)
            for versions in ([1, 2], ['3'], None):
                capabilities['deploymentManifestVersions'] = versions
                path.write_text(json.dumps(capabilities))
                with self.subTest(versions=versions), self.assertRaises(HTTPException) as denied:
                    _check_component_library_configuration(payload)
                self.assertEqual(denied.exception.detail, 'RELEASE_RUNTIME_INCOMPATIBLE')
            capabilities['deploymentManifestVersions'] = [1, 2, 3]
            path.write_text(json.dumps(capabilities))
            _check_component_library_configuration(payload)


if __name__ == '__main__':
    unittest.main()

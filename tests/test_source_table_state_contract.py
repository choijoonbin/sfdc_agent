import copy
import unittest

from pydantic import ValidationError
from sfdc_agent.managed_contracts import ExecutionRequest
from sfdc_agent.source_table_state_contract import SourceTableStateBindings


BASE = {
    'jobId': '8b9ed4d2-ffb7-4fd5-9e14-1d4b1ec8b42d', 'attempt': 1,
    'executionScope': {'tenantId': 'aa000000-0000-4000-8000-000000000001',
                       'workspaceId': 'ad000000-0000-4000-8000-000000000001'},
    'timeoutSeconds': 10, 'deadlineEpochSeconds': 123456789,
    'kind': 'SCREEN', 'engine': 'NOVA', 'sourceReference': 'https://www.figma.com/design/FrameDocument',
    'designReleaseId': 'nextbss-2.0-' + 'a' * 64, 'promptReleaseId': 'prompts-v1',
}
RECIPE = {'schemaVersion': 1, 'targets': [{'tableNodeId': '29876:14788', 'countNodeId': 'I29876:14801;5645:2349'}]}


class SourceTableStateContractTests(unittest.TestCase):
    def test_absent_recipe_retains_the_legacy_wire_and_typed_recipe_is_copied(self):
        before = {**BASE, 'operation': 'ANALYZE', 'inputMode': 'SOURCE'}
        wire = ExecutionRequest.model_validate(BASE).model_dump(mode='json', exclude_none=True, exclude={'figmaConnection'})
        self.assertEqual(wire, before)
        recipe = copy.deepcopy(RECIPE)
        request = ExecutionRequest.model_validate({**BASE, 'tableStateBindings': recipe})
        recipe['targets'][0]['tableNodeId'] = 'changed'
        self.assertEqual(request.tableStateBindings.model_dump(exclude_none=True), RECIPE)
        self.assertEqual(request.model_dump(mode='json', exclude_none=True)['tableStateBindings'], RECIPE)

    def test_optional_count_is_omitted_and_eight_bounded_targets_are_supported(self):
        recipe = {'schemaVersion': 1, 'targets': [{'tableNodeId': f'table:{i}'} for i in range(8)]}
        self.assertEqual(SourceTableStateBindings.model_validate(recipe).model_dump(exclude_none=True), recipe)
        SourceTableStateBindings.model_validate({'schemaVersion': 1, 'targets': [{'tableNodeId': 'a' * 160}]})

    def test_default_serialization_round_trips_absent_options_without_inventing_explicit_null(self):
        for recipe in [None, {'schemaVersion': 1, 'targets': [{'tableNodeId': '1:2'}]}, RECIPE]:
            request = ExecutionRequest.model_validate({**BASE, **({'tableStateBindings': recipe} if recipe else {})})
            wire = request.model_dump(mode='json')
            self.assertEqual('tableStateBindings' in wire, recipe is not None)
            if recipe is not None:
                self.assertEqual(wire['tableStateBindings'], recipe)
            self.assertEqual(ExecutionRequest.model_validate(wire), request)

    def test_unknown_fields_null_versions_and_invalid_identities_are_rejected(self):
        for recipe in [None, [], {}, {'schemaVersion': True, 'targets': [{'tableNodeId': '1:2'}]},
                       {'schemaVersion': 1.0, 'targets': [{'tableNodeId': '1:2'}]},
                       {'schemaVersion': 2, 'targets': [{'tableNodeId': '1:2'}]},
                       {**RECIPE, 'rows': []}, {'schemaVersion': 1, 'targets': []},
                       {'schemaVersion': 1, 'targets': [{'tableNodeId': f'node:{i}'} for i in range(9)]}]:
            with self.subTest(recipe=recipe), self.assertRaises(ValidationError):
                ExecutionRequest.model_validate({**BASE, 'tableStateBindings': recipe})
        for target in [{'tableNodeId': ''}, {'tableNodeId': 'a' * 161}, {'tableNodeId': '1:2\n'},
                       {'tableNodeId': ' 1:2'}, {'tableNodeId': '한글'}, {'tableNodeId': 12},
                       {'tableNodeId': '1:2', 'countNodeId': None}, {'tableNodeId': '1:2', 'columnNodeIds': ['1:3']},
                       {'tableNodeId': '1:2', 'countNodeId': '1:2'}]:
            with self.subTest(target=target), self.assertRaises(ValidationError):
                SourceTableStateBindings.model_validate({'schemaVersion': 1, 'targets': [target]})

    def test_duplicate_and_cross_target_roles_are_rejected(self):
        for targets in [[{'tableNodeId': '1:2'}, {'tableNodeId': '1:2'}],
                        [{'tableNodeId': '1:2', 'countNodeId': '1:3'}, {'tableNodeId': '1:3'}],
                        [{'tableNodeId': '1:2'}, {'tableNodeId': '1:3', 'countNodeId': '1:2'}],
                        [{'tableNodeId': '1:2', 'countNodeId': '1:4'}, {'tableNodeId': '1:3', 'countNodeId': '1:4'}]]:
            with self.subTest(targets=targets), self.assertRaises(ValidationError):
                SourceTableStateBindings.model_validate({'schemaVersion': 1, 'targets': targets})

    def test_only_source_screen_nova_analysis_can_receive_a_new_recipe(self):
        for override in [{'kind': 'STORY'}, {'engine': 'KIRO'}, {'operation': 'GENERATE'}, {'inputMode': 'MEASURED'}]:
            with self.subTest(override=override), self.assertRaises(ValidationError):
                ExecutionRequest.model_validate({**BASE, **override, 'tableStateBindings': RECIPE})


if __name__ == '__main__':
    unittest.main()

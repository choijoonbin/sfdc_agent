from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

from sfdc_agent.kiro_gateway import ManagedOpenSession, OpenSession
from sfdc_agent.managed_execution import ExecutionRequest
from sfdc_agent.managed_prompts import PromptBundle, checked_bundle, template_digest
from sfdc_agent.prompt_api import Activate
from sfdc_agent.prompt_provider_eval import evaluate_prompt_provider
from sfdc_agent.prompt_registry import PromptRelease, runtime_release
from sfdc_agent.prompt_templates import load_baseline_templates
from sfdc_agent.providers import ProviderError
from tests.test_managed_execution import EXECUTION


class PromptReleaseIdTests(unittest.TestCase):
    def test_actual_spring_request_serialization_is_accepted_without_rewriting_pins(self):
        # Golden JSON serialized by the production Java Request record and its
        # Jackson 3 mapper. Every template and identifier is synthetic.
        fixture = Path(__file__).parent / 'fixtures/spring-execution-request.synthetic.json'
        cases = json.loads(fixture.read_text())['cases']
        self.assertEqual(len(cases), 6)
        for case in cases:
            with self.subTest(case=case['name']):
                request = ExecutionRequest.model_validate(case['request'])
                self.assertEqual(checked_bundle(request.promptBundle).id, request.promptReleaseId)
                self.assertEqual(request.componentLibrary.family, 'NOVA_PUBLIC')
                opened = ManagedOpenSession(fileKey='ContractOnly', readFigma=False,
                                            promptReleaseId=request.promptReleaseId)
                self.assertEqual(opened.promptReleaseId, request.promptReleaseId)
                self.assertFalse(opened.readFigma)
        mismatch = deepcopy(cases[-1]['request'])
        mismatch['promptReleaseId'] = 'prompts-v1'
        with self.assertRaises(ValidationError):
            ExecutionRequest.model_validate(mismatch)

    def test_pinned_release_does_not_enable_live_figma_reads(self):
        for contract in (OpenSession, ManagedOpenSession):
            with self.subTest(contract=contract.__name__), self.assertRaisesRegex(
                    ValidationError, 'captured Figma evidence'):
                contract(fileKey='ContractOnly', readFigma=True, promptReleaseId='prompts-v1')

    def consumers(self, release_id):
        templates = load_baseline_templates()
        bundle = {'releaseId': release_id, 'sha256': template_digest(templates), 'templates': templates}
        return (
            lambda: PromptBundle.model_validate(bundle),
            lambda: ExecutionRequest.model_validate({**EXECUTION, 'deadlineEpochSeconds': 1,
                                                    'promptReleaseId': release_id, 'promptBundle': bundle}),
            lambda: OpenSession.model_validate({'promptReleaseId': release_id}),
            lambda: ManagedOpenSession.model_validate({'promptReleaseId': release_id}),
            lambda: Activate.model_validate({'operation': 'screen.generate', 'actor': 'test',
                                             'version': 'v2', 'expectedReleaseId': release_id}),
        )

    def test_existing_and_imported_release_formats_work_across_protocols(self):
        for release_id in ('prompts-v1', 'prompts-' + 'a' * 32, 'prompts-nova-' + 'b' * 64):
            for validate in self.consumers(release_id):
                with self.subTest(release_id=release_id):
                    validate()

    def test_paths_urls_unknown_formats_and_noncanonical_hashes_remain_invalid(self):
        invalid = ('prompts-nova-' + 'a' * 63, 'prompts-nova-' + 'a' * 65,
                   'prompts-nova-' + 'A' * 64, 'prompts-nova-' + 'g' * 64,
                   'prompts-' + 'a' * 64, 'prompts-v2', '../prompts-v1',
                   'prompts-v1/path', 'https://example.invalid/prompts-v1',
                   'prompts-nova-' + 'a' * 64 + '?version=1')
        for release_id in invalid:
            for validate in self.consumers(release_id):
                with self.subTest(release_id=release_id), self.assertRaises(ValidationError):
                    validate()

    def test_imported_release_evaluation_keeps_exact_snapshot_and_pin(self):
        baseline = runtime_release('prompts-v1')
        release_id = 'prompts-nova-' + 'c' * 64
        release = PromptRelease(release_id, baseline.versions, baseline.templates)
        provider = Mock()
        provider.name, provider.model = 'openai', 'synthetic-model'
        provider.complete_rendered.return_value = {'workflows': [], 'unassigned': []}
        with patch('sfdc_agent.prompt_provider_eval.runtime_release', side_effect=AssertionError('Must use the supplied bundle')):
            response = evaluate_prompt_provider('story.classification',
                {'evidence': '{"headings":[],"screens":[],"links":[]}'}, 'openai',
                release_id=release_id, templates_release=release, provider_override=provider)
        self.assertEqual(response['promptReleaseId'], release_id)
        provider.complete_rendered.assert_called_once()
        with self.assertRaises(ProviderError):
            evaluate_prompt_provider('story.classification', {}, 'openai',
                release_id='prompts-nova-' + 'd' * 64, templates_release=release, provider_override=provider)
        self.assertEqual(provider.complete_rendered.call_count, 1)


if __name__ == '__main__':
    unittest.main()

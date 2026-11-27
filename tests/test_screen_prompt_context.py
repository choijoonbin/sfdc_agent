from copy import deepcopy
from hashlib import sha256
import json
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

from screen_prompt_context_fixtures import bridge, screen_fields
from sfdc_agent.config import engine_root
from sfdc_agent.kiro_gateway import Prompt
from sfdc_agent.prompt_provider_eval import evaluate_prompt_provider
from sfdc_agent.prompt_registry import BASELINE_RELEASE
from sfdc_agent.screen_prompt_context import read_screen_prompt_context
from sfdc_agent.screen_tasks import build_screen_task


class ScreenPromptContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fields = screen_fields()
        cls.output = bridge(cls.fields)
        cls.prepared = read_screen_prompt_context(cls.output['sourceJson'], cls.output['prepared'])

    def test_actual_shared_codec_preserves_typed_facts_and_uses_the_original_budget(self):
        receipt = self.prepared.receipt
        self.assertGreater(receipt['sourceBytes'], 512 * 1024)
        self.assertLessEqual(receipt['promptBytes'], 512 * 1024)
        self.assertEqual(receipt['encoding'], 'screen-record-tables-v1')
        self.assertEqual(json.loads(self.output['sourceJson']), self.fields['sourceContext'])
        self.assertEqual(self.prepared.matches(self.fields['sourceContext']), self.output['prepared']['json'])
        changed = deepcopy(self.fields['sourceContext'])
        changed['planningContext']['hidden'][0] = 0
        with self.assertRaises(ValueError):
            self.prepared.matches(changed)

    def test_shared_golden_keeps_unicode_special_keys_missing_null_and_numeric_json(self):
        path = engine_root() / 'nextbss/shared/fixtures/screen-prompt-context-v1.json'
        golden = json.loads(path.read_text())
        prepared = read_screen_prompt_context(golden['sourceJson'],
            {'json': golden['encodedJson'], 'receipt': golden['receipt']})
        self.assertEqual(prepared.matches(golden['source']), golden['encodedJson'])

    def test_all_screen_renderers_use_the_same_exact_prepared_json_without_wire_fields(self):
        for operation in ['screen.review-plan', 'screen.generate', 'screen.analyze.local', 'screen.repair']:
            with self.subTest(operation=operation):
                fields = deepcopy(self.fields)
                if operation == 'screen.repair':
                    fields = {'local': True, 'diagnostic': 'Synthetic compile finding', 'previousDraft': '{}', 'evidence': fields}
                before = deepcopy(fields)
                text = build_screen_task(operation, fields, prepared_context=self.prepared)
                self.assertTrue(self.prepared.prompt_json in text,
                                'Every renderer must preserve the exact prepared source JSON.')
                self.assertEqual(fields, before)
        with self.assertRaises(ValidationError):
            build_screen_task('screen.generate', {**self.fields, 'sourcePromptContext': self.output['prepared']})
        with self.assertRaises(ValidationError):
            Prompt.model_validate({'operation': 'screen.generate', 'fields': self.fields,
                                  '_screen_prompt_context': self.output['prepared']})

    def test_existing_complete_prompt_limit_is_kept_after_source_compaction(self):
        fields = deepcopy(self.fields)
        fields['reviewedAnalysis']['syntheticLargeNote'] = 'x' * (8 * 1024 * 1024)
        prompt = Prompt(operation='screen.generate', fields=fields)
        prompt._screen_prompt_context = self.prepared
        with self.assertRaisesRegex(ValueError, 'prompt exceeds'):
            prompt.build()

    def test_forged_receipts_paths_types_and_shared_values_fail_independent_decode(self):
        def change_prompt(change):
            value = deepcopy(self.output)
            encoded = json.loads(value['prepared']['json'])
            change(encoded)
            text = json.dumps(encoded, ensure_ascii=False, separators=(',', ':'))
            value['prepared']['json'] = text
            value['prepared']['receipt'].update(promptBytes=len(text.encode()), promptSha256=sha256(text.encode()).hexdigest())
            return value

        cases = [change_prompt(lambda encoded: encoded['tablePaths'].append(encoded['tablePaths'][0])),
                 change_prompt(lambda encoded: encoded.update(encoding='unknown-schema')),
                 change_prompt(lambda encoded: encoded['sharedValues'][0]['paths'].append(encoded['sharedValues'][0]['paths'][0])),
                 change_prompt(lambda encoded: encoded['sharedValues'][0].update(value={'different': True}))]
        changed = deepcopy(self.output)
        changed['prepared']['receipt']['sourceSha256'] = 'b' * 64
        cases.append(changed)
        for index, value in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(ValueError):
                read_screen_prompt_context(value['sourceJson'], value['prepared'])

    def test_direct_provider_evaluation_uses_the_shared_bridge_before_provider_setup(self):
        provider = Mock()
        provider.name, provider.model = 'fixture', 'fixture-model'
        provider.complete_rendered.return_value = {'synthetic': True}
        provider.provider_receipt = {'schemaVersion': 1, 'provider': 'fixture'}
        before = deepcopy(self.fields)
        with patch('sfdc_agent.kiro_screen_projection.engine_root', return_value=engine_root()):
            result = evaluate_prompt_provider('screen.generate', self.fields, 'fixture',
                release_id=BASELINE_RELEASE, provider_override=provider)
        sent = provider.complete_rendered.call_args.args[0]
        self.assertTrue(self.prepared.prompt_json in sent, 'Direct evaluation must use the prepared source JSON.')
        self.assertEqual(result['providerReceipt']['sourcePromptContext'], self.prepared.receipt)
        self.assertEqual(self.fields, before)
        self.assertEqual(set(result['providerReceipt']['sourcePromptContext']),
                         {'schemaVersion', 'encoding', 'sourceBytes', 'promptBytes', 'sourceSha256', 'promptSha256'})


if __name__ == '__main__':
    unittest.main()

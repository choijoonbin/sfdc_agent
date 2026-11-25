import copy
import unittest

from sfdc_agent.screen_tasks import build_screen_task
from sfdc_agent.nova_prompt_evidence import prompt_source_context


class NovaPromptEvidenceTests(unittest.TestCase):
    contract = {"version": "1.0.0", "modules": [{"name": "novaRadioGroup", "tag": "c-nova-radio-group", "events": ["change"]}]}

    def test_final_prompt_preserves_raw_text_and_lossless_runs_without_stale_api_guidance(self):
        context = {
            "nodes": [{"id": "10:1", "text": "An authored valuechange label"}],
            "appearance": [{"characterStyleOverrides": [0, 1], "styleOverrideTable": {"1": {"fontWeight": 700}}}],
            "verifiedComponentContracts": ['{"tag":"c-nova-radio-group","events":["change"]}'],
            "verifiedRadioGroups": [{"nodeId": "10:2", "tag": "c-nova-radio-group", "attributes": {"options": [{"value": "a", "label": "A"}]}}],
        }
        original = copy.deepcopy(context)
        fields = {"fileKey": "Fixture", "nodeId": "10:1", "name": "nextBssFixture", "local": True,
                  "layoutDimensions": {"width": 100, "height": 100}, "sourceContext": context,
                  "version": "1.9", "tokenDeclarations": "", "typographyGroups": [], "componentVariants": "",
                  "novaContract": self.contract}
        for operation in ("screen.review-plan", "screen.generate"):
            text = build_screen_task(operation, fields)
            self.assertIn('"characterStyleOverrides":[0,1]', text)
            self.assertIn('"fontWeight":700', text)
            self.assertIn("An authored valuechange label", text)
            self.assertNotIn("c-next-bss-", text)
        self.assertEqual(context, original)

    def test_saved_derived_guidance_cannot_bypass_the_pinned_projection(self):
        for context in (
            {"verifiedComponentContracts": ["Use c-next-bss-input-action and onvaluechange"]},
            {"verifiedFields": [{"tag": "c-next-bss-input-action"}]},
            {"verifiedFields": [{"tag": "c-nova-text-field", "legacyTag": "c-next-bss-input-action"}]},
            {"verifiedSearchTriggers": [{"children": [{"template": "<c-next-bss-input-action></c-next-bss-input-action>"}]}]},
        ):
            with self.subTest(context=context), self.assertRaisesRegex(ValueError, "pinned NOVA projection"):
                prompt_source_context(context, self.contract)


if __name__ == "__main__":
    unittest.main()

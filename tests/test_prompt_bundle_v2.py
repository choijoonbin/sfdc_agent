import unittest
from sfdc_agent.prompt_templates import load_baseline_templates, load_versioned_templates, validate_template
from sfdc_agent.screen_tasks import build_screen_task
from tests.test_prompt_templates import template_cases


class PromptBundleV2Tests(unittest.TestCase):
    def test_common_visual_rules_are_versioned_without_rewriting_v1_or_story(self):
        v1, v2 = load_baseline_templates(), load_versioned_templates('v2')
        self.assertEqual(set(v1), set(v2))
        self.assertNotIn('Source visual acceptance contract', v1['screen.generate'])
        self.assertIn('Source visual acceptance contract', v2['screen.generate'])
        for operation in v2:
            validate_template(operation, v2[operation])
            if operation.startswith('story.'):
                self.assertEqual(v1[operation], v2[operation])
        operation, fields = template_cases()['generate_remote']
        text = build_screen_task(operation, fields, templates=v2)
        self.assertIn('root-relative CSS pixels', text)
        self.assertIn('do not clamp, scale, shrink', text)
        v2['screen.generate'] = 'altered local copy'
        self.assertNotEqual(v2, load_versioned_templates('v2'))

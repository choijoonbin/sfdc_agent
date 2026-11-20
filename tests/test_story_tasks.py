import json
import unittest

from sfdc_agent.story_tasks import build_story_task


class StoryTaskTests(unittest.TestCase):
    def test_analysis_retains_serialized_source_and_source_limits(self):
        evidence = '{"width":24.75,"title":"확인","visible":false}'
        categories = '{"layout-geometry":"Measure exact bounds."}'
        prompt = build_story_task("story.analysis", {
            "evidence": evidence, "categories": categories, "maxFacts": 48,
        })

        self.assertTrue(prompt.startswith("# NOVA Prompt v1.0.0 · Storyboard Specification\n"))
        self.assertIn("Use up to 48 high-priority facts", prompt)
        self.assertIn(categories, prompt.splitlines())
        self.assertEqual(prompt.rstrip().splitlines()[-1], evidence)
        with self.assertRaises(ValueError):
            build_story_task("story.analysis", {"evidence": evidence, "categories": "[]", "maxFacts": 48})

    def test_generation_preserves_frozen_contract_and_bounds_retry_diagnostics(self):
        digest = "a" * 64
        bundle = "nextBssSampleStory"
        reviewed = json.dumps({"bundleName": bundle, "specDigest": digest, "spec": {"plan": {}}})
        diagnostic = '{"code":"BENCHMARK_C_TEMPLATE_INVALID","message":"컴파일 오류"}'
        prompt = build_story_task("story.generate", {"input": reviewed, "diagnostic": diagnostic})
        contract = json.loads(next(line for line in prompt.splitlines() if line.startswith('{"specDigest"')))

        self.assertEqual(contract["specDigest"], digest)
        self.assertEqual(list(contract["files"]), [bundle + suffix for suffix in (".html", ".css", ".js")])
        self.assertIn(reviewed, prompt.splitlines())
        self.assertIn("## Retry diagnostic data", prompt)
        self.assertTrue(prompt.rstrip().endswith(diagnostic))
        with self.assertRaises(ValueError):
            build_story_task("story.generate", {"input": reviewed, "diagnostic": '{"code":"invalid"}'})

    def test_classification_rejects_non_object_evidence(self):
        evidence = '{"headings":[],"screens":[],"links":[]}'
        self.assertTrue(build_story_task("story.classification", {"evidence": evidence}).rstrip().endswith(evidence))
        with self.assertRaises(ValueError):
            build_story_task("story.classification", {"evidence": "[]"})


if __name__ == "__main__":
    unittest.main()

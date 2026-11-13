import unittest

from pydantic import ValidationError

from sfdc_agent.screen_tasks import build_screen_task


class ScreenTasksTests(unittest.TestCase):
    reference = {"fileKey": "FixtureFile", "nodeId": "29876:15045"}
    evidence = {
        **reference, "name": "nextBssScreen29876x15045",
        "layoutDimensions": {"width": 1380, "height": 1716},
        "version": "1.9", "tokenDeclarations": "--nb-color-t-blue-600: #3617ce;",
        "typographyGroups": ["--nb-typography-example"],
        "componentVariants": "Button: Filled, Ghost",
    }

    def test_capture_analysis_and_metadata_preserve_source_identity(self):
        capture = build_screen_task("screen.capture", {
            **self.reference, "maxDimension": 4096, "readLayout": True,
        })
        self.assertIn('"maxDimension":4096,"enableBase64Response":true', capture)
        self.assertIn('get_metadata exactly once with {"fileKey":"FixtureFile","nodeId":"29876:15045"}', capture)
        analysis = build_screen_task("screen.analyze", {
            "fileKey": "FixtureFile", "nodeIds": ["29876:15045"], "includeScreenshot": True,
        })
        self.assertIn('"skillNames":"figma-design-to-code","excludeScreenshot":false', analysis)
        self.assertIn("Do not generate code yet.", analysis)
        metadata = build_screen_task("screen.metadata", {"fileKey": "FixtureFile", "nodeIds": ["12:35"]})
        self.assertIn('{"fileKey":"FixtureFile","nodeId":"12:35"}', metadata)
        self.assertIn("get_metadata", metadata)

    def test_remote_generation_keeps_release_rules_and_reports_incomplete_source(self):
        evidence = {
            **self.evidence,
            "coverage": {"split": True, "nodesRead": ["12:34"], "unresolved": ["12:35"], "rateLimited": True},
            "actions": [{"kind": "search", "nodeId": "12:44"}],
        }
        prompt = build_screen_task("screen.generate", evidence)
        for required in (
            "https://www.figma.com/design/FixtureFile?node-id=29876-15045",
            "verified child get_design_context responses: 12:34",
            "Design context coverage remains incomplete for these nodes: 12:35",
            "original_width and original_height",
            "NOVA verified the selected node's Figma layout as 1380 × 1716px",
            "NextBSS v1.9",
            "--nb-color-t-blue-600",
            "Render each field label once",
            "preserve the measured outer size with border-box sizing",
            "Unset widths are zero and an unset stroke color is transparent",
            '"kind":"search","nodeId":"12:44"',
            "CustomEvent('search', { detail: { values } })",
            "assign each scoped field its resetValue",
        ):
            self.assertIn(required, prompt)

    def test_local_analysis_and_generation_share_exact_measured_data(self):
        evidence = {
            **self.evidence, "local": True,
            "sourceContext": {
                "nodes": [{"id": "29876:15045", "box": [0, 0, 1380, 1716]}],
                "actions": [{"kind": "reset", "nodeId": "12:46"}],
            },
            "actions": [{"kind": "reset", "nodeId": "12:46"}],
        }
        analysis = build_screen_task("screen.analyze.local", evidence)
        prompt = build_screen_task("screen.generate", evidence)
        for value in (analysis, prompt):
            self.assertIn('"id":"29876:15045","box":[0,0,1380,1716]', value)
            self.assertIn('"kind":"reset","nodeId":"12:46"', value)
            self.assertNotIn("get_screenshot", value)
            self.assertNotIn("get_design_context", value)
        self.assertIn("Do not write code yet.", analysis)
        self.assertIn("NOVA measured the selected node in the local Figma copy as 1380 × 1716px", prompt)
        self.assertIn("one-edge separator into a full outline", prompt)

    def test_schema_rejects_invalid_source_and_unknown_operation(self):
        with self.assertRaises(ValidationError):
            build_screen_task("screen.capture", {**self.reference, "maxDimension": 99999})
        with self.assertRaises(ValidationError):
            build_screen_task("screen.analyze", {"fileKey": "FixtureFile", "nodeIds": ["other file"]})
        with self.assertRaises(ValueError):
            build_screen_task("screen.generate", {**self.evidence, "local": True})
        with self.assertRaises(ValueError):
            build_screen_task("screen.unknown", {})

    def test_semantic_plan_keeps_source_digest_and_original_context(self):
        digest = "a" * 64
        prompt = build_screen_task("screen.plan", {
            "source": {"id": "29876:15045"}, "sourceDigest": digest, "referenceContext": "Pinned evidence",
        })
        self.assertIn(f'"sourceDigest":"{digest}"', prompt)
        self.assertIn('{"id":"29876:15045"}', prompt)
        self.assertIn("Pinned evidence", prompt)
        repair = build_screen_task("screen.plan.repair", {"diagnostic": "Invalid scope"})
        self.assertIn('Validation error: "Invalid scope"', repair)

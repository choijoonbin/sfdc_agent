import json
import unittest

from pydantic import ValidationError

from sfdc_agent.screen_tasks import build_screen_task


class ScreenTasksTests(unittest.TestCase):
    reference = {"fileKey": "FixtureFile", "nodeId": "29876:15045"}
    evidence = {
        **reference, "name": "nextBssScreen29876x15045",
        "layoutDimensions": {"width": 1380, "height": 1716},
        "version": "1.9", "tokenDeclarations": "--nova-color-t-blue-600: #3617ce;",
        "typographyGroups": ["--nova-typography-example"],
        "componentVariants": "Button: Filled, Ghost",
        "novaContract": {"version":"1.0.0", "tokenModule":"c/novaTokens", "modules":[{"name":"novaButton","kind":"ui","tag":"c-nova-button","events":["click"]}]},
    }

    def test_capture_analysis_and_metadata_preserve_source_identity(self):
        capture = build_screen_task("screen.capture", {
            **self.reference, "maxDimension": 4096, "readLayout": True,
        })
        self.assertIn('"maxDimension":4096,"enableBase64Response":true', capture)
        self.assertIn('`get_metadata` exactly once with {"fileKey":"FixtureFile","nodeId":"29876:15045"}', capture)
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
            "NOVA LWC 1.0.0 contracts (Figma design 1.9)",
            "--nova-color-t-blue-600",
            "Render each field label once",
            "Pinned NOVA public contract JSON:",
            '"tokenModule":"c/novaTokens"',
            "never JSON.stringify strings",
            "onrequest_close",
            "heading, message and actions array",
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

    def test_saved_remote_analysis_is_supplied_verbatim_to_generation(self):
        captured = '<div data-node-id="12:44">조회</div>'
        fields = {
            **self.evidence,
            "capturedContext": [{"nodeId": self.reference["nodeId"], "text": captured}],
        }
        review = build_screen_task("screen.review-plan", fields)
        self.assertIn("Make no tool calls and do not write code.", review)
        self.assertIn('"requiresReview":true', review)
        self.assertIn('data-node-id=\\"12:44\\"', review)

        analysis = {
            "id": "frozen-analysis", "digest": "a" * 64,
            "mappings": [{"nodeId": "12:44", "label": "조회", "component": "Button"}],
            "issues": [{"code": "KIRO_SCREEN_BEHAVIOR_UNCONFIRMED", "nodeId": "12:44", "requiresReview": True}],
        }
        generation = build_screen_task("screen.generate", {**fields, "reviewedAnalysis": analysis})
        self.assertIn('"id":"frozen-analysis","digest":"' + analysis["digest"], generation)
        self.assertIn('"nodeId":"12:44","label":"조회","component":"Button"', generation)
        self.assertIn(captured, generation)
        self.assertIn("Do not re-analyze a newer Figma revision or make any tool calls.", generation)
        self.assertNotIn("preceding turns", generation)
        self.assertNotIn("call Figma get_screenshot", generation)

    def test_transient_overlay_policy_separates_comparison_visibility_from_runtime_behavior(self):
        snackbar = {
            "id": "29876:17596", "type": "INSTANCE", "name": "State=Close", "visible": True,
            "box": [381, 570, 608, 74],
        }
        fields = {
            **self.evidence, "local": True,
            "sourceContext": {"nodes": [snackbar]},
        }

        review = build_screen_task("screen.review-plan", fields)
        self.assertIn("A visible overlay remains visible in the captured state", review)
        self.assertIn("`Close` and `Action + Close` describe a close affordance or actions", review)
        self.assertIn("runtime display condition is unresolved", review)
        self.assertIn('"name":"State=Close","visible":true', review)

        generation = build_screen_task("screen.generate", fields)
        self.assertIn("The draft's first render is the Figma comparison state", generation)
        self.assertIn("Neither string means that the overlay is closed", generation)
        self.assertIn("운영 표시 조건 확인 필요", generation)
        self.assertIn("never hide the captured overlay", generation)
        self.assertIn("persistent `duration=0` contract", generation)

        repair = build_screen_task("screen.repair", {
            "local": True,
            "diagnostic": "Snackbar 29876:17596 is visible in Figma but hidden in the draft.",
        })
        self.assertIn("Do not interpret `Close` or `Action + Close` as `open=false`", repair)
        self.assertIn("Never repair a visual mismatch by silently hiding", repair)

        repair_with_evidence = build_screen_task("screen.repair", {
            "local": True, "evidence": fields,
            "diagnostic": "Snackbar visibility mismatch", "previousDraft": '{"html":"<template></template>"}',
        })
        self.assertIn("The draft's first render is the Figma comparison state", repair_with_evidence)
        self.assertIn("leave an unresolved runtime trigger explicit in notes", repair_with_evidence)

    def test_verified_history_table_evidence_reaches_review_and_generation(self):
        table = {
            "nodeId": "12:40", "headerNodeId": "12:41", "box": [20, 100, 190, 120],
            "columns": [
                {"nodeId": "12:42", "label": "첫째 열", "width": 72},
                {"nodeId": "12:43", "label": "둘째 열", "width": 118},
            ],
            "empty": {"nodeId": "12:44", "label": "비어 있습니다."},
            "rowCount": 0, "style": {"--nova-table-header-height": "48px"},
        }
        serialized = json.dumps([table], ensure_ascii=False, separators=(",", ":"))
        for source in (
            {"historyTables": [table]},
            {"local": True, "sourceContext": {"historyTables": [table]}, "historyTables": [table]},
            {"local": True, "sourceContext": {"historyTables": [table]}},
        ):
            fields = {**self.evidence, **source}
            review = build_screen_task("screen.review-plan", fields)
            generation = build_screen_task("screen.generate", fields)
            self.assertEqual(review.count(serialized), 1)
            self.assertEqual(generation.count(serialized), 1)
            self.assertIn("Do not report those facts as missing.", review)
            self.assertIn("Bind collection/object props to actual JS arrays or objects", generation)
            self.assertIn("rows are { id, cells: [...] }", generation)
            self.assertIn("entry.nodeId in data-figma-node-id on the component host", generation)
            self.assertIn("preserve measured source typography and baseline", generation)

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
        self.assertIn("## Validation error data", repair)
        self.assertIn('"Invalid scope"', repair)

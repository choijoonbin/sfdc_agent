import copy
import json
from pathlib import Path
import tempfile
import unittest

from sfdc_agent.kiro_headless import SessionError
from sfdc_agent.kiro_mcp import validate_design_context_output
from sfdc_agent.managed_figma_mcp import SnapshotStore
from sfdc_agent.figma_mcp_connections import ConnectionScope
from sfdc_agent.mcp_acquisition_readiness import (assess_design_context_output, assess_mcp_acquisition_readiness,
                                                require_design_context_content)

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAXpeqz8AAAAASUVORK5CYII="

def output(text):
    return {"isError": False, "content": [{"type": "text", "text": text}]}


def source():
    return "```tsx\nexport default function Screen() { return <div className=\"flex w-full p-4\">private-visible-copy</div>; }\n```"


def metadata():
    return {"nodes": [{"id": "1:2", "parentId": None, "type": "FRAME", "name": "private-layer-label",
                       "metadataBounds": {"x": 100, "y": 200, "width": 300, "height": 100}},
                      {"id": "1:3", "parentId": "1:2", "type": "TEXT", "name": "private-text-label",
                       "metadataBounds": {"x": 0, "y": 0, "width": 100, "height": 20}}]}


def tools(text=None):
    return [{"name": name, "status": "completed", "input": {"fileKey": "FixtureFile", "nodeId": "1:2"},
             "output": {"isError": False, "content": [{"type": "image", "mimeType": "image/png", "data": PNG}]}
             if name == "get_screenshot" else output(text if name == "get_design_context" else "private-provider-value")}
            for name in ("get_metadata", "get_design_context", "get_screenshot")]


class McpAcquisitionReadinessTest(unittest.TestCase):
    def test_completed_code_connect_question_is_not_actual_design_context(self):
        # Synthetic provider-envelope fixture. No captured production response or
        # production file/node ID is stored in the test repository.
        notice = ("Some selected design components are missing Code Connect mappings. Please ask the user "
                  "if they would like to connect them. Would you like to connect code components? "
                  "If the user answers no, call get_design_context again. private-provider-instruction")
        value = output(notice)
        before = copy.deepcopy(value)
        report = assess_design_context_output(value)
        self.assertEqual(report["status"], "RETRY_REQUIRED")
        self.assertIn("DESIGN_CONTEXT_GUIDANCE_ONLY", report["codes"])
        self.assertEqual(report["implementationBlockCount"], 0)
        self.assertEqual(report["geometryEvidence"], "NOT_PROVIDED")
        self.assertFalse(report["providerInstructionsExecuted"])
        self.assertNotIn("private-", json.dumps(report))
        self.assertEqual(value, before)
        with self.assertRaises(SessionError) as caught:
            require_design_context_content(value)
        self.assertEqual(caught.exception.code, "FIGMA_MCP_EVIDENCE_INCOMPLETE")
        self.assertNotIn("private-", str(caught.exception))

    def test_actual_code_and_geometry_are_observations_only_not_authored_parity(self):
        value = output(source())
        report = require_design_context_content(value)
        self.assertEqual(report["status"], "CONTEXT_OBSERVED")
        self.assertEqual(report["implementationEvidence"], "OBSERVED")
        self.assertEqual(report["geometryEvidence"], "OBSERVED")
        self.assertEqual(report["authoredCompleteness"], "NOT_PROVEN")
        self.assertNotIn("private-", json.dumps(report))

    def test_provider_footer_cannot_create_or_discard_source_implementation(self):
        notice = "Please ask the user about Code Connect. Would you like to map components?"
        report = assess_design_context_output(output(source() + "\n" + notice))
        self.assertEqual(report["status"], "CONTEXT_OBSERVED")
        self.assertEqual(report["providerGuidanceBlockCount"], 1)
        authored = 'const message = "The response was truncated due to the output limit. Code Connect: ask the user";'
        self.assertEqual(require_design_context_content(output(authored))["implementationEvidence"], "OBSERVED")

    def test_missing_layout_or_text_only_metadata_stays_retry_required(self):
        report = assess_design_context_output(output('const label = "private-source-copy";'))
        self.assertEqual(report["implementationEvidence"], "OBSERVED")
        self.assertIn("DESIGN_CONTEXT_GEOMETRY_NOT_OBSERVED", report["codes"])
        report = assess_design_context_output(output('<frame id="1:2" width="100" height="100"/>'))
        self.assertEqual(report["implementationEvidence"], "NOT_PROVIDED")
        self.assertEqual(report["status"], "RETRY_REQUIRED")

    def test_explicit_size_notices_or_truncation_flags_fail_without_scanning_authored_copy_as_notices(self):
        for value in [output("The response is too large and truncated. Call get_design_context on child nodes."),
                      {**output(source()), "truncated": True}, {**output(source()), "isError": True}, output(""), {}]:
            with self.subTest(value=value):
                with self.assertRaises(SessionError):
                    require_design_context_content(value)
        value = output('<div className="w-full">The response is too large and truncated.</div>')
        self.assertEqual(assess_design_context_output(value)["status"], "CONTEXT_OBSERVED")

    def test_same_selected_tool_receipt_plus_geometry_are_required_without_label_inference(self):
        calls = tools(source())
        context = metadata()
        report = assess_mcp_acquisition_readiness(calls, file_key="FixtureFile", node_id="1-2", metadata_context=context)
        self.assertTrue(report["readyForAnalysis"])
        self.assertEqual(report["status"], "INPUTS_OBSERVED")
        self.assertEqual(report["metadata"]["geometryObserved"], 2)
        self.assertEqual(report["metadata"]["authoredTextCompleteness"], "NOT_PROVEN")
        self.assertEqual(report["originalFigmaRevision"], "UNVERIFIED")
        self.assertEqual(report["sameRevision1xPng"], "NOT_PROVEN")
        self.assertFalse(report["changesAcquisitionRoute"])
        self.assertNotIn("private-", json.dumps(report))
        calls[1]["input"]["fileKey"] = "OtherFile"
        wrong = assess_mcp_acquisition_readiness(calls, file_key="FixtureFile", node_id="1:2", metadata_context=context)
        self.assertFalse(wrong["readyForAnalysis"])
        self.assertIn("ACQUISITION_DESIGN_CONTEXT_MISSING", wrong["codes"])

    def test_metadata_geometry_gaps_duplicate_ids_and_missing_root_are_explicit(self):
        for change, expected in [
            (lambda value: value["nodes"][1].pop("metadataBounds"), "ACQUISITION_METADATA_GEOMETRY_INCOMPLETE"),
            (lambda value: value["nodes"].append(copy.deepcopy(value["nodes"][1])), "ACQUISITION_METADATA_ID_UNVERIFIED"),
            (lambda value: value["nodes"].pop(0), "ACQUISITION_METADATA_SELECTED_ROOT_MISSING"),
        ]:
            context = metadata()
            change(context)
            report = assess_mcp_acquisition_readiness(tools(source()), file_key="FixtureFile", node_id="1:2", metadata_context=context)
            self.assertFalse(report["readyForAnalysis"])
            self.assertIn(expected, report["codes"])

    def test_successful_geometry_and_screenshot_do_not_compensate_for_notice_only_design_context(self):
        calls = tools("Missing Code Connect mappings. Please ask the user. Would you like to connect components?")
        report = assess_mcp_acquisition_readiness(calls, file_key="FixtureFile", node_id="1:2", metadata_context=metadata())
        self.assertEqual(report["metadata"]["geometryObserved"], 2)
        self.assertFalse(report["readyForAnalysis"])
        self.assertIn("ACQUISITION_DESIGN_CONTEXT_RETRY_REQUIRED", report["codes"])
        self.assertEqual(report["designContexts"][0]["implementationBlockCount"], 0)

    def test_live_validator_and_historical_cache_both_reject_provider_only_guidance(self):
        notice = "Missing Code Connect mappings. Please ask the user. Would you like to connect components?"
        with self.assertRaises(SessionError) as live:
            validate_design_context_output(output(notice))
        with self.assertRaises(SessionError) as cached:
            SnapshotStore.response({"toolEvidence": tools(notice), "collectedAtEpochSeconds": 0}, "a" * 64, reused=True)
        self.assertEqual(live.exception.code, "FIGMA_MCP_EVIDENCE_INCOMPLETE")
        self.assertEqual(cached.exception.code, "FIGMA_MCP_EVIDENCE_INCOMPLETE")
        actual = SnapshotStore.response({"toolEvidence": tools(source()), "collectedAtEpochSeconds": 0}, "a" * 64, reused=True)
        self.assertEqual(actual["coverage"]["status"], "PARTIAL")
        self.assertEqual(actual["coverage"]["designContextReadiness"]["status"], "CONTEXT_OBSERVED")
        self.assertEqual(actual["coverage"]["designContextReadiness"]["authoredCompleteness"], "NOT_PROVEN")

    def test_notice_only_collection_is_rejected_before_a_snapshot_or_index_is_published(self):
        scope = ConnectionScope(tenantId="00000000-0000-0000-0000-000000000001",
                                workspaceId="00000000-0000-0000-0000-000000000002",
                                actorUserId="00000000-0000-0000-0000-000000000003")
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(Path(directory), scope, "synthetic-job", "FixtureFile", "1:2")
            with self.assertRaises(SessionError):
                store.save({"toolEvidence": tools("Missing Code Connect. Please ask the user. Would you like to connect?"),
                            "rawTranscript": []})
            self.assertFalse(store.root.exists())

    def test_hidden_metadata_node_does_not_make_up_geometry_and_raw_evidence_is_unchanged(self):
        calls = tools(source())
        context = metadata()
        context["nodes"][1]["visible"] = False
        del context["nodes"][1]["metadataBounds"]
        before = copy.deepcopy((calls, context))
        report = assess_mcp_acquisition_readiness(calls, file_key="FixtureFile", node_id="1:2", metadata_context=context)
        self.assertTrue(report["readyForAnalysis"])
        self.assertEqual(report["metadata"]["geometryObserved"], 1)
        self.assertEqual((calls, context), before)

    def test_structured_node_geometry_is_observed_but_missing_geometry_is_not_filled(self):
        actual = {"id": "1:2", "type": "FRAME", "absoluteBoundingBox": {"x": 0, "y": 0, "width": 100, "height": 100}}
        report = assess_design_context_output(output(json.dumps(actual)))
        self.assertEqual(report["status"], "CONTEXT_OBSERVED")
        del actual["absoluteBoundingBox"]["width"]
        report = assess_design_context_output(output(json.dumps(actual)))
        self.assertEqual(report["status"], "RETRY_REQUIRED")
        self.assertEqual(report["geometryEvidence"], "NOT_PROVIDED")

    def test_text_screenshot_and_zero_root_geometry_do_not_pass_source_readiness(self):
        calls = tools(source())
        calls[2]["output"] = output("The screenshot is available at a private URL")
        report = assess_mcp_acquisition_readiness(calls, file_key="FixtureFile", node_id="1:2", metadata_context=metadata())
        self.assertFalse(report["readyForAnalysis"])
        self.assertIn("ACQUISITION_SCREENSHOT_PNG_UNVERIFIED", report["codes"])
        context = metadata()
        context["nodes"][0]["metadataBounds"]["width"] = 0
        report = assess_mcp_acquisition_readiness(tools(source()), file_key="FixtureFile", node_id="1:2", metadata_context=context)
        self.assertFalse(report["readyForAnalysis"])
        self.assertIn("ACQUISITION_METADATA_ROOT_GEOMETRY_INVALID", report["codes"])


if __name__ == "__main__":
    unittest.main()

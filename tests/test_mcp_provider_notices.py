"""Provider-only acquisition notices are not authored design context."""

import unittest

from sfdc_agent.kiro_headless import SessionError
from sfdc_agent.kiro_mcp import FigmaToolPolicy
from sfdc_agent.managed_figma_mcp import SnapshotStore


PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAXpeqz8AAAAASUVORK5CYII="


def collect(design_text):
    policy = FigmaToolPolicy("figma_fixture", "FixtureFile", "1:2")
    inputs = {"fileKey": "FixtureFile", "nodeId": "1:2"}
    outputs = {
        "get_metadata": {"content": [{"type": "text", "text": '<frame id="1:2" width="1" height="1"/>'}]},
        "get_design_context": {"content": [{"type": "text", "text": design_text}]},
        "get_screenshot": {"content": [{"type": "image", "mimeType": "image/png", "data": PNG}]},
    }
    for tool, output in outputs.items():
        policy.update({"sessionUpdate": "tool_call", "toolCallId": tool, "rawInput": inputs})
        policy.permission({"toolCall": {"toolCallId": tool, "rawInput": inputs},
            "options": [{"kind": "allow_once", "optionId": "yes"}],
            "_meta": {"kiro": {"mcpTool": {"version": 1,
                "identity": {"serverName": "figma_fixture", "toolName": tool}}}}})
        policy.update({"sessionUpdate": "tool_call_update", "toolCallId": tool,
                       "status": "completed", "rawOutput": output})
    return policy.evidence()


class McpProviderNoticeTest(unittest.TestCase):
    def test_provider_only_truncation_or_size_notices_cannot_complete_acquisition(self):
        notices = [
            "The response is too large and was truncated. Retrieve child nodes with get_metadata.",
            "The output is too long. Please call get_metadata to discover child nodes, then get_design_context for each child.",
            "The response was truncated due to the output limit. Call get_design_context on individual child nodes.",
            "The node is too large to return the full design context. Use get_metadata and get_design_context on smaller child nodes.",
            "Error: The response is incomplete because it exceeded the output limit. Please retry with a smaller node.",
        ]
        for notice in notices:
            with self.subTest(notice=notice):
                with self.assertRaises(SessionError) as error:
                    collect(notice)
                self.assertEqual(error.exception.code, "FIGMA_MCP_EVIDENCE_INCOMPLETE")

    def test_authored_code_and_visible_copy_containing_notice_words_remain_verbatim(self):
        contexts = [
            'const message = "The response was truncated due to the output limit.";',
            '<div className="truncated">The response is too large and was truncated.</div>',
            '```tsx\nexport const text = "The output is too long. Please call get_metadata.";\n```',
            '{"visibleText":"The response was truncated due to the output limit.","nodeId":"1:2"}',
            'The response was truncated.',
            'const truncated = false; const outputLimit = 120; export default function Screen() { return null; }',
        ]
        for context in contexts:
            with self.subTest(context=context):
                evidence = collect(context)
                actual = next(call for call in evidence if call["name"] == "get_design_context")
                self.assertEqual(actual["output"]["content"][0]["text"], context)

    def test_historical_cached_notice_is_not_reused_as_successful_source(self):
        tools = collect('const label = "Source text";')
        context = next(call for call in tools if call["name"] == "get_design_context")
        context["output"]["content"][0]["text"] = "The response was truncated due to the output limit."
        with self.assertRaises(SessionError) as error:
            SnapshotStore.response({"toolEvidence": tools, "collectedAtEpochSeconds": 0}, "a" * 64, reused=True)
        self.assertEqual(error.exception.code, "FIGMA_MCP_EVIDENCE_INCOMPLETE")

    def test_no_recognized_truncation_does_not_claim_complete_authored_evidence(self):
        response = SnapshotStore.response({"toolEvidence": collect('const label = "Source text";'),
                                           "collectedAtEpochSeconds": 0}, "a" * 64, reused=True)
        coverage = response["coverage"]
        self.assertEqual(coverage["status"], "PARTIAL")
        self.assertEqual(coverage["truncation"], {"status": "NOT_DETECTED",
                         "scope": "recognized-provider-notices-and-output-flags", "authoredCompleteness": "NOT_PROVEN"})
        self.assertNotIn("truncationDetected", coverage)
        self.assertIn("omissionsWithoutProviderMarkers", coverage["unknown"])


if __name__ == "__main__":
    unittest.main()

"""Same-prompt metadata delivery; all processes and providers are mocked."""

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from sfdc_agent.failed_mcp_evidence import safe_source_read_barrier
from sfdc_agent.kiro_headless import SessionError
from sfdc_agent.kiro_mcp import KiroMcpReader, _tool_content
from sfdc_agent.managed_figma_mcp import Collection

MODULE = "sfdc_agent.kiro_mcp"
SESSION = "PRIVATE_FIXTURE_SESSION"
XML = '<frame id="1:2" width="200" height="100"><text id="1:3" name="fixture"/></frame>'
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAXpeqz8AAAAASUVORK5CYII="


class MetadataBarrierTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / ".kiro/sessions/project" / SESSION / "tool-outputs/source.txt"
        self.path.parent.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def reader(self):
        reader = KiroMcpReader(home=self.root, work=self.root, server="figma_fixture", file_key="FixtureFile",
                               node_id="1:2", key="PRIVATE_FIXTURE_KEY")
        reader.session_id = reader.policy.session_id = SESSION
        reader.collection_stage = "metadata"
        return reader

    def approve(self, reader, name="get_metadata", call_id="m", extra=None):
        inputs = {"fileKey": "FixtureFile", "nodeId": "1:2", **(extra or {})}
        permission = {"toolCall": {"toolCallId": call_id, "rawInput": inputs},
            "options": [{"kind": "allow_once", "optionId": "yes"}],
            "_meta": {"kiro": {"mcpTool": {"version": 1, "identity": {"serverName": "figma_fixture", "toolName": name}}}}}
        self.assertEqual(reader.policy.permission(permission)["outcome"]["optionId"], "yes")
        reader.policy.update({"sessionUpdate": "tool_call", "toolCallId": call_id, "rawInput": inputs})

    def event(self, *, path=None, call_id="m", output=None):
        return {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "status": "completed",
            "rawOutput": output or {"response": "excerpt", "imageBase64Urls": []},
            "_meta": {"kiro": {"outputTransformation": {"kind": "offloaded", "absFilePath": str(path or self.path),
                "totalChars": len(XML.encode("utf-16-le")) // 2}}}}

    async def dispatch(self, reader, event):
        message = {"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": SESSION, "update": event}}
        reader._last_wire = json.dumps(message).encode()
        await reader._dispatch(message)

    async def pending(self, reader=None):
        reader = reader or self.reader()
        self.approve(reader)
        await self.dispatch(reader, self.event())
        self.assertIsNotNone(reader._pending_offload)
        return reader

    async def test_inline_metadata_keeps_immediate_verified_scope_without_barrier(self):
        reader = self.reader()
        self.approve(reader)
        await self.dispatch(reader, {"sessionUpdate": "tool_call_update", "toolCallId": "m", "status": "completed",
            "rawOutput": {"content": [{"type": "text", "text": XML}]}})
        self.assertIn("1:3", reader.policy.nodes)
        self.assertFalse(reader._metadata_barrier_used)
        self.assertIsNone(reader._pending_offload)
        report, _ = reader.failed_evidence(SessionError("FIGMA_MCP_EVIDENCE_INCOMPLETE", "fixture"))
        self.assertNotIn("sourceReadBarrier", report)
        with self.assertRaises(ValidationError):
            Collection(fileKey="FixtureFile", nodeId="1:2", sourceReadBarrier=True)

    async def test_late_materialization_validates_once_after_end_turn_without_early_scope(self):
        with patch(MODULE + "._tool_content", wraps=_tool_content) as content:
            reader = await self.pending()
            self.assertEqual(content.call_count, 1)
            self.assertEqual(reader.policy.nodes, {"1:2"})
            await self.dispatch(reader, self.event())  # Exact duplicate completion.
            self.assertEqual(content.call_count, 1)
            self.path.write_text(XML)
            with self.assertRaises(SessionError) as caught:
                reader.policy.evidence()
            self.assertEqual(caught.exception.evidence_reason, "TOOL_OFFLOAD_BARRIER_PENDING")
            self.assertEqual(content.call_count, 1)
            reader._send = AsyncMock()
            await reader._dispatch({"method": "session/request_permission", "id": 7, "params": {
                "sessionId": SESSION, "toolCall": {"toolCallId": "child", "rawInput": {"fileKey": "FixtureFile", "nodeId": "1:3"}},
                "options": [{"kind": "allow_once", "optionId": "yes"}, {"kind": "reject_once", "optionId": "no"}]}})
            self.assertNotIn("child", reader.policy.approvals)
            self.assertEqual(reader._metadata_barrier_receipt["deniedPermissionCount"], 1)
            reader._finish_offload_barrier({"stopReason": "end_turn"})
            self.assertEqual(content.call_count, 2)
            self.approve(reader, call_id="second")
            with self.assertRaises(SessionError):
                await self.dispatch(reader, self.event(path=self.path.parent / "second.txt", call_id="second"))
            self.assertIsNone(reader._pending_offload)
            self.assertEqual(reader._metadata_barrier_receipt["barrierAttempts"], 1)
            self.assertEqual(reader.policy.calls["m"]["_normalizedOutput"]["content"][0]["text"], XML)
            self.assertIn("1:3", reader.policy.nodes)
            self.assertEqual(reader.policy.pending_offloads, set())
            self.assertEqual(reader._metadata_barrier_receipt["outcome"], "VERIFIED_AT_BARRIER")
            reader._finish_offload_barrier({"stopReason": "end_turn"})
            self.assertEqual(content.call_count, 3)  # Second call's initial check, no second barrier.

    async def test_permanent_absence_stays_failed_with_one_barrier_and_safe_receipt(self):
        with patch(MODULE + "._tool_content", wraps=_tool_content) as content:
            reader = await self.pending()
            with self.assertRaises(SessionError) as caught:
                reader._finish_offload_barrier({"stopReason": "end_turn"})
            self.assertEqual(caught.exception.offload_assessment["osErrorCode"], "ENOENT")
            report, _ = reader.failed_evidence(caught.exception)
            self.assertEqual(report["sourceReadBarrier"]["outcome"], "REJECTED_AT_BARRIER")
            self.assertEqual(report["sourceReadBarrier"]["barrierAttempts"], 1)
            self.assertFalse(report["snapshotRegistered"])
            self.assertEqual(content.call_count, 2)
            with self.assertRaises(SessionError):
                reader._finish_offload_barrier({"stopReason": "end_turn"})
            self.assertEqual(content.call_count, 2)
            for private in (str(self.root), SESSION, XML, "PRIVATE_FIXTURE_KEY"):
                self.assertNotIn(private, json.dumps(report))

    async def test_frozen_source_scope_input_output_and_transformation_cannot_change(self):
        changes = [lambda r: r.policy.calls["m"]["rawInput"].update(nodeId="9:9"),
                   lambda r: r.policy.calls["m"]["rawOutput"].update(response="changed source"),
                   lambda r: r.policy.calls["m"]["_meta"]["kiro"]["outputTransformation"].update(totalChars=2),
                   lambda r: setattr(r, "session_id", "different-session"),
                   lambda r: setattr(r.policy, "file_key", "OtherFile"),
                   lambda r: r.policy.nodes.add("1:99"),
                   lambda r: setattr(r, "_counter", r._counter+1)]
        for change in changes:
            reader = await self.pending()
            change(reader)
            with patch(MODULE + "._tool_content", side_effect=AssertionError("changed source must not be read")):
                with self.assertRaises(SessionError) as caught:
                    reader._finish_offload_barrier({"stopReason": "end_turn"})
            self.assertEqual(caught.exception.evidence_reason, "TOOL_OFFLOAD_BARRIER_CHANGED")
            self.assertNotIn("_normalizedOutput", reader.policy.calls["m"])
        reader = await self.pending()
        event = self.event(); event["rawOutput"]["response"] = "changed event"
        with self.assertRaises(SessionError) as caught:
            await self.dispatch(reader, event)
        self.assertEqual(caught.exception.evidence_reason, "TOOL_OFFLOAD_BARRIER_CHANGED")

    async def test_only_approved_metadata_enoent_and_same_prompt_end_turn_are_eligible(self):
        for stage, name, path, failed_auth in [("design-context", "get_metadata", self.path, False),
                ("metadata", "get_design_context", self.path, False),
                ("metadata", "get_metadata", self.root / "outside.txt", False),
                ("metadata", "get_metadata", self.path, True)]:
            reader = self.reader(); reader.collection_stage = stage; reader.failed_authorization = failed_auth
            self.approve(reader, name=name)
            with self.assertRaises(SessionError):
                await self.dispatch(reader, self.event(path=path))
            self.assertIsNone(reader._pending_offload)
        reader = await self.pending()
        with patch(MODULE + "._tool_content", side_effect=AssertionError("unfinished prompt must not read source")):
            with self.assertRaises(SessionError):
                reader._finish_offload_barrier({"stopReason": "cancelled"})
        self.assertEqual(reader._metadata_barrier_receipt["barrierAttempts"], 0)

    async def test_all_original_file_guards_still_run_at_barrier(self):
        for kind in ("count", "utf8", "symlink", "hardlink", "oversized"):
            with self.subTest(kind=kind):
                self.path.unlink(missing_ok=True)
                reader = await self.pending()
                extra = self.path.parent / "second.txt"; extra.unlink(missing_ok=True)
                if kind == "count": self.path.write_text("different length")
                elif kind == "utf8": self.path.write_bytes(b"\xff")
                elif kind == "symlink": extra.write_text(XML); self.path.symlink_to(extra)
                elif kind == "hardlink": self.path.write_text(XML); os.link(self.path, extra)
                else:
                    with self.path.open("wb") as stream: stream.truncate(8*1024*1024+1)
                with self.assertRaises(SessionError) as caught:
                    reader._finish_offload_barrier({"stopReason": "end_turn"})
                self.assertEqual(caught.exception.evidence_reason, "TOOL_OFFLOAD_UNVERIFIED")
                self.assertEqual(reader._metadata_barrier_receipt["outcome"], "REJECTED_AT_BARRIER")
                self.assertIn("m", reader.policy.pending_offloads)

    async def test_private_boundary_receipt_has_no_paths_sessions_or_capability_envelope(self):
        value = safe_source_read_barrier({"outcome": "VERIFIED_AT_BARRIER", "barrierAttempts": 1,
            "path": str(self.path), "session": SESSION, "token": "SECRET",
            "extensionMethods": ["_kiro/fixture/readOutput"], "unknown": "SECRET"})
        self.assertEqual(value, {"schemaVersion": 1, "outcome": "VERIFIED_AT_BARRIER", "barrierAttempts": 1})
        self.assertNotIn("SECRET", json.dumps(value))
        self.assertNotIn(SESSION, json.dumps(value))
        self.assertNotIn(str(self.root), json.dumps(value))

    async def test_collect_waits_same_turn_and_sends_three_original_prompts_only(self):
        reader = self.reader(); reader.connected = True
        observed = []

        async def rpc(method, params):
            observed.append(method)
            if method == "initialize":
                return {"agentCapabilities": {"_meta": {"kiro": {"extensionMethods": ["_kiro/fixture/readOutput"]}}}}
            if method == "session/new": return {"sessionId": SESSION}
            stage = reader.collection_stage
            self.approve(reader, name={"metadata": "get_metadata", "design-context": "get_design_context", "capture": "get_screenshot"}[stage], call_id=stage)
            if stage == "metadata":
                await self.dispatch(reader, self.event(call_id=stage))
                self.assertIsNotNone(reader._pending_offload)
                self.assertEqual(reader.policy.nodes, {"1:2"})
                self.path.write_text(XML)  # Provider materializes only after completion update.
            else:
                output = {"content": [{"type": "text", "text": '<div style="width:200px">fixture</div>'}]} if stage == "design-context" else {
                    "content": [{"type": "image", "mimeType": "image/png", "data": PNG}]}
                await self.dispatch(reader, {"sessionUpdate": "tool_call_update", "toolCallId": stage, "status": "completed", "rawOutput": output})
            return {"stopReason": "end_turn"}

        reader._rpc = AsyncMock(side_effect=rpc)
        process = SimpleNamespace(returncode=0)
        with patch.dict(os.environ, {"NOVA_KIRO_CLI": "/fixture/never-executed"}), \
             patch(MODULE + ".asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)) as spawn, \
             patch(MODULE + ".OwnedProcesses", return_value=SimpleNamespace()), \
             patch(MODULE + "._terminate", new=AsyncMock()), \
             patch(MODULE + ".cli_identity", return_value={"version": "offline-fixture"}):
            result = await reader.collect(timeout=180)
        self.assertEqual(observed, ["initialize", "session/new", "session/prompt", "session/prompt", "session/prompt"])
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(len(result["toolEvidence"]), 3)
        self.assertEqual(result["sourceReadBarrier"]["outcome"], "VERIFIED_AT_BARRIER")
        self.assertEqual(result["sourceReadBarrier"]["barrierAttempts"], 1)
        self.assertNotIn("extensionMethods", result["sourceReadBarrier"])
        self.assertEqual(result["toolEvidence"][0]["output"]["content"][0]["text"], XML)

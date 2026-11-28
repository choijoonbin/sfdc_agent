"""Offline observability regressions; no CLI/provider/network is executed."""

import asyncio
import base64
import copy
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import tempfile
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from sfdc_agent.failed_mcp_evidence import build_failed_evidence, write_private_object
from sfdc_agent.figma_mcp_connections import ConnectionScope
from sfdc_agent.kiro_headless import SessionError
from sfdc_agent.kiro_mcp import KiroMcpReader, _tool_content
from sfdc_agent.managed_figma_mcp import SnapshotStore, collect


SCOPE = ConnectionScope(tenantId="00000000-0000-0000-0000-000000000001",
                        workspaceId="00000000-0000-0000-0000-000000000002",
                        actorUserId="00000000-0000-0000-0000-000000000003")
JOB = "00000000-0000-0000-0000-000000000004"
SESSION = "DO_NOT_WRITE_PROVIDER_SESSION"
MODULE = "sfdc_agent.managed_figma_mcp"


def wire(reader, method, params, *, text=None):
    body = (text or json.dumps({"jsonrpc": "2.0", "method": method, "params": params}, ensure_ascii=False)).encode()
    reader.wire_transcript.append({"sha256": sha256(body).hexdigest(), "jsonlBase64": base64.b64encode(body).decode()})


def approved(reader, name="get_design_context", inputs=None, call_id="read"):
    inputs = inputs or {"fileKey": "FixtureFile", "nodeId": "1:2"}
    permission = {"sessionId": SESSION, "toolCall": {"toolCallId": call_id, "rawInput": inputs},
                  "options": [{"kind": "allow_once", "optionId": "yes"}],
                  "_meta": {"kiro": {"mcpTool": {"version": 1, "identity": {
                      "serverName": "figma_fixture", "toolName": name}}}}}
    assert reader.policy.permission(permission)["outcome"]["optionId"] == "yes"
    wire(reader, "session/request_permission", permission)
    reader.policy.update({"sessionUpdate": "tool_call", "toolCallId": call_id, "rawInput": inputs})
    return inputs


def finish(reader, output, *, status="completed", call_id="read", text=None):
    update = {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "status": status, "rawOutput": output}
    wire(reader, "session/update", {"sessionId": SESSION, "update": update}, text=text)
    try:
        reader.policy.update(update)
    except SessionError as error:
        return error
    return SessionError("FIGMA_MCP_EVIDENCE_INCOMPLETE", "fixture failure after partial collection")


class FailedMcpEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.reader = KiroMcpReader(home=self.root, work=self.root, server="figma_fixture", file_key="FixtureFile",
                                    node_id="1:2", key="DO_NOT_WRITE_KIRO_KEY")
        self.reader.session_id = self.reader.policy.session_id = SESSION
        self.reader.collection_stage = "design-context"

    def tearDown(self):
        self.temp.cleanup()

    def test_notice_keeps_exact_json_value_bytes_and_safe_assessment_without_wire(self):
        approved(self.reader)
        output = {"content": [{"type": "text", "text": "The response is too large. Call get_metadata for smaller nodes. 원본"}]}
        raw = '{ "content" : [{"type":"text", "text":"The response is too large. Call get_metadata for smaller nodes. \\uc6d0\\ubcf8"}] }'
        text = ('{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"' + SESSION
                + '","update":{"sessionUpdate":"tool_call_update","toolCallId":"read","status":"completed","rawOutput":'
                + raw + ',"_meta":{"privateSession":"DO_NOT_WRITE_META"}}}}')
        error = finish(self.reader, output, text=text)
        self.assertEqual(error.code, "FIGMA_MCP_EVIDENCE_INCOMPLETE")
        before = copy.deepcopy(self.reader.policy.calls)
        report, objects = self.reader.failed_evidence(error)
        self.assertEqual(self.reader.policy.calls, before)
        item = report["toolEvidence"][0]
        self.assertIn("DESIGN_CONTEXT_PROVIDER_INCOMPLETE", item["assessment"]["codes"])
        self.assertEqual(report["reasonCode"], "DESIGN_CONTEXT_RETRY_REQUIRED")
        self.assertEqual(objects[item["output"]["sha256"]], raw.encode())
        self.assertEqual(item["output"]["fidelity"], "EXACT_WIRE_JSON_VALUE_UTF8")
        public = json.dumps(report).encode() + b"".join(objects.values())
        for private in [SESSION, "DO_NOT_WRITE_META", "DO_NOT_WRITE_KIRO_KEY", "session/update", "jsonlBase64"]:
            self.assertNotIn(private.encode(), public)
        self.assertFalse(report["resumable"])
        self.assertFalse(report["snapshotRegistered"])
        self.assertFalse(report["jobTerminalStatusObserved"])
        with self.assertRaises(SessionError):
            self.reader.policy.evidence()

    def test_declared_truncation_and_failed_limit_are_evidence_not_success(self):
        for status, output, expected in [
            ("completed", {"content": [{"type": "text", "text": '<div style="width:20px">source</div>'}], "truncated": True}, "FIGMA_MCP_EVIDENCE_INCOMPLETE"),
            ("failed", {"response": "You've reached the Figma MCP tool call limit for your View seat."}, "FIGMA_MCP_RATE_LIMITED"),
        ]:
            with self.subTest(status=status):
                self.reader.policy.calls.clear(); self.reader.policy.approvals.clear(); self.reader.wire_transcript.clear()
                approved(self.reader)
                error = finish(self.reader, output, status=status)
                self.assertEqual(error.code, expected)
                report, objects = self.reader.failed_evidence(error)
                item = report["toolEvidence"][0]
                self.assertEqual(json.loads(objects[item["output"]["sha256"]]), output)
                self.assertEqual(report["outcome"], "TERMINAL_FAILURE")
                with self.assertRaises(SessionError):
                    self.reader.policy.evidence()

    def test_malformed_known_payload_preserved_but_unknown_envelope_and_invalid_wire_omitted(self):
        approved(self.reader)
        error = finish(self.reader, {"content": "malformed-known-payload"})
        report, objects = self.reader.failed_evidence(error)
        item = report["toolEvidence"][0]
        self.assertEqual(item["output"]["status"], "PRESERVED")
        self.assertIn("DESIGN_CONTEXT_FORMAT_UNVERIFIED", item["assessment"]["codes"])
        self.reader.policy.calls["read"]["rawOutput"] = {"unknownServerEnvelope": "DO_NOT_WRITE_UNKNOWN"}
        report, objects = self.reader.failed_evidence(error)
        self.assertEqual(report["toolEvidence"][0]["output"]["code"], "OUTPUT_ENVELOPE_UNVERIFIED")
        self.assertNotIn(b"DO_NOT_WRITE_UNKNOWN", b"".join(objects.values()))
        self.reader.policy.calls["read"]["rawOutput"] = {"content": "malformed-known-payload"}
        self.reader.wire_transcript.clear()
        wire(self.reader, None, None, text='{"params":{"update":{}},"params":{"private":"DO_NOT_WRITE_DUPLICATE"}}')
        report, objects = self.reader.failed_evidence(error)
        self.assertEqual(report["toolEvidence"][0]["output"]["code"], "WIRE_VALUE_UNAVAILABLE")
        self.assertEqual(objects, {})

    def test_auth_secrets_and_unknown_inputs_are_omitted_in_full(self):
        outputs = [
            {"content": [{"type": "text", "text": "OAuth authorization needed https://auth.invalid/oauth?code=DO_NOT_WRITE"}]},
            {"content": [{"type": "text", "text": "Bearer DO_NOT_WRITE"}]},
            {"content": [], "structuredContent": {"headers": {"Authorization": "DO_NOT_WRITE"}}},
            {"response": '{"access_token": "DO_NOT_WRITE"}', "imageBase64Urls": []},
        ]
        for output in outputs:
            with self.subTest(output=output):
                self.reader.policy.calls.clear(); self.reader.policy.approvals.clear(); self.reader.wire_transcript.clear()
                approved(self.reader)
                error = finish(self.reader, output)
                report, objects = self.reader.failed_evidence(error)
                self.assertEqual(report["toolEvidence"][0]["output"]["code"], "AUTH_OR_UNKNOWN_INPUT_ENVELOPE")
                self.assertEqual(objects, {})
                self.assertNotIn("DO_NOT_WRITE", json.dumps(report))
        self.reader.policy.calls.clear(); self.reader.policy.approvals.clear(); self.reader.wire_transcript.clear()
        approved(self.reader, inputs={"fileKey": "FixtureFile", "nodeId": "1:2", "unrecognized": "DO_NOT_WRITE"})
        report, objects = self.reader.failed_evidence(finish(self.reader, {"content": []}))
        self.assertEqual(objects, {})

    def test_unapproved_changed_and_foreign_session_values_never_become_evidence(self):
        approved(self.reader)
        self.reader.policy.calls["read"]["rawInput"]["nodeId"] = "9:9"
        error = finish(self.reader, {"content": [{"type": "text", "text": "DO_NOT_WRITE_CHANGED"}]})
        report, objects = self.reader.failed_evidence(error)
        self.assertEqual(report["toolEvidence"], [])
        self.assertEqual(objects, {})
        self.reader.policy.calls.clear(); self.reader.policy.approvals.clear(); self.reader.wire_transcript.clear()
        approved(self.reader)
        output = {"content": [{"type": "text", "text": "source"}]}
        self.reader.policy.calls["read"].update(status="completed", rawOutput=output)
        wire(self.reader, "session/update", {"sessionId": "foreign", "update": {
            "sessionUpdate": "tool_call_update", "toolCallId": "read", "rawOutput": output}})
        self.reader.policy.calls["unapproved"] = {"rawOutput": {"content": [{"text": "DO_NOT_WRITE_UNAPPROVED"}]}}
        report, objects = self.reader.failed_evidence(error)
        self.assertEqual(report["toolEvidence"][0]["output"]["code"], "WIRE_VALUE_UNAVAILABLE")
        self.assertNotIn(b"source", b"".join(objects.values()))

    def test_verified_offload_text_retained_without_session_path_and_mixed_auth_omitted(self):
        approved(self.reader)
        path = self.root / ".kiro/sessions/hash" / SESSION / "tool-outputs/context.txt"
        path.parent.mkdir(parents=True)
        source = 'The response is too large. Call get_metadata for smaller nodes. 원본 😀'
        path.write_bytes(source.encode())
        output = {"response": "excerpt", "imageBase64Urls": []}
        call = {"rawOutput": output, "_meta": {"kiro": {"outputTransformation": {"kind": "offloaded",
            "absFilePath": str(path), "totalChars": len(source.encode("utf-16-le")) // 2}}}}
        normalized = _tool_content(call, home=self.root, session_id=SESSION)
        error = finish(self.reader, output)
        self.reader.policy.calls["read"]["_normalizedOutput"] = normalized
        report, objects = self.reader.failed_evidence(error)
        item = report["toolEvidence"][0]
        self.assertEqual(objects[item["recoveredTextBlocks"][0]["sha256"]], source.encode())
        self.assertNotIn(str(path).encode(), b"".join(objects.values()))
        self.reader.policy.calls["read"]["_normalizedOutput"]["content"][0]["text"] = "Bearer DO_NOT_WRITE_OFFLOAD"
        report, objects = self.reader.failed_evidence(error)
        self.assertEqual(objects, {})

    def test_private_store_owns_job_and_never_registers_failed_snapshot(self):
        approved(self.reader)
        error = finish(self.reader, {"content": [{"type": "text", "text": "The output is truncated. Call get_metadata for smaller nodes."}]})
        store = SnapshotStore(self.root, SCOPE, JOB, "FixtureFile", "1:2")
        digest = store.save_failure(self.reader, error)
        receipt = self.root / "failed-evidence" / (digest + ".failure.json")
        value = json.loads(receipt.read_bytes())
        self.assertEqual(sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), digest)
        self.assertEqual(value["identity"]["jobId"], JOB)
        self.assertEqual(value["identity"]["connectionRef"], store.identity["connectionRef"])
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        self.assertEqual(receipt.parent.stat().st_mode & 0o777, 0o700)
        for item in (receipt.parent / "objects").iterdir():
            self.assertEqual(item.stat().st_mode & 0o777, 0o600)
            self.assertEqual(sha256(item.read_bytes()).hexdigest() + ".bin", item.name)
        self.assertIsNone(store.load())
        with self.assertRaises(SessionError):
            store.load(digest)
        self.assertFalse(store.root.exists())

    def test_first_failure_is_frozen_before_later_cleanup_tool_updates(self):
        approved(self.reader)
        output = {"content": [{"type": "text", "text": "The output is incomplete. Call get_metadata for smaller nodes."}]}
        error = finish(self.reader, output)
        self.reader._capture_failure(error)
        first, objects = self.reader.failed_evidence(error)
        self.reader.policy.calls["read"]["rawOutput"] = {"content": [{"type": "text", "text": "later cleanup value"}]}
        self.reader.collection_stage = "capture"
        retained, retained_objects = self.reader.failed_evidence(SessionError("FIGMA_MCP_TIMEOUT", "later failure"))
        self.assertEqual(retained, first)
        self.assertEqual(retained_objects, objects)
        self.assertEqual(retained["stage"], "design-context")
        self.assertEqual(retained["failureCode"], error.code)
        self.assertEqual(json.loads(objects[first["toolEvidence"][0]["output"]["sha256"]]), output)

    def test_whole_objects_omitted_at_bound_and_insecure_cas_rejected(self):
        approved(self.reader)
        output = {"content": [{"type": "text", "text": '<div style="width:20px">' + "X" * 2400 + '</div>'}]}
        error = finish(self.reader, output)
        with patch("sfdc_agent.failed_mcp_evidence.MAX_FAILED_BYTES", 4096):
            report, objects = self.reader.failed_evidence(error)
        item = report["toolEvidence"][0]
        self.assertEqual(json.loads(objects[item["output"]["sha256"]]), output)
        self.assertEqual(item["recoveredTextBlocks"][0]["code"], "FAILED_EVIDENCE_BYTE_LIMIT")
        self.assertLessEqual(report["preservedBytes"], 4096)
        body = b"exact-bytes"
        digest = sha256(body).hexdigest()
        directory = self.root / "cas"
        write_private_object(directory, digest, body)
        path = directory / (digest + ".bin")
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            write_private_object(directory, digest, body)
        path.unlink()
        path.symlink_to(self.root / "outside")
        with self.assertRaises(OSError):
            write_private_object(directory, digest, body)

    def test_managed_failure_hook_keeps_original_response_even_when_diagnostic_write_fails(self):
        approved(self.reader)
        error = finish(self.reader, {"content": [{"type": "text", "text": "The output is incomplete. Call get_metadata for smaller nodes."}]})
        self.reader.collect = AsyncMock(side_effect=error)
        context = SimpleNamespace(job_id=JOB, alive=lambda: True, purpose="screen", mode="headless", remote=True,
            file_key="FixtureFile", node_id="1:2", actor_user_id=str(SCOPE.actorUserId),
            execution_scope=(str(SCOPE.tenantId), str(SCOPE.workspaceId)), expires=monotonic()+60, sessions=set())
        context.bind = context.sessions.add

        async def run():
            async def receive():
                return {"type": "http.request", "body": b'{"fileKey":"FixtureFile","nodeId":"1:2"}', "more_body": False}
            request = Request({"type": "http", "headers": [(b"x-sfdc-agent", b"1")]}, receive)
            with patch(MODULE + ".managed_execution_context", return_value=context), \
                 patch(MODULE + ".read_connection", return_value=(self.root, {})), \
                 patch(MODULE + ".prepare_home", return_value=(self.root, self.root, self.root, "figma_fixture")), \
                 patch(MODULE + ".current_managed_release", return_value=SimpleNamespace(templates={}, id="pinned-prompt")), \
                 patch(MODULE + ".read_key", return_value="DO_NOT_WRITE_KIRO_KEY"), \
                 patch(MODULE + ".KiroMcpReader", return_value=self.reader):
                return await collect(request)

        response = asyncio.run(run())
        self.assertEqual(response.status_code, 502)
        self.assertEqual(json.loads(response.body)["error"]["code"], "FIGMA_MCP_EVIDENCE_INCOMPLETE")
        self.assertEqual(len(list((self.root / "failed-evidence").glob("*.failure.json"))), 1)
        self.assertFalse((self.root / "snapshots").exists())
        with patch.object(SnapshotStore, "save_failure", side_effect=OSError("DO_NOT_WRITE_ERROR")), \
             self.assertLogs(MODULE, level="WARNING") as logs:
            response = asyncio.run(run())
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("DO_NOT_WRITE", "".join(logs.output))
        self.assertEqual(context.sessions, set())

    def test_unexpected_diagnostic_bug_cannot_mask_first_failure_or_recapture_later_values(self):
        error = SessionError("FIGMA_MCP_EVIDENCE_INCOMPLETE", "original failure")
        with patch("sfdc_agent.failed_mcp_evidence.build_failed_evidence", side_effect=RuntimeError("DO_NOT_WRITE_BUG")), \
             self.assertLogs("sfdc_agent.kiro_mcp", level="WARNING") as logs:
            self.reader._capture_failure(error)
        self.assertNotIn("DO_NOT_WRITE", "".join(logs.output))
        with self.assertRaises(ValueError):
            self.reader.failed_evidence(error)


class OffloadFailureAssessmentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / ".kiro/sessions/project-hash" / SESSION / "tool-outputs/source.txt"
        self.path.parent.mkdir(parents=True)
        self.source = "원본 😀"
        self.path.write_bytes(self.source.encode())

    def tearDown(self):
        self.temp.cleanup()

    def call(self, path=None, total=None):
        return {"rawOutput": {"response": "excerpt", "imageBase64Urls": []},
                "_meta": {"kiro": {"outputTransformation": {"kind": "offloaded", "absFilePath": str(path or self.path),
                    "totalChars": total if total is not None else len(self.source.encode("utf-16-le")) // 2}}}}

    def failure(self, call, session=SESSION):
        with self.assertRaises(SessionError) as caught:
            _tool_content(call, home=self.root, session_id=session)
        self.assertEqual(caught.exception.code, "FIGMA_MCP_EVIDENCE_INCOMPLETE")
        self.assertEqual(caught.exception.evidence_reason, "TOOL_OFFLOAD_UNVERIFIED")
        value = caught.exception.offload_assessment
        encoded = json.dumps(value)
        for private in [str(self.path), str(self.root), SESSION, self.source, "absFilePath"]:
            self.assertNotIn(private, encoded)
        return value

    def test_valid_unicode_offload_unchanged_and_path_failures_distinguished(self):
        value = _tool_content(self.call(), home=self.root, session_id=SESSION)
        self.assertEqual(value["content"][0]["text"], self.source)
        self.assertEqual(value["_sfdcCaptureReceipt"]["utf16Chars"], len(self.source.encode("utf-16-le")) // 2)
        outside = self.root / "outside.txt"
        cases = [
            (self.call(outside), SESSION, "OFFLOAD_PATH_OUTSIDE_HOME_ROOT"),
            (self.call(self.path.parent / "extra/source.txt"), SESSION, "OFFLOAD_PATH_PART_COUNT_MISMATCH"),
            (self.call(), "different-session", "OFFLOAD_SESSION_MISMATCH"),
            (self.call(self.path.parent.parent / "other/source.txt"), SESSION, "OFFLOAD_DIRECTORY_MISMATCH"),
            (self.call(self.path.parent / "missing.txt"), SESSION, "OFFLOAD_CANONICAL_PATH_UNAVAILABLE"),
            (self.call(), None, "OFFLOAD_SESSION_ID_UNVERIFIED"),
        ]
        for call, session, expected in cases:
            with self.subTest(expected=expected):
                facts = self.failure(call, session)
                self.assertEqual(facts["code"], expected)
                self.assertIsNone(facts["fileBytes"])
        self.path.unlink(); self.path.symlink_to(outside)
        outside.write_bytes(self.source.encode())
        facts = self.failure(self.call())
        self.assertEqual(facts["code"], "OFFLOAD_NONCANONICAL_PATH")
        self.assertFalse(facts["canonicalPathVerified"])
        invalid = self.call(); invalid["_meta"]["kiro"]["outputTransformation"]["absFilePath"] = 42
        self.assertEqual(self.failure(invalid)["code"], "OFFLOAD_METADATA_INVALID")

    def test_file_ownership_type_link_and_byte_limits_are_still_rejected(self):
        info = self.path.stat()
        for field, changed, code in [("st_uid", os.getuid()+1, "OFFLOAD_FILE_OWNER_MISMATCH"),
                                     ("st_mode", stat.S_IFDIR | 0o700, "OFFLOAD_NOT_REGULAR_FILE")]:
            facts = SimpleNamespace(st_uid=info.st_uid, st_mode=info.st_mode, st_nlink=info.st_nlink, st_size=info.st_size)
            setattr(facts, field, changed)
            with self.subTest(code=code), patch("sfdc_agent.kiro_mcp.os.fstat", return_value=facts):
                assessment = self.failure(self.call())
                self.assertEqual(assessment["code"], code)
                self.assertIsNone(assessment["readBytes"])
        link = self.path.parent / "hardlink.txt"
        os.link(self.path, link)
        facts = self.failure(self.call())
        self.assertEqual(facts["code"], "OFFLOAD_LINK_COUNT_MISMATCH")
        self.assertEqual(facts["fileLinkCount"], 2)
        link.unlink()
        with self.path.open("wb") as stream:
            stream.truncate(8 * 1024 * 1024 + 1)
        facts = self.failure(self.call())
        self.assertEqual(facts["code"], "OFFLOAD_FILE_BYTE_LIMIT")
        self.assertEqual(facts["fileBytes"], 8 * 1024 * 1024 + 1)
        self.assertEqual(facts["byteLimit"], 8 * 1024 * 1024)
        self.assertIsNone(facts["readBytes"])

    def test_utf8_and_declared_utf16_and_stat_counts_are_separate(self):
        count = len(self.source.encode("utf-16-le")) // 2
        facts = self.failure(self.call(total=count-1))
        self.assertEqual(facts["code"], "OFFLOAD_DECLARED_CHAR_COUNT_MISMATCH")
        self.assertEqual(facts["utf16Units"], count)
        self.assertEqual(facts["reportedCharCount"], count-1)
        self.assertEqual(facts["readSha256"], sha256(self.source.encode()).hexdigest())
        missing = self.call(); del missing["_meta"]["kiro"]["outputTransformation"]["totalChars"]
        facts = self.failure(missing)
        self.assertEqual(facts["reportedCharCountType"], "MISSING")
        self.assertFalse(facts["reportedCharCountPresent"])
        self.assertIsNone(facts["reportedCharCount"])
        facts = self.failure(self.call(total="DO_NOT_WRITE_AUTH_SENTENCE"))
        self.assertEqual(facts["reportedCharCountType"], "STRING")
        self.assertIsNone(facts["reportedCharCount"])
        self.assertNotIn("DO_NOT_WRITE", json.dumps(facts))
        info = self.path.stat()
        enlarged = SimpleNamespace(st_uid=info.st_uid, st_mode=info.st_mode, st_nlink=info.st_nlink, st_size=info.st_size+1)
        with patch("sfdc_agent.kiro_mcp.os.fstat", return_value=enlarged):
            facts = self.failure(self.call())
        self.assertEqual(facts["code"], "OFFLOAD_STAT_READ_LENGTH_MISMATCH")
        self.assertFalse(facts["statReadBytesEqual"])
        self.assertTrue(facts["declaredCharCountEqual"])
        self.path.write_bytes(b"\xff\xfe")
        facts = self.failure(self.call())
        self.assertEqual(facts["code"], "OFFLOAD_UTF8_INVALID")
        self.assertFalse(facts["utf8Decoded"])
        self.assertEqual(facts["readBytes"], 2)
        self.assertIsNone(facts["utf16Units"])

    def test_receipt_closed_projection_survives_raw_payload_omission_without_paths(self):
        with self.assertRaises(SessionError) as caught:
            _tool_content(self.call(total=1), home=self.root, session_id=SESSION)
        error = caught.exception
        error.offload_assessment.update(path=str(self.path), sessionId=SESSION, source=self.source,
                                         reportedCharCount="DO_NOT_WRITE_AUTH_SENTENCE")
        reader = KiroMcpReader(home=self.root, work=self.root, server="figma_fixture", file_key="FixtureFile",
                               node_id="1:2", key="DO_NOT_WRITE_KIRO_KEY")
        reader.collection_stage = "metadata"
        reader.session_id = reader.policy.session_id = SESSION
        approved(reader, name="get_metadata")
        finish(reader, {"response": "OAuth authorization required", "imageBase64Urls": []})
        report, objects = reader.failed_evidence(error)
        self.assertEqual(report["offloadAssessment"]["code"], "OFFLOAD_DECLARED_CHAR_COUNT_MISMATCH")
        self.assertEqual(objects, {})
        encoded = json.dumps(report)
        for private in [str(self.root), str(self.path), SESSION, self.source, "DO_NOT_WRITE"]:
            self.assertNotIn(private, encoded)
        store = SnapshotStore(self.root, SCOPE, JOB, "FixtureFile", "1:2")
        digest = store.save_failure(reader, error)
        receipt = json.loads((self.root / "failed-evidence" / (digest + ".failure.json")).read_text())
        self.assertEqual(receipt["offloadAssessment"], report["offloadAssessment"])
        self.assertFalse(receipt["snapshotRegistered"])

    def test_diagnostic_bug_keeps_original_offload_failure(self):
        with patch("sfdc_agent.failed_mcp_evidence.assess_offload_failure", side_effect=RuntimeError("DO_NOT_WRITE_BUG")):
            facts = self.failure(self.call(total=1))
        self.assertEqual(facts["code"], "OFFLOAD_DIAGNOSTIC_UNAVAILABLE")

import json
import base64
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sfdc_agent.figma_mcp_connections import (
    ConnectionScope, OAuthConfig, configure_connection, connection_ref, disable_connection,
    prepare_home, read_connection, _write_private,
)
from sfdc_agent.kiro_headless import SessionError
from sfdc_agent.kiro_mcp import FigmaToolPolicy
from sfdc_agent.kiro_mcp import _tool_content
from sfdc_agent.managed_figma_mcp import SnapshotStore, Collection, _scope, _safe_error
from fastapi import HTTPException
from sfdc_agent.figma_mcp_secrets import McpSecretStore


SCOPE = ConnectionScope(tenantId="00000000-0000-0000-0000-000000000001",
                        workspaceId="00000000-0000-0000-0000-000000000002",
                        actorUserId="00000000-0000-0000-0000-000000000003")


def permission(tool, inputs, call_id="call", server="figma_fixture"):
    return {"toolCall": {"toolCallId": call_id, "rawInput": inputs},
            "options": [{"kind": "allow_once", "optionId": "yes"}, {"kind": "reject_once", "optionId": "no"}],
            "_meta": {"kiro": {"mcpTool": {"version": 1, "identity": {"serverName": server, "toolName": tool},
                                            "annotations": {"readOnlyHint": True}}}}}


class FigmaMcpPolicyTest(unittest.TestCase):
    def setUp(self):
        self.policy = FigmaToolPolicy("figma_fixture", "FixtureFile", "1:2")

    def test_only_host_identity_and_exact_file_node_grant_one_time_consent(self):
        p = permission("get_metadata", {"fileKey": "FixtureFile", "nodeId": "1:2"})
        self.assertEqual(self.policy.permission(p)["outcome"]["optionId"], "yes")
        for change in ({"fileKey": "OtherFile", "nodeId": "1:2"}, {"fileKey": "FixtureFile", "nodeId": "9:9"}):
            self.assertEqual(self.policy.permission(permission("get_metadata", change, call_id="other"))["outcome"]["optionId"], "no")
        p["_meta"] = {}
        p["toolCall"]["title"] = "@figma_fixture/get_metadata"
        self.assertEqual(self.policy.permission(p)["outcome"]["optionId"], "no")

    def test_readonly_hint_cannot_authorize_writes_or_other_servers(self):
        inputs = {"fileKey": "FixtureFile", "nodeId": "1:2"}
        for p in (permission("use_figma", inputs), permission("get_metadata", inputs, server="attacker")):
            self.assertEqual(self.policy.permission(p)["outcome"]["optionId"], "no")

    def complete(self, tool, output, call_id="call", node_id="1:2"):
        inputs = {"fileKey": "FixtureFile", "nodeId": node_id}
        self.policy.update({"sessionUpdate": "tool_call", "toolCallId": call_id, "rawInput": inputs})
        self.policy.permission(permission(tool, inputs, call_id))
        self.policy.update({"sessionUpdate": "tool_call_update", "toolCallId": call_id,
                            "status": "completed", "rawOutput": output})

    def test_metadata_descendants_must_come_from_a_valid_root_tree(self):
        self.complete("get_metadata", {"content": [{"type": "text", "text": '<frame id="1:2"><instance id="1:3"/></frame>\nIMPORTANT: design context is separate.'}]})
        p = permission("get_design_context", {"fileKey": "FixtureFile", "nodeId": "1:3"}, "child")
        self.assertEqual(self.policy.permission(p)["outcome"]["optionId"], "yes")
        self.complete("get_metadata", {"content": [{"type": "text", "text": '<frame id="9:8"><frame id="9:9"/></frame>'}]}, "badroot")
        self.assertNotIn("9:9", self.policy.nodes)

    def test_model_narration_does_not_count_as_raw_source(self):
        self.policy.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "all tools completed"}})
        with self.assertRaisesRegex(SessionError, "메타데이터"):
            self.policy.evidence()

    def test_only_configured_reader_schema_loads_are_infrastructure(self):
        self.policy.update({"sessionUpdate": "tool_call", "toolCallId": "load", "rawInput": {"id": "tool::figma_fixture::get_metadata"},
                            "_meta": {"kiro": {"toolOrigin": "default"}}})
        self.policy.update({"sessionUpdate": "tool_call_update", "toolCallId": "load", "status": "completed", "rawOutput": {"message": "schema"}})
        with self.assertRaises(SessionError) as unavailable:
            self.policy.evidence()
        self.assertEqual(unavailable.exception.code, "FIGMA_MCP_EVIDENCE_INCOMPLETE")
        with self.assertRaises(SessionError):
            self.policy.update({"sessionUpdate": "tool_call", "toolCallId": "write", "rawInput": {"id": "tool::figma_fixture::use_figma"},
                                "_meta": {"kiro": {"toolOrigin": "default"}}, "status": "completed", "rawOutput": {"message": "schema"}})

    def test_host_offload_recovers_full_utf16_source_and_rejects_other_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            output = home / ".kiro/sessions/hash/session-fixture/tool-outputs/context.txt"
            output.parent.mkdir(parents=True)
            text = 'const label = "원본 😀";'
            output.write_text(text)
            call = {"rawOutput": {"response": "excerpt", "imageBase64Urls": []},
                    "_meta": {"kiro": {"outputTransformation": {"kind": "offloaded", "absFilePath": str(output),
                        "totalChars": len(text.encode("utf-16-le")) // 2}}}}
            recovered = _tool_content(call, home=home, session_id="session-fixture")
            self.assertEqual(recovered["content"][0]["text"], text)
            self.assertEqual(recovered["_sfdcCaptureReceipt"]["offloadBytes"], len(text.encode()))
            with self.assertRaises(SessionError):
                _tool_content(call, home=home, session_id="another-session")
            call["_meta"]["kiro"]["outputTransformation"]["totalChars"] -= 1
            with self.assertRaises(SessionError):
                _tool_content(call, home=home, session_id="session-fixture")
            output.unlink()
            output.symlink_to(home / "outside.txt")
            with self.assertRaises(SessionError):
                _tool_content(call, home=home, session_id="session-fixture")

    def test_changed_parameters_and_unapproved_execution_fail_closed(self):
        p = permission("get_metadata", {"fileKey": "FixtureFile", "nodeId": "1:2"})
        self.policy.permission(p)
        with self.assertRaises(SessionError) as changed:
            self.policy.update({"sessionUpdate": "tool_call", "toolCallId": "call", "status": "completed",
                                "rawInput": {"fileKey": "OtherFile", "nodeId": "1:2"}, "rawOutput": {"content": []}})
        self.assertEqual(changed.exception.code, "FIGMA_MCP_TOOL_SCOPE_DENIED")

    def test_provider_rate_limit_stops_collection_immediately(self):
        self.policy.permission(permission("get_metadata", {"fileKey": "FixtureFile", "nodeId": "1:2"}))
        with self.assertRaises(SessionError) as limited:
            self.policy.update({"sessionUpdate": "tool_call_update", "toolCallId": "call", "status": "failed",
                                "rawOutput": {"response": "You've reached the Figma MCP tool call limit for your View seat."}})
        self.assertEqual(limited.exception.code, "FIGMA_MCP_RATE_LIMITED")

    def test_tool_raw_payloads_remain_verbatim_and_truncation_fails(self):
        metadata = {"content": [{"type": "text", "text": '<frame id="1:2" width="200" height="100"/>'}]}
        design = {"content": [{"type": "text", "text": "SOURCE: keep these exact bytes"}], "structuredContent": {"foo": [1, 2]}}
        self.complete("get_metadata", metadata, "m")
        self.complete("get_design_context", design, "d")
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAXpeqz8AAAAASUVORK5CYII="
        self.complete("get_screenshot", {"content": [{"type": "image", "mimeType": "image/png", "data": png}]}, "s")
        evidence = self.policy.evidence()
        self.assertEqual(next(v for v in evidence if v["name"] == "get_design_context")["output"], design)
        self.policy.calls["d"]["rawOutput"]["truncated"] = True
        with self.assertRaises(SessionError) as truncated:
            self.policy.evidence()
        self.assertEqual(truncated.exception.code, "FIGMA_MCP_EVIDENCE_INCOMPLETE")


class FigmaMcpStorageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / "mcp"
        self.env = patch.dict(os.environ, {"SFDC_AGENT_FIGMA_MCP_ROOT": str(self.root)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_scope_is_principal_specific_and_no_configuration_is_not_authenticated(self):
        with self.assertRaises(SessionError) as missing:
            read_connection(SCOPE)
        self.assertEqual(missing.exception.code, "FIGMA_MCP_CONFIG_REQUIRED")
        receipt = configure_connection(SCOPE, OAuthConfig())
        self.assertEqual(receipt["authorization"], "UNVERIFIED")
        other = SCOPE.model_copy(update={"actorUserId": SCOPE.workspaceId})
        self.assertNotEqual(connection_ref(SCOPE), connection_ref(other))
        with self.assertRaises(SessionError):
            read_connection(other)

    def test_kiro_key_errors_are_distinct_from_figma_oauth(self):
        for code in ("KIRO_KEY_MISSING", "KIRO_KEY_INVALID", "KIRO_KEY_AUTH_REQUIRED", "FIGMA_MCP_AUTH_REQUIRED"):
            self.assertEqual(_safe_error(SessionError(code, "fixture")), code)

    def test_managed_collection_scope_comes_from_live_actor_not_request(self):
        context = SimpleNamespace(alive=lambda: True, purpose="screen", mode="headless", remote=True,
                                  file_key="FixtureFile", node_id="1:2", actor_user_id=str(SCOPE.actorUserId),
                                  execution_scope=(str(SCOPE.tenantId), str(SCOPE.workspaceId)))
        self.assertEqual(_scope(context, Collection(fileKey="FixtureFile", nodeId="1:2")), SCOPE)
        for update in ({"mode": "nova"}, {"actor_user_id": None}, {"node_id": "1:3"}, {"purpose": "story"}):
            invalid = SimpleNamespace(**{**context.__dict__, **update})
            with self.assertRaises(HTTPException) as failure:
                _scope(invalid, Collection(fileKey="FixtureFile", nodeId="1:2"))
            self.assertEqual(failure.exception.status_code, 403)

    def test_private_home_native_scope_readonly_profile_and_revoke(self):
        configure_connection(SCOPE, OAuthConfig())
        directory, home, work, server = prepare_home(SCOPE)
        profile_path = work / ".kiro/agents/sfdc-figma-reader.json"
        profile = json.loads(profile_path.read_text())
        self.assertEqual(profile["mcpServers"][server]["oauthScopes"], ["mcp:connect"])
        self.assertNotIn("clientSecret", profile["mcpServers"][server]["oauth"])
        self.assertFalse(profile["includeMcpJson"])
        self.assertTrue(all(tool.startswith("@" + server + "/get_") for tool in profile["tools"]))
        self.assertEqual(profile_path.stat().st_mode & 0o777, 0o600)
        disable_connection(SCOPE)
        self.assertFalse(profile_path.exists())
        with self.assertRaises(SessionError) as revoked:
            read_connection(SCOPE)
        self.assertEqual(revoked.exception.code, "FIGMA_MCP_AUTH_REVOKED")
        configure_connection(SCOPE, OAuthConfig())
        self.assertNotEqual(prepare_home(SCOPE)[1], home)

    def test_same_job_reuses_verified_snapshot_other_jobs_do_not(self):
        configure_connection(SCOPE, OAuthConfig())
        directory, _ = read_connection(SCOPE)
        store = SnapshotStore(directory, SCOPE, "job-a", "FixtureFile", "1:2")
        collected = {"toolEvidence": [{"name": "get_metadata", "input": {"fileKey": "FixtureFile", "nodeId": "1:2"},
                                        "status": "completed", "output": {"content": []}}], "rawTranscript": [{"original": True}]}
        saved = store.save(collected)
        loaded = store.load(saved["snapshotId"])
        self.assertTrue(loaded["reused"])
        self.assertEqual(loaded["toolEvidence"], collected["toolEvidence"])
        self.assertEqual(loaded["provenance"]["comparison"], "REFERENCE_ONLY")
        self.assertIsNone(SnapshotStore(directory, SCOPE, "job-b", "FixtureFile", "1:2").load())
        with self.assertRaises(SessionError):
            store.load("f" * 64)

    def test_partial_or_tampered_snapshot_never_becomes_complete(self):
        configure_connection(SCOPE, OAuthConfig())
        directory, _ = read_connection(SCOPE)
        store = SnapshotStore(directory, SCOPE, "job-a", "FixtureFile", "1:2")
        self.assertIsNone(store.load())
        saved = store.save({"toolEvidence": [], "rawTranscript": []})
        _write_private(store.root / (saved["snapshotId"] + ".json"), {"identity": store.identity, "toolEvidence": "changed"})
        with self.assertRaises(SessionError) as changed:
            store.load()
        self.assertEqual(changed.exception.code, "FIGMA_MCP_SNAPSHOT_CHANGED")

    def test_insecure_configuration_and_callback_fail(self):
        configure_connection(SCOPE, OAuthConfig())
        directory, _ = read_connection(SCOPE)
        (directory / "connection.json").chmod(0o644)
        with self.assertRaises(SessionError):
            read_connection(SCOPE)
        with self.assertRaises(ValueError):
            OAuthConfig(redirectUri="https://attacker.example/callback")
        with self.assertRaises(ValueError):
            OAuthConfig(clientId="id-only")

    def test_acp_credential_callbacks_are_private_generation_bound_and_revocable(self):
        configure_connection(SCOPE, OAuthConfig())
        directory, home, _, _ = prepare_home(SCOPE)
        vault = McpSecretStore(SCOPE, home)
        key = "kiro.mcp." + "a" * 64 + ".tokens"
        self.assertIsNone(vault.request("_kiro/secret/get", {"key": key})["value"])
        vault.request("_kiro/secret/store", {"key": key, "value": "fixture-not-a-token"})
        self.assertEqual(vault.request("_kiro/secret/get", {"key": key})["value"], "fixture-not-a-token")
        self.assertEqual(next(vault.root.glob("*.json")).stat().st_mode & 0o777, 0o600)
        with self.assertRaises(SessionError):
            vault.request("_kiro/secret/get", {"key": "other-app/../../token"})
        disable_connection(SCOPE)
        self.assertEqual(list(vault.root.glob("*.json")), [])
        with self.assertRaises(SessionError):
            vault.validate()
        configure_connection(SCOPE, OAuthConfig())
        with self.assertRaises(SessionError):
            vault.validate()


if __name__ == "__main__":
    unittest.main()

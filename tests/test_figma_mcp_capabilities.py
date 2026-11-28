import base64
import copy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from sfdc_agent.figma_mcp_capabilities import mcp_capability_receipt
from sfdc_agent.figma_mcp_connections import ConnectionScope
from sfdc_agent.managed_figma_mcp import SnapshotStore


SERVER = "figma_" + "a" * 32
CORE = ("get_metadata", "get_design_context", "get_screenshot")


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def wire(method, params):
    raw = encode({"jsonrpc": "2.0", "method": method, "params": params})
    return {"sha256": sha256(raw).hexdigest(), "jsonlBase64": base64.b64encode(raw).decode()}


def fixture():
    value = {"schemaVersion": 1, "identity": {"fileKey": "FixtureFile", "nodeId": "1:2"},
             "collectedAtEpochSeconds": 0, "rawTranscript": [], "rawWireTranscript": [], "toolEvidence": []}
    for name in CORE:
        arguments = {"fileKey": "FixtureFile", "nodeId": "1:2"}
        call_id = "source-" + name
        permission = {"toolCall": {"toolCallId": call_id, "rawInput": arguments},
                      "_meta": {"kiro": {"mcpTool": {"version": 1,
                          "identity": {"serverName": SERVER, "toolName": name}}}}}
        value["rawWireTranscript"].append(wire("session/request_permission", permission))
        value["rawTranscript"].append({"event": "permission", "callId": call_id, "allowed": True})
        value["toolEvidence"].append({"callId": call_id, "name": name, "status": "completed", "input": arguments,
                                      "output": {"content": [{"type": "text", "text":
                                          '<div className="w-full">private-source-copy</div>' if name == "get_design_context"
                                          else '<frame id="1:2" width="100" height="100"/>'}]}})
        identifier = f"tool::{SERVER}::{name}"
        loaded = {"loaded": [{"tool_id": identifier,
                   "resource_identity": {"kind": "mcp", "serverName": SERVER, "toolName": name},
                   "description": "private-description https://private.invalid/?secret=private-key",
                   "input_schema": {"type": "object", "properties": {"fileKey": {"type": "string"},
                       "nodeId": {"type": "string"}}, "required": ["fileKey", "nodeId"], "additionalProperties": False}}],
                  "not_found": []}
        call = {"sessionUpdate": "tool_call_update", "toolCallId": "schema-" + name,
                "status": "completed", "rawInput": {"id": identifier},
                "_meta": {"kiro": {"toolOrigin": "default"}}, "rawOutput": {"message": json.dumps(loaded)}}
        value["rawTranscript"].append({"event": "tool", "value": call})
        value["rawWireTranscript"].append(wire("session/update", {"sessionId": "fixture-session", "update": call}))
    return value


def receipt(value, **kwargs):
    return mcp_capability_receipt(value, sha256(encode(value)).hexdigest(), **kwargs)


def schema_change(value, name, change):
    event = next(item for item in value["rawTranscript"] if item.get("event") == "tool"
                 and item["value"]["rawInput"]["id"].endswith("::" + name))
    loaded = json.loads(event["value"]["rawOutput"]["message"])
    change(loaded["loaded"][0]["input_schema"])
    event["value"]["rawOutput"]["message"] = json.dumps(loaded)
    refresh_load_wires(value)


def refresh_load_wires(value):
    value["rawWireTranscript"] = [item for item in value["rawWireTranscript"]
        if json.loads(base64.b64decode(item["jsonlBase64"]))["method"] != "session/update"]
    value["rawWireTranscript"].extend(wire("session/update", {"sessionId": "fixture-session", "update": event["value"]})
        for event in value["rawTranscript"] if event.get("event") == "tool")


class FigmaMcpCapabilitiesTest(unittest.TestCase):
    def test_closed_host_bound_schemas_observe_absence_without_authorizing_versions(self):
        value = fixture()
        before = copy.deepcopy(value)
        result = receipt(value)
        self.assertEqual(result["status"], "OBSERVED_NO_REVISION_ARGUMENT")
        self.assertFalse(result["historicalRevisionVerified"])
        self.assertFalse(result["comparisonPromotionAllowed"])
        self.assertEqual(value, before)
        for item in result["tools"]:
            if item["name"] in CORE:
                self.assertEqual(item["properties"], ["fileKey", "nodeId"])
                self.assertEqual(item["revisionArgumentNames"], [])
                self.assertRegex(item["schemaSha256"], r"^[a-f0-9]{64}$")
            else:
                self.assertEqual(item["status"], "UNKNOWN")
        public = json.dumps(result)
        for forbidden in ["private-", "http", SERVER, "rawInput", "rawOutput", "FixtureFile", "1:2"]:
            self.assertNotIn(forbidden, public)

    def test_missing_legacy_evidence_and_self_claimed_capability_remain_unknown(self):
        for missing in ["rawTranscript", "rawWireTranscript", "toolEvidence"]:
            value = fixture()
            del value[missing]
            value["capabilities"] = {"status": "VERIFIED", "version": "private-version"}
            with self.subTest(missing=missing):
                self.assertEqual(receipt(value)["status"], "UNKNOWN")

    def test_unverified_configured_endpoint_or_checkpoint_hash_remains_unknown(self):
        value = fixture()
        self.assertEqual(receipt(value, configured_endpoint="https://another.invalid/mcp")["status"], "UNKNOWN")
        self.assertEqual(mcp_capability_receipt(value, "b" * 64)["status"], "UNKNOWN")

    def test_wire_tampering_cannot_substitute_narration_or_a_forged_schema(self):
        value = fixture()
        value["rawTranscript"][1]["value"]["_meta"]["kiro"]["toolOrigin"] = "model"
        self.assertEqual(receipt(value)["status"], "UNKNOWN")
        value = fixture()
        value["rawWireTranscript"][0]["sha256"] = "0" * 64
        self.assertEqual(receipt(value)["status"], "UNKNOWN")

    def test_wrong_schema_origin_input_status_and_host_server_are_not_observations(self):
        changes = [lambda call: call["_meta"]["kiro"].update(toolOrigin="model"),
                   lambda call: call["rawInput"].update(other="private-input"),
                   lambda call: call.update(status="failed"),
                   lambda call: call["rawInput"].update(id="tool::figma_" + "b" * 32 + "::get_metadata")]
        for change in changes:
            value = fixture()
            change(value["rawTranscript"][1]["value"])
            refresh_load_wires(value)
            with self.subTest(change=change):
                self.assertEqual(receipt(value)["status"], "UNKNOWN")

    def test_schema_resource_identity_must_match_exact_loaded_tool(self):
        value = fixture()
        call = value["rawTranscript"][1]["value"]
        loaded = json.loads(call["rawOutput"]["message"])
        loaded["loaded"][0]["resource_identity"]["toolName"] = "use_figma"
        call["rawOutput"]["message"] = json.dumps(loaded)
        refresh_load_wires(value)
        self.assertEqual(receipt(value)["status"], "UNKNOWN")

    def test_missing_or_denied_source_permission_is_not_host_bound_evidence(self):
        value = fixture()
        value["rawTranscript"][0]["allowed"] = False
        self.assertEqual(receipt(value)["status"], "UNKNOWN")
        value = fixture()
        value["toolEvidence"][0]["input"] = {"fileKey": "OtherFile", "nodeId": "1:2"}
        self.assertEqual(receipt(value)["status"], "UNKNOWN")

    def test_permission_without_input_uses_only_matching_raw_host_call_updates(self):
        value = fixture()
        item = value["rawWireTranscript"][0]
        message = json.loads(base64.b64decode(item["jsonlBase64"]))
        arguments = message["params"]["toolCall"].pop("rawInput")
        value["rawWireTranscript"][0] = wire(message["method"], message["params"])
        update = {"sessionUpdate": "tool_call", "toolCallId": "source-get_metadata", "status": "pending",
                  "rawInput": arguments, "_meta": {"kiro": {"serverName": SERVER}}}
        value["rawWireTranscript"].append(wire("session/update", {"sessionId": "fixture-session", "update": update}))
        self.assertEqual(receipt(value)["status"], "OBSERVED_NO_REVISION_ARGUMENT")
        update["rawInput"] = {"fileKey": "OtherFile", "nodeId": "1:2"}
        value["rawWireTranscript"][-1] = wire("session/update", {"sessionId": "fixture-session", "update": update})
        self.assertEqual(receipt(value)["status"], "UNKNOWN")

    def test_revision_argument_is_observed_but_never_promoted_to_supported(self):
        value = fixture()
        schema_change(value, "get_design_context", lambda schema: schema["properties"].update(version={"type": "string"}))
        result = receipt(value)
        self.assertEqual(result["status"], "REVISION_ARGUMENT_OBSERVED_UNVERIFIED")
        self.assertFalse(result["historicalRevisionVerified"])
        self.assertFalse(result["comparisonPromotionAllowed"])

    def test_open_or_composed_schema_cannot_prove_absence_of_revision_argument(self):
        for change in [lambda schema: schema.update(additionalProperties=True),
                       lambda schema: schema.update(allOf=[{"properties": {"version": {"type": "string"}}}]),
                       lambda schema: schema["properties"].update(private_secret={"type": "string"}),
                       lambda schema: schema.update(required=["unlisted"])]:
            value = fixture()
            schema_change(value, "get_metadata", change)
            with self.subTest(change=change):
                self.assertEqual(receipt(value)["status"], "UNKNOWN")
                self.assertNotIn("private_", json.dumps(receipt(value)))

    def test_conflicting_schema_receipts_for_the_same_tool_are_ambiguous(self):
        value = fixture()
        duplicate = copy.deepcopy(value["rawTranscript"][1])
        duplicate["value"]["toolCallId"] = "schema-second"
        message = json.loads(duplicate["value"]["rawOutput"]["message"])
        message["loaded"][0]["input_schema"]["properties"]["nodeId"]["minLength"] = 1
        duplicate["value"]["rawOutput"]["message"] = json.dumps(message)
        value["rawTranscript"].append(duplicate)
        refresh_load_wires(value)
        self.assertEqual(receipt(value)["status"], "UNKNOWN")

    def test_duplicate_json_properties_or_oversized_schema_messages_remain_unknown(self):
        for message in ['{"loaded":[],"loaded":[],"not_found":[]}', "x" * (128 * 1024 + 1)]:
            value = fixture()
            value["rawTranscript"][1]["value"]["rawOutput"]["message"] = message
            refresh_load_wires(value)
            self.assertEqual(receipt(value)["status"], "UNKNOWN")

    def test_snapshot_response_only_adds_safe_provenance_and_preserves_grade(self):
        value = fixture()
        before = copy.deepcopy(value)
        result = SnapshotStore.response(value, sha256(encode(value)).hexdigest(), reused=True)
        self.assertEqual(result["provenance"]["capabilities"]["status"], "OBSERVED_NO_REVISION_ARGUMENT")
        self.assertEqual(result["provenance"]["revisionStatus"], "UNVERIFIED")
        self.assertEqual(result["provenance"]["comparison"], "REFERENCE_ONLY")
        self.assertEqual(result["coverage"]["status"], "PARTIAL")
        self.assertEqual(result["toolEvidence"], before["toolEvidence"])
        self.assertEqual(value, before)

    def test_existing_private_snapshot_replay_does_not_rewrite_raw_cache(self):
        scope = ConnectionScope(tenantId="00000000-0000-0000-0000-000000000001",
            workspaceId="00000000-0000-0000-0000-000000000002", actorUserId="00000000-0000-0000-0000-000000000003")
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(Path(directory).resolve(), scope, "fixture-job", "FixtureFile", "1:2")
            data = fixture()
            collected = {key: value for key, value in data.items() if key not in {"identity", "collectedAtEpochSeconds", "schemaVersion"}}
            saved = store.save(collected)
            snapshot = store.root / (saved["snapshotId"] + ".json")
            original = snapshot.read_bytes()
            loaded = store.load(saved["snapshotId"])
            self.assertEqual(loaded["provenance"]["capabilities"]["status"], "OBSERVED_NO_REVISION_ARGUMENT")
            self.assertEqual(snapshot.read_bytes(), original)
            self.assertNotIn(b'"capabilities"', original)


if __name__ == "__main__":
    unittest.main()

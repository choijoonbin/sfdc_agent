"""Content-free capability observations from a validated private MCP checkpoint.

This is diagnostic evidence, never permission to read a saved revision. Callers
must own and validate the checkpoint first. Neither model narration nor a
capability object supplied inside the collected payload is consulted.
"""

import base64
from hashlib import sha256
import json
import re

from .figma_mcp_connections import MCP_URL, READ_TOOLS

_CORE = frozenset({"get_metadata", "get_design_context", "get_screenshot"})
_SERVER = re.compile(r"^figma_[a-f0-9]{32}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_REVISION_NAMES = frozenset({"version", "versionId", "version_id", "revision", "revisionId", "revision_id", "fileVersion"})
_PROPERTY_NAMES = frozenset({"fileKey", "nodeId", "sessionId", "clientLanguages", "clientFrameworks",
    "forceCode", "disableCodeConnect", "skillNames", "excludeScreenshot", "contentsOnly", "enableBase64Response",
    "maxDimension"}) | _REVISION_NAMES
_SCHEMA_KEYS = frozenset({"type", "properties", "required", "additionalProperties", "description", "title", "$schema"})


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def _json(text):
    return json.loads(text, object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def _base(snapshot_id, reason="EVIDENCE_UNAVAILABLE"):
    return {"schemaVersion": 1, "status": "UNKNOWN", "scope": "observed-design-read-tool-contracts",
            "snapshotId": snapshot_id if isinstance(snapshot_id, str) and _DIGEST.fullmatch(snapshot_id) else None,
            "reason": reason, "historicalRevisionVerified": False, "comparisonPromotionAllowed": False,
            "tools": [{"name": name, "status": "UNKNOWN"} for name in sorted(READ_TOOLS)]}


def _wire(value):
    entries = value.get("rawWireTranscript")
    if not isinstance(entries, list) or not 0 < len(entries) <= 1024:
        raise ValueError
    updates, permissions, call_inputs, total = set(), [], {}, 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"sha256", "jsonlBase64"}:
            raise ValueError
        encoded = entry["jsonlBase64"]
        if not isinstance(encoded, str) or len(encoded) > 12 * 1024 * 1024:
            raise ValueError
        raw = base64.b64decode(encoded, validate=True)
        total += len(raw)
        if total > 64 * 1024 * 1024 or sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError
        message = _json(raw)
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise ValueError
        params = message.get("params", {})
        if not isinstance(params, dict):
            raise ValueError
        if message.get("method") == "session/update" and isinstance(params.get("update"), dict):
            update = params["update"]
            updates.add(sha256(_encode(update)).hexdigest())
            if (isinstance(update.get("toolCallId"), str) and isinstance(update.get("rawInput"), dict)
                    and update.get("sessionUpdate") in {"tool_call", "tool_call_update"}):
                call_inputs.setdefault(update["toolCallId"], []).append({"input": update["rawInput"],
                    "server": update.get("_meta", {}).get("kiro", {}).get("serverName")})
        if message.get("method") == "session/request_permission":
            permissions.append(params)
    return updates, permissions, call_inputs


def _server(value, permissions, call_inputs):
    """Tie schema identity to a host-described, granted, completed source call."""
    transcript, tools, identity = value.get("rawTranscript"), value.get("toolEvidence"), value.get("identity")
    if not isinstance(transcript, list) or len(transcript) > 4096 or not isinstance(tools, list) or not isinstance(identity, dict):
        raise ValueError
    granted = {event.get("callId") for event in transcript if isinstance(event, dict)
               and event.get("event") == "permission" and event.get("allowed") is True}
    servers, observed = set(), set()
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("name") not in READ_TOOLS or tool.get("status") != "completed":
            raise ValueError
        arguments, call_id = tool.get("input"), tool.get("callId")
        if (not isinstance(arguments, dict) or arguments.get("fileKey") != identity.get("fileKey")
                or not isinstance(arguments.get("nodeId"), str) or call_id not in granted):
            raise ValueError
        matches = []
        for permission in permissions:
            host = permission.get("_meta", {}).get("kiro", {}).get("mcpTool", {})
            host_identity = host.get("identity", {})
            call = permission.get("toolCall", {})
            if (type(host.get("version")) is int and host["version"] == 1 and isinstance(host_identity, dict) and isinstance(call, dict)
                    and host_identity.get("toolName") == tool["name"] and call.get("toolCallId") == call_id):
                # ACP may omit rawInput in its permission message. As in the
                # live policy, use only the same host call's retained input,
                # never the model's narration or the evidence object's claim.
                if "rawInput" in call:
                    same_input = call["rawInput"] == arguments
                else:
                    anchors = call_inputs.get(call_id, [])
                    same_input = bool(anchors) and all(item["input"] == arguments
                        and item["server"] == host_identity.get("serverName") for item in anchors)
                if same_input:
                    matches.append(host_identity.get("serverName"))
        if len(matches) != 1 or not isinstance(matches[0], str) or not _SERVER.fullmatch(matches[0]):
            raise ValueError
        servers.add(matches[0])
        observed.add(tool["name"])
    if len(servers) != 1 or not _CORE <= observed:
        raise ValueError
    return next(iter(servers))


def _schema_observation(schema):
    if (not isinstance(schema, dict) or schema.get("type") != "object" or set(schema) - _SCHEMA_KEYS
            or not isinstance(schema.get("properties"), dict) or len(schema["properties"]) > 32
            or not isinstance(schema.get("required", []), list)):
        raise ValueError
    properties, required = schema["properties"], schema.get("required", [])
    if (not all(isinstance(name, str) and name in _PROPERTY_NAMES and isinstance(item, dict)
                for name, item in properties.items()) or not all(isinstance(name, str) and name in properties for name in required)
            or len(set(required)) != len(required) or not {"fileKey", "nodeId"} <= properties.keys()):
        raise ValueError
    revision = sorted(set(properties) & _REVISION_NAMES)
    status = "REVISION_ARGUMENT_OBSERVED_UNVERIFIED" if revision else (
        "OBSERVED_NO_REVISION_ARGUMENT" if schema.get("additionalProperties") is False else "UNKNOWN")
    return {"status": status, "schemaSha256": sha256(_encode(schema)).hexdigest(),
            "properties": sorted(properties), "required": sorted(required),
            "additionalPropertiesAllowed": schema.get("additionalProperties") is not False,
            "revisionArgumentNames": revision}


def mcp_capability_receipt(value, snapshot_id, *, configured_endpoint=MCP_URL):
    """Observe retained official reader schemas without changing collection/grade.

    Official endpoint identity comes from server configuration, not snapshot text.
    A future revision argument remains unverified until matching source-version
    evidence is implemented separately. Legacy/incomplete caches stay UNKNOWN.
    """
    result = _base(snapshot_id)
    if configured_endpoint != MCP_URL:
        result["reason"] = "OFFICIAL_ENDPOINT_UNVERIFIED"
        return result
    try:
        if not isinstance(value, dict) or sha256(_encode(value)).hexdigest() != snapshot_id:
            result["reason"] = "SNAPSHOT_DIGEST_UNVERIFIED"
            return result
        wire_updates, permissions, call_inputs = _wire(value)
        server = _server(value, permissions, call_inputs)
        observations, invalid = {}, set()
        for event in value["rawTranscript"]:
            call = event.get("value") if isinstance(event, dict) and event.get("event") == "tool" else None
            if not isinstance(call, dict) or call.get("status") != "completed":
                continue
            arguments = call.get("rawInput")
            identifier = arguments.get("id") if isinstance(arguments, dict) else None
            if not isinstance(identifier, str) or not identifier.startswith("tool::"):
                continue
            parts = identifier.split("::")
            name = parts[-1]
            if name not in READ_TOOLS:
                continue
            try:
                if (len(parts) != 3 or parts[1] != server or set(arguments) != {"id"}
                        or call.get("_meta", {}).get("kiro", {}).get("toolOrigin") != "default"
                        or sha256(_encode(call)).hexdigest() not in wire_updates):
                    raise ValueError
                output = call.get("rawOutput")
                message = output.get("message") if isinstance(output, dict) and output.get("isError") is not True else None
                if not isinstance(message, str) or len(message) > 128 * 1024:
                    raise ValueError
                loaded = _json(message)
                if (not isinstance(loaded, dict) or loaded.get("not_found") != []
                        or not isinstance(loaded.get("loaded"), list) or len(loaded["loaded"]) != 1):
                    raise ValueError
                tool = loaded["loaded"][0]
                if (not isinstance(tool, dict) or tool.get("tool_id") != identifier
                        or tool.get("resource_identity") != {"kind": "mcp", "serverName": server, "toolName": name}):
                    raise ValueError
                observed = _schema_observation(tool.get("input_schema"))
                observed["schemaMessageSha256"] = sha256(message.encode()).hexdigest()
                prior = observations.get(name)
                if prior and prior["schemaSha256"] != observed["schemaSha256"]:
                    raise ValueError
                observations[name] = observed
            except (ValueError, TypeError, KeyError, RecursionError):
                invalid.add(name)
        result["tools"] = [{"name": name, **observations[name]} if name in observations and name not in invalid
                           else {"name": name, "status": "UNKNOWN"} for name in sorted(READ_TOOLS)]
        core = [item for item in result["tools"] if item["name"] in _CORE]
        if all(item["status"] == "OBSERVED_NO_REVISION_ARGUMENT" for item in core):
            result.update(status="OBSERVED_NO_REVISION_ARGUMENT", reason="CLOSED_OBSERVED_CORE_TOOL_SCHEMAS")
        elif any(item["status"] == "REVISION_ARGUMENT_OBSERVED_UNVERIFIED" for item in core):
            result.update(status="REVISION_ARGUMENT_OBSERVED_UNVERIFIED", reason="RETURNED_REVISION_PROOF_NOT_IMPLEMENTED")
        else:
            result["reason"] = "SCHEMA_EVIDENCE_INCOMPLETE"
        result["endpointAuthority"] = "SERVER_CONFIGURED_FIGMA_OFFICIAL_REMOTE"
        result["transport"] = "kiro-acp-v3-host-tool-receipts"
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError, AttributeError):
        result = _base(snapshot_id, "SCHEMA_EVIDENCE_UNVERIFIED")
    return result

"""Private terminal-failure evidence. Never a resumable Figma snapshot.

Only approved, unchanged Figma read inputs/outputs are eligible. Exact JSON
value lexemes are extracted in memory; ACP envelopes, sessions and auth traffic
are never written. UTF-8 content bytes are also retained when already recovered
by the existing offload adapter. Unknown/mixed credential envelopes are omitted.
"""

import base64
import errno
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from .figma_mcp_connections import READ_TOOLS, _directory
from .mcp_acquisition_readiness import assess_design_context_output

MAX_FAILED_BYTES = 64 * 1024 * 1024  # Existing reader's complete wire byte budget.
_INPUT_KEYS = {"fileKey", "nodeId", "clientFrameworks", "clientLanguages", "skillNames", "excludeScreenshot",
               "maxDimension", "enableBase64Response", "contentsOnly", "forceCode", "disableCodeConnect"}
_OUTPUT_KEYS = {"content", "structuredContent", "isError", "truncated", "isTruncated", "hasMore",
                "response", "imageBase64Urls"}
_SECRET_KEY = re.compile(r"^(?:authorization|proxyauthorization|cookie|setcookie|headers?|credentials?|password|"
                         r"\w*secret|\w*token|apikey|session(?:id)?|oauth\w*|codeverifier)$", re.I)
_SECRET_TEXT = re.compile(r"\bBearer\s+\S+|\b(?:access|refresh|id)[_-]token[\"']?\s*[:=]|"
                          r"\b(?:client[_-]?secret|api[_-]?key|password|code[_-]?verifier)[\"']?\s*[:=]|"
                          r"\b(?:session[_-]?id|authorization|cookie)[\"']?\s*[:=]|/\.kiro/sessions/|"
                          r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b|"
                          r"https?://[^/\s\"<>]+@|"
                          r"https?://[^\s\"<>]*(?:[?&](?:code|token|access_token|refresh_token|client_secret)=|/oauth(?:/|\?))", re.I)
_AUTH_NOTICE = re.compile(r"\b(?:oauth|authenticate|authentication|authorization|sign\s+in|log\s+in)\b|\b(?:401|403)\b", re.I)
_STAGES = {"startup", "initialize", "session-new", "connection", "metadata", "design-context", "capture", "evidence-validation"}
_FAILURES = {"FIGMA_MCP_AUTH_REQUIRED", "FIGMA_MCP_AUTH_REVOKED", "FIGMA_MCP_CONNECTION_REVOKED",
             "FIGMA_MCP_EVIDENCE_INCOMPLETE", "FIGMA_MCP_RATE_LIMITED", "FIGMA_MCP_TOOL_SCOPE_DENIED",
             "FIGMA_MCP_INVALID_INPUT", "FIGMA_MCP_TRANSPORT_FAILED", "FIGMA_MCP_TRANSPORT_INVALID",
             "FIGMA_MCP_TRANSPORT_UNSUPPORTED", "FIGMA_MCP_TIMEOUT", "FIGMA_MCP_COLLECTION_FAILED",
             "FIGMA_MCP_SNAPSHOT_CHANGED", "KIRO_KEY_AUTH_REQUIRED", "KIRO_KEY_MISSING", "KIRO_KEY_INVALID",
             "KIRO_UNAVAILABLE"}
_REASONS = {"DESIGN_CONTEXT_RETRY_REQUIRED", "TOOL_RAW_OUTPUT_MISSING", "TOOL_TRANSFORMATION_UNVERIFIED", "TOOL_WRAPPER_UNVERIFIED",
            "TOOL_OFFLOAD_UNVERIFIED", "TOOL_IMAGE_MISSING", "TOOL_IMAGE_FORMAT_UNVERIFIED", "TOOL_IMAGE_DATA_INVALID",
            "TOOL_CALL_SET_UNVERIFIED", "TOOL_STATUS_FAILED", "TOOL_PROVIDER_ERROR", "TOOL_CALL_UNAPPROVED",
            "TOOL_INPUT_CHANGED", "TOOL_PENDING_OR_RAW_MISSING", "TOOL_CONTENT_FORMAT_UNVERIFIED",
            "TOOL_DECLARED_TRUNCATED", "REQUIRED_ROOT_TOOLS_MISSING", "ROOT_PNG_UNVERIFIED",
            "ROOT_METADATA_UNVERIFIED", "TRANSPORT_EOF", "TRANSPORT_BYTE_LIMIT", "TRANSPORT_INVALID_JSON", "PROMPT_STOP_INCOMPLETE"}
_REASONS.update({"TOOL_OFFLOAD_BARRIER_PENDING", "TOOL_OFFLOAD_BARRIER_CHANGED"})
_OFFLOAD_PHASES = ("path-metadata", "path-relative", "path-validation", "file-open", "file-stat", "file-validation",
                   "file-read", "utf8-decode", "utf16-measure", "length-validation")
_OFFLOAD_BOOLS = {"sessionIdTypeValid", "pathUnderHomeRoot", "sessionMatches", "toolOutputsSegmentMatches",
                  "fileRegular", "fileOwnedByProcess", "fileSingleLink", "utf8Decoded", "reportedCharCountPresent",
                  "statReadBytesEqual", "declaredCharCountEqual", "canonicalPathVerified", "operatingSystemError"}
_OFFLOAD_COUNTS = {"relativePartCount", "fileLinkCount", "fileBytes", "readBytes", "utf16Units", "reportedCharCount"}
_OFFLOAD_CODES = {"OFFLOAD_METADATA_INVALID", "OFFLOAD_PATH_OUTSIDE_HOME_ROOT", "OFFLOAD_SESSION_ID_UNVERIFIED",
                  "OFFLOAD_PATH_PART_COUNT_MISMATCH", "OFFLOAD_SESSION_MISMATCH", "OFFLOAD_DIRECTORY_MISMATCH",
                  "OFFLOAD_CANONICAL_PATH_UNAVAILABLE", "OFFLOAD_NONCANONICAL_PATH", "OFFLOAD_FILE_OPEN_FAILED",
                  "OFFLOAD_FILE_STAT_FAILED", "OFFLOAD_NOT_REGULAR_FILE", "OFFLOAD_FILE_OWNER_MISMATCH",
                  "OFFLOAD_LINK_COUNT_MISMATCH", "OFFLOAD_FILE_BYTE_LIMIT", "OFFLOAD_FILE_VALIDATION_UNVERIFIED",
                  "OFFLOAD_READ_FAILED", "OFFLOAD_UTF8_INVALID", "OFFLOAD_UTF16_INVALID",
                  "OFFLOAD_STAT_READ_LENGTH_MISMATCH", "OFFLOAD_DECLARED_CHAR_COUNT_MISMATCH",
                  "OFFLOAD_DIAGNOSTIC_UNAVAILABLE"}


def _safe_offload_assessment(value):
    """Closed projection: no error text, path, session, source or unknown key."""
    if not isinstance(value, dict) or value.get("phase") not in _OFFLOAD_PHASES or value.get("code") not in _OFFLOAD_CODES:
        return None
    result = {"schemaVersion": 1, "phase": value["phase"], "code": value["code"], "byteLimit": 8 * 1024 * 1024,
              "expectedRelativePartCount": 4}
    for name in _OFFLOAD_BOOLS:
        result[name] = value.get(name) if isinstance(value.get(name), bool) else None
    for name in _OFFLOAD_COUNTS:
        count = value.get(name)
        result[name] = count if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 2**63-1 else None
    result["reportedCharCountType"] = value.get("reportedCharCountType") if value.get("reportedCharCountType") in {
        "MISSING", "INTEGER", "BOOLEAN", "STRING", "OTHER"} else "OTHER"
    result["readSha256"] = value.get("readSha256") if isinstance(value.get("readSha256"), str) and re.fullmatch(
        r"[a-f0-9]{64}", value["readSha256"]) else None
    result["osErrorCode"] = value.get("osErrorCode") if value.get("osErrorCode") in {
        "ENOENT", "EACCES", "EPERM", "ELOOP", "ENOTDIR", "ENAMETOOLONG", "EIO", "OTHER_OS_ERROR"} else None
    return result


def assess_offload_failure(phase, facts, *, os_error=None):
    """Interpret a failed original guard using already-observed scalar facts."""
    value = dict(facts)
    value["phase"] = phase
    value["osErrorCode"] = errno.errorcode.get(os_error, "OTHER_OS_ERROR") if value.get("operatingSystemError") else None
    reported = value.get("reportedCharCount")
    value["reportedCharCountType"] = "MISSING" if not value.get("reportedCharCountPresent") else (
        "BOOLEAN" if isinstance(reported, bool) else "INTEGER" if isinstance(reported, int) else "STRING" if isinstance(reported, str) else "OTHER")
    value["canonicalPathVerified"] = True if phase in _OFFLOAD_PHASES[3:] else None
    code = "OFFLOAD_DIAGNOSTIC_UNAVAILABLE"
    if phase == "path-metadata":
        code = "OFFLOAD_METADATA_INVALID"
    elif phase == "path-relative":
        code = "OFFLOAD_PATH_OUTSIDE_HOME_ROOT"
    elif phase == "path-validation":
        if not value.get("sessionIdTypeValid"):
            code = "OFFLOAD_SESSION_ID_UNVERIFIED"
        elif value.get("relativePartCount") != 4:
            code = "OFFLOAD_PATH_PART_COUNT_MISMATCH"
        elif not value.get("sessionMatches"):
            code = "OFFLOAD_SESSION_MISMATCH"
        elif not value.get("toolOutputsSegmentMatches"):
            code = "OFFLOAD_DIRECTORY_MISMATCH"
        elif value.get("operatingSystemError"):
            code = "OFFLOAD_CANONICAL_PATH_UNAVAILABLE"
        else:
            code = "OFFLOAD_NONCANONICAL_PATH"
            value["canonicalPathVerified"] = False
    elif phase in {"file-open", "file-stat", "file-read", "utf8-decode", "utf16-measure"}:
        code = {"file-open": "OFFLOAD_FILE_OPEN_FAILED", "file-stat": "OFFLOAD_FILE_STAT_FAILED",
                "file-read": "OFFLOAD_READ_FAILED", "utf8-decode": "OFFLOAD_UTF8_INVALID", "utf16-measure": "OFFLOAD_UTF16_INVALID"}[phase]
    elif phase == "file-validation":
        code = "OFFLOAD_NOT_REGULAR_FILE" if not value.get("fileRegular") else (
            "OFFLOAD_FILE_OWNER_MISMATCH" if not value.get("fileOwnedByProcess") else (
                "OFFLOAD_LINK_COUNT_MISMATCH" if not value.get("fileSingleLink") else (
                    "OFFLOAD_FILE_BYTE_LIMIT" if value.get("fileBytes", 0) > 8*1024*1024 else "OFFLOAD_FILE_VALIDATION_UNVERIFIED")))
    elif phase == "length-validation":
        code = "OFFLOAD_STAT_READ_LENGTH_MISMATCH" if not value.get("statReadBytesEqual") else "OFFLOAD_DECLARED_CHAR_COUNT_MISMATCH"
    value["code"] = code
    return _safe_offload_assessment(value)


def safe_source_read_barrier(value):
    if not isinstance(value, dict) or value.get("outcome") not in {"WAITING_SAME_PROMPT", "SOURCE_CHANGED",
            "REJECTED_AT_BARRIER", "VERIFIED_AT_BARRIER", "PROMPT_NOT_END_TURN"}:
        return None
    result = {"schemaVersion": 1, "outcome": value["outcome"]}
    for key in ("barrierAttempts", "deniedPermissionCount", "nodesBefore", "nodesAfter", "verifiedSourceBytes", "verifiedUtf16Units"):
        number = value.get(key)
        if isinstance(number, int) and not isinstance(number, bool) and 0 <= number <= 2**63-1:
            result[key] = number
    for key in ("promptReturnedEndTurn", "sourceBindingUnchanged"):
        if isinstance(value.get(key), bool):
            result[key] = value[key]
    for key in ("sourceBindingSha256", "verifiedSourceSha256"):
        if isinstance(value.get(key), str) and re.fullmatch(r"[a-f0-9]{64}", value[key]):
            result[key] = value[key]
    for key in ("initialAssessment", "barrierAssessment"):
        assessment = _safe_offload_assessment(value.get(key))
        if assessment is not None:
            result[key] = assessment
    return result


def _strict_loads(body):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON member")
            result[key] = value
        return result
    return json.loads(body, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def _member_bytes(body, path):
    """Return a JSON value's exact UTF-8 lexeme, not the surrounding wire."""
    text = body.decode("utf-8", errors="strict")
    decoder = json.JSONDecoder()
    start = 0
    for name in path:
        start = re.compile(r"\S").search(text, start).start()
        if text[start] != "{":
            raise ValueError
        position, found = start + 1, None
        while True:
            position = re.compile(r"\S").search(text, position).start()
            if text[position] == "}":
                break
            key, position = decoder.raw_decode(text, position)
            position = re.compile(r"\S").search(text, position).start()
            if text[position] != ":":
                raise ValueError
            position = re.compile(r"\S").search(text, position + 1).start()
            _, end = decoder.raw_decode(text, position)
            if key == name:
                found = position
                break
            position = re.compile(r"\S").search(text, end).start()
            if text[position] == "}":
                break
            if text[position] != ",":
                raise ValueError
            position += 1
        if found is None:
            raise ValueError
        start = found
    _, end = decoder.raw_decode(text, start)
    return text[start:end].encode("utf-8")


def _sensitive(value):
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > 500_000 or depth > 64:
            return True
        if isinstance(item, dict):
            if any(not isinstance(key, str) or _SECRET_KEY.fullmatch(re.sub(r"[-_]", "", key)) for key in item):
                return True
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str) and _SECRET_TEXT.search(item):
            return True
    return False


def _auth_notice(output):
    texts = [output.get("response", "")]
    if isinstance(output.get("content"), list):
        texts.extend(block.get("text", "") for block in output["content"] if isinstance(block, dict))
    return any(isinstance(text, str) and _AUTH_NOTICE.search(text) for text in texts)


def _wire_values(reader):
    """Find only final approved call values; never copy an ACP envelope out."""
    values = {}
    for entry in reader.wire_transcript:
        try:
            wire = base64.b64decode(entry["jsonlBase64"], validate=True)
            if len(wire) > MAX_FAILED_BYTES or sha256(wire).hexdigest() != entry["sha256"]:
                continue
            message = _strict_loads(wire)
            params = message.get("params", {})
            if not isinstance(params, dict) or params.get("sessionId") != reader.session_id:
                continue
            permission = message.get("method") == "session/request_permission"
            parent = "toolCall" if permission else "update"
            call = params.get(parent, {})
            if (not isinstance(call, dict) or not permission and (message.get("method") != "session/update"
                    or call.get("sessionUpdate") not in {"tool_call", "tool_call_update"})):
                continue
            call_id = call.get("toolCallId")
            if call_id not in reader.policy.approvals:
                continue
            final = reader.policy.calls.get(call_id, {})
            for name in ("rawInput", "rawOutput"):
                expected = final.get(name)
                if name == "rawInput" and expected is None:
                    expected = _strict_loads(reader.policy.approvals[call_id][1])
                if name in call and call[name] == expected:
                    values[(call_id, name)] = _member_bytes(wire, ("params", parent, name))
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
            continue
    return values


def build_failed_evidence(reader, error):
    """Bounded value-free assessment + eligible exact byte objects, no writes."""
    code = getattr(error, "code", None)
    code = code if code in _FAILURES else "FIGMA_MCP_COLLECTION_FAILED"
    reason = getattr(error, "evidence_reason", None)
    if reason is None and isinstance(getattr(error, "assessment", None), dict):
        reason = "DESIGN_CONTEXT_RETRY_REQUIRED"
    report = {"schemaVersion": 1, "kind": "FAILED_COLLECTION_EVIDENCE", "outcome": "TERMINAL_FAILURE",
              "failureCode": code, "reasonCode": reason if reason in _REASONS else "UNCLASSIFIED_FAILURE",
              "stage": reader.collection_stage if reader.collection_stage in _STAGES else "startup",
              "terminalBoundary": "managed-collection-exception", "jobTerminalStatusObserved": False,
              "resumable": False, "snapshotRegistered": False, "rawWirePreserved": False,
              "authOrSessionTrafficPreserved": False, "authoredCompleteness": "NOT_PROVEN", "toolEvidence": []}
    offload = _safe_offload_assessment(getattr(error, "offload_assessment", None))
    if offload is not None:
        report["offloadAssessment"] = offload
    if getattr(reader, "_metadata_barrier_used", False):
        report["sourceReadBarrier"] = safe_source_read_barrier(reader._metadata_barrier_receipt)
    objects, size = {}, 0
    wires = _wire_values(reader)

    def add(body, fidelity):
        nonlocal size
        digest = sha256(body).hexdigest()
        if digest not in objects:
            if size + len(body) > MAX_FAILED_BYTES:
                return {"status": "OMITTED", "code": "FAILED_EVIDENCE_BYTE_LIMIT"}
            objects[digest] = body
            size += len(body)
        return {"status": "PRESERVED", "sha256": digest, "bytes": len(body), "fidelity": fidelity}

    for call_id, call in list(reader.policy.calls.items())[:64]:
        approval = reader.policy.approvals.get(call_id)
        if not approval or reader.policy._schema_load(call):
            continue
        identity, stamp = approval
        inputs = call.get("rawInput", _strict_loads(stamp))
        if (identity[0] != reader.policy.server or identity[1] not in READ_TOOLS
                or not reader.policy._valid_input(identity, inputs)
                or json.dumps(inputs, sort_keys=True, separators=(",", ":")) != stamp):
            continue
        item = {"ordinal": len(report["toolEvidence"]), "callIdSha256": sha256(call_id.encode()).hexdigest(),
                "name": identity[1], "status": call.get("status") if call.get("status") in {"pending", "in_progress", "completed", "failed"} else "UNKNOWN",
                "codes": [], "input": {"status": "OMITTED", "code": "WIRE_VALUE_UNAVAILABLE"},
                "output": {"status": "OMITTED", "code": "WIRE_VALUE_UNAVAILABLE"}}
        report["toolEvidence"].append(item)
        output = call.get("rawOutput")
        normalized = call.get("_normalizedOutput")
        if not isinstance(normalized, dict) and isinstance(output, dict):
            normalized = output if isinstance(output.get("content"), list) else {"content": [{"type": "text", "text": output["response"]}],
                "isError": output.get("isError", False)} if isinstance(output.get("response"), str) else output
        if identity[1] == "get_design_context":
            item["assessment"] = assess_design_context_output(normalized)
            item["codes"] = item["assessment"]["codes"]
        if (set(inputs) - _INPUT_KEYS or _sensitive(inputs) or reader.failed_authorization or code in {"FIGMA_MCP_AUTH_REQUIRED", "FIGMA_MCP_AUTH_REVOKED",
                "FIGMA_MCP_CONNECTION_REVOKED", "KIRO_KEY_AUTH_REQUIRED", "KIRO_KEY_INVALID", "KIRO_KEY_MISSING"}
                or isinstance(output, dict) and (_sensitive(output) or _auth_notice(output))):
            item["input"] = item["output"] = {"status": "OMITTED", "code": "AUTH_OR_UNKNOWN_INPUT_ENVELOPE"}
            continue
        if isinstance(call.get("_normalizedOutput"), dict) and (_sensitive(call["_normalizedOutput"])
                or _auth_notice(call["_normalizedOutput"])):
            item["input"] = item["output"] = {"status": "OMITTED", "code": "AUTH_OR_UNKNOWN_INPUT_ENVELOPE"}
            continue
        raw = wires.get((call_id, "rawInput"))
        if raw is not None:
            item["input"] = add(raw, "EXACT_WIRE_JSON_VALUE_UTF8")
        if not isinstance(output, dict) or set(output) - _OUTPUT_KEYS:
            item["output"] = {"status": "OMITTED", "code": "OUTPUT_ENVELOPE_UNVERIFIED"}
            continue
        raw = wires.get((call_id, "rawOutput"))
        if raw is not None:
            item["output"] = add(raw, "EXACT_WIRE_JSON_VALUE_UTF8")
        # Offloaded text was verified by the unchanged adapter before any guard
        # rejected it. Preserve recovered content too, without its absolute path.
        if isinstance(call.get("_normalizedOutput"), dict):
            for block in call["_normalizedOutput"].get("content", []) if isinstance(call["_normalizedOutput"].get("content"), list) else []:
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                    item.setdefault("recoveredTextBlocks", []).append(add(block["text"].encode(), "EXACT_RECOVERED_CONTENT_UTF8"))
    report["preservedBytes"] = size
    report["objectCount"] = len(objects)
    report["byteLimit"] = MAX_FAILED_BYTES
    return report, objects


def write_private_object(root: Path, digest, body):
    """Content-addressed bytes with the connection store's ownership rules."""
    if sha256(body).hexdigest() != digest or not re.fullmatch(r"[a-f0-9]{64}", digest) or len(body) > MAX_FAILED_BYTES:
        raise ValueError
    _directory(root)
    path = root / (digest + ".bin")
    if path.exists() or path.is_symlink():
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                    or info.st_mode & 0o077 or info.st_size != len(body) or stream.read(len(body) + 1) != body):
                raise ValueError
        return
    temporary = root / ("." + digest + "." + uuid4().hex + ".part")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        temporary.unlink(missing_ok=True)

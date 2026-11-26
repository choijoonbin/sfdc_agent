"""Read-only readiness checks for recorded Figma MCP evidence.

Tool completion is a transport fact, not proof of usable design input. Provider
prose is untrusted data. This module never follows its instructions, calls a
tool, changes acquisition routes or declares MCP/REST source equivalence.
"""

from hashlib import sha256
import base64
import json
import math
import re
import struct
import zlib

_MAX_TEXT_BYTES = 16 * 1024 * 1024
_NODE = re.compile(r"^(?:I)?\d+[:-]\d+(?:;\d+[:-]\d+)*$")
_FENCE = re.compile(r"```(?:tsx?|jsx?|javascript|typescript|html|css|scss|json)?[^\n]*\n([\s\S]*?)```", re.IGNORECASE)
_MARKUP = re.compile(r"<(?:[a-z][a-z0-9-]*|[A-Z][A-Za-z0-9_.]*)(?=[\s/>])[^>]*>", re.DOTALL)
_DECLARATION = re.compile(r"(?:^|[\n;])\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?(?:function\s+\w+|(?:const|let|var)\s+\w+\s*=)")
_CSS = re.compile(r"(?:^|[\n}])\s*[.#][\w\s.#:[\]()>+~-]+\s*\{[^}]*\b(?:display|position|width|height|padding|margin|font-size)\s*:", re.IGNORECASE)
_CODE_LAYOUT = re.compile(r"\b(?:className|class)\s*=\s*[^\n]*(?:\b(?:flex|grid|absolute|relative|w-|h-|p-|px-|py-|gap-))"
                          r"|\b(?:width|height|display|position|padding|gap|gridTemplateColumns)\s*:", re.IGNORECASE)
_CODE_CONNECT_NOTICE = re.compile(r"\bcode\s*connect\b", re.IGNORECASE)
_ACTION_NOTICE = re.compile(r"\b(?:please\s+ask|ask\s+the\s+user|would\s+you\s+like|if\s+(?:the\s+)?user\s+(?:answers?|says?)|call\s+get_design_context\s+again)\b", re.IGNORECASE)
_SIZE_NOTICE = re.compile(r"^(?:(?:error|warning|notice|important)\s*:\s*)?(?:(?:the|this|requested)\s+)?"
                          r"(?:response|output|result|node|design\s+context|content)\b", re.IGNORECASE)
_INCOMPLETE = re.compile(r"\b(?:truncated|incomplete|omitted)\b|\btoo\s+(?:large|long)\b", re.IGNORECASE)
_INCOMPLETE_CONTEXT = re.compile(r"\b(?:output|response|token|context)\s+(?:size\s+)?(?:limit|budget)\b"
                                 r"|\bget_(?:metadata|design_context)\b|\b(?:smaller|individual)\s+(?:child\s+)?nodes?\b", re.IGNORECASE)


def _digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _source_shape(text):
    """Observe bounded source syntax; it is not proof of correct authored data."""
    markup = bool(_MARKUP.search(text)) and not bool(re.match(r"^\s*<(?:frame|instance|text|group)\s+id=", text))
    declaration = bool(_DECLARATION.search(text))
    css = bool(_CSS.search(text))
    structured = False
    geometry = bool(_CODE_LAYOUT.search(text)) and (markup or declaration or css)
    if text.lstrip().startswith(("{", "[")):
        try:
            value = json.loads(text)
            pending = [value]
            visited = 0
            while pending and visited < 10_000:
                node = pending.pop()
                visited += 1
                if isinstance(node, dict):
                    bounds = node.get("absoluteBoundingBox", node.get("bounds"))
                    if isinstance(bounds, dict) and all(isinstance(bounds.get(k), (int, float)) and not isinstance(bounds.get(k), bool)
                                                        and math.isfinite(bounds[k]) for k in ("x", "y", "width", "height")):
                        structured = structured or isinstance(node.get("type"), str) and isinstance(node.get("id"), str)
                        geometry = geometry or structured
                    pending.extend(v for v in node.values() if isinstance(v, (dict, list)))
                elif isinstance(node, list):
                    pending.extend(node)
        except (ValueError, TypeError):
            pass
    return markup or declaration or css or structured, geometry


def _has_png(output):
    """Require original bounded PNG chunks, not a text URL or model assertion."""
    blocks = output.get("content")
    if not isinstance(blocks, list):
        return False
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "image" or block.get("mimeType") != "image/png":
            continue
        data = block.get("data")
        if not isinstance(data, str) or len(data) > 16 * 1024 * 1024:
            continue
        try:
            image = base64.b64decode(data, validate=True)
            if len(image) < 45 or image[:8] != b"\x89PNG\r\n\x1a\n" or image[12:16] != b"IHDR":
                continue
            width, height = struct.unpack(">II", image[16:24])
            if not (0 < width <= 65536 and 0 < height <= 65536):
                continue
            offset, data_seen = 8, False
            while offset + 12 <= len(image):
                length = struct.unpack(">I", image[offset:offset + 4])[0]
                end = offset + length + 12
                if end > len(image):
                    break
                kind = image[offset + 4:offset + 8]
                payload = image[offset + 8:offset + 8 + length]
                crc = struct.unpack(">I", image[offset + 8 + length:end])[0]
                if zlib.crc32(kind + payload) & 0xffffffff != crc:
                    break
                data_seen = data_seen or kind == b"IDAT"
                if kind == b"IEND":
                    if not length and end == len(image) and data_seen:
                        return True
                    break
                offset = end
        except (ValueError, TypeError, struct.error):
            continue
    return False


def assess_design_context_output(output):
    """Return a value-free diagnostic; no prose/asset URL is copied into it.

    Explicit notice-only output must be retried even if isError is false.
    A syntax observation is kept separate from actual authored completeness.
    """
    codes = []
    if not isinstance(output, dict) or not isinstance(output.get("content"), list):
        return {"schemaVersion": 1, "status": "RETRY_REQUIRED", "codes": ["DESIGN_CONTEXT_FORMAT_UNVERIFIED"],
                "implementationEvidence": "NOT_PROVIDED", "geometryEvidence": "NOT_PROVIDED",
                "authoredCompleteness": "NOT_PROVEN", "providerInstructionsExecuted": False}
    texts = [block["text"] for block in output["content"] if isinstance(block, dict)
             and block.get("type") == "text" and isinstance(block.get("text"), str)]
    text_bytes = sum(len(text.encode()) for text in texts)
    if output.get("isError") is True:
        codes.append("DESIGN_CONTEXT_PROVIDER_ERROR")
    if any(output.get(key) is True for key in ("truncated", "isTruncated", "hasMore")):
        codes.append("DESIGN_CONTEXT_DECLARED_TRUNCATED")
    if not any(text.strip() for text in texts):
        codes.append("DESIGN_CONTEXT_EMPTY")
    if text_bytes > _MAX_TEXT_BYTES:
        codes.append("DESIGN_CONTEXT_BUDGET_EXCEEDED")
    code_count = layout_count = notice_count = 0
    if text_bytes <= _MAX_TEXT_BYTES:
        for text in texts:
            fragments = _FENCE.findall(text)
            # Preserve the complete content; fences only select syntax evidence.
            # Footer instructions cannot create code or geometry observations.
            code, geometry = _source_shape(text) if not fragments else (
                any(_source_shape(fragment)[0] for fragment in fragments),
                any(_source_shape(fragment)[1] for fragment in fragments))
            code_count += int(code)
            layout_count += int(geometry)
            prose = _FENCE.sub("", text).strip()
            if _CODE_CONNECT_NOTICE.search(prose) and _ACTION_NOTICE.search(prose):
                notice_count += 1
            if (len(prose) <= 4096 and _SIZE_NOTICE.match(prose) and _INCOMPLETE.search(prose) and _INCOMPLETE_CONTEXT.search(prose)
                    and not any(marker in prose for marker in ("{", "}", "<", ">"))):
                codes.append("DESIGN_CONTEXT_PROVIDER_INCOMPLETE")
    if not code_count:
        codes.append("DESIGN_CONTEXT_GUIDANCE_ONLY" if notice_count else "DESIGN_CONTEXT_IMPLEMENTATION_NOT_OBSERVED")
    if not layout_count:
        codes.append("DESIGN_CONTEXT_GEOMETRY_NOT_OBSERVED")
    return {"schemaVersion": 1, "status": "RETRY_REQUIRED" if codes else "CONTEXT_OBSERVED",
            "codes": sorted(set(codes)), "implementationEvidence": "OBSERVED" if code_count else "NOT_PROVIDED",
            "geometryEvidence": "OBSERVED" if layout_count else "NOT_PROVIDED",
            "textBlockCount": len(texts), "textUtf8Bytes": text_bytes,
            "textSha256": _digest(texts), "implementationBlockCount": code_count,
            "geometryBlockCount": layout_count, "providerGuidanceBlockCount": notice_count,
            "authoredCompleteness": "NOT_PROVEN", "providerInstructionsExecuted": False}


def assess_mcp_acquisition_readiness(tool_evidence, *, file_key, node_id, metadata_context=None):
    """Check the selected raw route and its explicit metadata projection only.

    Metadata labels cannot stand in for text characters. Parent-relative metadata
    coordinates also do not establish REST coordinate equivalence. The caller
    must keep all raw tools and pass the context projected from this receipt.
    """
    canonical = lambda value: value.replace("-", ":") if isinstance(value, str) and len(value) <= 200 and _NODE.fullmatch(value) else None
    selected = canonical(node_id)
    codes = []
    summaries = []
    if (not selected or not isinstance(file_key, str) or not re.fullmatch(r"[A-Za-z0-9]{1,160}", file_key)
            or not isinstance(tool_evidence, list) or len(tool_evidence) > 64):
        codes.append("ACQUISITION_SELECTION_UNVERIFIED")
        tool_evidence = []
    root_calls = {}
    for call in tool_evidence:
        if not isinstance(call, dict) or call.get("name") not in {"get_metadata", "get_design_context", "get_screenshot"}:
            continue
        inputs = call.get("input")
        if not isinstance(inputs, dict) or inputs.get("fileKey") != file_key or canonical(inputs.get("nodeId")) != selected:
            continue
        name = call["name"]
        if call.get("status") != "completed" or not isinstance(call.get("output"), dict) or call["output"].get("isError") is True:
            codes.append("ACQUISITION_REQUIRED_TOOL_INCOMPLETE")
            continue
        if any(call["output"].get(key) is True for key in ("truncated", "isTruncated", "hasMore")):
            codes.append("ACQUISITION_REQUIRED_TOOL_TRUNCATED")
        root_calls.setdefault(name, []).append(call["output"])
        if name == "get_design_context":
            summaries.append(assess_design_context_output(call["output"]))
    for name in ("get_metadata", "get_design_context", "get_screenshot"):
        if not root_calls.get(name):
            codes.append({"get_metadata": "ACQUISITION_METADATA_MISSING", "get_design_context": "ACQUISITION_DESIGN_CONTEXT_MISSING",
                          "get_screenshot": "ACQUISITION_SCREENSHOT_MISSING"}[name])
    if not any(summary["status"] == "CONTEXT_OBSERVED" for summary in summaries):
        codes.append("ACQUISITION_DESIGN_CONTEXT_RETRY_REQUIRED")
    if root_calls.get("get_screenshot") and not any(_has_png(output) for output in root_calls["get_screenshot"]):
        codes.append("ACQUISITION_SCREENSHOT_PNG_UNVERIFIED")
    nodes = metadata_context.get("nodes") if isinstance(metadata_context, dict) else None
    root_found = False
    valid_geometry = missing_geometry = 0
    if not isinstance(nodes, list) or not nodes or len(nodes) > 5000:
        codes.append("ACQUISITION_METADATA_CONTEXT_UNVERIFIED")
        nodes = []
    seen, parents = set(), {}
    for node in nodes:
        if not isinstance(node, dict):
            codes.append("ACQUISITION_METADATA_CONTEXT_UNVERIFIED")
            continue
        identity = canonical(node.get("id"))
        if not identity or identity in seen:
            codes.append("ACQUISITION_METADATA_ID_UNVERIFIED")
        seen.add(identity)
        parents[identity] = canonical(node.get("parentId")) if node.get("parentId") is not None else None
        root_found = root_found or identity == selected and node.get("parentId") is None
        if node.get("visible") is False:
            if identity == selected:
                codes.append("ACQUISITION_METADATA_ROOT_GEOMETRY_INVALID")
            continue
        box = node.get("metadataBounds")
        valid = isinstance(box, dict) and all(isinstance(box.get(k), (int, float)) and not isinstance(box.get(k), bool)
                                            and math.isfinite(box[k]) for k in ("x", "y", "width", "height"))
        valid = valid and box["width"] >= 0 and box["height"] >= 0
        if identity == selected and (not valid or not box["width"] or not box["height"] or node.get("visible") is False):
            codes.append("ACQUISITION_METADATA_ROOT_GEOMETRY_INVALID")
        valid_geometry += int(valid)
        missing_geometry += int(not valid)
    if not root_found:
        codes.append("ACQUISITION_METADATA_SELECTED_ROOT_MISSING")
    for identity, parent in parents.items():
        if identity != selected and (not parent or parent not in seen):
            codes.append("ACQUISITION_METADATA_PARENT_UNVERIFIED")
    resolved = set()
    for identity in parents:
        path = set()
        while identity is not None and identity not in resolved:
            if identity in path:
                codes.append("ACQUISITION_METADATA_HIERARCHY_CYCLE")
                break
            path.add(identity)
            identity = parents.get(identity)
        resolved.update(path)
    if missing_geometry:
        codes.append("ACQUISITION_METADATA_GEOMETRY_INCOMPLETE")
    return {"schemaVersion": 1, "purpose": "mcp-acquisition-readiness", "status": "RETRY_REQUIRED" if codes else "INPUTS_OBSERVED",
            "readyForAnalysis": not codes, "codes": sorted(set(codes)), "selectedNodeId": selected,
            "metadata": {"nodeCount": len(nodes), "geometryObserved": valid_geometry, "geometryNotProvided": missing_geometry,
                         "coordinateEquivalence": "UNVERIFIED", "authoredTextCompleteness": "NOT_PROVEN"},
            "designContexts": summaries, "originalFigmaRevision": "UNVERIFIED", "sameRevision1xPng": "NOT_PROVEN",
            "rawToolEvidenceSha256": _digest(tool_evidence), "authoredCompleteness": "NOT_PROVEN",
            "changesAcquisitionRoute": False, "providerInstructionsExecuted": False}


def require_design_context_content(output):
    """Guard notice-only results without upgrading other unproven source input.

    This bounded hard guard is intentionally narrower than readiness: consumers
    may record arbitrary original text, but explicit tool instructions alone can
    never be accepted as successful design source. Readiness remains separate.
    """
    assessment = assess_design_context_output(output)
    fatal = {"DESIGN_CONTEXT_PROVIDER_ERROR", "DESIGN_CONTEXT_DECLARED_TRUNCATED", "DESIGN_CONTEXT_EMPTY",
             "DESIGN_CONTEXT_FORMAT_UNVERIFIED", "DESIGN_CONTEXT_BUDGET_EXCEEDED", "DESIGN_CONTEXT_PROVIDER_INCOMPLETE",
             "DESIGN_CONTEXT_GUIDANCE_ONLY"}
    if fatal.intersection(assessment["codes"]):
        from .kiro_headless import SessionError
        error = SessionError("FIGMA_MCP_EVIDENCE_INCOMPLETE", "Figma MCP가 실제 디자인 원문 대신 안내 또는 불완전한 응답을 반환했습니다. 디자인 원문을 다시 수집해 주세요.")
        error.assessment = assessment
        raise error
    return assessment

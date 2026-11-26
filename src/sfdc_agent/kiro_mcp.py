"""A bounded ACP v3 Figma reader with argument-scoped one-time consent.

This is deliberately separate from tool-free analysis/generation.  Only raw tool
results become evidence; the model's narration never becomes original Figma data.
"""

import asyncio
import base64
from hashlib import sha256
import json
import math
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import zlib
from time import monotonic
import xml.etree.ElementTree as ET

from .figma_mcp_connections import PROFILE_NAME, READ_TOOLS, kiro_environment
from .kiro_headless import SessionError, _settle_cleanup, _terminate
from .kiro_processes import OwnedProcesses
from .prompt_templates import PromptTemplate, load_baseline_templates
from .figma_mcp_secrets import McpSecretStore
from .provider_capabilities import cli_identity, model_identifier

_MAX_BYTES = 64 * 1024 * 1024
_MAX_CALLS = 64
_MAX_NODES = 5000
_NODE = re.compile(r"^(?:[0-9]+:[0-9]+|I[0-9]+:[0-9]+(?:;[0-9]+:[0-9]+)*)$")
_FILE = re.compile(r"^[A-Za-z0-9]{1,160}$")
logger = logging.getLogger(__name__)


def _error(code, message):
    return SessionError(code, message)


def _png_dimensions(image):
    if image[:8] != b"\x89PNG\r\n\x1a\n" or image[12:16] != b"IHDR" or len(image) < 45:
        raise ValueError
    width, height = struct.unpack(">II", image[16:24])
    if not (0 < width <= 65536 and 0 < height <= 65536):
        raise ValueError
    offset, data_seen = 8, False
    while offset + 12 <= len(image):
        length = struct.unpack(">I", image[offset:offset + 4])[0]
        end = offset + 12 + length
        if end > len(image):
            raise ValueError
        kind = image[offset + 4:offset + 8]
        payload = image[offset + 8:offset + 8 + length]
        crc = struct.unpack(">I", image[offset + 8 + length:end])[0]
        if zlib.crc32(kind + payload) & 0xffffffff != crc:
            raise ValueError
        data_seen = data_seen or kind == b"IDAT"
        if kind == b"IEND":
            if length != 0 or end != len(image) or not data_seen:
                raise ValueError
            return width, height
        offset = end
    raise ValueError


def _host_identity(value):
    meta = value.get("_meta", {}).get("kiro", {}).get("mcpTool", {})
    identity = meta.get("identity", {})
    if meta.get("version") != 1 or not isinstance(identity, dict):
        return None
    return identity.get("serverName"), identity.get("toolName")


def _text_blocks(output):
    if not isinstance(output, dict):
        return []
    return [item["text"] for item in output.get("content", [])
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)]


_NOTICE_START = re.compile(
    r"^(?:(?:error|warning|notice|important)\s*:\s*)?(?:(?:the|this|requested)\s+)?"
    r"(?:response|output|result|node|design\s+context|content)\b", re.IGNORECASE)
_NOTICE_INCOMPLETE = re.compile(r"\btoo\s+(?:large|long)\b|\b(?:truncated|incomplete|omitted)\b", re.IGNORECASE)
_NOTICE_CONTEXT = re.compile(
    r"\b(?:output|response|token|context)\s+(?:size\s+)?(?:limit|budget)\b"
    r"|\bget_(?:metadata|design_context)\b|\b(?:smaller|individual)\s+(?:child\s+)?nodes?\b", re.IGNORECASE)


def validate_design_context_output(output):
    """Reject explicit provider notices, never words inside authored code/copy.

    An unmarked omission is still unknown. This only recognizes a bounded,
    standalone prose envelope, not arbitrary source text or generated code.
    """
    if not isinstance(output, dict) or any(output.get(key) is True for key in ("truncated", "isTruncated", "hasMore")):
        raise _error("FIGMA_MCP_EVIDENCE_INCOMPLETE", "잘린 Figma 디자인 원문은 생성 근거로 사용할 수 없습니다.")
    texts = _text_blocks(output)
    if not any(text.strip() for text in texts):
        raise _error("FIGMA_MCP_EVIDENCE_INCOMPLETE", "Figma 디자인 컨텍스트 원문이 비어 있습니다.")
    for text in texts:
        notice = text.strip()
        if (len(notice) > 4096 or not _NOTICE_START.match(notice)
                or any(marker in notice for marker in ("```", "~~~", "{", "}", "<", ">"))):
            continue
        if _NOTICE_INCOMPLETE.search(notice) and _NOTICE_CONTEXT.search(notice):
            raise _error("FIGMA_MCP_EVIDENCE_INCOMPLETE", "Figma 공급자가 원문 잘림 또는 크기 제한을 알렸습니다. 더 작은 원본 범위로 다시 수집해 주세요.")


def _metadata_root(text):
    # Figma appends plain tool guidance after the XML document. Discover only
    # the first complete XML root; the full untouched text stays in evidence.
    parser = ET.XMLPullParser(events=("start", "end"))
    root, depth = None, 0
    for line in text.splitlines(keepends=True):
        parser.feed(line)
        for event, node in parser.read_events():
            if event == "start":
                root = root if root is not None else node
                depth += 1
            else:
                depth -= 1
                if depth == 0:
                    return root
    raise ET.ParseError("Incomplete metadata root.")


def _tool_content(call, *, home=None, session_id=None):
    output = call.get("rawOutput")
    if not isinstance(output, dict):
        raise _error("FIGMA_MCP_EVIDENCE_INCOMPLETE", "Figma MCP 원문 응답이 없습니다.")
    transformation = call.get("_meta", {}).get("kiro", {}).get("outputTransformation")
    if transformation is not None and transformation.get("kind") != "offloaded":
        raise _error("FIGMA_MCP_EVIDENCE_INCOMPLETE", "변환되거나 잘린 MCP 응답의 원문을 확인해야 합니다.")
    if isinstance(output.get("content"), list) and transformation is None:
        return output
    # Installed Kiro 3.6.2 preserves text and inline images in this wrapper.
    # The raw wrapper stays in the transcript; this is a lossless content adapter.
    response, images = output.get("response"), output.get("imageBase64Urls")
    if not isinstance(response, str) or not isinstance(images, list):
        raise _error("FIGMA_MCP_EVIDENCE_INCOMPLETE", "지원하지 않는 Kiro MCP 원문 응답 형식입니다.")
    receipt = {"transportAdapter": "kiro-acp-v3-wrapper", "offloaded": transformation is not None}
    if transformation is not None:
        try:
            path = Path(transformation["absFilePath"])
            root = Path(home) / ".kiro" / "sessions"
            relative = path.relative_to(root)
            if (not isinstance(session_id, str) or len(relative.parts) != 4 or relative.parts[1] != session_id
                    or relative.parts[2] != "tool-outputs" or path.resolve(strict=True) != path):
                raise ValueError
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                        or info.st_size > 8 * 1024 * 1024):
                    raise ValueError
                body = source.read(8 * 1024 * 1024 + 1)
            full = body.decode("utf-8", errors="strict")
            # JS reports UTF-16 code units, not UTF-8 bytes or Python codepoints.
            chars = len(full.encode("utf-16-le")) // 2
            if len(body) != info.st_size or chars != transformation.get("totalChars"):
                raise ValueError
            response = full
            receipt.update({"offloadSha256": sha256(body).hexdigest(), "offloadBytes": len(body), "utf16Chars": chars})
        except (ValueError, TypeError, OSError, KeyError, UnicodeError):
            raise _error("FIGMA_MCP_EVIDENCE_INCOMPLETE", "Kiro가 분리 저장한 Figma 원문의 경로·크기·전체 길이를 확인할 수 없습니다.") from None
    content = [{"type": "text", "text": response}]
    for uri in images:
        if not isinstance(uri, str) or not uri.startswith("data:image/") or ";base64," not in uri:
            raise _error("FIGMA_MCP_EVIDENCE_INCOMPLETE", "Figma MCP 캡처 원본 바이트가 없습니다.")
        header, data = uri.split(",", 1)
        mime = header.removeprefix("data:").removesuffix(";base64")
        if mime not in {"image/png", "image/jpeg", "image/webp"} or len(data) > 16 * 1024 * 1024:
            raise _error("FIGMA_MCP_EVIDENCE_INCOMPLETE", "Figma MCP 캡처 형식 또는 크기를 확인해 주세요.")
        try:
            decoded = base64.b64decode(data, validate=True)
            if not decoded:
                raise ValueError
        except ValueError:
            raise _error("FIGMA_MCP_EVIDENCE_INCOMPLETE", "Figma MCP 캡처 데이터가 올바르지 않습니다.") from None
        content.append({"type": "image", "mimeType": mime, "data": data})
    return {"content": content, "isError": output.get("isError", False), "_sfdcCaptureReceipt": receipt}


class FigmaToolPolicy:
    def __init__(self, server, file_key, node_id, *, home=None):
        if not _FILE.fullmatch(file_key) or not _NODE.fullmatch(node_id):
            raise _error("FIGMA_MCP_INVALID_INPUT", "Figma 파일과 노드 ID를 확인해 주세요.")
        self.server, self.file_key, self.node_id = server, file_key, node_id
        self.nodes = {node_id}
        self.approvals = {}
        self.calls = {}
        self.transcript = []
        self.home, self.session_id = home, None

    def _schema_load(self, call):
        inputs = call.get("rawInput")
        origin = call.get("_meta", {}).get("kiro", {}).get("toolOrigin")
        return (origin == "default" and isinstance(inputs, dict) and set(inputs) == {"id"}
                and inputs["id"] in {f"tool::{self.server}::{name}" for name in READ_TOOLS})

    def _valid_input(self, identity, inputs):
        return (isinstance(identity, tuple) and identity[0] == self.server and identity[1] in READ_TOOLS
                and isinstance(inputs, dict) and inputs.get("fileKey") == self.file_key
                and inputs.get("nodeId") in self.nodes)

    def permission(self, params):
        call = params.get("toolCall", {})
        call_id = call.get("toolCallId")
        identity = _host_identity(params)
        inputs = call.get("rawInput")
        existing = self.calls.get(call_id, {})
        if inputs is None:
            inputs = existing.get("rawInput")
        allowed = (isinstance(call_id, str) and len(self.approvals) < _MAX_CALLS
                   and self._valid_input(identity, inputs))
        option = next((v for v in params.get("options", []) if isinstance(v, dict)
                       and v.get("kind") == ("allow_once" if allowed else "reject_once")
                       and isinstance(v.get("optionId"), str)), None)
        if allowed and option:
            stamp = json.dumps(inputs, sort_keys=True, separators=(",", ":"))
            previous = self.approvals.get(call_id)
            if previous and previous != (identity, stamp):
                allowed = False
                option = None
            else:
                self.approvals[call_id] = (identity, stamp)
        self.transcript.append({"event": "permission", "callId": call_id, "allowed": bool(allowed and option)})
        if option:
            return {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
        return {"outcome": {"outcome": "cancelled"}}

    def update(self, value):
        if value.get("sessionUpdate") not in {"tool_call", "tool_call_update"}:
            return
        call_id = value.get("toolCallId")
        if not isinstance(call_id, str) or len(self.calls) >= _MAX_CALLS and call_id not in self.calls:
            raise _error("FIGMA_MCP_EVIDENCE_INCOMPLETE", "Figma MCP 도구 호출 범위를 확인할 수 없습니다.")
        call = self.calls.setdefault(call_id, {})
        for key, item in value.items():
            if item is not None:
                call[key] = item
        # Retain the tool event as received, not reconstructed model text.
        self.transcript.append({"event": "tool", "value": value})
        if call.get("status") == "failed":
            output = call.get("rawOutput", {})
            message = output.get("response", "") if isinstance(output, dict) else ""
            if (call_id in self.approvals and isinstance(message, str)
                    and "Figma MCP tool call limit" in message):
                raise _error("FIGMA_MCP_RATE_LIMITED", "Figma MCP 호출 한도에 도달했습니다. 계정의 시트·플랜과 한도를 확인해 주세요.")
            raise _error("FIGMA_MCP_EVIDENCE_INCOMPLETE", "Figma MCP 도구가 실패하여 원본 수집을 완료하지 못했습니다.")
        if call.get("status") == "completed" and "rawOutput" in call:
            if self._schema_load(call):
                # Kiro 3.6.2 lazily loads the configured MCP reader's schema.
                # It is infrastructure, not Figma source or arbitrary builtin
                # permission. No displayed tool title is used as authority.
                return
            approval = self.approvals.get(call_id)
            if not approval:
                raise _error("FIGMA_MCP_TOOL_SCOPE_DENIED", "승인되지 않은 Figma MCP 도구 호출이 감지됐습니다.")
            identity, stamp = approval
            if json.dumps(call.get("rawInput"), sort_keys=True, separators=(",", ":")) != stamp:
                raise _error("FIGMA_MCP_TOOL_SCOPE_DENIED", "Figma MCP 도구 입력이 승인 범위와 다릅니다.")
            output = call["rawOutput"]
            if not isinstance(output, dict) or output.get("isError") is True:
                raise _error("FIGMA_MCP_EVIDENCE_INCOMPLETE", "Figma MCP 원문 응답을 완전히 확보하지 못했습니다.")
            normalized = _tool_content(call, home=self.home, session_id=self.session_id)
            call["_normalizedOutput"] = normalized
            if identity[1] == "get_design_context":
                validate_design_context_output(normalized)
            if identity[1] == "get_metadata":
                self._discover_nodes(normalized)

    def _discover_nodes(self, output):
        for text in _text_blocks(output):
            # Metadata is parsed as XML only. An LLM summary cannot add authorized
            # node IDs, and XML entities/declarations are rejected before parsing.
            if "<!" in text or len(text) > 4 * 1024 * 1024:
                continue
            try:
                root = _metadata_root(text)
            except ET.ParseError:
                continue
            ids = {item.attrib.get("id") for item in root.iter() if _NODE.fullmatch(item.attrib.get("id", ""))}
            if root.attrib.get("id") in self.nodes and len(self.nodes | ids) <= _MAX_NODES:
                self.nodes.update(ids)

    def evidence(self):
        result = []
        for call_id, call in self.calls.items():
            if self._schema_load(call):
                continue
            approval = self.approvals.get(call_id)
            if not approval or call.get("status") != "completed" or not isinstance(call.get("rawOutput"), dict):
                raise _error("FIGMA_MCP_EVIDENCE_INCOMPLETE", "완료되지 않았거나 잘린 Figma MCP 호출이 있습니다.")
            identity, _ = approval
            output = call.get("_normalizedOutput") or _tool_content(call, home=self.home, session_id=self.session_id)
            if not isinstance(output.get("content"), list) or output.get("isError") is True:
                raise _error("FIGMA_MCP_EVIDENCE_INCOMPLETE", "Figma MCP 원문 콘텐츠 형식을 확인할 수 없습니다.")
            if any(output.get(key) is True for key in ("truncated", "isTruncated", "hasMore")):
                raise _error("FIGMA_MCP_EVIDENCE_INCOMPLETE", "잘린 Figma MCP 원문은 생성 근거로 사용할 수 없습니다.")
            if identity[1] == "get_design_context":
                validate_design_context_output(output)
            result.append({"callId": call_id, "name": identity[1], "status": "completed",
                           "input": call["rawInput"], "output": output})
        required = {"get_metadata", "get_design_context", "get_screenshot"}
        root_tools = {call["name"] for call in result if call["input"]["nodeId"] == self.node_id}
        if not required <= root_tools:
            raise _error("FIGMA_MCP_EVIDENCE_INCOMPLETE", "화면 메타데이터·디자인 원문·캡처를 모두 확보해야 합니다.")
        for call in result:
            if call["input"]["nodeId"] != self.node_id:
                continue
            blocks = call["output"]["content"]
            if call["name"] == "get_screenshot":
                image_valid = False
                for block in blocks:
                    if not isinstance(block, dict) or block.get("type") != "image" or block.get("mimeType") != "image/png":
                        continue
                    try:
                        if not isinstance(block.get("data"), str) or len(block["data"]) > 16 * 1024 * 1024:
                            raise ValueError
                        image = base64.b64decode(block["data"], validate=True)
                        _png_dimensions(image)
                        image_valid = True
                    except (ValueError, struct.error):
                        continue
                    if image_valid:
                        break
                if not image_valid:
                    raise _error("FIGMA_MCP_EVIDENCE_INCOMPLETE", "Figma MCP 캡처의 원본 PNG 바이트를 확인할 수 없습니다.")
            if call["name"] == "get_metadata":
                valid_root = False
                for text in _text_blocks(call["output"]):
                    if "<!" in text:
                        continue
                    try:
                        root = _metadata_root(text)
                        dimensions = [float(root.attrib.get(key, "nan")) for key in ("width", "height")]
                        valid_root = root.attrib.get("id") == self.node_id and all(math.isfinite(n) and n > 0 for n in dimensions)
                    except (ET.ParseError, ValueError):
                        continue
                    if valid_root:
                        break
                if not valid_root:
                    raise _error("FIGMA_MCP_EVIDENCE_INCOMPLETE", "요청한 Figma 노드의 구조와 크기를 확인할 수 없습니다.")
        return result


class KiroMcpReader:
    def __init__(self, *, home, work, server, file_key, node_id, key, templates=None, prompt_release_id=None,
                 connection_scope=None, open_external_url=None):
        self.home, self.work, self.server, self.key = home, work, server, key
        self.policy = FigmaToolPolicy(server, file_key, node_id, home=home)
        self.session_id = None
        self.connected = False
        self.failed_authorization = False
        self._counter = 0
        self._bytes = 0
        self._process = None
        self._owned = None
        self.templates = templates
        self.prompt_release_id = prompt_release_id
        self.secrets = McpSecretStore(connection_scope, home) if connection_scope else None
        self.open_external_url = open_external_url
        self.selected_model_option = None
        self.wire_transcript = []
        self._last_wire = None
        self._extensions = set()

    async def _send(self, value):
        self._process.stdin.write((json.dumps(value, separators=(",", ":")) + "\n").encode())
        await self._process.stdin.drain()

    async def _read(self):
        while True:
            if self.secrets:
                self.secrets.validate()
            try:
                line = await asyncio.wait_for(self._process.stdout.readline(), timeout=1)
                break
            except TimeoutError:
                await self._owned.observe()
                if self._process.returncode is not None:
                    raise _error("FIGMA_MCP_TRANSPORT_FAILED", "Figma MCP 수집 연결이 종료됐습니다.") from None
        self._bytes += len(line)
        if not line or self._bytes > _MAX_BYTES:
            raise _error("FIGMA_MCP_EVIDENCE_INCOMPLETE", "Figma MCP 원문 응답이 종료되었거나 허용 크기를 초과했습니다.")
        try:
            value = json.loads(line)
            if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
                raise ValueError
            self._last_wire = line
            return value
        except (ValueError, RecursionError):
            raise _error("FIGMA_MCP_TRANSPORT_INVALID", "Figma MCP 전송 형식을 확인할 수 없습니다.") from None

    def _mcp_status(self, value):
        # V3 publishes a whole status snapshot. Unknown shapes stay unverified;
        # they must never authorize model execution just because startup returned.
        def visit(item):
            if isinstance(item, dict):
                name = item.get("name", item.get("serverName"))
                if name == self.server:
                    if item.get("failedAuthorization") is True:
                        self.failed_authorization = True
                    if item.get("status") == "connected" or item.get("state") == "connected":
                        self.connected = True
                for child in item.values():
                    visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)
        visit(value)

    async def _dispatch(self, message):
        method, params = message.get("method"), message.get("params", {})
        if not isinstance(params, dict):
            raise _error("FIGMA_MCP_TRANSPORT_INVALID", "Figma MCP 전송 입력이 올바르지 않습니다.")
        update = params.get("update", {})
        if (method == "session/request_permission" or method == "session/update" and isinstance(update, dict)
                and update.get("sessionUpdate") in {"tool_call", "tool_call_update"}):
            if self._last_wire:
                self.wire_transcript.append({"sha256": sha256(self._last_wire).hexdigest(),
                                             "jsonlBase64": base64.b64encode(self._last_wire).decode()})
        if method == "session/update":
            if self.session_id is None or params.get("sessionId") == self.session_id:
                update = params.get("update", {})
                self.policy.update(update)
                if update.get("sessionUpdate") == "config_option_update":
                    for option in update.get("configOptions", []):
                        if isinstance(option, dict) and option.get("id") == "model":
                            self.selected_model_option = model_identifier(option.get("currentValue"))
        elif method == "_kiro/mcp/status":
            self._mcp_status(params)
        if "id" in message and method:
            if method in {"_kiro/secret/get", "_kiro/secret/store", "_kiro/secret/delete"} and self.secrets:
                result = self.secrets.request(method, params)
                await self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})
            elif method == "_kiro/openExternalUrl" and self.open_external_url:
                await self.open_external_url(params)
                await self._send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
            elif method == "session/request_permission" and params.get("sessionId") == self.session_id:
                result = self.policy.permission(params)
                await self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})
            else:
                # No fs/terminal/auth callback/network capability is exposed to
                # headless collection. First OAuth approval is a visible CLI step.
                await self._send({"jsonrpc": "2.0", "id": message["id"],
                                  "error": {"code": -32601, "message": "Client capability is disabled."}})

    async def _rpc(self, method, params):
        self._counter += 1
        request_id = self._counter
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            message = await self._read()
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise _error("FIGMA_MCP_TRANSPORT_FAILED", "Kiro MCP 요청을 완료하지 못했습니다.")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise _error("FIGMA_MCP_TRANSPORT_INVALID", "Kiro MCP 응답 형식이 올바르지 않습니다.")
                return result
            await self._dispatch(message)

    async def _delete_session(self):
        if not self.session_id or "_kiro/session/delete" not in self._extensions or self._process.returncode is not None:
            return
        try:
            async with asyncio.timeout(3):
                await self._rpc("_kiro/session/delete", {"sessionId": self.session_id})
        except (SessionError, TimeoutError, OSError):
            # Preserve source/cancellation as the primary outcome. Cleanup is
            # explicit and bounded; source evidence and the OAuth vault are
            # separate from provider-owned session history.
            logger.warning("Figma MCP provider session cleanup could not be confirmed")

    async def collect(self, timeout=180, *, readiness_only=False, authorize=False):
        binary = os.environ.get("NOVA_KIRO_CLI") or shutil.which("kiro-cli")
        if not binary:
            raise _error("KIRO_UNAVAILABLE", "Kiro CLI 실행 파일을 찾지 못했습니다.")
        started = monotonic()
        primary = None
        try:
            async with asyncio.timeout(timeout):
                creation = asyncio.create_task(asyncio.create_subprocess_exec(
                    binary, "acp", "--agent-engine=v3", cwd=self.work,
                    env=kiro_environment(self.home, self.key), start_new_session=True,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    limit=8 * 1024 * 1024))
                try:
                    self._process = await asyncio.shield(creation)
                except asyncio.CancelledError as cancelled:
                    # A cancellation between OS spawn and returned Process must
                    # still take ownership and reap the child group.
                    self._process = await _settle_cleanup(creation, cancelled)
                    raise
                self._owned = OwnedProcesses(self._process, self.work)
                initialized = await self._rpc("initialize", {"protocolVersion": 1,
                    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False,
                        "_meta": {"kiro": {"secretStorage": self.secrets is not None,
                                            "openExternalUrl": self.open_external_url is not None}}},
                    "clientInfo": {"name": "sfdc-agent-figma-reader", "version": "1"}})
                if initialized.get("agentCapabilities", {}).get("_meta", {}).get("kiro") is None:
                    raise _error("FIGMA_MCP_TRANSPORT_UNSUPPORTED", "Kiro ACP V3 연결과 도구 식별 정보를 확인해야 합니다.")
                self._extensions = set(initialized["agentCapabilities"]["_meta"]["kiro"].get("extensionMethods", []))
                session = await self._rpc("session/new", {"cwd": str(self.work), "mcpServers": [],
                    "_meta": {"kiro": {"modeId": PROFILE_NAME}}})
                self.session_id = session.get("sessionId")
                self.policy.session_id = self.session_id
                if not isinstance(self.session_id, str):
                    raise _error("FIGMA_MCP_TRANSPORT_INVALID", "Kiro MCP 세션을 확인할 수 없습니다.")
                # Wait for the actual MCP connection before any model prompt.
                async with asyncio.timeout(190 if authorize else 20):
                    while not self.connected:
                        if self.failed_authorization:
                            if authorize:
                                self.failed_authorization = False
                                # Kiro 3.6.2 implements this documented v3 MCP
                                # control. It runs only after the visible CLI
                                # authorize action, never inside collection.
                                await self._rpc("_kiro/mcp/resetServer", {"sessionId": self.session_id,
                                    "serverName": self.server, "startOAuth": True})
                            else:
                                raise _error("FIGMA_MCP_AUTH_REQUIRED", "Figma MCP 최초 승인 또는 재인증이 필요합니다.")
                        await self._dispatch(await self._read())
                if readiness_only:
                    return {"authorization": "CONNECTED", "sourceCollectionVerified": False,
                            "performance": {"connectionMs": max(0, round((monotonic() - started) * 1000))}}
                connection_ms = max(0, round((monotonic() - started) * 1000))
                # Reuse the immutable job-pinned MCP prompts. Acquisition does
                # not introduce an unversioned screen-specific system prompt.
                reference = {"fileKey": self.policy.file_key, "nodeId": self.policy.node_id}
                arguments = json.dumps(reference, separators=(",", ":"))
                templates = self.templates or load_baseline_templates()
                metadata = PromptTemplate("screen.metadata", templates).render(arguments=arguments)
                design = PromptTemplate("screen.analyze", templates).render(arguments=json.dumps({
                    **reference, "clientFrameworks": "LWC", "clientLanguages": "html,css,javascript",
                    "skillNames": "figma-design-to-code", "excludeScreenshot": False}, separators=(",", ":")))
                screenshot = PromptTemplate("screen.capture", templates).render(screenshot=json.dumps({
                    **reference, "maxDimension": 8192, "enableBase64Response": True, "contentsOnly": False},
                    separators=(",", ":")), metadata="")
                stages = []
                for stage, prompt in (("metadata", metadata), ("design-context", design), ("capture", screenshot)):
                    stage_started = monotonic()
                    result = await self._rpc("session/prompt", {"sessionId": self.session_id,
                        "prompt": [{"type": "text", "text": prompt}]})
                    stages.append({"stage": stage, "modelAndToolMs": max(0, round((monotonic() - stage_started) * 1000))})
                    if result.get("stopReason") != "end_turn":
                        raise _error("FIGMA_MCP_EVIDENCE_INCOMPLETE", "Figma MCP 수집이 정상 완료되지 않았습니다.")
                evidence = self.policy.evidence()
                return {"toolEvidence": evidence, "rawTranscript": self.policy.transcript,
                        "rawWireTranscript": self.wire_transcript,
                        "promptReleaseId": self.prompt_release_id,
                        "acquisitionTemplates": {name: sha256(templates[name].encode()).hexdigest()
                            for name in ("screen.metadata", "screen.analyze", "screen.capture")},
                        "providerReceipt": {"provider": "kiro", "transport": "acp-v3", "reportedModel": None,
                                            "configuredModelOption": self.selected_model_option, "modelSelection": "provider-managed",
                                            "cli": cli_identity(binary)},
                        "performance": {"connectionMs": connection_ms, "stages": stages,
                                        "collectionMs": max(0, round((monotonic() - started) * 1000))}}
        except TimeoutError:
            primary = _error("FIGMA_MCP_AUTH_REQUIRED" if not self.connected else "FIGMA_MCP_TIMEOUT",
                             "Figma MCP 인증·연결을 확인해 주세요." if not self.connected else "Figma MCP 수집 시간이 초과됐습니다.")
            raise primary from None
        except BaseException as error:
            primary = error
            raise
        finally:
            self.key = ""
            if self._process is not None:
                if self._owned is None:
                    self._owned = OwnedProcesses(self._process, self.work)
                await _settle_cleanup(self._delete_session(), primary)
                await _settle_cleanup(_terminate(self._process, self._owned, ()), primary)

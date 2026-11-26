"""ACP credential callbacks isolated from source evidence and model tools."""

from hashlib import sha256
import re

from .figma_mcp_connections import _directory, _read_private, _write_private, read_connection
from .kiro_headless import SessionError

_KEY = re.compile(r"^kiro\.mcp\.[a-f0-9]{64}\.(?:client|discovery|first-party-document-refused|tokens|verifier)$")


class McpSecretStore:
    def __init__(self, scope, home):
        self.scope, self.home = scope, home
        self.root = home / "sfdc-mcp-secret-store"

    def validate(self):
        directory, _ = read_connection(self.scope)
        manifest = _read_private(directory / "connection.json")
        if self.home != directory / ("home-" + manifest["generation"]):
            raise SessionError("FIGMA_MCP_AUTH_REVOKED", "Figma MCP 연결의 인증 세대가 변경됐습니다.")
        _directory(self.home)
        if self.root.exists() or self.root.is_symlink():
            _directory(self.root)

    def request(self, method, params):
        self.validate()
        key = params.get("key")
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise SessionError("FIGMA_MCP_TOOL_SCOPE_DENIED", "Figma MCP 인증 저장 범위를 벗어난 요청입니다.")
        path = self.root / (sha256(key.encode()).hexdigest() + ".json")
        if method == "_kiro/secret/get":
            if not path.exists() and not path.is_symlink():
                return {"value": None}
            value = _read_private(path, limit=128 * 1024)
            if value.get("key") != key or not isinstance(value.get("value"), str):
                raise SessionError("FIGMA_MCP_CONFIG_INVALID", "Figma MCP 인증 저장 형식을 확인해 주세요.")
            return {"value": value["value"]}
        if method == "_kiro/secret/store":
            value = params.get("value")
            if not isinstance(value, str) or len(value.encode()) > 64 * 1024:
                raise SessionError("FIGMA_MCP_CONFIG_INVALID", "Figma MCP 인증 저장 값이 올바르지 않습니다.")
            _directory(self.root)
            _write_private(path, {"key": key, "value": value})
            return {}
        if method == "_kiro/secret/delete":
            path.unlink(missing_ok=True)
            return {}
        raise SessionError("FIGMA_MCP_TOOL_SCOPE_DENIED", "지원하지 않는 Figma MCP 인증 요청입니다.")

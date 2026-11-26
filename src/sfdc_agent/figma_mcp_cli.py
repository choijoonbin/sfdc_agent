"""Operator commands for first consent; no credential values enter stdout."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
from urllib.parse import urlsplit
import webbrowser

from .figma_mcp_connections import (
    ConnectionScope, OAuthConfig, configure_connection, connection_lock,
    connection_ref, disable_connection, prepare_home, read_connection, _read_private,
)
from .kiro_headless import SessionError, read_key
from .kiro_mcp import KiroMcpReader


async def open_figma_authorization(params):
    url = params.get("url")
    try:
        parsed = urlsplit(url)
        valid = (isinstance(url, str) and len(url) <= 8192 and parsed.scheme == "https"
                 and parsed.hostname in {"figma.com", "www.figma.com"} and parsed.path.startswith("/oauth")
                 and parsed.username is None and parsed.password is None)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise SessionError("FIGMA_MCP_AUTH_REQUIRED", "Figma의 공식 승인 페이지 주소를 확인할 수 없습니다.")
    if sys.platform == "darwin":
        process = await asyncio.create_subprocess_exec("pbcopy", stdin=asyncio.subprocess.PIPE,
                                                       stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await process.communicate(url.encode())
    webbrowser.open(url)
    print("Figma 승인 페이지를 열었습니다. 브라우저에서 접근 승인을 완료하세요. 인증 URL은 로그에 기록하지 않습니다.", flush=True)


def add_parser(sub):
    command = sub.add_parser("figma-mcp", help="Configure and authorize one user's Figma MCP connection.")
    actions = command.add_subparsers(dest="mcp_action", required=True)
    for action in ("configure", "authorize", "status", "revoke"):
        parser = actions.add_parser(action)
        parser.add_argument("--tenant-id", required=True)
        parser.add_argument("--workspace-id", required=True)
        parser.add_argument("--user-id", required=True)
        if action == "configure":
            parser.add_argument("--redirect-uri", default="http://localhost:7778/oauth/callback")
            parser.add_argument("--oauth-config", type=Path,
                                help="Optional 0600 JSON OAuth client config; default uses native Kiro registration.")


def main(args):
    try:
        scope = ConnectionScope(tenantId=args.tenant_id, workspaceId=args.workspace_id, actorUserId=args.user_id)
        if args.mcp_action == "configure":
            oauth = OAuthConfig.model_validate(_read_private(args.oauth_config)) if args.oauth_config else OAuthConfig(redirectUri=args.redirect_uri)
            result = configure_connection(scope, oauth)
        elif args.mcp_action == "revoke":
            result = disable_connection(scope)
        else:
            directory, _ = read_connection(scope)
            with connection_lock(directory):
                _, home, work, server = prepare_home(scope, authorizing=args.mcp_action == "authorize")
                binary = os.environ.get("NOVA_KIRO_CLI") or shutil.which("kiro-cli")
                if not binary:
                    raise SessionError("KIRO_UNAVAILABLE", "Kiro CLI 실행 파일을 찾지 못했습니다.")
                key = read_key()
                if args.mcp_action == "authorize":
                    if not sys.stdin.isatty():
                        raise SessionError("FIGMA_MCP_AUTH_REQUIRED", "실제 터미널에서 최초 Figma MCP 승인을 진행해 주세요.")
                    reader = KiroMcpReader(home=home, work=work, server=server, file_key="Readiness", node_id="0:1", key=key,
                                           connection_scope=scope, open_external_url=open_figma_authorization)
                    result = asyncio.run(reader.collect(timeout=210, readiness_only=True, authorize=True))
                else:
                    reader = KiroMcpReader(home=home, work=work, server=server, file_key="Readiness", node_id="0:1", key=key,
                                           connection_scope=scope)
                    result = asyncio.run(reader.collect(timeout=45, readiness_only=True))
                result["connectionRef"] = connection_ref(scope)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except SessionError as error:
        print(f"{error.code}: {error}", file=sys.stderr)
        return 1
    except (ValueError, OSError):
        print("FIGMA_MCP_CONFIG_INVALID: 연결 범위, OAuth 설정 파일과 권한을 확인해 주세요.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Figma MCP 인증을 중단했습니다. 원격 승인은 Figma 계정 설정에서 관리하세요.", file=sys.stderr)
        return 130

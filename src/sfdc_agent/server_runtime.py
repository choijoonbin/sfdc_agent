"""Independent managed Uvicorn entrypoint; never starts the local BACK runner."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

from .server_preflight import PreflightError, preflight, read_contract

# Existing visual requests permit 34 MiB and analysis views/revisions 24 MiB.
# Each downstream API retains its own narrower limit (executions: 16 MiB).
MAX_REQUEST_BYTES = 34 * 1024 * 1024


class ServerCapabilityBoundary:
    """Keep disabled providers explicit, and reject cross-scope worker requests.

    The existing API continues to authenticate service and per-job context
    tokens. This layer adds deployment policy; it never changes engine/source.
    """

    def __init__(self, app, contract):
        self.app, self.contract = app, contract

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        capabilities = self.contract["capabilities"]

        async def reject(status, code):
            body = json.dumps({"error": {"code": code, "message": "The selected server capability or worker scope is unavailable."}}).encode()
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": body})

        if path == "/internal/v1/figma-mcp" or path.startswith("/internal/v1/figma-mcp/"):
            return await reject(503, "FIGMA_MCP_SERVER_NOT_READY")
        if not capabilities["kiroHeadless"] and (path == "/internal/kiro/sessions" or path.startswith("/internal/kiro/sessions/")):
            return await reject(503, "KIRO_SERVER_DISABLED")
        if not capabilities["visualCapture"] and path == "/internal/v1/visual-comparisons":
            return await reject(503, "VISUAL_CAPTURE_SERVER_UNAVAILABLE")
        if scope.get("method") != "POST" or not path.startswith("/internal/v1/"):
            return await self.app(scope, receive, send)
        messages, body = [], bytearray()
        try:
            async with asyncio.timeout(15):
                while True:
                    message = await receive()
                    messages.append(message)
                    if message["type"] == "http.disconnect":
                        return
                    if message["type"] != "http.request":
                        return await reject(400, "SERVER_REQUEST_INVALID")
                    body.extend(message.get("body", b""))
                    if len(body) > MAX_REQUEST_BYTES:
                        return await reject(413, "SERVER_REQUEST_TOO_LARGE")
                    if not message.get("more_body"):
                        break
        except TimeoutError:
            return await reject(408, "SERVER_REQUEST_TIMEOUT")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            payload = None  # Preserve the API's existing parse/validation response.
        if isinstance(payload, dict):
            acquisition = payload.get("sourceAcquisition")
            try:
                remote = isinstance(payload.get("sourceReference"), str) and urlsplit(payload["sourceReference"]).scheme.lower() == "https"
            except ValueError:
                remote = False  # The endpoint reports invalid source syntax.
            # The Node engine's implicit Kiro Screen policy is MCP_ONLY. Do not
            # turn an omitted policy into a silent REST selection on a server.
            implicit_mcp = (path == "/internal/v1/executions" and payload.get("kind") == "SCREEN"
                            and payload.get("engine") == "KIRO" and payload.get("operation", "ANALYZE") == "ANALYZE"
                            and remote and acquisition is None and payload.get("inputMode", "SOURCE") != "MEASURED")
            if acquisition == "MCP_ONLY" or implicit_mcp:
                return await reject(503, "FIGMA_MCP_SERVER_NOT_READY")
            if payload.get("engine") == "KIRO" and not capabilities["kiroHeadless"]:
                return await reject(503, "KIRO_SERVER_DISABLED")
            request_scope = payload.get("executionScope")
            if request_scope is not None:
                expected = self.contract["worker"]["scope"]
                if not isinstance(request_scope, dict) or any(request_scope.get(key) != value for key, value in expected.items()):
                    return await reject(403, "SERVER_WORKER_SCOPE_FORBIDDEN")
            uses_rest = isinstance(acquisition, str) and acquisition in {"REST_FIRST", "REST_ONLY"} or (remote and not payload.get("archive")
                        and payload.get("operation", "ANALYZE") != "GENERATE")
            if (uses_rest or path == "/internal/v1/source-reference") and not capabilities["figmaRest"]:
                return await reject(503, "FIGMA_REST_SERVER_DISABLED")
        position = 0

        async def replay():
            nonlocal position
            if position < len(messages):
                message = messages[position]
                position += 1
                return message
            return await receive()

        await self.app(scope, replay, send)


def create_app(startup):
    # Import after the preflight and secret mount resolution. Existing local
    # commands still import api:app directly and keep their current behavior.
    from .api import app
    from .managed_http import authorize_request as _authorize
    from fastapi import Request

    app.add_middleware(ServerCapabilityBoundary, contract=startup.contract)

    @app.get("/internal/v1/server-readiness", include_in_schema=False)
    def server_readiness(request: Request):
        _authorize(request)
        return startup.report

    return app


def start(contract_path, *, check_only=False):
    startup = preflight(read_contract(contract_path))
    if check_only:
        print(json.dumps(startup.report))
        return
    os.environ.update(startup.secret_environment)
    # The service HOME is private disposable state, never a mounted personal
    # ~/.kiro, .sf, .aws or sibling repository. Headless jobs isolate it further.
    private_home = Path(startup.contract["paths"]["stateRoot"]) / "empty-home"
    private_home.mkdir(mode=0o700, exist_ok=True)
    if private_home.is_symlink() or private_home.resolve() != private_home or private_home.stat().st_mode & 0o077:
        raise PreflightError("SERVER_HOME_INVALID")
    os.environ["HOME"] = str(private_home)
    tempfile.tempdir = startup.contract["paths"]["scratchRoot"]
    import uvicorn
    app = create_app(startup)
    network = startup.contract["network"]
    print(json.dumps(startup.report), flush=True)
    uvicorn.run(app, host=network["bindHost"], port=network["port"], workers=1, reload=False,
                proxy_headers=False, access_log=False, server_header=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", required=True, help="Public server contract JSON; never include secret values")
    parser.add_argument("--check", action="store_true", help="Validate local configuration without starting a listener or contacting providers")
    args = parser.parse_args()
    try:
        start(args.contract, check_only=args.check)
    except PreflightError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()

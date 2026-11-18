"""Explicit boundaries between the local development service and managed execution."""

import os
import re

from starlette.responses import JSONResponse


def managed_only():
    return os.environ.get("SFDC_AGENT_MANAGED_ONLY") == "1"


def trusted_hosts():
    configured = os.environ.get("SFDC_AGENT_TRUSTED_HOSTS", "")
    hosts = ["localhost", "127.0.0.1"]
    for value in configured.split(","):
        host = value.strip().lower()
        if not host:
            continue
        if len(host) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in host.split(".")):
            raise ValueError("SFDC_AGENT_TRUSTED_HOSTS requires explicit hostnames without wildcards, schemes or ports.")
        if host not in hosts:
            hosts.append(host)
        if len(hosts) > 32:
            raise ValueError("SFDC_AGENT_TRUSTED_HOSTS exceeds the supported host count.")
    return hosts


class ManagedModeBoundary:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and managed_only():
            path = scope.get("path", "")
            allowed = path == "/health" or any(path == prefix or path.startswith(prefix + "/")
                for prefix in ("/internal/v1", "/internal/kiro/sessions"))
            if not allowed:
                return await JSONResponse({"detail": "Not found."}, status_code=404)(scope, receive, send)
        await self.app(scope, receive, send)

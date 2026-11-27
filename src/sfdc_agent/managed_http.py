"""Common internal-service authentication, bounded requests and runner readiness."""

import asyncio
import hmac
import os
from pathlib import Path
import shutil
from typing import TypeVar

from fastapi import HTTPException, Request
from pydantic import BaseModel
from starlette.requests import ClientDisconnect

from .config import engine_root
from .strict_json import decode_json

ModelT = TypeVar('ModelT', bound=BaseModel)


def authorize_request(request: Request) -> None:
    token = os.environ.get("SFDC_AGENT_EXECUTION_TOKEN")
    if not token or not token.strip():
        raise HTTPException(503, "Managed execution is disabled.")
    headers = request.headers.getlist("authorization")
    scheme, _, provided = (headers[0] if len(headers) == 1 else "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(provided.encode("latin-1"), token.encode()):
        raise HTTPException(401, "Service authentication required.", headers={"WWW-Authenticate": "Bearer"})
    if 'origin' in request.headers or 'sec-fetch-site' in request.headers:
        raise HTTPException(403, 'Service requests only.')


async def parse_request(request: Request, model: type[ModelT], *, max_bytes: int = 16 * 1024 * 1024) -> ModelT:
    body = bytearray()
    try:
        async with asyncio.timeout(15):
            async for chunk in request.stream():
                if len(body) + len(chunk) > max_bytes:
                    raise HTTPException(413, "Managed execution request is too large.")
                body.extend(chunk)
        return model.model_validate(decode_json(body))
    except (ValueError, RecursionError):
        raise HTTPException(422, "Invalid managed execution request.") from None
    except TimeoutError:
        raise HTTPException(408, "Managed execution request timed out.") from None
    except ClientDisconnect:
        raise HTTPException(499, "Managed execution request disconnected.") from None


def runner_configuration() -> tuple[str, Path, int]:
    binary = shutil.which(os.environ.get("SFDC_MANAGED_EXECUTION_NODE", "node"))
    runner = Path(os.environ.get("SFDC_MANAGED_EXECUTION_RUNNER", engine_root() / "nextbss/server/managed-execution.mjs"))
    try:
        timeout = int(os.environ.get("SFDC_MANAGED_EXECUTION_TIMEOUT_SECONDS", "540"))
        runner = runner.resolve(strict=True)
        if not binary or not runner.is_file() or not 1 <= timeout <= 900:
            raise ValueError
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(503, "Managed execution runner is unavailable.") from None
    return str(Path(binary).resolve()), runner, timeout


async def wait_for_disconnect(request: Request) -> None:
    while (await request.receive())["type"] != "http.disconnect":
        pass


"""Capture a supplied immutable preview manifest without fetching outside its scope."""

import asyncio
import base64
import tempfile

from fastapi import APIRouter, HTTPException, Request
from .config import engine_root

router = APIRouter(prefix="/internal/v1/visual-comparisons", include_in_schema=False)
_REQUEST_LIMIT = 16 * 1024 * 1024


@router.post("")
async def compare(request: Request):
    from .managed_execution import (
        IncompleteExecution,
        _authorize,
        _decode_json,
        _encode_json,
        _runner_configuration,
        _run_process,
        _WORKER_SLOTS,
    )
    _authorize(request)
    if "origin" in request.headers or "sec-fetch-site" in request.headers:
        raise HTTPException(403, "Service requests only.")
    if not _WORKER_SLOTS.acquire(blocking=False):
        raise HTTPException(503, "Managed capture is busy.")
    try:
        body = bytearray()
        async with asyncio.timeout(15):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > _REQUEST_LIMIT:
                    raise HTTPException(413, "Visual input is too large.")
        value = _decode_json(body)
        if not isinstance(value, dict):
            raise ValueError()
        binary, _, _ = _runner_configuration()
        runner = (engine_root() / "nextbss/server/managed-visual-capture.mjs").resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix="sfdc-visual-capture-") as scratch:
            output = await _run_process(binary, runner, scratch, 60, _encode_json(value))
        result = _decode_json(output)
        if (
            not isinstance(result, dict)
            or result.get("resultId") != value.get("resultId")
            or result.get("referenceSha256") != value.get("referenceSha256")
            or result.get("previewSha256") != value.get("previewSha256")
            or not isinstance(result.get("report"), dict)
            or not isinstance(result.get("captures"), list)
            or len(result["captures"]) != 3
        ):
            raise ValueError()
        for item in result["captures"]:
            if (
                not isinstance(item, dict)
                or item.get("key") not in {"source", "rendered", "difference"}
                or len(base64.b64decode(item["base64"], validate=True)) > 2 * 1024 * 1024
            ):
                raise ValueError()
        return result
    except TimeoutError:
        raise HTTPException(504, "Visual capture timed out.") from None
    except (ValueError, KeyError, TypeError, OSError, IncompleteExecution):
        raise HTTPException(422, "The supplied preview and PNG could not be compared.") from None
    finally:
        _WORKER_SLOTS.release()

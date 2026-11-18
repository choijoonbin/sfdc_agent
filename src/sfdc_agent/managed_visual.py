"""Capture a supplied immutable preview manifest without fetching outside its scope."""

import asyncio
import base64
from hashlib import sha256
import tempfile

from fastapi import APIRouter, HTTPException, Request
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse
from .config import engine_root

router = APIRouter(prefix="/internal/v1/visual-comparisons", include_in_schema=False)
_REQUEST_LIMIT = 34 * 1024 * 1024
_OUTPUT_LIMIT = 68 * 1024 * 1024
_CAPTURE_LIMIT = 16 * 1024 * 1024


async def _capture(value):
    from .managed_execution import _decode_json, _encode_json, _runner_configuration, _run_process
    binary, _, _ = _runner_configuration()
    runner = (engine_root() / "nextbss/server/managed-visual-capture.mjs").resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="sfdc-visual-capture-") as scratch:
        output = await _run_process(binary, runner, scratch, 60, _encode_json(value), output_limit=_OUTPUT_LIMIT)
    result = _decode_json(output)
    if (not isinstance(result, dict) or result.get("resultId") != value.get("resultId")
            or result.get("referenceSha256") != value.get("referenceSha256")
            or result.get("previewSha256") != value.get("previewSha256")
            or not isinstance(result.get("report"), dict) or not isinstance(result.get("captures"), list)
            or len(result["captures"]) != 3):
        raise ValueError()
    keys = set()
    for item in result["captures"]:
        if not isinstance(item, dict) or item.get("key") not in {"source", "rendered", "difference"} or item["key"] in keys:
            raise ValueError()
        keys.add(item["key"])
        data = base64.b64decode(item["base64"], validate=True)
        if not data or len(data) > _CAPTURE_LIMIT or sha256(data).hexdigest() != item.get("sha256"):
            raise ValueError()
    return result


@router.post("")
async def compare(request: Request):
    from .managed_execution import (
        IncompleteExecution,
        _authorize,
        _decode_json,
        _WORKER_SLOTS,
        _wait_for_disconnect, _finish_owned_cleanup, _cancel_request_tasks,
    )
    _authorize(request)
    if "origin" in request.headers or "sec-fetch-site" in request.headers:
        raise HTTPException(403, "Service requests only.")
    if not _WORKER_SLOTS.acquire(blocking=False):
        return JSONResponse({"code": "EXECUTION_BUSY"}, status_code=503, headers={"Retry-After": "5"})
    tasks = ()
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
        execution = asyncio.create_task(_capture(value))
        disconnect = asyncio.create_task(_wait_for_disconnect(request))
        tasks = (execution, disconnect)
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if execution.done():
            return execution.result()
        raise HTTPException(499, "Visual capture request disconnected.")
    except TimeoutError:
        raise HTTPException(504, "Visual capture timed out.") from None
    except (ValueError, KeyError, TypeError, OSError, IncompleteExecution):
        raise HTTPException(422, "The supplied preview and PNG could not be compared.") from None
    except ClientDisconnect:
        raise HTTPException(499, "Visual capture request disconnected.") from None
    finally:
        try:
            await _finish_owned_cleanup(_cancel_request_tasks(tasks))
        finally:
            _WORKER_SLOTS.release()

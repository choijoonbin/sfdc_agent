"""Read-only original UI projection using the packaged original engine's public serializers."""

import asyncio
import base64
import hashlib
import os
import tempfile
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import ClientDisconnect

from .managed_runtime import (
    IncompleteExecution,
    WORKER_SLOTS as _WORKER_SLOTS,
    read_output as _read_output,
    stop_process as _stop_process,
    write_input as _write_input,
)
from .managed_http import (
    authorize_request as _authorize,
    runner_configuration as _runner_configuration,
    wait_for_disconnect as _wait_for_disconnect,
)
from .strict_json import decode_json as _decode_json, encode_json as _encode_json

router = APIRouter(prefix='/internal/v1/analysis-views', include_in_schema=False)
LIMIT = 16 * 1024 * 1024


class Projection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['SCREEN', 'STORY']
    sha256: Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
    raw: Annotated[str, Field(min_length=1, max_length=24 * 1024 * 1024)]


async def project(payload: Projection):
    try:
        raw = base64.b64decode(payload.raw, validate=True)
        if not raw or len(raw) > LIMIT or hashlib.sha256(raw).hexdigest() != payload.sha256:
            raise ValueError
        document = _decode_json(raw)
        binary, execution_runner, _ = _runner_configuration()
        runner = execution_runner.with_name('managed-original-analysis.mjs')
        if not runner.is_file() or runner.is_symlink():
            raise HTTPException(503, 'Original analysis projection is unavailable.')
        with tempfile.TemporaryDirectory(prefix='sfdc-analysis-view-') as scratch:
            environment = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'LC_CTYPE') if key in os.environ}
            environment.update({'TMPDIR': scratch, 'TEMP': scratch, 'TMP': scratch})
            process = await asyncio.create_subprocess_exec(binary, str(runner), cwd=scratch, env=environment,
                start_new_session=os.name == 'posix', stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            finished = False
            tasks = (asyncio.create_task(_write_input(process, _encode_json({'kind': payload.kind, 'document': document}))),
                     asyncio.create_task(_read_output(process.stdout)))
            try:
                async with asyncio.timeout(30):
                    _, output = await asyncio.gather(*tasks)
                    code = await process.wait()
                finished = True
                if code != 0 or len(output) > 8 * 1024 * 1024:
                    raise IncompleteExecution
            finally:
                if not finished:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await _stop_process(process)
        value = _decode_json(output)
        if not isinstance(value, dict) or not isinstance(value.get('id'), str):
            raise IncompleteExecution
        return value
    except HTTPException:
        raise
    except TimeoutError:
        raise HTTPException(504, 'Original analysis projection timed out.') from None
    except (ValueError, OSError, IncompleteExecution):
        raise HTTPException(422, 'Original analysis cannot be projected safely.') from None


@router.post('')
async def original_view(request: Request):
    _authorize(request)
    if not _WORKER_SLOTS.acquire(blocking=False):
        raise HTTPException(503, 'Managed execution is busy.', headers={'Retry-After': '2'})
    tasks = ()
    try:
        body = bytearray()
        async with asyncio.timeout(15):
            async for chunk in request.stream():
                if len(body) + len(chunk) > 24 * 1024 * 1024:
                    raise HTTPException(413, 'Original analysis request is too large.')
                body.extend(chunk)
        try:
            payload = Projection.model_validate(_decode_json(body))
        except (ValueError, RecursionError):
            raise HTTPException(422, 'Invalid original analysis request.') from None
        execution = asyncio.create_task(project(payload))
        disconnect = asyncio.create_task(_wait_for_disconnect(request))
        tasks = (execution, disconnect)
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if execution.done():
            return execution.result()
        raise HTTPException(499, 'Original analysis request disconnected.')
    except TimeoutError:
        raise HTTPException(408, 'Original analysis request timed out.') from None
    except ClientDisconnect:
        raise HTTPException(499, 'Original analysis request disconnected.') from None
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        _WORKER_SLOTS.release()

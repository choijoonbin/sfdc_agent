"""Shared bounded Node process lifecycle; no HTTP routes or job-state authority."""

import asyncio
import logging
import os
from pathlib import Path
import signal
from threading import BoundedSemaphore

from .execution_diagnostics import WorkerExecutionFailure, decode_worker_failure
from .managed_prompts import runner_token

WORKER_SLOTS = BoundedSemaphore(2)
OUTPUT_BYTES = 12 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024
_STDERR_CODE_BYTES = 82
_STDERR_CODES = frozenset(code.encode('ascii') for code in (
    'KIRO_INVALID_BUNDLE', 'KIRO_INVALID_LWC_TEMPLATE', 'KIRO_INVALID_LWC_CSS', 'KIRO_INVALID_LWC_JS',
    'KIRO_INVALID_NOVA_BINDING', 'KIRO_INVALID_NOVA_EVENT', 'KIRO_INVALID_NOVA_PROPERTY',
    'KIRO_FIGMA_CONTEXT_UNAVAILABLE', 'KIRO_FIGMA_CONTEXT_UNVERIFIED', 'KIRO_FIGMA_SOURCE_MISMATCH',
    'KIRO_FIGMA_VERSION_UNSUPPORTED', 'KIRO_LOCAL_SOURCE_UNVERIFIED', 'KIRO_UNKNOWN_LWC_COMPONENT',
    'KIRO_NOVA_CONTRACT_UNAVAILABLE', 'KIRO_REST_SOURCE_UNVERIFIED', 'NOVA_COMPONENT_HELD', 'KIRO_FAILED',
    'MANAGED_EXECUTION_FAILED', 'EXECUTION_OUTPUT_TOO_LARGE', 'EXECUTION_PREVIEW_UNAVAILABLE',
    'EXECUTION_LOGICAL_SCOPE_INVALID', 'LOGICAL_SURFACE_BUNDLE_INVALID', 'KIRO_INVALID_LOGICAL_CHILD_EVENT',
))
logger = logging.getLogger(__name__)
_RUNNER_ENVIRONMENT = (
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    "SFDC_MANAGED_RELEASE_ROOT", "SFDC_MANAGED_AGENT_ORIGIN", "SFDC_AGENT_SCREEN_IMAGE_INPUT",
    "SFDC_MANAGED_SOURCE_ROOT", "SFDC_CONVERSION_ATTEMPT_ROOT",
)


class IncompleteExecution(Exception):
    """Unexpected runner response or process state."""


async def write_input(process: asyncio.subprocess.Process, payload: bytes) -> None:
    try:
        process.stdin.write(payload)
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        process.stdin.close()


async def read_output(stream: asyncio.StreamReader, limit: int = OUTPUT_BYTES) -> bytes:
    output = bytearray()
    while chunk := await stream.read(_CHUNK_BYTES):
        if len(output) + len(chunk) > limit:
            raise IncompleteExecution
        output.extend(chunk)
    return bytes(output)


async def _read_stderr_code(stream: asyncio.StreamReader) -> str:
    # Match the entire bounded stderr exactly. Whitespace, extra lines and
    # unknown values are discarded rather than becoming provider-controlled logs.
    candidate = bytearray()
    oversized = False
    while chunk := await stream.read(_CHUNK_BYTES):
        if not oversized:
            if len(candidate) + len(chunk) > _STDERR_CODE_BYTES:
                candidate.clear()
                oversized = True
            else:
                candidate.extend(chunk)
    code = bytes(candidate)
    return code.decode('ascii') if not oversized and code in _STDERR_CODES else 'UNKNOWN'


async def _discard_output(stream: asyncio.StreamReader) -> None:
    while await stream.read(_CHUNK_BYTES):
        pass


async def stop_process(process: asyncio.subprocess.Process) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()
    except ProcessLookupError:
        pass
    # Both pipes must drain after their consumer tasks are cancelled, including
    # a child created while the caller was already disconnecting.
    await asyncio.gather(*(_discard_output(stream) for stream in (process.stdout, process.stderr) if stream is not None))
    await process.wait()


async def finish_owned_cleanup(cleanup):
    task = asyncio.create_task(cleanup)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            pass
    return task.result()


async def cancel_request_tasks(tasks):
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_process(binary: str, runner: Path, scratch: str, timeout: int, payload: bytes,
                       environment_overrides: dict[str, str] | None = None,
                       output_limit: int = OUTPUT_BYTES) -> bytes:
    if type(output_limit) is not int or not 1 <= output_limit <= 68 * 1024 * 1024:
        raise ValueError('Invalid managed output bound.')
    environment = {key: os.environ[key] for key in _RUNNER_ENVIRONMENT if key in os.environ}
    environment.update({"SFDC_EXECUTION_SCRATCH": scratch, "TMPDIR": scratch, "TEMP": scratch, "TMP": scratch})
    environment.update(environment_overrides or {})
    token = runner_token()
    if token:
        environment["SFDC_MANAGED_PROMPT_CONTEXT"] = token
    opening = asyncio.create_task(asyncio.create_subprocess_exec(
        binary, str(runner), cwd=scratch, env=environment, start_new_session=os.name == "posix",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    ))
    try:
        process = await asyncio.shield(opening)
    except BaseException:
        async def reap_opening():
            try:
                child = await opening
                await stop_process(child)
            except (OSError, asyncio.CancelledError):
                pass
        await finish_owned_cleanup(reap_opening())
        raise
    tasks = (asyncio.create_task(write_input(process, payload)), asyncio.create_task(read_output(process.stdout, output_limit)),
             asyncio.create_task(_read_stderr_code(process.stderr)))
    finished = False
    try:
        async with asyncio.timeout(timeout):
            _, output, stderr_code = await asyncio.gather(*tasks)
            exit_code = await process.wait()
        if exit_code != 0:
            logger.warning('Managed worker failed code=%s exit_code=%s stdout_bytes=%s', stderr_code, exit_code, len(output))
            diagnostic = decode_worker_failure(output)
            if diagnostic is not None:
                raise WorkerExecutionFailure(diagnostic)
            raise IncompleteExecution
        finished = True
        return output
    finally:
        if not finished:
            async def cleanup():
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await stop_process(process)
            await finish_owned_cleanup(cleanup())


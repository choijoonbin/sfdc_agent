"""Validate Spring prompt snapshots and bind them to one managed execution."""

from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
from datetime import datetime, timezone
import json
from secrets import token_urlsafe
from threading import RLock
from time import monotonic

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from .managed_http import authorize_request as _authorize
from .prompt_registry import PROMPTS, _release
from .prompt_release_id import PromptReleaseId
from .prompt_templates import load_baseline_templates, template_placeholders, validate_template
from .prompt_validation import preview_operation
from .prompt_variable_details import prompt_variable_details


router = APIRouter(prefix="/internal/v1/prompts", include_in_schema=False)
_TOKEN = ContextVar("managed_prompt_token", default=None)
_CONTEXTS = {}
_LOCK = RLock()


class PromptBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    releaseId: PromptReleaseId
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    templates: dict[str, str] = Field(min_length=13, max_length=13)


def template_digest(templates):
    canonical = "".join(key + "\n" + sha256(body.encode()).hexdigest() + "\n" for key, body in sorted(templates.items()))
    return sha256(canonical.encode()).hexdigest()


def checked_bundle(bundle):
    if set(bundle.templates) != set(load_baseline_templates()) or template_digest(bundle.templates) != bundle.sha256:
        raise ValueError("The prompt bundle content changed.")
    for operation, body in bundle.templates.items():
        validate_template(operation, body)
    return _release(bundle.releaseId, dict.fromkeys(bundle.templates, bundle.sha256), bundle.templates)


def validate_operation(operation, templates):
    if operation not in PROMPTS or set(templates) != {operation}:
        raise ValueError("Choose an editable operation.")
    source = load_baseline_templates()
    source.update(templates)
    validate_template(operation, source[operation])
    preview, cases = preview_operation(operation, source)
    return {"valid": True, "sha256": sha256(source[operation].encode()).hexdigest(),
            "preview": preview, "cases": cases}


@contextmanager
def execution_prompts(bundle):
    if bundle is None:
        yield []
        return
    release = checked_bundle(bundle)
    traces = []
    token = token_urlsafe(32)
    with _LOCK:
        _CONTEXTS[token] = (monotonic() + 3600, release, traces)
    handle = _TOKEN.set(token)
    try:
        yield traces
    finally:
        _TOKEN.reset(handle)
        with _LOCK:
            _CONTEXTS.pop(token, None)


def runner_token():
    return _TOKEN.get()


def require_managed_context(request):
    values = request.headers.getlist("x-sfdc-managed-prompt")
    with _LOCK:
        entry = _CONTEXTS.get(values[0]) if len(values) == 1 else None
    if not entry or entry[0] <= monotonic():
        raise HTTPException(403, "A current managed job context is required.")
    return values[0]


def managed_context_alive(token):
    with _LOCK:
        entry = _CONTEXTS.get(token)
    return bool(entry and entry[0] > monotonic())


def session_prompt_release(request, release_id):
    values = request.headers.getlist("x-sfdc-managed-prompt")
    if not values:
        return None
    if len(values) != 1:
        raise HTTPException(403, "Managed prompt context is invalid.")
    with _LOCK:
        entry = _CONTEXTS.get(values[0])
    if not entry or entry[0] <= monotonic() or entry[1].id != release_id:
        raise HTTPException(403, "Managed prompt context is unavailable.")
    return entry[1]


def current_managed_release(request):
    """Read the exact job pin for tool collection, without selecting another release."""
    token = require_managed_context(request)
    with _LOCK:
        return _CONTEXTS[token][1]


def managed_trace_enabled(request):
    values = request.headers.getlist("x-sfdc-managed-prompt")
    with _LOCK:
        entry = _CONTEXTS.get(values[0]) if len(values) == 1 else None
        return bool(entry and entry[0] > monotonic())


def bounded_trace(capture, release, digest, prompt_bytes, mode):
    remaining = 128 * 1024
    substitutions = {}
    for name, occurrences in capture.values.items():
        output = []
        for occurrence in occurrences[:20]:
            item = dict(occurrence)
            value = item.get("value")
            if isinstance(value, str):
                length = len(value.encode())
                if length > 32 * 1024 or length > remaining:
                    item = {"section": item.get("section", "main"), "omitted": True}
                else:
                    remaining -= length
            output.append(item)
        substitutions[name] = output
    return {"operation": capture.operation, "executedAt": datetime.now(timezone.utc).isoformat(),
            "templateSha256": sha256(release.templates[capture.operation].encode()).hexdigest(),
            "bundleSha256": template_digest(release.templates), "promptDigest": digest, "promptBytes": prompt_bytes,
            "executionMode": mode, "values": substitutions}


def record_managed_trace(request, capture, release, text, mode):
    """Export bounded already-redacted substitutions only to the owning execution."""
    values = request.headers.getlist("x-sfdc-managed-prompt")
    with _LOCK:
        entry = _CONTEXTS.get(values[0]) if len(values) == 1 else None
        if not entry or entry[0] <= monotonic() or entry[1].id != release.id or len(entry[2]) >= 24:
            return
        trace = bounded_trace(capture, release, sha256(text.encode()).hexdigest(), len(text.encode()), mode)
        if sum(len(json.dumps(row, ensure_ascii=False).encode()) for row in entry[2]) + len(json.dumps(trace, ensure_ascii=False).encode()) <= 512 * 1024:
            entry[2].append(trace)




@router.get("/schema")
async def schema(request: Request):
    _authorize(request)
    return {"operations": [
        {"key": key, "label": label, "group": "Screen" if key.startswith("screen.") else "Story",
         "variables": template_placeholders(key), "variableDetails": prompt_variable_details(key, template_placeholders(key))}
        for key, (label, _category, _mcp) in PROMPTS.items()
    ]}


@router.post("/validate")
async def validate(request: Request):
    _authorize(request)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 256 * 1024:
            raise HTTPException(413, "Prompt input is too large.")
    try:
        value = json.loads(body)
        if set(value) != {"operation", "templates"} or not isinstance(value["templates"], dict):
            raise ValueError()
        return await run_in_threadpool(validate_operation, value["operation"], value["templates"])
    except (KeyError, ValueError, TypeError):
        return {"valid": False, "message": "원래 섹션과 변수의 이름·개수를 유지하고 LF 본문을 확인해주세요."}

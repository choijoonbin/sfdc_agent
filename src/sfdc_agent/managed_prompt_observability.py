"""Service-only prompt evaluations using immutable Spring snapshots."""

import asyncio
import os
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from .managed_prompts import PromptBundle, _authorize, bounded_trace, checked_bundle
from .prompt_provider_eval import EVALUATION_OPERATIONS, evaluate_prompt_provider
from .prompt_run_values import _SENSITIVE, capture_prompt_run
from .providers import ProviderError

router = APIRouter(prefix="/internal/v1/prompts", include_in_schema=False)
_SECRET_KEYS = {"authorization", "cookie", "setcookie", "password", "passwd", "clientsecret", "apikey",
                "accesstoken", "refreshtoken", "sessionid", "privatekey", "subscriptionkey", "secret", "credential", "credentials"}


class Evaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str
    provider: str = Field(pattern=r"^(kiro|openai|azure)$")
    model: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
    fields: dict
    promptBundle: PromptBundle


class _ManagedHeadlessProvider:
    name = "kiro"
    model = None

    def __init__(self, purpose):
        self.purpose = purpose

    def complete_rendered(self, text):
        from .kiro_headless import HeadlessKiroSession, SessionError, read_key
        from .providers import _parse_object
        async def complete():
            session = HeadlessKiroSession(read_key(), purpose=self.purpose)
            try:
                response = await session.call("prompt", {"text": text}, timeout=120)
                return _parse_object(response["text"])
            finally:
                await session.close()
        try:
            return asyncio.run(complete())
        except SessionError as error:
            raise ProviderError(error.code, "The configured managed Kiro execution did not complete.") from None


def managed_provider(payload):
    if payload.provider != "kiro":
        return None, "provider"
    purpose = "story" if payload.operation.startswith("story.") else "screen"
    mode = os.environ.get("SFDC_AGENT_" + purpose.upper() + "_KIRO_MODE", "acp")
    if mode not in {"headless", "acp"}:
        raise ProviderError("KIRO_MODE_INVALID", "Choose the configured Kiro execution mode.")
    if payload.model:
        raise ProviderError("KIRO_MODEL_CONFIG", "The managed Kiro service owns its model selection.")
    return (_ManagedHeadlessProvider(purpose) if mode == "headless" else None), mode


def private_value(value, depth=0):
    if depth > 24:
        return {"omitted": True}
    if isinstance(value, dict):
        output = {key: {"redacted": True} if any(key.replace("_", "").replace("-", "").lower().endswith(secret) for secret in _SECRET_KEYS)
                  else private_value(member, depth + 1) for key, member in list(value.items())[:512]}
        if len(value) > 512:
            output["omitted"] = True
        return output
    if isinstance(value, list):
        return [private_value(member, depth + 1) for member in value[:512]] + ([{"omitted": True}] if len(value) > 512 else [])
    if isinstance(value, str) and _SENSITIVE.search(value):
        return {"redacted": True}
    return value


def configured_providers():
    azure = False
    try:
        from .azure_provider import AzureSettings
        AzureSettings.from_environment()
        azure = True
    except ProviderError:
        pass
    installed = Path.home() / ".local/bin/kiro-cli"
    binary = shutil.which(os.environ.get("NOVA_KIRO_CLI", str(installed) if installed.is_file() else "kiro-cli"))
    kiro_operations = []
    for purpose in ("screen", "story"):
        mode = os.environ.get("SFDC_AGENT_" + purpose.upper() + "_KIRO_MODE", "acp")
        configured = bool(binary and mode == "acp")
        if binary and mode == "headless":
            from .kiro_headless import SessionError, read_key
            try:
                read_key()
                configured = True
            except SessionError:
                pass
        if configured:
            kiro_operations.extend(operation for operation in EVALUATION_OPERATIONS if operation.startswith(purpose + "."))
    return [
        {"key": "kiro", "label": "Kiro", "configured": bool(kiro_operations), "configuredOperations": kiro_operations},
        {"key": "openai", "label": "OpenAI", "configured": bool(os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_MODEL"))},
        {"key": "azure", "label": "Azure OpenAI", "configured": azure},
    ]


@router.get("/evaluation-capabilities")
async def capabilities(request: Request):
    _authorize(request)
    from .prompt_validation import _CASES
    examples = {operation: {"label": next(iter(_CASES[operation])), "fields": next(iter(_CASES[operation].values()))}
                for operation in EVALUATION_OPERATIONS}
    return {"operations": list(EVALUATION_OPERATIONS), "providers": configured_providers(), "examples": examples}


@router.post("/evaluate")
async def evaluate(request: Request):
    from .managed_execution import _decode_json, _encode_json, _WORKER_SLOTS
    _authorize(request)
    if not _WORKER_SLOTS.acquire(blocking=False):
        raise HTTPException(503, "Managed evaluation is busy.")
    try:
        body = bytearray()
        async with asyncio.timeout(15):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 1024 * 1024:
                    raise HTTPException(413, "Evaluation is too large.")
        payload = Evaluation.model_validate(_decode_json(body))
        if len(_encode_json(payload.fields)) > 256 * 1024 or private_value(payload.fields) != payload.fields:
            raise ValueError()
        release = checked_bundle(payload.promptBundle)
        provider, mode = managed_provider(payload)
        with capture_prompt_run(payload.operation) as capture:
            result = await run_in_threadpool(evaluate_prompt_provider, payload.operation, payload.fields, payload.provider,
                                            model=payload.model, timeout=120, release_id=release.id, templates_release=release,
                                            provider_override=provider)
        result["executionMode"] = mode
        result["promptTrace"] = bounded_trace(capture, release, result["promptDigest"], result["promptBytes"], mode)
        result["result"] = private_value(result["result"])
        if len(_encode_json(result)) > 256 * 1024:
            result["result"] = {"omitted": True}
        return result
    except ProviderError as error:
        code = error.code if re.fullmatch(r"[A-Z][A-Z_]{1,79}", error.code) else "PROVIDER_FAILED"
        return {"errorCode": code}
    except TimeoutError:
        raise HTTPException(408, "Evaluation input timed out.") from None
    except (ValueError, TypeError, KeyError, OSError, RecursionError):
        raise HTTPException(422, "Use bounded structured evidence without credentials and an immutable prompt bundle.") from None
    finally:
        _WORKER_SLOTS.release()

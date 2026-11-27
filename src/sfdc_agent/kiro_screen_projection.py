"""Enrich structured Screen evidence with verified projections from the UI engine."""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .config import engine_root
from .screen_prompt_context import ScreenPromptContext, read_screen_prompt_context
from .strict_json import loads


logger = logging.getLogger("uvicorn.error")
_BRIDGE = Path(__file__).with_suffix(".mjs")
_OPERATIONS = {"screen.analyze.local", "screen.review-plan", "screen.generate", "screen.repair"}
_PROJECTION_ENVIRONMENT = ("PATH", "LANG", "LC_ALL", "LC_CTYPE")


def _projection_environment() -> dict[str, str]:
    # Projection only reads the selected immutable release. It needs no Agent,
    # provider, workspace, HOME or Node preload authority from its parent.
    environment = {key: os.environ[key] for key in _PROJECTION_ENVIRONMENT if key in os.environ}
    if not os.environ.get("SFDC_MANAGED_ENGINE_ROOT") and "SFDC_MANAGED_RELEASE_ROOT" in os.environ:
        # Preserve the existing nonmanaged release-store fallback; managed
        # engine/release paths already travel explicitly in argv below.
        environment["SFDC_MANAGED_RELEASE_ROOT"] = os.environ["SFDC_MANAGED_RELEASE_ROOT"]
    return environment


@dataclass(frozen=True)
class PreparedScreenFields:
    fields: dict | None
    prompt_context: ScreenPromptContext | None = None


async def prepare_screen_fields(operation: str | None, fields: dict | None) -> PreparedScreenFields:
    if operation not in _OPERATIONS or not isinstance(fields, dict):
        return PreparedScreenFields(fields)
    evidence = fields.get("evidence") if operation == "screen.repair" else fields
    if not isinstance(evidence, dict) or not isinstance(evidence.get("sourceContext"), dict):
        return PreparedScreenFields(fields)
    # Captured MCP text has a different evidence contract and renderer. Its
    # incidental metadata is not a measured source scene or a model payload.
    if (evidence.get('local') is not True or evidence.get('sourceKind') == 'figma-mcp'
            or not isinstance(evidence['sourceContext'].get('nodes'), list)
            or not evidence['sourceContext']['nodes']):
        return PreparedScreenFields(fields)

    try:
        release_root = os.environ.get("SFDC_MANAGED_RELEASE_ROOT", "") if os.environ.get("SFDC_MANAGED_ENGINE_ROOT") else ""
        process = await asyncio.create_subprocess_exec(
            os.environ.get("SFDC_MANAGED_EXECUTION_NODE", "node"), str(_BRIDGE), str(engine_root()), release_root,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env=_projection_environment(),
        )
        try:
            request = json.dumps({"fields": evidence}, ensure_ascii=False, allow_nan=False).encode()
            output, _ = await asyncio.wait_for(process.communicate(request), timeout=15)
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        if process.returncode != 0:
            raise ValueError("Projection bridge failed")
        projection = loads(output)
        if not isinstance(projection, dict) or set(projection) != {'sourceJson', 'prepared'}:
            raise ValueError("Invalid projection response")
        prepared = read_screen_prompt_context(projection['sourceJson'], projection['prepared'])
        context = loads(prepared.source_json)
    except (OSError, ValueError, TimeoutError, KeyError, TypeError, RecursionError):
        if evidence.get("componentLibrary") is not None or len(json.dumps(evidence['sourceContext'], ensure_ascii=False).encode()) > 512 * 1024:
            raise ValueError("The pinned component-library projection is unavailable.") from None
        logger.warning("NextBSS visual projection unavailable for %s", operation)
        return PreparedScreenFields(fields)

    enriched = {**evidence, "sourceContext": context}
    result = {**fields, "evidence": enriched} if operation == "screen.repair" else enriched
    return PreparedScreenFields(result, prepared)


async def project_screen_fields(operation: str | None, fields: dict | None) -> dict | None:
    """Retain the canonical-field projection API for non-rendering consumers."""
    return (await prepare_screen_fields(operation, fields)).fields

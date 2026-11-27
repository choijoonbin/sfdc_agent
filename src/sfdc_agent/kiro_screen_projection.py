"""Enrich structured Screen evidence with verified projections from the UI engine."""

import asyncio
import json
import logging
import os
from pathlib import Path

from .config import engine_root


logger = logging.getLogger("uvicorn.error")
_BRIDGE = Path(__file__).with_suffix(".mjs")
_OPERATIONS = {"screen.review-plan", "screen.generate", "screen.repair"}
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


async def project_screen_fields(operation: str | None, fields: dict | None) -> dict | None:
    if operation not in _OPERATIONS or not isinstance(fields, dict):
        return fields
    evidence = fields.get("evidence") if operation == "screen.repair" else fields
    if not isinstance(evidence, dict) or not isinstance(evidence.get("sourceContext"), dict):
        return fields
    if not evidence["sourceContext"].get("inputControls") and not evidence["sourceContext"].get("components"):
        return fields

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
        projection = json.loads(output)
        if not isinstance(projection, dict):
            raise ValueError("Invalid projection response")
    except (OSError, ValueError, TimeoutError):
        if evidence.get("componentLibrary") is not None:
            raise ValueError("The pinned component-library projection is unavailable.") from None
        logger.warning("NextBSS visual projection unavailable for %s", operation)
        return fields

    enriched = {**evidence, "sourceContext": {**evidence["sourceContext"], **projection}}
    return {**fields, "evidence": enriched} if operation == "screen.repair" else enriched

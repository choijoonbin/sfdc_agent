"""Evaluate a shared conversion prompt with an explicitly selected provider."""

import hashlib
import re
from typing import Any

from .prompt_registry import runtime_release
from .providers import ProviderError, create_provider
from .screen_tasks import build_screen_task
from .story_tasks import build_story_task


EVALUATION_OPERATIONS = (
    "screen.review-plan", "screen.generate", "screen.repair", "screen.plan", "screen.plan.repair",
    "story.classification", "story.analysis", "story.generate",
)
_REPAIRS = {"screen.repair", "screen.plan.repair"}
_REVIEWED = {"screen.generate", "screen.repair"}
_RELEASE_ID = re.compile(r"prompts-(?:v1|[a-f0-9]{32})")


def _check_evidence(operation: str, fields: dict[str, Any]) -> None:
    evidence = fields
    if operation in _REPAIRS:
        evidence = fields.get("evidence")
        previous = fields.get("previousDraft")
        if not isinstance(evidence, dict) or not isinstance(previous, str) or not previous.strip():
            raise ValueError("Repair requires prior draft output and source evidence.")
    if operation in _REVIEWED and not isinstance(evidence.get("reviewedAnalysis"), dict):
        raise ValueError("Generation requires the saved Screen analysis.")
    if operation in {"screen.review-plan", *_REVIEWED}:
        if not (evidence.get("sourceContext") or evidence.get("capturedContext") or evidence.get("sourceSections")):
            raise ValueError("Screen tasks require captured Figma evidence.")
    if operation in {"screen.plan", "screen.plan.repair"} and not evidence.get("source"):
        raise ValueError("A Screen plan requires captured Figma evidence.")


def evaluate_prompt_provider(
    operation: str, fields: dict[str, Any], provider_name: str, *,
    model: str | None = None, timeout: float = 120, release_id: str | None = None,
) -> dict[str, Any]:
    """Return the provider's parsed JSON and prompt provenance, without saving a conversion."""
    if operation not in EVALUATION_OPERATIONS:
        raise ProviderError("PROMPT_EVALUATION_UNSUPPORTED", "Choose a task that can run from saved evidence without Figma MCP.")
    if not isinstance(fields, dict):
        raise ProviderError("PROMPT_EVALUATION_INVALID", "Supply the task's structured JSON fields.")
    if release_id is not None and (not isinstance(release_id, str) or not _RELEASE_ID.fullmatch(release_id)):
        raise ProviderError("PROMPT_EVALUATION_INVALID", "Supply a valid prompt release ID.")
    release = runtime_release(release_id)
    try:
        _check_evidence(operation, fields)
        builder = build_screen_task if operation.startswith("screen.") else build_story_task
        text = builder(operation, fields, templates=release.templates)
        payload = text.encode("utf-8")
        if not payload or len(payload) > 8 * 1024 * 1024:
            raise ValueError
    except (ValueError, TypeError, KeyError, RecursionError):
        raise ProviderError("PROMPT_EVALUATION_INVALID", "The task fields or captured evidence do not match this prompt.") from None
    provider = create_provider(provider_name, model=model, timeout=timeout)
    result = provider.complete_rendered(text)
    return {
        "operation": operation,
        "provider": provider.name,
        "model": provider.model,
        "promptReleaseId": release.id,
        "promptVersion": release.versions[operation],
        "promptDigest": hashlib.sha256(payload).hexdigest(),
        "promptBytes": len(payload),
        "result": result,
    }

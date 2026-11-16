"""Story model task templates used by the agent service and pilot."""

import json
import re
from collections.abc import Mapping
from typing import Any

from .prompt_templates import PromptTemplate


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Unsupported JSON constant: {value}")


def _object_field(fields: dict[str, Any], key: str) -> dict[str, Any]:
    text = fields.get(key)
    if not isinstance(text, str):
        raise ValueError(f"{key} must be serialized JSON")
    result = json.loads(text, parse_constant=_reject_json_constant)
    if not isinstance(result, dict):
        raise ValueError(f"{key} must be a JSON object")
    return result


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def build_story_task(operation: str, fields: dict[str, Any], *, templates: Mapping[str, str] | None = None) -> str:
    """Build the prompt, keeping serialized source evidence byte-for-byte intact."""
    if operation == "story.classification" and set(fields) == {"evidence"}:
        _object_field(fields, "evidence")
        return PromptTemplate(operation, templates).render(evidence=fields["evidence"])
    if operation == "story.analysis" and set(fields) == {"evidence", "categories", "maxFacts"}:
        _object_field(fields, "evidence")
        _object_field(fields, "categories")
        max_facts = fields["maxFacts"]
        if type(max_facts) is not int or not 1 <= max_facts <= 100:
            raise ValueError("Invalid Story fact limit")
        return PromptTemplate(operation, templates).render(
            evidence=fields["evidence"], categories=fields["categories"], max_facts=str(max_facts),
        )
    if operation == "story.generate" and set(fields) in ({"input"}, {"input", "diagnostic"}):
        reviewed = _object_field(fields, "input")
        digest, bundle_name = reviewed.get("specDigest"), reviewed.get("bundleName")
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not isinstance(bundle_name, str) or not re.fullmatch(r"nextBss[A-Z][a-zA-Z0-9]{1,72}", bundle_name)):
            raise ValueError("Invalid reviewed Story digest or bundle name")
        diagnostic = fields.get("diagnostic")
        if "diagnostic" in fields:
            diagnostic_data = _object_field(fields, "diagnostic")
            if set(diagnostic_data) != {"code", "message"} or any(
                not isinstance(diagnostic_data[key], str) or len(diagnostic_data[key]) > limit
                for key, limit in (("code", 100), ("message", 800))
            ):
                raise ValueError("Invalid Story compiler diagnostic")
        return _generation(PromptTemplate(operation, templates), fields["input"], digest, bundle_name, diagnostic)
    raise ValueError("Invalid Story task operation or fields")


def _generation(template: PromptTemplate, reviewed: str, digest: str, bundle_name: str, diagnostic: str | None) -> str:
    output_contract = {
        "specDigest": digest,
        "files": {
            bundle_name + ".html": "LWC template",
            bundle_name + ".css": "scoped CSS",
            bundle_name + ".js": "LWC JavaScript",
        },
        "unresolved": [{
            "kind": "missing|conflict|unsupported|cosmetic",
            "screenId": "affected screen ID",
            "scenarioId": "affected scenario ID when known",
            "reason": "unresolved issue",
            "evidenceIds": [],
            "nextAction": "what to verify or repair",
        }],
    }
    prompt = template.render(output_contract=_compact_json(output_contract), reviewed=reviewed)
    if diagnostic is None:
        return prompt
    return prompt + "\n" + template.render("retry", diagnostic=diagnostic)

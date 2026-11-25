"""Fence derived renderer guidance without rewriting authored Figma content."""

import re


_RETIRED = re.compile(r"c-next-bss-|c/nextBss|--nb-|\b(?:on)?valuechange\b", re.IGNORECASE)


def prompt_source_context(context: dict | None, contract: dict) -> dict | None:
    """Use immutable NOVA APIs for derived mappings; keep raw nodes verbatim.

    The Node projector owns structural conversion. This final prompt boundary
    rejects any stale derived tags or guidance that escaped it. Original layer
    names, text, style and other evidence stay data. It does not maintain a second
    projection or prompt rule source beside the pinned Node public contract.
    """
    if not isinstance(context, dict) or not isinstance(contract.get("modules"), list):
        return context
    def checked(record):
        if not isinstance(record, dict):
            return
        tag = record.get("tag")
        if isinstance(tag, str):
            if _RETIRED.search(tag):
                raise ValueError("Derived component evidence requires its pinned NOVA projection.")
        if isinstance(record.get("legacyTag"), str) and _RETIRED.search(record["legacyTag"]):
            raise ValueError("Derived component evidence requires its pinned NOVA projection.")
        if isinstance(record.get("template"), str) and _RETIRED.search(record["template"]):
            raise ValueError("Derived component evidence requires its pinned NOVA projection.")
        for key in ("children", "fields", "buttons"):
            if isinstance(record.get(key), list):
                for item in record[key]:
                    checked(item)

    for key, value in context.items():
        if key.startswith("verified") and key != "verifiedComponentContracts" and isinstance(value, list):
            for item in value:
                checked(item)
    if "verifiedComponentContracts" in context:
        if not isinstance(context["verifiedComponentContracts"], list):
            raise ValueError("Derived component guidance requires its pinned NOVA projection.")
        if any(isinstance(value, str) and _RETIRED.search(value) for value in context["verifiedComponentContracts"]):
            raise ValueError("Derived component guidance requires its pinned NOVA projection.")
    return context

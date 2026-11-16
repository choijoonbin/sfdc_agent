"""Load the packaged prompt baseline and render checked text templates."""

import re
from collections import Counter
from collections.abc import Mapping
from functools import lru_cache
from importlib.resources import files

from .prompt_run_values import record_prompt_arguments


_SECTION = re.compile(r"(?:\A|\n)@@section ([a-z][a-z0-9_]*)\n")
_VARIABLE = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
_MAX_TEMPLATE_BYTES = 128 * 1024


@lru_cache(maxsize=1)
def _baseline() -> dict[str, str]:
    directory = files("sfdc_agent").joinpath("prompt_templates_v1")
    return {
        resource.name.removesuffix(".txt"): resource.read_bytes().decode("utf-8")
        for resource in sorted(directory.iterdir(), key=lambda item: item.name)
        if resource.name.endswith(".txt")
    }


def load_baseline_templates() -> dict[str, str]:
    return _baseline().copy()


def _sections(text: str) -> dict[str, str]:
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > _MAX_TEMPLATE_BYTES:
        raise ValueError("A prompt template must be nonempty and at most 128 KiB.")
    if "\r" in text or "\0" in text:
        raise ValueError("Use LF line endings and remove null characters.")
    headings = list(_SECTION.finditer(text))
    if not headings:
        return {"main": text}
    if headings[0].start() != 0 or headings[0].group(1) != "main":
        raise ValueError("Prompt sections must start with @@section main.")
    sections = {}
    for index, heading in enumerate(headings):
        name = heading.group(1)
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        section = text[heading.end():end]
        if name in sections or not section.strip():
            raise ValueError(f"Prompt section {name} must appear once and contain text.")
        sections[name] = section
    return sections


def _signature(text: str) -> Counter:
    if "{{" in _VARIABLE.sub("", text) or re.search(r"^@@section\b", text, re.MULTILINE):
        raise ValueError("Invalid prompt section or placeholder marker.")
    return Counter(_VARIABLE.findall(text))


@lru_cache(maxsize=None)
def _baseline_signatures(operation: str) -> dict[str, Counter]:
    if operation not in _baseline():
        raise ValueError("Unknown prompt template.")
    return {name: _signature(text) for name, text in _sections(_baseline()[operation]).items()}


def template_placeholders(operation: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(name for section in _baseline_signatures(operation).values() for name in section))


def _validated_sections(operation: str, text: str) -> dict[str, str]:
    source = _sections(text)
    baseline = _baseline_signatures(operation)
    if source.keys() != baseline.keys():
        raise ValueError("Keep the original prompt section names.")
    for name, section in source.items():
        if _signature(section) != baseline[name]:
            raise ValueError(f"Keep the original placeholders in section {name}.")
    return source


def validate_template(operation: str, text: str) -> None:
    _validated_sections(operation, text)


def preview_template(operation: str, text: str) -> str:
    """Show substitutions for all sections with synthetic values."""
    sections = _validated_sections(operation, text)
    rendered = {name: _VARIABLE.sub(lambda match: f"〈{match[1]}〉", section) for name, section in sections.items()}
    if len(rendered) == 1:
        return rendered["main"]
    return "\n".join(f"@@section {name}\n{section}" for name, section in rendered.items())


class PromptTemplate:
    def __init__(self, operation: str, templates: Mapping[str, str] | None = None):
        source = _baseline() if templates is None else templates
        if operation not in source:
            raise ValueError("Missing prompt template.")
        self._templates = source
        self.operation = operation
        self.sections = _validated_sections(operation, source[operation])
        self.signatures = _baseline_signatures(operation)

    def related(self, operation: str) -> "PromptTemplate":
        return PromptTemplate(operation, self._templates)

    def render(self, section: str = "main", **values: str) -> str:
        if self.signatures[section].keys() != values.keys():
            raise ValueError(f"Missing or unexpected values for prompt section {section}.")
        rendered = _VARIABLE.sub(lambda match: values[match[1]], self.sections[section])
        record_prompt_arguments(self.operation, section, values)
        return rendered

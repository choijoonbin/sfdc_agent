"""The immutable prompt pins shared by managed and local prompt consumers."""

import re
from typing import Annotated

from pydantic import Field


# Existing local releases use v1 or a UUID; imported NOVA releases use the
# complete publication digest. Pins remain identifiers, never paths or URLs.
PROMPT_RELEASE_ID_PATTERN = r"^prompts-(?:v1|[a-f0-9]{32}|nova-[a-f0-9]{64})$"
PromptReleaseId = Annotated[str, Field(pattern=PROMPT_RELEASE_ID_PATTERN)]
_RELEASE_ID = re.compile(PROMPT_RELEASE_ID_PATTERN)


def is_prompt_release_id(value):
    return isinstance(value, str) and _RELEASE_ID.fullmatch(value) is not None

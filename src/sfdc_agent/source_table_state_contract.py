"""Bounded view selectors for an explicitly requested, immutable NOVA analysis."""

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NodeId = Annotated[str, Field(strict=True, min_length=1, max_length=160)]
_INVALID = "SOURCE_TABLE_STATE_BINDING_INVALID"


class SourceTableTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=False)
    tableNodeId: NodeId
    countNodeId: NodeId | None = None

    @model_validator(mode="before")
    @classmethod
    def optional_count_is_not_null(cls, value):
        if isinstance(value, dict) and "countNodeId" in value and value["countNodeId"] is None:
            raise ValueError(_INVALID)
        return value

    @field_validator("tableNodeId", "countNodeId")
    @classmethod
    def source_identity(cls, value):
        if value is not None and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_:;.-]{0,159}", value) is None:
            raise ValueError(_INVALID)
        return value


class SourceTableStateBindings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schemaVersion: Annotated[int, Field(strict=True, ge=1, le=1)]
    targets: Annotated[list[SourceTableTarget], Field(min_length=1, max_length=8)]

    @model_validator(mode="after")
    def unique_roles(self):
        identities = set()
        for target in self.targets:
            for node in (target.tableNodeId, target.countNodeId):
                if node is None:
                    continue
                if node in identities:
                    raise ValueError(_INVALID)
                identities.add(node)
        return self

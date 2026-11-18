"""The Spring-selected immutable library pin; this never selects filesystem paths."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class ComponentLibrary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schemaVersion: Annotated[int, Field(ge=1, le=1)]
    family: Literal["NEXTBSS_LEGACY", "NOVA_PUBLIC"]
    libraryVersion: str | None
    descriptorSha256: Digest
    contractSha256: Digest | None
    moduleManifestSha256: Digest | None
    tokenCssSha256: Digest

    @model_validator(mode="after")
    def family_assets(self):
        if self.family == "NOVA_PUBLIC":
            if self.libraryVersion != "1.0.0" or self.contractSha256 is None or self.moduleManifestSha256 is None:
                raise ValueError("A NOVA release requires its complete immutable contract.")
        elif self.libraryVersion is not None or self.contractSha256 is not None or self.moduleManifestSha256 is not None:
            raise ValueError("A legacy release cannot invent absent NOVA assets.")
        return self

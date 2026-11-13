"""Pilot request and model output contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


ProviderName = Literal["kiro", "openai", "azure"]


class PilotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: ProviderName = "kiro"
    model: str | None = None
    target: Literal["all", "screen", "story"] = "all"
    reference_manifest: str | None = None
    model_timeout: int = Field(default=300, ge=10, le=600)


class ScreenFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidenceId: str = Field(min_length=1, max_length=100)
    explanation: str = Field(min_length=1, max_length=600)


class ScreenPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[ScreenFinding] = Field(min_length=1, max_length=12)


class StoryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transitions: list[dict] = Field(max_length=180)
    dialogs: list[dict] = Field(default_factory=list, max_length=32)
    actionRules: list[dict] = Field(default_factory=list, max_length=80)
    scenarios: list[dict] = Field(max_length=24)
    unresolved: list[dict] = Field(default_factory=list, max_length=30)
    controls: list[dict] = Field(default_factory=list, max_length=80)
    spec: dict | list[dict] | None = None

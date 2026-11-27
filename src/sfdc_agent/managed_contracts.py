"""Shared managed execution wire contracts, independent of route handlers."""

from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, PrivateAttr, model_validator

from .component_library import ComponentLibrary
from .managed_execution_progress import MAX_ATTEMPT, ExecutionScope
from .managed_prompts import PromptBundle
from .prompt_release_id import PromptReleaseId
from .screen_analysis_contract import validate_checkpoint as validate_screen_checkpoint
from .strict_json import encode_json
from .managed_figma_connections import FigmaConnectionProof

CHECKPOINT_BYTES = 3 * 1024 * 1024

ReleasePin = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")]
SourceReference = Annotated[str, Field(min_length=1, max_length=2048, pattern=r"^[^\x00-\x1f\x7f]+$")]
RegistryLabel = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[^\x00-\x1f\x7f]+$")]
ManifestDigest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
JsonObject = dict[str, JsonValue]


class ArchiveSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenantId: UUID
    workspaceId: UUID
    fileKey: Annotated[str, Field(pattern=r"^[A-Za-z0-9]{1,160}$")]
    sha256: ManifestDigest


class SourceDiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    executionScope: ExecutionScope
    figmaConnection: FigmaConnectionProof | None = None
    archive: ArchiveSelection | None = None
    sourceReference: SourceReference | None = None
    pageId: Annotated[str, Field(pattern=r"^\d+:\d+$")] | None = None
    version: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,160}$")] | None = None
    previewNode: Annotated[str, Field(pattern=r"^\d+:\d+$")] | None = None

    @model_validator(mode="after")
    def source_kind(self):
        if bool(self.archive) == bool(self.sourceReference):
            raise ValueError("Select a local archive or a registered remote source.")
        if self.archive and (self.archive.tenantId != self.executionScope.tenantId or self.archive.workspaceId != self.executionScope.workspaceId):
            raise ValueError('The selected archive belongs to a different execution scope.')
        return self


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    jobId: UUID
    attempt: Annotated[int, Field(ge=1, le=MAX_ATTEMPT, strict=True)]
    executionScope: ExecutionScope
    figmaConnection: FigmaConnectionProof | None = None
    timeoutSeconds: Annotated[int, Field(ge=1, le=3600, strict=True)]
    deadlineEpochSeconds: Annotated[int, Field(gt=0, strict=True)]
    kind: Literal["SCREEN", "STORY"]
    engine: Literal["NOVA", "KIRO"]
    operation: Literal["ANALYZE", "GENERATE"] = "ANALYZE"
    sourceReference: SourceReference
    designReleaseId: ReleasePin
    promptReleaseId: PromptReleaseId
    promptBundle: PromptBundle | None = None
    designManifestSha256: ManifestDigest | None = None
    componentLibrary: ComponentLibrary | None = None
    promptReleaseVersion: RegistryLabel | None = None
    analysisJobId: UUID | None = None
    analysis: JsonObject | None = None
    workflowId: RegistryLabel | None = None
    archive: ArchiveSelection | None = None
    inputMode: Literal['SOURCE', 'MEASURED'] = 'SOURCE'
    sourceAcquisition: Literal['REST_FIRST', 'REST_ONLY', 'LOCAL_FILE', 'MCP_ONLY', 'LEGACY'] | None = None
    sourceVersion: RegistryLabel | None = None
    renderNodeId: Annotated[str, Field(pattern=r'^\d+:\d+$', max_length=160)] | None = None
    _screen_contract: dict | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def validate_review(self):
        if self.renderNodeId is not None and (self.kind != 'SCREEN' or self.operation != 'ANALYZE' or self.inputMode != 'SOURCE'):
            raise ValueError('Render selection applies only to Screen analysis.')
        if self.figmaConnection is not None and self.sourceAcquisition in {'MCP_ONLY', 'LOCAL_FILE'}:
            raise ValueError('Organization REST authorization cannot authorize a personal MCP or local source.')
        if self.promptBundle is not None and self.promptBundle.releaseId != self.promptReleaseId:
            raise ValueError("Prompt release pin does not match.")
        if self.archive and (self.archive.tenantId != self.executionScope.tenantId
                             or self.archive.workspaceId != self.executionScope.workspaceId):
            raise ValueError("The selected archive belongs to a different execution scope.")
        remote = urlsplit(self.sourceReference).scheme.lower() == 'https'
        if self.sourceAcquisition in {'REST_FIRST', 'REST_ONLY'} and (self.kind != 'SCREEN'
                or self.inputMode != 'SOURCE' or not remote):
            raise ValueError('REST acquisition requires a Screen URL.')
        if self.operation=='ANALYZE' and ((self.sourceAcquisition=='REST_FIRST' and self.engine!='NOVA')
                or (self.sourceAcquisition=='REST_ONLY' and self.engine!='KIRO')):
            raise ValueError('Select NOVA API-first analysis or Kiro API-only analysis.')
        if self.sourceAcquisition == 'MCP_ONLY' and (self.kind != 'SCREEN' or (self.operation == 'ANALYZE' and self.engine != 'KIRO')
                or self.inputMode != 'SOURCE' or not remote or self.archive
                or not self.executionScope.actorUserId):
            raise ValueError('MCP acquisition requires an actor-scoped Kiro Screen URL without an archive.')
        if self.sourceAcquisition == 'LOCAL_FILE' and (not self.archive
                or (remote and self.inputMode != 'MEASURED')):
            raise ValueError('Local acquisition requires an explicitly selected archive.')
        if self.inputMode == 'MEASURED':
            if self.kind != 'SCREEN' or self.operation == 'ANALYZE' and self.engine != 'NOVA' or not self.archive or self.sourceVersion != 'local-' + self.archive.sha256:
                raise ValueError('Measured input requires the exact registered Screen archive.')
        elif self.sourceVersion is not None:
            raise ValueError('Source version is only supplied for measured input.')
        if self.operation == "GENERATE":
            if self.analysisJobId is None or self.analysisJobId == self.jobId or not self.analysis:
                raise ValueError("Generation requires a prior analysis checkpoint.")
            if len(encode_json(self.analysis)) > CHECKPOINT_BYTES:
                raise ValueError("Analysis checkpoint is too large.")
            self._screen_contract = validate_screen_checkpoint(self,self.analysis)
            if self.inputMode=='MEASURED' and self.engine!='NOVA' and self._screen_contract is None:
                raise ValueError('Cross-engine measured generation requires a saved Screen contract.')
        elif self.analysisJobId is not None or self.analysis is not None or self.workflowId is not None:
            raise ValueError("Analysis starts without a prior checkpoint.")
        return self


class ExecutionArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: Annotated[str, Field(min_length=1, max_length=240)]
    mediaType: Literal["image/png", "application/zip", "application/octet-stream"]
    base64: str


class AnalysisView(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    protocolVersion: Annotated[int, Field(ge=1, le=1, strict=True)]
    manifestSha256: ManifestDigest


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["REVIEW_REQUIRED", "COMPLETED"]
    title: Annotated[str, Field(min_length=1, max_length=240)]
    artifacts: Annotated[list[ExecutionArtifact], Field(max_length=512)]
    summary: JsonObject | None = None
    checkpoint: JsonObject | None = None
    promptTraces: list[JsonObject] | None = None
    analysisView: AnalysisView | None = None


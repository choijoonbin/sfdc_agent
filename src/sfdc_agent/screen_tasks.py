"""Compose Kiro instructions for Screen requests from verified NOVA evidence."""

import json
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, StrictBool, StrictInt

from .prompt_templates import PromptTemplate
from .component_library import ComponentLibrary
from .nova_prompt_evidence import prompt_source_context
from .prompt_projection import source_for_prompt, contract_for_prompt
from .screen_prompt_context import ScreenPromptContext


class TaskFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fileKey: str = Field(pattern=r"^[a-zA-Z0-9]+$", max_length=160)


class NodeFields(TaskFields):
    nodeId: str = Field(pattern=r"^\d+:\d+$", max_length=40)


class CaptureFields(NodeFields):
    maxDimension: StrictInt = Field(ge=1, le=8192)
    readLayout: StrictBool = False


class DiscoveryFields(TaskFields):
    fileKey: str = Field(pattern=r"^[a-zA-Z0-9]+$", max_length=160)
    nodeId: str | None = Field(default=None, pattern=r"^\d+:\d+$", max_length=40)


class ContextFields(TaskFields):
    nodeIds: list[str] = Field(min_length=1, max_length=4)
    includeScreenshot: StrictBool = False

    def tool_arguments(self):
        return [NodeFields(fileKey=self.fileKey, nodeId=node_id).model_dump() for node_id in self.nodeIds]


class RepairFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local: StrictBool
    diagnostic: str = Field(min_length=1, max_length=128_000)
    evidence: "GenerationFields | None" = None
    previousDraft: str | None = Field(default=None, max_length=384 * 1024)


class PlanFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: dict
    sourceDigest: str = Field(pattern=r"^[a-f0-9]{64}$")
    referenceContext: str | None = None


class PlanRepairFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diagnostic: str = Field(min_length=1, max_length=128_000)
    evidence: PlanFields | None = None
    previousDraft: str | None = Field(default=None, max_length=384 * 1024)


class Dimensions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: float = Field(gt=0, le=8192)
    height: float = Field(gt=0, le=8192)


class ContextCoverage(BaseModel):
    split: StrictBool = False
    nodesRead: list[str] = Field(default_factory=list, max_length=40)
    unresolved: list[str] = Field(default_factory=list, max_length=2000)


class SourceSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodeId: str = Field(pattern=r"^(?:I)?\d+:\d+(?:;\d+:\d+)*$", max_length=200)
    text: str = Field(max_length=250_000)


class GenerationFields(NodeFields):
    name: str = Field(pattern=r"^nextBss[A-Z][a-zA-Z0-9]{0,72}$")
    local: StrictBool = False
    sourceKind: Literal["figma-rest", "figma-mcp"] | None = None
    layoutDimensions: Dimensions | None = None
    actions: list[dict] = Field(default_factory=list)
    coverage: ContextCoverage | None = None
    splitLayout: list[dict] = Field(default_factory=list)
    sourceSections: list[SourceSection] = Field(default_factory=list)
    capturedContext: list[SourceSection] = Field(default_factory=list, max_length=41)
    sourceContext: dict | None = None
    historyTables: list[dict] = Field(default_factory=list, max_length=1500)
    reviewedAnalysis: dict | None = None
    version: str = Field(pattern=r"^\d+(?:\.\d+)+$")
    tokenDeclarations: str = Field(max_length=128_000)
    typographyGroups: list[str]
    componentVariants: str = Field(max_length=128_000)
    novaContract: dict = Field(default_factory=dict)
    componentLibrary: ComponentLibrary | None = None
    designReleaseId: str | None = Field(default=None, pattern=r"^nextbss-\d{1,4}(?:\.\d{1,4}){1,2}-[a-f0-9]{64}$")
    _prompt_context: ScreenPromptContext | None = PrivateAttr(default=None)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _analysis(args: ContextFields, template: PromptTemplate) -> str:
    arguments = "\n".join(_json({
        **node, "clientFrameworks": "LWC", "clientLanguages": "html,css,javascript",
        "skillNames": "figma-design-to-code", "excludeScreenshot": not args.includeScreenshot,
    }) for node in args.tool_arguments())
    return template.render(arguments=arguments)


def _capture(args: CaptureFields, template: PromptTemplate) -> str:
    reference = {"fileKey": args.fileKey, "nodeId": args.nodeId}
    screenshot = {**reference, "maxDimension": args.maxDimension,
                  "enableBase64Response": True, "contentsOnly": True}
    metadata = template.render("metadata", reference=_json(reference)) if args.readLayout else ""
    return template.render(screenshot=_json(screenshot), metadata=metadata)


def _discovery(args: DiscoveryFields, template: PromptTemplate) -> str:
    kind = template.render("page" if args.nodeId else "document")
    return template.render(reference=_json(args.model_dump(exclude_none=True)), kind=kind)


def _repair(args: RepairFields, template: PromptTemplate) -> str:
    if args.evidence is not None:
        if args.previousDraft is None or args.local != args.evidence.local:
            raise ValueError("Screen repair requires the original evidence and previous draft.")
        original = _generation(args.evidence, template.related("screen.generate"))
        return _repair_with_evidence(template, original, args.diagnostic, args.previousDraft)
    return template.render("local" if args.local else "main", diagnostic=args.diagnostic)


def _generation(args: GenerationFields, template: PromptTemplate) -> str:
    canonical_context = prompt_source_context(args.sourceContext, args.novaContract)
    source_json = args._prompt_context.matches(canonical_context) if args._prompt_context else _json(source_for_prompt(canonical_context))
    contract = contract_for_prompt(args.novaContract, canonical_context, stage='generation')
    dimensions = args.layoutDimensions
    width, height = (dimensions.width, dimensions.height) if dimensions else (1234, 700)
    has_review = args.reviewedAnalysis is not None
    if args.local:
        if not dimensions or canonical_context is None:
            raise ValueError("Local generation requires measured source evidence.")
        mode = "rest" if args.sourceKind == "figma-rest" else "local"
        source_instruction = template.render(f"{mode}_source_instruction")
        source_identity = template.render(f"{mode}_source_identity")
        layout_evidence = template.render(f"{mode}_layout_evidence")
        reading = template.render(f"{mode}_reading")
        payload = template.render(
            "local_payload", source_label=template.render(f"{mode}_source_label"),
            source_context=source_json,
        )
        action_evidence = template.render("local_action_evidence") if args.actions else ""
        accordion_evidence = template.render("local_accordion_evidence")
    else:
        mode = "saved" if has_review else "remote"
        source_instruction = template.render(f"{mode}_source_instruction")
        source_identity = template.render("remote_source_identity")
        layout_evidence = template.render("remote_layout_evidence")
        reading_lines = [template.render(f"{mode}_reading")]
        if args.coverage and args.coverage.split:
            reading_lines.append(template.render("split_reading", nodes_read=", ".join(args.coverage.nodesRead)))
        else:
            reading_lines.append(template.render("root_reading"))
        if args.splitLayout:
            reading_lines.append(template.render("split_layout_reading", split_layout=_json(args.splitLayout)))
        if args.coverage and args.coverage.unresolved:
            reading_lines.append(template.render("unresolved_reading", unresolved_nodes=", ".join(args.coverage.unresolved)))
        if args.sourceSections:
            reading_lines.append(template.render("recovered_reading"))
        reading_lines.append(template.render(f"{mode}_layout_reading"))
        reading = "\n".join(reading_lines)
        payload = "\n".join(
            template.render("captured_context", node_id=section.nodeId, context_text=section.text)
            for section in (args.capturedContext or args.sourceSections)
        )
        action_evidence = template.render("remote_action_evidence", actions=_json(args.actions)) if args.actions else ""
        accordion_evidence = template.render(f"{mode}_accordion_evidence")
    dimensions_line = template.render(
        "dimensions", layout_evidence=layout_evidence, width=f"{width:g}", height=f"{height:g}",
    ) if dimensions else ""
    example = _json({
        "fileKey": args.fileKey, "nodeId": args.nodeId, "title": "Actual screen name",
        "width": width, "height": height, "html": "<template>...</template>",
        "css": "@import 'c/novaTokens';\n...",
        "javascript": "import { LightningElement } from 'lwc';\nexport default class Screen extends LightningElement {}",
        "notes": [],
    })
    table_rules = ""
    if args.historyTables or (args.sourceContext or {}).get("historyTables"):
        table_rules = "\n" + template.render("history_table_rules") + "\n"
        if args.historyTables and (not args.local or (args.sourceContext or {}).get("historyTables") != args.historyTables):
            table_rules += template.render("history_source", history_tables=_json(args.historyTables))
    reviewed = template.render("reviewed_plan", reviewed_analysis=_json(args.reviewedAnalysis)) if has_review else ""
    return template.render(
        source_instruction=source_instruction, file_key=args.fileKey,
        node_url=args.nodeId.replace(":", "-"), node_id=args.nodeId, source_identity=source_identity,
        dimensions_line=dimensions_line, reading=reading, rules=template.render("source_rules", version=args.version),
        action_evidence=action_evidence, reviewed=reviewed, additional_rules=template.render("additional_rules"),
        table_rules=table_rules, accordion=template.render("accordion_rules", evidence=accordion_evidence),
        component_contracts=template.render("component_contracts") + "\nPinned NOVA public contract JSON:\n" + _json(contract),
        component_variants=args.componentVariants,
        token_declarations=args.tokenDeclarations, typography_groups=", ".join(args.typographyGroups),
        output_format=template.render("output_format", name=args.name), example=example, payload=payload,
    )


def _review_plan(args: GenerationFields, template: PromptTemplate) -> str:
    source = {
        "fileKey": args.fileKey, "nodeId": args.nodeId, "local": args.local,
        "dimensions": args.layoutDimensions.model_dump() if args.layoutDimensions else None,
        "coverage": args.coverage.model_dump() if args.coverage else None,
        "splitLayout": args.splitLayout, "actions": args.actions, "novaContract": args.novaContract,
    }
    if args.local and args._prompt_context is not None:
        canonical_context = prompt_source_context(args.sourceContext, args.novaContract)
        context_json = args._prompt_context.matches(canonical_context)
        key = "figmaRestSnapshot" if args.sourceKind == "figma-rest" else "localSource"
        # Embed the engine's exact JSON text rather than reserializing floats or
        # numeric property keys differently on the Python side.
        source_json = _json(source)[:-1] + ',' + _json(key) + ':' + context_json
        if args.historyTables and canonical_context.get("historyTables") != args.historyTables:
            source_json += ',"historyTables":' + _json(args.historyTables)
        source_json += '}'
        return template.render(version=args.version, component_variants=args.componentVariants, source=source_json)
    if args.local:
        source["figmaRestSnapshot" if args.sourceKind == "figma-rest" else "localSource"] = source_for_prompt(prompt_source_context(args.sourceContext, args.novaContract))
    else:
        source["figmaContext"] = [section.model_dump() for section in (args.capturedContext or args.sourceSections)]
    if args.historyTables and (not args.local or (args.sourceContext or {}).get("historyTables") != args.historyTables):
        source["historyTables"] = args.historyTables
    return template.render(version=args.version, component_variants=args.componentVariants, source=_json(source))


def _metadata(args: ContextFields, template: PromptTemplate) -> str:
    arguments = "\n".join(map(_json, args.tool_arguments()))
    return template.render(arguments=arguments)


def _local_analysis(args: GenerationFields, template: PromptTemplate) -> str:
    if not args.local or not args.layoutDimensions or args.sourceContext is None:
        raise ValueError("Local analysis requires measured source evidence.")
    canonical_context = prompt_source_context(args.sourceContext, args.novaContract)
    source_json = args._prompt_context.matches(canonical_context) if args._prompt_context else _json(canonical_context)
    return template.render(node_id=args.nodeId, source_context=source_json)


def _plan(args: PlanFields, template: PromptTemplate) -> str:
    context = template.render("context", context=args.referenceContext) if args.referenceContext else ""
    return template.render(source_digest=args.sourceDigest, source=_json(args.source), context=context)


def _plan_repair(args: PlanRepairFields, template: PromptTemplate) -> str:
    if args.evidence is not None:
        if args.previousDraft is None:
            raise ValueError("Screen plan repair requires the previous draft.")
        original = _plan(args.evidence, template.related("screen.plan"))
        return _repair_with_evidence(template.related("screen.repair"), original, args.diagnostic, args.previousDraft)
    return template.render(diagnostic=_json(args.diagnostic))


def _repair_with_evidence(template: PromptTemplate, prompt: str, diagnostic: str, previous: str) -> str:
    return template.render("with_evidence", prompt=prompt, diagnostic=_json(diagnostic), previous_draft=_json(previous))


_TASKS = {
    "screen.analyze": (ContextFields, _analysis),
    "screen.analyze.local": (GenerationFields, _local_analysis),
    "screen.metadata": (ContextFields, _metadata),
    "screen.capture": (CaptureFields, _capture),
    "screen.discovery": (DiscoveryFields, _discovery),
    "screen.generate": (GenerationFields, _generation),
    "screen.review-plan": (GenerationFields, _review_plan),
    "screen.repair": (RepairFields, _repair),
    "screen.plan": (PlanFields, _plan),
    "screen.plan.repair": (PlanRepairFields, _plan_repair),
}


def build_screen_task(operation: str, fields: dict, *, templates: Mapping[str, str] | None = None,
                      prepared_context: ScreenPromptContext | None = None) -> str:
    try:
        contract, build_prompt = _TASKS[operation]
    except KeyError:
        raise ValueError("Unknown Screen Kiro operation.") from None
    args = contract.model_validate(fields)
    if prepared_context is not None:
        target = args.evidence if isinstance(args, RepairFields) else args
        if not isinstance(target, GenerationFields) or not isinstance(prepared_context, ScreenPromptContext):
            raise ValueError('Prepared source context requires a structured Screen task.')
        prepared_context.matches(target.sourceContext)
        target._prompt_context = prepared_context
    return build_prompt(args, PromptTemplate(operation, templates))

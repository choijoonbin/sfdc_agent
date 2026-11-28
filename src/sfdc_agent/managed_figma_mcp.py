"""Managed, principal- and job-bound Figma MCP evidence checkpoints."""

import asyncio
from hashlib import sha256
import json
import logging
import re
from time import time, monotonic
import xml.etree.ElementTree as ET

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.responses import JSONResponse

from .figma_mcp_connections import (ConnectionScope, connection_lock, connection_ref, prepare_home,
                                    read_connection, _directory, _read_private, _write_private)
from .kiro_headless import SessionError, read_key
from .kiro_mcp import KiroMcpReader
from .kiro_mcp import _metadata_root, validate_design_context_output
from .managed_execution_progress import managed_execution_context
from .managed_prompts import current_managed_release
from .figma_mcp_capabilities import mcp_capability_receipt

router = APIRouter(prefix="/internal/v1/figma-mcp", include_in_schema=False)
_SAFE_CODES = {"FIGMA_MCP_AUTH_REQUIRED", "FIGMA_MCP_CONFIG_REQUIRED", "FIGMA_MCP_CONNECTION_REVOKED",
               "FIGMA_MCP_EVIDENCE_INCOMPLETE", "FIGMA_MCP_SCOPE_FORBIDDEN", "FIGMA_MCP_COLLECTION_FAILED",
               "FIGMA_MCP_SNAPSHOT_CHANGED", "FIGMA_MCP_SNAPSHOT_UNAVAILABLE", "FIGMA_MCP_RATE_LIMITED",
               "KIRO_KEY_AUTH_REQUIRED", "KIRO_KEY_MISSING", "KIRO_KEY_INVALID"}
logger = logging.getLogger(__name__)


class Collection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fileKey: str = Field(pattern=r"^[A-Za-z0-9]{1,160}$")
    nodeId: str = Field(pattern=r"^(?:[0-9]+:[0-9]+|I[0-9]+:[0-9]+(?:;[0-9]+:[0-9]+)*)$")
    snapshotId: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


class SnapshotStore:
    def __init__(self, directory, scope, job_id, file_key, node_id):
        self.directory, self.scope = directory, scope
        self.identity = {"connectionRef": connection_ref(scope), "jobId": job_id,
                         "fileKey": file_key, "nodeId": node_id}
        self.key = sha256(_encode(self.identity)).hexdigest()
        self.root = directory / "snapshots"

    def load(self, expected=None):
        index = self.root / (self.key + ".index.json")
        if not index.exists() and not index.is_symlink():
            if expected:
                raise SessionError("FIGMA_MCP_SNAPSHOT_UNAVAILABLE", "저장된 Figma MCP 근거를 찾을 수 없습니다.")
            return None
        try:
            entry = _read_private(index)
            digest = entry.get("snapshotId")
            if entry.get("identity") != self.identity or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError
            if expected is not None and digest != expected:
                raise ValueError
            value = _read_private(self.root / (digest + ".json"), limit=64 * 1024 * 1024)
            if value.get("identity") != self.identity or sha256(_encode(value)).hexdigest() != digest:
                raise ValueError
            return self.response(value, digest, reused=True)
        except (SessionError, ValueError, TypeError, OSError):
            raise SessionError("FIGMA_MCP_SNAPSHOT_CHANGED", "저장된 Figma MCP 근거의 소유 범위 또는 해시가 일치하지 않습니다.") from None

    def save(self, collected):
        # Caller holds the per-connection lock. No index is made visible until
        # the whole tool transcript has been validated and fsynced atomically.
        value = {"schemaVersion": 1, "identity": self.identity, "collectedAtEpochSeconds": int(time()), **collected}
        digest = sha256(_encode(value)).hexdigest()
        response = self.response(value, digest, reused=False)
        _directory(self.root)
        path = self.root / (digest + ".json")
        if not path.exists():
            _write_private(path, value)
        _write_private(self.root / (self.key + ".index.json"), {"identity": self.identity, "snapshotId": digest})
        return response

    def save_failure(self, reader, error):
        # Separate tree, no snapshot/index registration and no source reuse API.
        # The connection lock and the original managed-job identity still own it.
        from .failed_mcp_evidence import write_private_object
        report, objects = reader.failed_evidence(error)
        value = {**report, "identity": self.identity, "collectedAtEpochSeconds": int(time())}
        digest = sha256(_encode(value)).hexdigest()
        root = self.directory / "failed-evidence"
        _directory(root)
        for object_digest, body in objects.items():
            write_private_object(root / "objects", object_digest, body)
        _write_private(root / (digest + ".failure.json"), value)
        return digest

    @staticmethod
    def response(value, digest, reused):
        tools = value["toolEvidence"]
        design_assessments = []
        for call in tools:
            if any(call["output"].get(key) is True for key in ("truncated", "isTruncated", "hasMore")):
                raise SessionError("FIGMA_MCP_EVIDENCE_INCOMPLETE", "저장된 Figma MCP 원문이 잘려 다시 수집해야 합니다.")
            if call["name"] == "get_design_context":
                # The same raw-content guard runs again on historical cache
                # entries; a completed provider question is not source input.
                design_assessments.append(validate_design_context_output(call["output"]))
        provenance = {"provider": "figma-mcp", "revisionStatus": "UNVERIFIED", "comparison": "REFERENCE_ONLY",
                      "collectedAtEpochSeconds": value["collectedAtEpochSeconds"], "snapshotId": digest,
                      "promptReleaseId": value.get("promptReleaseId"), "acquisitionTemplates": value.get("acquisitionTemplates", {}),
                      "providerReceipt": value.get("providerReceipt", {}),
                      "capabilities": mcp_capability_receipt(value, digest)}
        metadata_nodes, properties = {}, set()
        for call in tools:
            if call["name"] != "get_metadata":
                continue
            for item in call["output"].get("content", []):
                text = item.get("text") if item.get("type") == "text" else None
                if not isinstance(text, str) or "<!" in text:
                    continue
                try:
                    root = _metadata_root(text)
                    for node in root.iter():
                        if node_id := node.attrib.get("id"):
                            metadata_nodes[node_id] = {"type": node.tag, "properties": sorted(node.attrib)}
                            properties.update(node.attrib)
                except (ET.ParseError, ValueError, TypeError):
                    # Raw text is still retained. Failure to interpret a metadata
                    # fragment can never be counted as complete authored data.
                    continue
        coverage = {"status": "PARTIAL", "transportRawPreserved": True,
                    "tools": sorted({item["name"] for item in tools}),
                    "designContextReadiness": {"status": "CONTEXT_OBSERVED" if design_assessments
                        and all(item["status"] == "CONTEXT_OBSERVED" for item in design_assessments) else "RETRY_REQUIRED",
                        "contextCount": len(design_assessments),
                        "implementationBlockCount": sum(item.get("implementationBlockCount", 0) for item in design_assessments),
                        "geometryBlockCount": sum(item.get("geometryBlockCount", 0) for item in design_assessments),
                        "codes": sorted({code for item in design_assessments for code in item["codes"]}),
                        "assessmentSha256": sha256(_encode(design_assessments)).hexdigest(), "authoredCompleteness": "NOT_PROVEN"},
                    "truncation": {"status": "NOT_DETECTED", "scope": "recognized-provider-notices-and-output-flags",
                                   "authoredCompleteness": "NOT_PROVEN"},
                    "metadata": {"nodeCount": len(metadata_nodes), "nodes": metadata_nodes,
                                 "availableProperties": sorted(properties)},
                    "authoredDescriptions": {"status": "NOT_PROVEN", "reason": "Metadata and reference code do not prove all authored planning descriptions."},
                    "unknown": ["originalFigmaRevision", "completeAuthoredScene", "sameRevision1xPng",
                                "allComponentProperties", "allAuthoredDescriptions", "omissionsWithoutProviderMarkers"]}
        return {"schemaVersion": 1, "provider": "figma-mcp", "snapshotId": digest, "reused": reused,
                "toolEvidence": tools, "sourceContext": {"provider": "figma-mcp", "toolEvidence": tools,
                                                           "provenance": provenance, "coverage": coverage},
                "coverage": coverage, "provenance": provenance, "performance": value.get("performance", {})}


def _scope(context, payload):
    if (context is None or not context.alive() or context.purpose != "screen" or context.mode != "headless" or not context.remote
            or context.file_key != payload.fileKey or context.node_id != payload.nodeId
            or not context.actor_user_id or not context.execution_scope):
        raise HTTPException(403, "FIGMA_MCP_SCOPE_FORBIDDEN")
    return ConnectionScope(tenantId=context.execution_scope[0], workspaceId=context.execution_scope[1],
                           actorUserId=context.actor_user_id)


def _safe_error(error):
    code = error.code
    if code == "FIGMA_MCP_AUTH_REVOKED":
        return "FIGMA_MCP_CONNECTION_REVOKED"
    if code in {"FIGMA_MCP_TOOL_SCOPE_DENIED", "FIGMA_MCP_INVALID_INPUT"}:
        return "FIGMA_MCP_SCOPE_FORBIDDEN"
    return code if code in _SAFE_CODES else "FIGMA_MCP_COLLECTION_FAILED"


@router.post("/collect")
async def collect(request: Request):
    if (request.headers.get("x-sfdc-agent") != "1" or "origin" in request.headers or "sec-fetch-site" in request.headers):
        raise HTTPException(403, "FIGMA_MCP_SCOPE_FORBIDDEN")
    context = managed_execution_context(request)
    body = bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 4096:
                    raise ValueError
        payload = Collection.model_validate_json(body)
    except (ValidationError, ValueError, TimeoutError):
        raise HTTPException(422, "FIGMA_MCP_SCOPE_FORBIDDEN") from None
    scope = _scope(context, payload)
    try:
        directory, _ = read_connection(scope)
        with connection_lock(directory):
            store = SnapshotStore(directory, scope, context.job_id, payload.fileKey, payload.nodeId)
            cached = store.load(payload.snapshotId)
            if cached:
                return cached
            _, home, work, server = prepare_home(scope)
            release = current_managed_release(request)
            reader = KiroMcpReader(home=home, work=work, server=server, file_key=payload.fileKey,
                                   node_id=payload.nodeId, key=read_key(), templates=release.templates, prompt_release_id=release.id,
                                   connection_scope=scope)
            task = asyncio.create_task(reader.collect(timeout=max(1, min(180, context.expires - monotonic()))))
            try:
                # Retiring a managed job closes bound sessions and cancels this
                # collection before any partial snapshot can become resumable.
                class OwnedCollection:
                    async def close(self):
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                owned = OwnedCollection()
                context.bind(owned)
                collected = await task
                _scope(context, payload)
                return store.save(collected)
            except Exception as error:
                try:
                    store.save_failure(reader, error)
                except Exception:
                    # No source prose, auth state or exception text in logs; the
                    # original collection failure remains the product outcome.
                    logger.warning("Figma MCP failed-evidence persistence unavailable")
                raise
            finally:
                context.sessions.discard(owned)
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
    except SessionError as error:
        busy = error.code == "FIGMA_MCP_BUSY"
        limited = error.code == "FIGMA_MCP_RATE_LIMITED"
        message = str(error) if error.code in _SAFE_CODES else "Kiro·Figma MCP 연결과 원본 수집 상태를 확인해 주세요."
        return JSONResponse({"error": {"code": _safe_error(error), "message": message}},
                            status_code=429 if limited else 503 if busy else 502, headers={"Retry-After": "5"} if busy else None)

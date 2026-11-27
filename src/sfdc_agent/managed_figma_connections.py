"""Ephemeral Spring authorization to reuse the operator's REST/Plan identity.

This proof contains no provider secret and is never a Node/model/storage DTO.
Current membership and binding revocation remain Spring's responsibility.
"""

from hashlib import sha256
import hmac
import os
from time import time
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

Digest = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
Version = Annotated[int, Field(strict=True, ge=1, le=9_007_199_254_740_991)]


class FigmaConnectionProof(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    schemaVersion: Annotated[int, Field(strict=True, ge=1, le=1)]
    connectionId: UUID
    connectionVersion: Version
    bindingVersion: Version
    tenantId: UUID
    workspaceId: UUID
    runtimeProfile: Literal['PROCESS_DEFAULT']
    purpose: Literal['EXECUTION', 'DISCOVERY', 'REFERENCE', 'DESIGN_BUILD']
    contextSha256: Digest
    issuedAtEpochSeconds: Annotated[int, Field(strict=True, gt=0)]
    expiresAtEpochSeconds: Annotated[int, Field(strict=True, gt=0)]
    signature: Digest


def _get(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _digest(value):
    return sha256(value.encode('utf-8')).hexdigest()


def request_context(payload, purpose):
    """Fixed ASCII framing; no cross-language reserialization of authored JSON."""
    source = _get(payload, 'sourceReference')
    if purpose == 'EXECUTION':
        scope, archive = _get(payload, 'executionScope'), _get(payload, 'archive')
        values = [str(_get(payload, 'jobId')), str(_get(payload, 'attempt')), _get(payload, 'kind'),
                  _get(payload, 'engine'), _get(payload, 'operation', 'ANALYZE'), _digest(source),
                  str(_get(scope, 'actorUserId') or '-'), _get(payload, 'renderNodeId') or '-', _get(archive, 'sha256') or '-']
    elif purpose == 'DISCOVERY':
        values = [_digest(source), _get(payload, 'pageId') or '-', _get(payload, 'version') or '-']
    elif purpose == 'REFERENCE':
        values = [_digest(_get(payload, 'reference'))]
    elif purpose == 'DESIGN_BUILD':
        values = [str(_get(payload, 'buildId')), _get(payload, 'baseManifestSha256')]
        sources = _get(payload, 'sources')
        for name in ('guide', 'library'):
            item = _get(sources, name)
            values.append(':'.join([name, _get(item, 'fileKey'), _get(item, 'revision'), _get(item, 'nodeId') or '-']) if item else '-')
    else:
        raise ValueError('Invalid Figma proof purpose')
    return _digest('\n'.join([purpose, *values]))


def arguments(payload, purpose):
    proof = _get(payload, 'figmaConnection')
    return {} if proof is None else {'proof': proof, 'purpose': purpose, 'context': request_context(payload, purpose),
                                     'deadline': _get(payload, 'deadlineEpochSeconds') if purpose == 'EXECUTION' else None}


def verify(proof, selected, allocated, purpose, context, deadline=None):
    """Only a same-owner-tenant proof can expand the legacy workspace allocation."""
    try:
        proof = proof if isinstance(proof, FigmaConnectionProof) else FigmaConnectionProof.model_validate(proof)
        key = os.environ.get('SFDC_AGENT_EXECUTION_TOKEN', '')
        now = time()
        if (not key or any(char.isspace() for char in key) or len(selected) != 2
                or selected != (str(proof.tenantId), str(proof.workspaceId)) or allocated[0] != selected[0]
                or proof.purpose != purpose or proof.contextSha256 != context
                or proof.issuedAtEpochSeconds > now + 30 or proof.expiresAtEpochSeconds <= now
                or purpose == 'EXECUTION' and (type(deadline) is not int or proof.expiresAtEpochSeconds != deadline + 30)
                or not 0 < proof.expiresAtEpochSeconds - proof.issuedAtEpochSeconds <= 960):
            return False
        value = '\n'.join(['sfdc-figma-connection-v1', str(proof.connectionId), str(proof.connectionVersion), str(proof.bindingVersion),
                           str(proof.tenantId), str(proof.workspaceId), proof.runtimeProfile, proof.purpose, proof.contextSha256,
                           str(proof.issuedAtEpochSeconds), str(proof.expiresAtEpochSeconds)])
        expected = hmac.new(key.encode('utf-8'), value.encode('utf-8'), 'sha256').hexdigest()
        return hmac.compare_digest(expected, proof.signature)
    except (ValueError, TypeError, AttributeError):
        return False

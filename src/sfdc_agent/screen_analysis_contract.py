"""Bindings for the saved Screen contract; opaque scene interpretation stays in Node."""

import base64
from hashlib import sha256
import json
import re
from urllib.parse import parse_qs, urlsplit

from .strict_json import loads
from .logical_scope_contract import validate_logical_scope, validate_scope_review

_HASH = re.compile(r'^[a-f0-9]{64}$')
_ENGINES = {'NOVA', 'KIRO'}
_MAX_BYTES = 3 * 1024 * 1024
_ACTUAL = {'REST_FIRST': {'REST', 'LOCAL_FILE'}, 'REST_ONLY': {'REST'},
           'MCP_ONLY': {'MCP'}, 'LOCAL_FILE': {'LOCAL_FILE'}}


def _invalid():
    return ValueError('SCREEN_ANALYSIS_CONTRACT_INVALID')


def _hash(value):
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def validate_planning_selection(analysis, captured, reference, render_node=None):
    selection = analysis.get('planningSelection')
    contract = analysis.get('contract', {})
    if selection is None:
        if 'planningSelection' in analysis or 'planningSelectionSha256' in contract or 'planningSelectionSha256' in captured or render_node is not None:
            raise _invalid()
        return None
    fields = {'schemaVersion', 'requestedNodeId', 'planningRootId', 'renderNodeId', 'sourceVersion',
              'planningSourceSha256', 'renderSourceSha256', 'evidenceSha256'}
    if not isinstance(selection, dict) or set(selection) != fields or type(selection.get('schemaVersion')) is not int or selection['schemaVersion'] != 1:
        raise _invalid()
    for field in ('requestedNodeId', 'planningRootId', 'renderNodeId'):
        value = selection[field]
        if not isinstance(value, str) or len(value) > 160 or re.fullmatch(r'[0-9]+:[0-9]+', value) is None:
            raise _invalid()
    if not isinstance(selection['sourceVersion'], str) or re.fullmatch(r'[A-Za-z0-9_-]{1,160}', selection['sourceVersion']) is None:
        raise _invalid()
    if any(not _hash(selection[field]) for field in ('planningSourceSha256', 'renderSourceSha256', 'evidenceSha256')):
        raise _invalid()
    requested = parse_qs(urlsplit(reference).query).get('node-id', [])
    source = analysis.get('source', {})
    if (len(requested) != 1 or requested[0].replace('-', ':') != selection['requestedNodeId']
            or source.get('nodeId') != selection['renderNodeId'] or source.get('version') != selection['sourceVersion']
            or render_node is not None and render_node != selection['renderNodeId']):
        raise _invalid()
    # This receipt contains only bounded ASCII strings and integer 1. Opaque
    # authored scene numbers are never reserialized to imitate Node hashes.
    digest = sha256(json.dumps(selection, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    if contract.get('planningSelectionSha256') != digest or captured.get('planningSelectionSha256') != digest:
        raise _invalid()
    return selection


def _planning_image_source(analysis, captured, reference, selection):
    """Authorize only the saved, receipt-bound render identity, never model fields.

    Older receipts without source evidence keep their existing context. They do
    not gain wrapper-to-child image authority from an unverified alias.
    """
    evidence = analysis.get('sourceEvidence')
    if selection is None or not isinstance(evidence, dict) or evidence.get('schemaVersion') != 2:
        return None
    identity, source = evidence.get('identity'), analysis.get('source', {})
    contract = analysis['contract']
    url = urlsplit(reference)
    match = re.match(r'^/(?:design|file)/([A-Za-z0-9]{1,160})(?:/|$)', url.path)
    file_key = match[1] if url.scheme == 'https' and match else url.netloc if url.scheme == 'figma-local' else None
    versions = parse_qs(url.query).get('version-id', parse_qs(url.query).get('version', []))
    if (not isinstance(identity, dict) or not file_key or source.get('fileKey') != file_key
            or identity.get('fileKey') != file_key or identity.get('nodeId') != selection['renderNodeId']
            or source.get('nodeId') != selection['renderNodeId']
            or identity.get('actualRevision') != selection['sourceVersion']
            or versions and (len(versions) != 1 or versions[0] != selection['sourceVersion'])
            or identity.get('requestedRevision') is not None
            and identity['requestedRevision'] != selection['sourceVersion']
            or not _hash(evidence.get('rawSnapshotSha256'))
            or evidence['rawSnapshotSha256'] != contract['sourceDigest']
            or evidence['rawSnapshotSha256'] != captured['sourceDigest']
            or not _hash(evidence.get('sceneSha256'))
            or evidence['sceneSha256'] != contract.get('sceneSha256')
            or evidence['sceneSha256'] != captured.get('sceneSha256')):
        raise _invalid()
    return {'fileKey': file_key, 'nodeId': selection['renderNodeId'],
            'sourceVersion': selection['sourceVersion'], 'analysisSourceDigest': analysis['sourceDigest']}


def validate_checkpoint(payload, checkpoint):
    """Return small validated contract facts, or None for a legacy checkpoint.

    The exact checkpoint bytes bind opaque input and its legacy Node input hash.
    Do not reserialize authored numbers in Python to imitate JSON.stringify.
    """
    if payload.kind != 'SCREEN' or not isinstance(checkpoint, dict) or not isinstance(checkpoint.get('bytes'), str):
        return None
    # Historic direct integrations did not supply a versioned checkpoint receipt.
    # The Node legacy path retains its existing validation and cannot switch engines.
    if payload.sourceAcquisition is None and not _hash(checkpoint.get('digest')):
        return None
    try:
        encoded = checkpoint['bytes']
        if len(encoded) > _MAX_BYTES * 2 or not _hash(checkpoint.get('digest')):
            raise _invalid()
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > _MAX_BYTES or sha256(raw).hexdigest() != checkpoint['digest']:
            raise _invalid()
        record = loads(raw, error_factory=_invalid)
        analysis = record.get('analysis', {})
        if not isinstance(analysis, dict):
            raise _invalid()
        contract = analysis.get('contract')
        if contract is None:
            if ('allowedGenerationEngines' in analysis or 'generationOptions' in analysis or 'logicalScope' in analysis
                    or isinstance(record.get('input'), dict) and isinstance(record['input'].get('screenInput'), dict)
                    and 'logicalScopeSha256' in record['input']['screenInput'] or 'scopeReview' in record):
                raise _invalid()
            return None
        source_input = record.get('input', {})
        captured = source_input.get('screenInput', {}) if isinstance(source_input, dict) else {}
        if (not isinstance(contract, dict) or type(contract.get('schemaVersion')) is not int or contract['schemaVersion'] != 1
                or not isinstance(captured, dict) or type(captured.get('schemaVersion')) is not int or captured['schemaVersion'] != 1
                or analysis.get('id') != checkpoint.get('id') or not _hash(analysis.get('digest'))
                or not _hash(analysis.get('sourceDigest'))):
            raise _invalid()
        analysis_engine = contract.get('analysisEngine')
        if (analysis_engine not in _ENGINES or analysis.get('analysisMethod') != analysis_engine.lower()
                or payload.operation == 'ANALYZE' and analysis_engine != payload.engine
                or contract.get('sourceAcquisition') != payload.sourceAcquisition
                or contract.get('actualAcquisition') not in _ACTUAL.get(payload.sourceAcquisition, set())
                or not _hash(contract.get('sourceDigest')) or not _hash(contract.get('generatorSourceSha256'))
                or contract.get('designReleaseId') != payload.designReleaseId
                or contract.get('promptReleaseId') != payload.promptReleaseId):
            raise _invalid()
        for field in ('sourceAcquisition', 'actualAcquisition', 'sourceDigest', 'sceneSha256'):
            if contract.get(field) != captured.get(field):
                raise _invalid()
        if 'sceneSha256' in contract and not _hash(contract['sceneSha256']):
            raise _invalid()
        planning_selection = validate_planning_selection(analysis, captured, payload.sourceReference, getattr(payload, 'renderNodeId', None))
        logical_scope = validate_logical_scope(analysis, captured)
        scope_review = validate_scope_review(logical_scope, getattr(payload, 'scopeReview', None)) if payload.operation == 'GENERATE' else None
        actual, storage = contract['actualAcquisition'], captured.get('sourceStorage')
        if actual == 'MCP':
            if ('sceneSha256' in contract or storage != 'mcp-evidence' or not isinstance(source_input.get('mcpSource'), dict)
                    or source_input['mcpSource'].get('snapshotId') != contract['sourceDigest']):
                raise _invalid()
        elif storage == 'nova-plan':
            if not isinstance(source_input.get('plan'), str) or not source_input['plan']:
                raise _invalid()
        elif storage == 'snapshot':
            if not isinstance(captured.get('snapshot'), str) or not captured['snapshot']:
                raise _invalid()
        else:
            raise _invalid()
        if actual == 'LOCAL_FILE':
            archive = payload.archive
            source = analysis.get('source', {})
            if (archive is None or source.get('local') is not True
                    or source.get('version') != 'local-' + archive.sha256):
                raise _invalid()
        engines, options = analysis.get('allowedGenerationEngines'), analysis.get('generationOptions')
        if (not isinstance(engines, list) or len(engines) > 2 or any(not isinstance(engine, str) or engine not in _ENGINES for engine in engines)
                or len(set(engines)) != len(engines) or not isinstance(options, list) or len(options) > 2
                or 'NOVA' in engines and (actual == 'MCP' or storage != 'nova-plan')):
            raise _invalid()
        seen = set()
        for option in options:
            if not isinstance(option, dict) or option.get('engine') not in _ENGINES or type(option.get('available')) is not bool:
                raise _invalid()
            engine = option['engine']
            if engine in seen or option['available'] != (engine in engines):
                raise _invalid()
            seen.add(engine)
            if 'code' in option and (not isinstance(option['code'], str) or re.fullmatch(r'[A-Z][A-Z0-9_]{0,99}', option['code']) is None):
                raise _invalid()
            if 'message' in option and (not isinstance(option['message'], str) or len(option['message']) > 1000):
                raise _invalid()
        if not set(engines) <= seen or payload.operation == 'GENERATE' and payload.engine not in engines:
            raise _invalid()
        image_source = _planning_image_source(analysis, captured, payload.sourceReference, planning_selection)
        return {'analysisDigest': analysis['digest'], 'engines': tuple(engines), 'contract': contract,
                'logicalScope': logical_scope, 'scopeReview': scope_review,
                **({'planningImageSource': image_source} if image_source else {})}
    except (KeyError, TypeError, AttributeError, UnicodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise _invalid() from None

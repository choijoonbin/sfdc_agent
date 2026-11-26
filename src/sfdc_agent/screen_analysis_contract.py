"""Bindings for the saved Screen contract; opaque scene interpretation stays in Node."""

import base64
from hashlib import sha256
import json
import re

_HASH = re.compile(r'^[a-f0-9]{64}$')
_ENGINES = {'NOVA', 'KIRO'}
_MAX_BYTES = 3 * 1024 * 1024
_ACTUAL = {'REST_FIRST': {'REST', 'LOCAL_FILE'}, 'REST_ONLY': {'REST'},
           'MCP_ONLY': {'MCP'}, 'LOCAL_FILE': {'LOCAL_FILE'}}


def _invalid():
    return ValueError('SCREEN_ANALYSIS_CONTRACT_INVALID')


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def _hash(value):
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


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
        record = json.loads(raw, object_pairs_hook=_object,
                            parse_constant=lambda _: (_ for _ in ()).throw(_invalid()))
        analysis = record.get('analysis', {})
        if not isinstance(analysis, dict):
            raise _invalid()
        contract = analysis.get('contract')
        if contract is None:
            if 'allowedGenerationEngines' in analysis or 'generationOptions' in analysis:
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
        return {'analysisDigest': analysis['digest'], 'engines': tuple(engines), 'contract': contract}
    except (KeyError, TypeError, AttributeError, UnicodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise _invalid() from None

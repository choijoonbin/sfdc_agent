"""Preserve the generated entry's public interface without inventing runtime support."""

import base64
from hashlib import sha256
import math
import re
from xml.etree import ElementTree

from .strict_json import decode_json

TARGET = 'lightning__AppPage'
_ALIAS = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]{0,79}$')
_NAME = re.compile(r'^[a-z][A-Za-z0-9]{0,79}$')
_HOST = {'supportedTargets': [TARGET], 'recordContext': 'unsupported', 'flow': 'unsupported', 'quickAction': 'unsupported'}


def checked_field_aliases(value):
    if not isinstance(value, dict) or len(value) > 512:
        raise ValueError('GENERATED_SCREEN_ALIASES_INVALID')
    aliases = set()
    for source, alias in value.items():
        if (not isinstance(source, str) or not 1 <= len(source) <= 240
                or any(ord(char) < 32 for char in source) or source in {'__proto__', 'prototype', 'constructor'}
                or not isinstance(alias, str) or not _ALIAS.fullmatch(alias)
                or alias in {'__proto__', 'prototype', 'constructor'} or alias in aliases):
            raise ValueError('GENERATED_SCREEN_ALIASES_INVALID')
        aliases.add(alias)
    return value


def checked_contract(value):
    def require(condition):
        if not condition:
            raise ValueError('GENERATED_SCREEN_CONTRACT_INVALID')

    require(isinstance(value, dict) and set(value) == {'schemaVersion', 'bundleName', 'generationKind',
            'genericModelSupported', 'host', 'statePolicy', 'businessVerified', 'fieldAliases', 'fields',
            'dataBindings', 'actions', 'publicApi', 'legacyApi'})
    require(type(value['schemaVersion']) is int and value['schemaVersion'] == 1
            and isinstance(value['bundleName'], str) and _NAME.fullmatch(value['bundleName'])
            and value['generationKind'] in {'NOVA_SCREEN', 'KIRO_SCREEN', 'STORY'}
            and type(value['genericModelSupported']) is bool and value['businessVerified'] is False
            and value['host'] == _HOST)
    aliases = checked_field_aliases(value['fieldAliases'])
    for name in ('fields', 'dataBindings', 'actions'):
        require(isinstance(value[name], list) and len(value[name]) <= 2048)
    fields, sources = {}, set()
    for field in value['fields']:
        require(isinstance(field, dict) and isinstance(field.get('key'), str) and _ALIAS.fullmatch(field['key'])
                and field['key'] not in fields and isinstance(field.get('sourceNodeId'), str)
                and bool(field['sourceNodeId']) and field['sourceNodeId'] not in sources
                and field.get('type') in {'string', 'boolean', 'number', 'string[]', 'date-range'}
                and type(field.get('stableBusinessKey')) is bool
                and field.get('identityScope') in {'explicit-alias', 'source-revision'})
        initial = field.get('initialValue')
        require(type(initial) is bool if field['type'] == 'boolean' else
                type(initial) in (int, float) and math.isfinite(initial) if field['type'] == 'number' else
                isinstance(initial, list) and all(isinstance(item, str) for item in initial) if field['type'] == 'string[]' else
                isinstance(initial, dict) and set(initial) == {'startValue', 'endValue'}
                and all(isinstance(item, str) for item in initial.values()) if field['type'] == 'date-range' else isinstance(initial, str))
        explicit = field['sourceNodeId'] in aliases
        require(field['stableBusinessKey'] == explicit and field['identityScope'] == ('explicit-alias' if explicit else 'source-revision')
                and field['key'] not in {'__proto__', 'prototype', 'constructor'} and (not explicit or field['key'] == aliases[field['sourceNodeId']])
                and (field.get('alias') == field['key'] if explicit else 'alias' not in field))
        fields[field['key']] = field
        sources.add(field['sourceNodeId'])
    require(set(aliases) <= sources)
    for collection in ('dataBindings', 'actions'):
        seen = set()
        for item in value[collection]:
            require(isinstance(item, dict) and isinstance(item.get('key'), str) and _ALIAS.fullmatch(item['key'])
                    and item['key'] not in {'__proto__', 'prototype', 'constructor'}
                    and item['key'] not in seen and isinstance(item.get('sourceNodeId'), str))
            seen.add(item['key'])
            if collection == 'dataBindings':
                require(item.get('type') in {'table-rows', 'history-rows', 'options', 'string[]', 'sort'}
                        and (item.get('initialValue') is None or isinstance(item.get('initialValue'), dict)
                             if item['type'] == 'sort' else isinstance(item.get('initialValue'), list)))
            else:
                require(isinstance(item.get('fieldKeys'), list) and all(isinstance(key, str) for key in item['fieldKeys'])
                        and set(item['fieldKeys']) <= set(fields))
    api = value['publicApi']
    require(isinstance(api, dict) and isinstance(value['legacyApi'], dict) and api.get('eventPropagation') == {'bubbles': False, 'composed': False}
            and all(type(api['eventPropagation'][key]) is bool for key in ('bubbles', 'composed'))
            and api.get('localEvents') == 'legacy-source-events-preserved')
    for name in ('properties', 'methods', 'events'):
        require(isinstance(api.get(name), list) and all(isinstance(item, str) for item in api[name]))
        require(isinstance(value['legacyApi'].get(name), list)
                and all(isinstance(item, str) for item in value['legacyApi'][name]))
    if value['genericModelSupported']:
        require(value['generationKind'] == 'NOVA_SCREEN' and value['statePolicy'] == 'local-default-controlled-opt-in'
                and api['properties'] == ['screenModel']
                and api['methods'] == ['getScreenSnapshot', 'validate', 'reportValidity', 'reset', 'requestAction']
                and api['events'] == ['screenchange', 'screenaction', 'screenreset'])
    else:
        require(value['statePolicy'] == 'generator-owned' and all(not api[name] for name in ('properties', 'methods', 'events')))
    return value


def verify_generated_screen_contract(result, *, required=False, expected_aliases=None, kind=None, engine=None):
    """Check sidecar bytes, summary, entry metadata and the emitted NOVA contract together.

    Legacy results without a contract remain readable. New releases/options can
    require this interface; a worker can never silently drop a declared one.
    """
    summary = dict(result.summary or {})
    sidecars = [artifact for artifact in result.artifacts if artifact.name.startswith('contracts/')]
    contract = summary.get('generatedScreenContract')
    if contract is None and not sidecars and not required:
        return result
    checked_contract(contract)
    expected_kind = 'STORY' if kind == 'STORY' else 'KIRO_SCREEN' if engine == 'KIRO' else 'NOVA_SCREEN'
    if (kind and contract['generationKind'] != expected_kind
            or expected_aliases is not None and contract['fieldAliases'] != expected_aliases):
        raise ValueError('GENERATED_SCREEN_CONTRACT_MISMATCH')
    entry = contract['bundleName']
    if len(sidecars) != 1 or sidecars[0].name != f'contracts/{entry}.json':
        raise ValueError('GENERATED_SCREEN_CONTRACT_MISSING')
    raw = base64.b64decode(sidecars[0].base64, validate=True)
    if len(raw) > 256 * 1024 or decode_json(raw) != contract or summary.get('generatedScreenContractSha256') != sha256(raw).hexdigest():
        raise ValueError('GENERATED_SCREEN_CONTRACT_MISMATCH')
    artifacts = {artifact.name: base64.b64decode(artifact.base64, validate=True) for artifact in result.artifacts}
    metadata = artifacts.get(f'lwc/{entry}/{entry}.js-meta.xml')
    if metadata is None:
        raise ValueError('GENERATED_SCREEN_METADATA_MISSING')
    try:
        root = ElementTree.fromstring(metadata)
        ns = '{http://soap.sforce.com/2006/04/metadata}'
        targets = [target.text for target in root.findall(f'{ns}targets/{ns}target')]
        if root.tag != ns + 'LightningComponentBundle' or any(target != TARGET for target in targets) or len(targets) > 1:
            raise ValueError('GENERATED_SCREEN_CONTEXT_UNSUPPORTED')
        if root.findtext(ns + 'isExposed') == 'true' and targets != [TARGET]:
            raise ValueError('GENERATED_SCREEN_CONTEXT_UNSUPPORTED')
        for config in root.findall(f'{ns}targetConfigs/{ns}targetConfig'):
            if any(target.strip() != TARGET for target in config.attrib.get('targets', '').split(',')):
                raise ValueError('GENERATED_SCREEN_CONTEXT_UNSUPPORTED')
    except ElementTree.ParseError as error:
        raise ValueError('GENERATED_SCREEN_METADATA_INVALID') from error
    if contract['genericModelSupported']:
        source = artifacts.get(f'lwc/{entry}/{entry}.js', b'').decode()
        match = re.search(r'^const generatedScreenContract = (.+);$', source, re.MULTILINE)
        if match is None or decode_json(match.group(1).encode()) != contract:
            raise ValueError('GENERATED_SCREEN_RUNTIME_CONTRACT_MISMATCH')
    result_metadata = artifacts.get('managed-result-meta.json')
    if result_metadata is not None:
        original = decode_json(result_metadata)
        if (not isinstance(original, dict) or original.get('generatedScreenContract') != contract
                or original.get('generatedScreenContractSha256') != summary['generatedScreenContractSha256']):
            raise ValueError('GENERATED_SCREEN_CONTRACT_MISMATCH')
    # Preserve the exact worker declaration; no synthetic success/provenance.
    return result.model_copy(update={'summary': summary})

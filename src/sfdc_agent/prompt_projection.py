"""Lossless source deduplication and bounded caching of derived API guidance.

Original fields/checkpoints stay unchanged. Analysis retains the full API prose;
generation keeps full guidance for selected components and all module signatures.
"""

from functools import lru_cache
import hashlib
import json


def _same_json(left, right):
    try:
        encode = lambda value: json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))
        return encode(left) == encode(right)
    except (ValueError, TypeError, RecursionError):
        return False


def source_for_prompt(context):
    if not isinstance(context, dict):
        return context
    coverage = context.get('sourceEvidence', {}).get('coverage') if isinstance(context.get('sourceEvidence'), dict) else None
    if coverage is not None and _same_json(context.get('evidenceCoverage'), coverage):
        result = dict(context)
        result.pop('evidenceCoverage')
        result['evidenceCoverageRef'] = 'sourceEvidence.coverage'
        return result
    return context


def _used_tags(context):
    tags = set()
    def visit(value):
        if isinstance(value, dict):
            if isinstance(value.get('tag'), str):
                tags.add(value['tag'])
            for child in value.values():
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    for key, value in (context or {}).items():
        if key.startswith('verified') or key == 'historyTables':
            visit(value)
    return tuple(sorted(tags))


@lru_cache(maxsize=16)
def _contract_projection(serialized, selected):
    contract = json.loads(serialized)
    full_sha = hashlib.sha256(serialized.encode()).hexdigest()
    modules = contract['modules']
    full_modules = []
    for module in modules:
        if module.get('kind') != 'ui' or module.get('tag') in selected:
            full_modules.append(module['name'])
            continue
        # Only derived documentation for unused APIs is deferred. API names,
        # property types/defaults, methods, events, slots and style hooks remain.
        module.pop('eventDescriptions', None)
        for prop in module.get('properties', []):
            prop.pop('description', None)
    contract['promptProjection'] = {'schemaVersion': 1, 'mode': 'defer-unused-api-prose',
        'fullContractSha256': full_sha, 'completeModuleSignatures': True,
        'fullGuidanceModules': full_modules}
    return json.dumps(contract, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def contract_for_prompt(contract, context, *, stage):
    if stage != 'generation' or not isinstance(contract, dict) or not isinstance(context, dict):
        return contract
    modules = contract.get('modules')
    if not isinstance(modules, list) or any(not isinstance(m, dict) or not isinstance(m.get('name'), str) for m in modules):
        return contract
    if any('properties' in m and (not isinstance(m['properties'], list)
           or any(not isinstance(prop, dict) for prop in m['properties'])) for m in modules):
        return contract
    selected = _used_tags(context)
    known = {m.get('tag') for m in modules if isinstance(m.get('tag'), str)}
    if not selected or any(tag not in known for tag in selected):
        return contract
    serialized = json.dumps(contract, ensure_ascii=False, allow_nan=False, separators=(',', ':'), sort_keys=True)
    if len(serialized.encode()) > 512 * 1024:
        return contract
    # The content itself is the cache key. Never reuse another revision merely
    # because a file path, release label, model or timestamp happens to match.
    return json.loads(_contract_projection(serialized, selected))

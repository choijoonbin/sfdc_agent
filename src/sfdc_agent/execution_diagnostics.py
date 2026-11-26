"""Bounded, content-free worker failures; stderr remains private and discarded."""

import json
from importlib.resources import files


_POLICY = json.loads(files('sfdc_agent').joinpath('execution_diagnostics_policy.json').read_text())
MAX_ERROR_BYTES = 1024


def _unique(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise ValueError('Repeated diagnostic key.')
        value[key] = child
    return value


def safe_diagnostic(value):
    if not isinstance(value, dict) or not {'schemaVersion', 'code', 'stage'} <= value.keys():
        return None
    if value.keys() - {'schemaVersion', 'code', 'stage', 'retryAfterSeconds'}:
        return None
    code, stage = value.get('code'), value.get('stage')
    declared = _POLICY['codes'].get(code) if isinstance(code, str) else None
    if type(value.get('schemaVersion')) is not int or value['schemaVersion'] != 1 or not declared or not isinstance(stage, str):
        return None
    allowed_stages = {'analysis', 'generation'} if declared == 'operation' else {declared}
    if stage not in allowed_stages:
        return None
    if 'retryAfterSeconds' in value:
        retry = value['retryAfterSeconds']
        if code != 'FIGMA_RATE_LIMITED' or type(retry) is not int or not 1 <= retry <= _POLICY['maxRetryAfterSeconds']:
            return None
    return dict(value)


def decode_worker_failure(body):
    if len(body) > MAX_ERROR_BYTES:
        return None
    try:
        packet = json.loads(body, object_pairs_hook=_unique)
        if not isinstance(packet, dict) or set(packet) != {'protocolVersion', 'status', 'error'}:
            return None
        if type(packet['protocolVersion']) is not int or packet['protocolVersion'] != 1 or packet['status'] != 'FAILED':
            return None
        return safe_diagnostic(packet['error'])
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None


class WorkerExecutionFailure(Exception):
    def __init__(self, diagnostic):
        validated = safe_diagnostic(diagnostic)
        if validated is None:
            raise ValueError('Invalid worker diagnostic.')
        self.diagnostic = validated
        super().__init__(validated['code'])

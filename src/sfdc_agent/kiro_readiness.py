"""Read local configuration and recent, credential-bound Kiro authentication evidence.

This endpoint never starts a CLI, session, network request or model turn. A syntactic
key check and ``whoami`` output are not proof that the provider accepted a request.
Only the headless runner's completed model response records VERIFIED evidence.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hmac
import os
from pathlib import Path
from secrets import token_bytes
import shutil
from threading import RLock
from time import monotonic

from fastapi import APIRouter, Request

from .managed_prompts import _authorize

router = APIRouter(prefix='/internal/v1/kiro', include_in_schema=False)
AUTH_ERROR_CODES = frozenset({
    'KIRO_KEY_MISSING', 'KIRO_KEY_INVALID', 'KIRO_KEY_AUTH_REQUIRED',
})
_EVIDENCE_TTL_SECONDS = 300
_BINDING_SECRET = token_bytes(32)
_LOCK = RLock()


@dataclass(frozen=True)
class _Evidence:
    credential: bytes = field(repr=False)
    authentication: str
    checked_at: str
    checked_monotonic: float
    reason_code: str | None


# One record per process, never an unbounded history of credential fingerprints.
_EVIDENCE: _Evidence | None = None


def _credential_binding(key):
    return hmac.digest(_BINDING_SECRET, key.encode(), 'sha256')


def record_model_success(key):
    """Called only after a complete, tool-free headless model response is parsed."""
    global _EVIDENCE
    evidence = _Evidence(_credential_binding(key), 'VERIFIED',
                         datetime.now(timezone.utc).isoformat(), monotonic(), None)
    with _LOCK:
        _EVIDENCE = evidence


def record_auth_failure(key, code):
    """Preserve only an observed authentication error, never infer one from a timeout."""
    global _EVIDENCE
    if code not in AUTH_ERROR_CODES:
        return
    evidence = _Evidence(_credential_binding(key), 'FAILED',
                         datetime.now(timezone.utc).isoformat(), monotonic(), code)
    with _LOCK:
        _EVIDENCE = evidence


def _authentication(key):
    with _LOCK:
        evidence = _EVIDENCE
    if evidence is None or not hmac.compare_digest(evidence.credential, _credential_binding(key)):
        return {'authentication': 'UNKNOWN', 'checkedAt': None,
                'reasonCode': 'KIRO_AUTH_NOT_VERIFIED', 'ready': None}
    # Expired evidence is unknown, not a rejected key or a provider outage.
    if not 0 <= monotonic() - evidence.checked_monotonic < _EVIDENCE_TTL_SECONDS:
        return {'authentication': 'UNKNOWN', 'checkedAt': evidence.checked_at,
                'reasonCode': 'KIRO_AUTH_VERIFICATION_EXPIRED', 'ready': None}
    return {'authentication': evidence.authentication, 'checkedAt': evidence.checked_at,
            'reasonCode': evidence.reason_code, 'ready': evidence.authentication == 'VERIFIED'}


def readiness_snapshot():
    from .kiro_headless import SessionError, read_key
    installed = Path.home() / '.local/bin/kiro-cli'
    binary = shutil.which(os.environ.get('NOVA_KIRO_CLI', str(installed) if installed.is_file() else 'kiro-cli'))
    operations = []
    for purpose in ('screen', 'story'):
        state = {'purpose': purpose, 'executionMode': 'headless',
                 'configured': False, 'authentication': 'UNKNOWN', 'checkedAt': None,
                 'reasonCode': 'KIRO_UNAVAILABLE', 'ready': False}
        if binary:
            try:
                key = read_key()
            except SessionError as error:
                state['reasonCode'] = error.code if error.code in AUTH_ERROR_CODES else 'KIRO_KEY_INVALID'
            else:
                state.update(configured=True, **_authentication(key))
        operations.append(state)
    return {'provider': 'kiro', 'authenticationEvidenceTtlSeconds': _EVIDENCE_TTL_SECONDS,
            'operations': operations}


@router.get('/readiness')
async def readiness(request: Request):
    _authorize(request)
    return readiness_snapshot()

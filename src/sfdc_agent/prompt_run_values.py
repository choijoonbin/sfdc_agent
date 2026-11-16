"""Short-lived inspection of arguments substituted during interactive conversions."""

import re
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from time import monotonic
from uuid import uuid4


TRACE_HEADER = "x-sfdc-prompt-trace-owner"
_OWNER = re.compile(r"[0-9a-f]{64}")
_ACTIVE_CAPTURE = ContextVar("prompt_run_capture", default=None)
_RETAIN_SECONDS = 60 * 60
_MAX_RUNS = 96
_MAX_TOTAL_BYTES = 24 * 1024 * 1024
_MAX_RUN_BYTES = 2 * 1024 * 1024
_MAX_VALUE_BYTES = 1024 * 1024
_MAX_OCCURRENCES = 50

# Runtime connection secrets aren't template inputs. Also withhold evidence that
# looks like it contains credentials or a data URL embedded by an upstream tool.
_SENSITIVE = re.compile(
    r"(?ix)"
    r"\b(?:bearer|basic)\s+[a-z0-9+/_=.-]{10,}"
    r"|\b(?:figd_|ksk_|sk-(?:proj-|svcacct-)?|gh[pousr]_|github_pat_)[a-z0-9_\-]{12,}"
    r"|\beyJ[a-z0-9_-]{8,}\.[a-z0-9_-]{6,}\.[a-z0-9_-]{8,}"
    r"|\b00D[a-z0-9]{12}!\S{16,}"
    r"|-----BEGIN\s+(?:[A-Z]+\s+)?PRIVATE\s+KEY-----"
    r"|data:[a-z0-9.+\-/]+;base64,"
    r"|\biVBORw0KGgo[A-Za-z0-9+/]{30,}"
    r"|\b(?:authorization|set[-_ ]?cookie|cookie|(?:access|refresh|security|session|api|figma|oauth)[-_ ]*token"
    r"|(?:client|api)[-_ ]?secret|(?:api|private|subscription)[-_ ]?key|password|passwd|session[-_ ]?id"
    r"|x-sfdc-prompt-admin)"
    r"""\\?["']?\s*[:=]\s*(?:\\?["'](?=[^"'\s\\])|\{(?=\s*[^\s}])|\[(?=\s*[^\s\]])"""
    r"""|(?!true\b|false\b|null\b)[^"'\s{\[<\\])"""
)


def trace_owner(headers):
    value = headers.get(TRACE_HEADER, "")
    return value if _OWNER.fullmatch(value) else None


class _Capture:
    def __init__(self, operation):
        self.operation = operation
        self.values = defaultdict(list)
        self.size = 0

    def add(self, operation, section, values):
        # The same repair renderer also composes the evidence-backed plan retry.
        if self.operation == "screen.plan.repair" and operation == "screen.repair":
            values = {"diagnostic": values["diagnostic"]} if "diagnostic" in values else {}
        elif self.operation != operation:
            return
        for name, value in values.items():
            details = self.values[name]
            item = {"section": section}
            if len(details) >= _MAX_OCCURRENCES:
                if len(details) == _MAX_OCCURRENCES:
                    details.append({**item, "omitted": True})
                continue
            size = len(value.encode())
            if size > _MAX_VALUE_BYTES or self.size + size > _MAX_RUN_BYTES:
                details.append({**item, "omitted": True})
            elif name == "prompt" or _SENSITIVE.search(value):
                details.append({**item, "redacted": True})
            else:
                details.append({**item, "value": value})
                self.size += size


@contextmanager
def capture_prompt_run(operation):
    capture = _Capture(operation)
    token = _ACTIVE_CAPTURE.set(capture)
    try:
        yield capture
    finally:
        _ACTIVE_CAPTURE.reset(token)


def record_prompt_arguments(operation, section, values):
    capture = _ACTIVE_CAPTURE.get()
    if capture is not None:
        capture.add(operation, section, values)


class PromptRunValues:
    """Keep only the latest successful invocation for each owner and operation."""

    def __init__(self):
        self._runs = OrderedDict()
        self._size = 0

    def _remove(self, key):
        _, size, _, _ = self._runs.pop(key)
        self._size -= size

    def purge(self):
        now = monotonic()
        for key, (started, _, _, _) in list(self._runs.items()):
            if now - started >= _RETAIN_SECONDS:
                self._remove(key)

    def save(self, owner, capture, *, started, release, mode):
        self.purge()
        key = (owner, capture.operation)
        previous = self._runs.get(key)
        if previous and previous[0] > started:
            return
        if previous:
            self._remove(key)
        run = {
            "id": uuid4().hex,
            "executedAt": datetime.now(timezone.utc).isoformat(),
            "promptReleaseId": release.id,
            "promptVersion": release.versions[capture.operation],
            "executionMode": mode,
        }
        self._runs[key] = (started, capture.size, run, dict(capture.values))
        self._size += capture.size
        while len(self._runs) > _MAX_RUNS or self._size > _MAX_TOTAL_BYTES:
            self._remove(next(iter(self._runs)))

    def get(self, owner, operation, variable):
        self.purge()
        stored = self._runs.get((owner, operation))
        if stored is None:
            return None
        _, _, run, values = stored
        return {**run, "values": values.get(variable, [])}

"""Client-side latency and explicitly reported usage without retaining model text."""

import json
from time import monotonic


class StreamObservation:
    """Observe arrival times, not provider-internal inference or network TTFT."""
    MAX_LINE = 32 * 1024

    def __init__(self, clock=monotonic):
        self.clock = clock
        self.started = clock()
        self.first_byte_ms = None
        self.first_content_ms = None
        self._line = bytearray()
        self._discarding = False

    def feed(self, chunk):
        elapsed = max(0, round((self.clock() - self.started) * 1000))
        if chunk and self.first_byte_ms is None:
            self.first_byte_ms = elapsed
        if self.first_content_ms is not None:
            return
        for fragment in chunk.splitlines(keepends=True):
            ended = fragment.endswith((b'\n', b'\r'))
            if len(self._line) + len(fragment) > self.MAX_LINE:
                self._line.clear()
                self._discarding = True
            if not self._discarding:
                self._line.extend(fragment)
            if ended:
                if not self._discarding and self._content(self._line):
                    self.first_content_ms = elapsed
                self._line.clear()
                self._discarding = False
                if self.first_content_ms is not None:
                    return

    @staticmethod
    def _content(body):
        try:
            event = json.loads(body)
            update = event['data']['update']
            content = update['content']
            return event['type'] == 'sessionUpdate' and update['sessionUpdate'] == 'agent_message_chunk' \
                and content['type'] == 'text' and isinstance(content['text'], str) and bool(content['text'])
        except (ValueError, TypeError, KeyError, RecursionError):
            return False

    def receipt(self):
        return {'firstStreamByteMs': self.first_byte_ms, 'firstContentMs': self.first_content_ms,
                'firstContentStatus': 'observed' if self.first_content_ms is not None else 'unobserved'}


def reported_usage(body):
    """Never estimate tokens or mistake absent metadata for zero usage."""
    result = {'inputTokens': None, 'outputTokens': None, 'usageStatus': 'unreported'}
    for line in reversed(body.splitlines()):
        try:
            event = json.loads(line)
            if not isinstance(event, dict) or event.get('type') != 'runFinished':
                continue
            usage = event.get('data', {}).get('usage')
            if not isinstance(usage, dict):
                return result
            for key in ['inputTokens', 'outputTokens']:
                value = usage.get(key)
                if type(value) is int and 0 <= value <= 1_000_000_000:
                    result[key] = value
            result['usageStatus'] = 'reported' if all(result[key] is not None for key in ['inputTokens', 'outputTokens']) \
                else 'partial' if any(result[key] is not None for key in ['inputTokens', 'outputTokens']) else 'unreported'
            return result
        except (ValueError, TypeError, AttributeError, RecursionError):
            continue
    return result

import json
import unittest

from sfdc_agent.kiro_headless import HeadlessKiroSession, SessionError
import test_kiro_headless as fixtures
from test_kiro_headless import TEST_KEY, completed, stream


PRIVATE = 'synthetic-private-stdout-stderr-prompt'
LOGGER = 'sfdc_agent.kiro_headless'


class HeadlessDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    fake_cli = fixtures.HeadlessProcessTests.fake_cli

    async def run_cli(self, body, *, timeout=5):
        self.fake_cli(f'''\
            import sys, time
            if sys.argv[1] == 'whoami':
                print('Authenticated with API key')
                sys.exit(0)
            sys.stdin.read()
            print({PRIVATE!r}, file=sys.stderr)
            {body}
            ''')
        session = HeadlessKiroSession(TEST_KEY)
        try:
            return await session.call('prompt', {'text': PRIVATE}, timeout=timeout)
        finally:
            await session.close()

    def metadata(self, captured):
        self.assertEqual(len(captured.records), 1)
        text = captured.records[0].getMessage()
        self.assertNotIn(PRIVATE, text)
        self.assertNotIn(TEST_KEY, text)
        return json.loads(text.removeprefix('Kiro headless failure '))

    async def test_nonzero_exit_retains_only_numeric_measurements(self):
        with self.assertLogs(LOGGER, level='WARNING') as captured:
            with self.assertRaises(SessionError) as failed:
                await self.run_cli(f'print({PRIVATE!r}); sys.exit(17)')
        self.assertEqual(failed.exception.code, 'KIRO_HEADLESS_FAILED')
        self.assertEqual(self.metadata(captured), {
            'stage': 'model', 'code': 'KIRO_HEADLESS_FAILED', 'exit_status': 17,
            'stdout_bytes': len(PRIVATE.encode()) + 1, 'parsed_events': 0,
        })

    async def test_invalid_stream_counts_only_structurally_decoded_events(self):
        body = stream({'type': 'fixture', 'data': {'private': PRIVATE}}) + b'malformed JSONL\n'
        with self.assertLogs(LOGGER, level='WARNING') as captured:
            with self.assertRaises(SessionError) as failed:
                await self.run_cli(f'sys.stdout.buffer.write({body!r})')
        self.assertEqual(failed.exception.code, 'KIRO_HEADLESS_INVALID')
        self.assertEqual(self.metadata(captured), {
            'stage': 'parse', 'code': 'KIRO_HEADLESS_INVALID', 'exit_status': 0,
            'stdout_bytes': len(body), 'parsed_events': 1,
        })

    async def test_timeout_keeps_unreturned_measurements_unknown(self):
        with self.assertLogs(LOGGER, level='WARNING') as captured:
            with self.assertRaises(SessionError) as failed:
                await self.run_cli(f'print({PRIVATE!r}, flush=True); time.sleep(60)', timeout=1.5)
        self.assertEqual(failed.exception.code, 'KIRO_TIMEOUT')
        self.assertEqual(self.metadata(captured), {
            'stage': 'model', 'code': 'KIRO_TIMEOUT', 'exit_status': None,
            'stdout_bytes': None, 'parsed_events': 0,
        })

    async def test_success_keeps_existing_response_and_emits_no_failure_metadata(self):
        body = stream(completed(finalText='{"ok":true}'))
        with self.assertNoLogs(LOGGER, level='WARNING'):
            result = await self.run_cli(f'sys.stdout.buffer.write({body!r})')
        self.assertEqual(result, {'text': '{"ok":true}', 'stopReason': 'end_turn', 'toolCalls': []})

import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from sfdc_agent.managed_runtime import IncompleteExecution, run_process


LOGGER = 'sfdc_agent.managed_runtime'


class WorkerStderrTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.binary = shutil.which('node')
        if self.binary is None:
            self.skipTest('Node is required for the actual managed process boundary.')
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.runner = Path(self.directory) / 'runner.mjs'

    def run_worker(self, timeout=5):
        return run_process(self.binary, self.runner, self.directory, timeout, b'{}')

    async def test_only_known_parser_codes_are_logged_without_changing_the_failure_wire(self):
        for code in ('KIRO_INVALID_BUNDLE', 'KIRO_INVALID_LWC_TEMPLATE', 'KIRO_INVALID_LWC_CSS',
                     'KIRO_INVALID_LWC_JS', 'KIRO_INVALID_NOVA_BINDING', 'KIRO_INVALID_NOVA_EVENT',
                     'KIRO_INVALID_NOVA_PROPERTY'):
            with self.subTest(code=code):
                self.runner.write_text('process.stderr.write(' + json.dumps(code)
                                       + ');process.stdout.write("{}");process.exitCode=17;')
                with self.assertLogs(LOGGER, level='WARNING') as captured:
                    with self.assertRaises(IncompleteExecution):
                        await self.run_worker()
                self.assertEqual([record.getMessage() for record in captured.records],
                                 [f'Managed worker failed code={code} exit_code=17 stdout_bytes=2'])

    async def test_private_unknown_multiline_invalid_encoding_and_large_stderr_are_discarded(self):
        cases = [
            "process.stderr.write('private-provider-value')",
            "process.stderr.write('UNTRUSTED_UPPERCASE_SECRET')",
            "process.stderr.write('KIRO_INVALID_BUNDLE\\nprivate-provider-value')",
            "process.stderr.write('KIRO_INVALID_BUNDLE\\n')",
            "process.stderr.write(Buffer.from([255,254,253]))",
            "process.stderr.write('KIRO_INVALID_BUNDLE');process.stderr.write(Buffer.alloc(2*1024*1024,65))",
        ]
        for source in cases:
            with self.subTest(case=cases.index(source)):
                self.runner.write_text(source + ';process.stdout.write("{}");process.exitCode=3;')
                with self.assertLogs(LOGGER, level='WARNING') as captured:
                    with self.assertRaises(IncompleteExecution):
                        await self.run_worker()
                self.assertEqual([record.getMessage() for record in captured.records],
                                 ['Managed worker failed code=UNKNOWN exit_code=3 stdout_bytes=2'])

    async def test_success_drains_large_stderr_without_logging_or_changing_stdout(self):
        self.runner.write_text("process.stderr.write(Buffer.alloc(2*1024*1024,65));"
                               "process.stdout.write('unchanged-protocol');")
        with self.assertNoLogs(LOGGER, level='WARNING'):
            self.assertEqual(await self.run_worker(), b'unchanged-protocol')

    @unittest.skipUnless(os.name == 'posix', 'The existing process-group cleanup contract requires POSIX.')
    async def test_timeout_and_cancellation_reap_workers_with_stderr_pressure(self):
        marker = Path(self.directory) / 'pid'
        self.runner.write_text("import {writeFileSync} from 'node:fs';"
                               "process.stderr.write(Buffer.alloc(2*1024*1024,65));"
                               'writeFileSync(' + json.dumps(str(marker)) + ',String(process.pid));'
                               'setInterval(()=>{},1000);')
        with self.assertRaises(TimeoutError):
            await self.run_worker(timeout=1)
        timed_out_pid = int(marker.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(timed_out_pid, 0)
        marker.unlink()
        pending = asyncio.create_task(self.run_worker())
        try:
            async with asyncio.timeout(3):
                while not marker.exists():
                    await asyncio.sleep(0.01)
            cancelled_pid = int(marker.read_text())
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(pending, timeout=3)
            with self.assertRaises(ProcessLookupError):
                os.kill(cancelled_pid, 0)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

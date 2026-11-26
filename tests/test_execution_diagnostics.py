import json
import asyncio
import os
import signal
from pathlib import Path
import unittest

from sfdc_agent.execution_diagnostics import decode_worker_failure, safe_diagnostic
import test_managed_execution as fixtures
from sfdc_agent.kiro_processes import _snapshot


DIAGNOSTIC = {'schemaVersion': 1, 'code': 'FIGMA_RATE_LIMITED', 'stage': 'source-preparation', 'retryAfterSeconds': 282454}


def packet(value=DIAGNOSTIC):
    return json.dumps({'protocolVersion': 1, 'status': 'FAILED', 'error': value}).encode()


class DiagnosticContractTests(unittest.TestCase):
    def test_quota_hint_survives_without_raw_worker_or_provider_text(self):
        self.assertEqual(decode_worker_failure(packet()), DIAGNOSTIC)
        self.assertEqual(safe_diagnostic(DIAGNOSTIC), DIAGNOSTIC)

    def test_unknown_ambiguous_or_unbounded_metadata_is_rejected(self):
        invalid = [
            {**DIAGNOSTIC, 'message': 'private-provider-value'},
            {**DIAGNOSTIC, 'code': 'private-provider-value'},
            {**DIAGNOSTIC, 'schemaVersion': True},
            {**DIAGNOSTIC, 'stage': 'generation'},
            *[{**DIAGNOSTIC, 'retryAfterSeconds': value} for value in [True, -1, 0, 604801, '5']],
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(decode_worker_failure(packet(value)))
        for raw in [b'private-provider-value', packet() + b'{}',
                    packet().replace(b'"schemaVersion": 1', b'"schemaVersion":1,"schemaVersion":1'),
                    packet() + b' ' * 1025]:
            self.assertIsNone(decode_worker_failure(raw))

    def test_packaged_policies_match_canonical_engine(self):
        root = Path(__file__).resolve().parents[2]
        canonical = (root/'SFDC_Ui/nextbss/shared/execution-diagnostics-policy.json').read_bytes()
        self.assertEqual(canonical, (root/'SFDC_Agent/src/sfdc_agent/execution_diagnostics_policy.json').read_bytes())
        self.assertEqual(canonical, (root/'SFDC_BACK/src/main/resources/execution-diagnostics-policy.json').read_bytes())


class ActualWorkerDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ManagedExecutionTests.setUp
    request = fixtures.ManagedExecutionTests.request
    write_runner = fixtures.ManagedExecutionTests.write_runner

    async def test_node_nonzero_exit_reaches_service_as_safe_failure_and_keeps_stderr_private(self):
        self.write_runner("process.stderr.write('private-provider-value'); process.stdout.write(" + repr(packet().decode()) + "); process.exitCode=4;")
        status, body, _ = await self.request(fixtures.EXECUTION)
        self.assertEqual(status, 502)
        self.assertEqual(body, DIAGNOSTIC)
        self.assertNotIn('private-provider-value', json.dumps(body))

    async def test_success_or_spoofed_unstructured_output_never_becomes_a_safe_failure(self):
        for output in [packet({**DIAGNOSTIC, 'message': 'private-provider-value'}), b'private-provider-value']:
            self.write_runner('process.stdout.write(' + repr(output.decode()) + '); process.exitCode=2;')
            status, body, _ = await self.request(fixtures.EXECUTION)
            self.assertEqual(status, 502)
            self.assertNotIn('private-provider-value', json.dumps(body))
            self.assertNotIn('code', body)

    @unittest.skipUnless(os.name == 'posix', 'Process-group cleanup requires POSIX')
    async def test_failed_exited_worker_cannot_leave_a_closed_stdout_child_running(self):
        marker=Path(self.directory)/'owned-child.json'
        self.write_runner("import {spawn} from 'node:child_process'; import {writeFileSync} from 'node:fs';\n"
            "const child=spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{stdio:'ignore'});child.unref();\n"
            "writeFileSync("+json.dumps(str(marker))+",JSON.stringify({child:child.pid,parent:process.pid}));\n"
            "process.stdout.write("+repr(packet().decode())+");process.exitCode=2;")
        child=None
        try:
            status,body,_=await self.request(fixtures.EXECUTION)
            self.assertEqual(status,502);self.assertEqual(body,DIAGNOSTIC)
            child=json.loads(marker.read_text())['child']
            for _ in range(40):
                if not any(row.pid==child and not row.state.startswith('Z') for row in _snapshot()):
                    break
                await asyncio.sleep(0.025)
            self.assertFalse(any(row.pid==child and not row.state.startswith('Z') for row in _snapshot()))
        finally:
            if child is not None:
                try:os.kill(child,signal.SIGKILL)
                except ProcessLookupError:pass

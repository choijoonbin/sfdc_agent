import json
import asyncio
import os
import signal
from importlib.resources import files
from pathlib import Path
import unittest

from sfdc_agent.execution_diagnostics import decode_worker_failure, safe_diagnostic
import test_managed_execution as fixtures
from sfdc_agent.kiro_processes import _snapshot


DIAGNOSTIC = {'schemaVersion': 1, 'code': 'FIGMA_RATE_LIMITED', 'stage': 'source-preparation', 'retryAfterSeconds': 282454}


def packet(value=DIAGNOSTIC):
    return json.dumps({'protocolVersion': 1, 'status': 'FAILED', 'error': value}).encode()


class DiagnosticContractTests(unittest.TestCase):
    def test_reference_handoff_and_bundle_validation_keep_safe_specific_stages(self):
        for code, stage in [('SOURCE_REFERENCE_CHANGED', 'output-validation'),
                            ('KIRO_INVALID_BUNDLE', 'generation'), ('KIRO_INVALID_LWC_TEMPLATE', 'generation'),
                            ('KIRO_INVALID_LWC_CSS', 'generation'), ('KIRO_INVALID_LWC_JS', 'generation'),
                            ('KIRO_INVALID_NOVA_BINDING', 'generation'), ('KIRO_INVALID_NOVA_EVENT', 'generation'),
                            ('KIRO_INVALID_NOVA_PROPERTY', 'generation'), ('KIRO_UNKNOWN_LWC_COMPONENT', 'generation'),
                            ('KIRO_FIGMA_CONTEXT_UNAVAILABLE', 'source-preparation'),
                            ('KIRO_FIGMA_CONTEXT_UNVERIFIED', 'source-preparation'),
                            ('KIRO_FIGMA_SOURCE_MISMATCH', 'source-preparation')]:
            value = {'schemaVersion': 1, 'code': code, 'stage': stage}
            self.assertEqual(decode_worker_failure(packet(value)), value)
            for extra in ({'message': 'private-source'}, {'png': 'private-image'}, {'retryAfterSeconds': 1}):
                self.assertIsNone(decode_worker_failure(packet({**value, **extra})))
            self.assertIsNone(safe_diagnostic({**value, 'stage': 'analysis'}))

    def test_checkpoint_capacity_keeps_the_declared_preparation_code_without_new_wire_fields(self):
        value = {'schemaVersion': 1, 'code': 'SCREEN_ANALYSIS_TOO_LARGE', 'stage': 'source-preparation'}
        self.assertEqual(safe_diagnostic(value), value)
        self.assertEqual(decode_worker_failure(packet(value)), value)
        self.assertIsNone(safe_diagnostic({**value, 'stage': 'analysis'}))
        self.assertIsNone(safe_diagnostic({**value, 'message': 'private-provider-value'}))
        self.assertIsNone(safe_diagnostic({**value, 'retryAfterSeconds': 1}))

    def test_oversize_source_context_preserves_only_its_declared_safe_code_and_phase(self):
        for code in ('KIRO_REST_CONTEXT_TOO_LARGE', 'KIRO_LOCAL_CONTEXT_TOO_LARGE'):
            value = {'schemaVersion': 1, 'code': code, 'stage': 'source-preparation'}
            with self.subTest(code=code):
                self.assertEqual(safe_diagnostic(value), value)
                self.assertEqual(decode_worker_failure(packet(value)), value)
                self.assertIsNone(safe_diagnostic({**value, 'stage': 'generation'}))
                self.assertIsNone(safe_diagnostic({**value, 'message': 'private-provider-value'}))
                self.assertIsNone(safe_diagnostic({**value, 'contextBytes': 625664}))
                self.assertIsNone(safe_diagnostic({**value, 'scopeMemberId': '50:2'}))
                self.assertIsNone(safe_diagnostic({**value, 'retryAfterSeconds': 1}))

    def test_planning_and_scope_selection_descriptors_remain_bounded_source_preparation_errors(self):
        for code in ('SCREEN_SCOPE_SELECTION_REQUIRED', 'SCREEN_RENDER_ROOT_AMBIGUOUS', 'SCREEN_RENDER_ROOT_INVALID',
                     'SCREEN_PLANNING_STRUCTURE_INVALID', 'SCREEN_PLANNING_EVIDENCE_TOO_LARGE', 'SCREEN_PLANNING_SOURCE_CHANGED'):
            value={'schemaVersion':1,'code':code,'stage':'source-preparation'}
            self.assertEqual(safe_diagnostic(value),value)
            self.assertEqual(decode_worker_failure(packet(value)),value)
            self.assertIsNone(safe_diagnostic({**value,'stage':'generation'}))
            self.assertIsNone(safe_diagnostic({**value,'rawSource':'private-provider-value'}))
        self.assertIsNone(safe_diagnostic({'schemaVersion':1,'code':'SCREEN_SCOPE_SELECTION_REQUIRED_OTHER','stage':'source-preparation'}))

    def test_logical_scope_descriptors_preserve_only_the_declared_safe_phase(self):
        expected = {
            'SCREEN_SCOPE_SOURCE_CHANGED': ('analysis', 'generation'),
            'SCREEN_SCOPE_SOURCE_UNVERIFIED': ('source-preparation',),
            'SCREEN_SCOPE_CAPTURE_LIMIT': ('source-preparation',),
            'SCREEN_SCOPE_REVIEW_REQUIRED': ('generation',),
            'SCREEN_SCOPE_REVIEW_INVALID': ('generation',),
            'SCREEN_SCOPE_REVIEW_CHANGED': ('generation',),
            'SCREEN_SCOPE_SURFACE_UNAVAILABLE': ('generation',),
            'SCREEN_SCOPE_STANDARD_REQUIRED': ('runtime',),
            'SCREEN_SCOPE_ASYNC_GENERATION_REQUIRED': ('generation',),
            'EXECUTION_LOGICAL_SCOPE_INVALID': ('output-validation',),
            'LOGICAL_SURFACE_BUNDLE_INVALID': ('generation',),
            'KIRO_INVALID_LOGICAL_CHILD_EVENT': ('generation',),
        }
        for code, stages in expected.items():
            for stage in stages:
                value = {'schemaVersion': 1, 'code': code, 'stage': stage}
                with self.subTest(code=code, stage=stage):
                    self.assertEqual(safe_diagnostic(value), value)
                    self.assertEqual(decode_worker_failure(packet(value)), value)
                    self.assertIsNone(safe_diagnostic({**value, 'message': 'private-provider-value'}))
                    self.assertIsNone(safe_diagnostic({**value, 'credential': 'private-provider-value'}))
                    self.assertIsNone(safe_diagnostic({**value, 'stage': 'untrusted-provider-stage'}))
        self.assertIsNone(safe_diagnostic({'schemaVersion': 1, 'code': 'SCREEN_SCOPE_REVIEW_UNKNOWN', 'stage': 'generation'}))

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

    def test_packaged_policy_matches_declared_canonical_engine(self):
        self.assertIn('SFDC_MANAGED_ENGINE_ROOT', os.environ, 'Declare the approved Node runtime for the policy contract.')
        root = Path(os.environ['SFDC_MANAGED_ENGINE_ROOT'])
        self.assertTrue(root.is_absolute(), 'The declared Node runtime must be an absolute path.')
        canonical = (root/'nextbss/shared/execution-diagnostics-policy.json').read_bytes()
        self.assertEqual(canonical, files('sfdc_agent').joinpath('execution_diagnostics_policy.json').read_bytes())
        # BACK checks its actual packaged JAR policy in its artifact delivery gate.


class ActualWorkerDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ManagedExecutionTests.setUp
    request = fixtures.ManagedExecutionTests.request
    write_runner = fixtures.ManagedExecutionTests.write_runner

    async def test_reference_handoff_failure_reaches_service_with_output_validation_code(self):
        value = {'schemaVersion': 1, 'code': 'SOURCE_REFERENCE_CHANGED', 'stage': 'output-validation'}
        self.write_runner("process.stderr.write('private-png-and-provider-response'); process.stdout.write(" + repr(packet(value).decode()) + "); process.exitCode=1;")
        status, body, _ = await self.request(fixtures.EXECUTION)
        self.assertEqual(status, 502)
        self.assertEqual(body, value)

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

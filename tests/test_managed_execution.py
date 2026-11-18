import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile
from threading import BoundedSemaphore
import unittest
from unittest.mock import AsyncMock, Mock, patch

from sfdc_agent.api import app


TOKEN = "managed-test-token"
RUNNER_MODULE = "sfdc_agent.managed_execution"
PATH = "/internal/v1/executions"
EXECUTION = {
    "jobId": "8b9ed4d2-ffb7-4fd5-9e14-1d4b1ec8b42d",
    "attempt": 1,
    "kind": "SCREEN",
    "engine": "NOVA",
    "sourceReference": "https://www.figma.com/design/FrameDocument",
    "designReleaseId": "nextbss-2.0-" + "a" * 64,
    "promptReleaseId": "prompts-v1",
}
GENERATION = {
    **EXECUTION,
    "operation": "GENERATE",
    "analysisJobId": "fc35acc9-9fbc-415e-bcd0-eacd1607fd56",
    "analysis": {"bytes": "ZmlnbWE="},
    "workflowId": "order-flow",
}
ANALYSIS_SCRIPT = """
import { readFileSync } from 'node:fs';
const request = JSON.parse(readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify({ status: 'REVIEW_REQUIRED', title: '검토 화면', artifacts: [],
  summary: { title: '검토 화면' }, checkpoint: {
    request, cwd: process.cwd(), scratch: process.env.SFDC_EXECUTION_SCRATCH,
    receivedServiceToken: Boolean(process.env.SFDC_AGENT_EXECUTION_TOKEN),
    receivedUnrelatedSecret: Boolean(process.env.UNRELATED_PRIVATE_VALUE),
    receivedFigmaToken: Boolean(process.env.FIGMA_ACCESS_TOKEN),
    archiveIndex: process.env.SFDC_MANAGED_ARCHIVE_INDEX
  }}));
"""
GENERATION_SCRIPT = """
import { readFileSync } from 'node:fs';
const request = JSON.parse(readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify({ status: 'COMPLETED', title: '결과',
  artifacts: [{ name: 'lwc/bundle.js', mediaType: 'application/octet-stream', base64: request.analysis.bytes }],
  summary: { workflow: request.workflowId } }));
"""


class ManagedExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, payload=None, *, authorization=f"Bearer {TOKEN}", raw=None, disconnect=None, path=PATH):
        headers = [(b"host", b"127.0.0.1:18765"), (b"content-type", b"application/json")]
        if authorization is not None:
            headers.append((b"authorization", authorization.encode()))
        body = raw if raw is not None else json.dumps(payload).encode()
        messages = []
        received = False

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": body, "more_body": False}
            if disconnect is not None:
                await disconnect.wait()
                return {"type": "http.disconnect"}
            await asyncio.Future()

        async def send(message):
            messages.append(message)

        await app({
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http",
            "method": "POST", "path": path, "root_path": "", "query_string": b"", "headers": headers,
            "server": ("127.0.0.1", 18765), "client": ("127.0.0.1", 40100),
        }, receive, send)
        document = json.loads(b"".join(message.get("body", b"") for message in messages))
        return messages[0]["status"], document, received

    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.runner = Path(self.directory) / "runner.mjs"
        self.enterContext(patch.dict(os.environ, {
            "SFDC_AGENT_EXECUTION_TOKEN": TOKEN,
            "SFDC_MANAGED_EXECUTION_RUNNER": str(self.runner),
            "SFDC_MANAGED_EXECUTION_NODE": shutil.which("node") or "node",
            "SFDC_MANAGED_EXECUTION_TIMEOUT_SECONDS": "10",
            "SFDC_MANAGED_ARCHIVE_INDEX": str(Path(self.directory) / "approved-index.json"),
            "FIGMA_ACCESS_TOKEN": "explicit-figma-test-token",
            "UNRELATED_PRIVATE_VALUE": "unrelated-test-secret",
        }, clear=True))

    def write_runner(self, source):
        if not shutil.which(os.environ["SFDC_MANAGED_EXECUTION_NODE"]):
            self.skipTest("Node is required to exercise the managed worker process.")
        self.runner.write_text(source)

    async def test_endpoint_disabled_without_service_token_and_authenticates_before_reading(self):
        with patch.dict(os.environ, {"SFDC_AGENT_EXECUTION_TOKEN": ""}):
            status, reply, received = await self.request(raw=b"untrusted request")
        self.assertEqual(status, 503)
        self.assertFalse(received)
        self.assertNotIn(TOKEN, repr(reply))
        for authentication in (None, "Basic x", "Bearer invalid", f"Bearer {TOKEN} "):
            status, _, received = await self.request(raw=b"untrusted request", authorization=authentication)
            self.assertEqual(status, 401)
            self.assertFalse(received)

    async def test_validates_contract_before_starting_worker(self):
        for invalid in (
            {**EXECUTION, "unknown": "sensitive field"},
            {**EXECUTION, "attempt": "1"},
            {**EXECUTION, "sourceReference": "https://www.figma.com/design/Frame\nDocument"},
            {**EXECUTION, "operation": "GENERATE"},
            {**GENERATION, "analysisJobId": EXECUTION["jobId"]},
            {**GENERATION, "analysis": {}},
            {**EXECUTION, "operation": "ANALYZE", "workflowId": "order-flow"},
        ):
            with self.subTest(invalid=list(invalid)):
                status, result, _ = await self.request(invalid)
                self.assertEqual(status, 422)
                self.assertEqual(result, {"detail": "Invalid managed execution request."})
        status, _, _ = await self.request(raw=b'{"attempt":1,"attempt":2}')
        self.assertEqual(status, 422)
        with patch(f"{RUNNER_MODULE}._REQUEST_BYTES", 25):
            status, _, _ = await self.request(EXECUTION)
        self.assertEqual(status, 413)

    async def test_kiro_requires_explicit_key_and_this_agents_local_origin(self):
        payload = {**EXECUTION, "engine": "KIRO"}
        for environment in ({}, {"KIRO_API_KEY": "present", "SFDC_MANAGED_AGENT_ORIGIN": "http://127.0.0.1:8765"}):
            with patch.dict(os.environ, environment):
                status, reply, _ = await self.request(payload)
            self.assertEqual(status, 503)
            self.assertEqual(reply, {"detail": "Managed Kiro runtime is unavailable."})
        self.write_runner(ANALYSIS_SCRIPT)
        with patch.dict(os.environ, {"KIRO_API_KEY": "present", "SFDC_MANAGED_AGENT_ORIGIN": "http://127.0.0.1:18765"}):
            status, reply, _ = await self.request(payload)
        self.assertEqual(status, 200)
        self.assertEqual(reply["checkpoint"]["request"]["engine"], "KIRO")

    async def test_review_response_keeps_checkpoint_separate_and_uses_unique_cleaned_scratch(self):
        self.write_runner(ANALYSIS_SCRIPT)
        working_directories = set()
        for _ in range(2):
            status, reply, _ = await self.request(EXECUTION)
            self.assertEqual(status, 200)
            self.assertEqual(reply["status"], "REVIEW_REQUIRED")
            self.assertEqual(reply["artifacts"], [])
            self.assertEqual(reply["summary"], {"title": "검토 화면"})
            checkpoint = reply["checkpoint"]
            self.assertEqual(checkpoint["request"], {**EXECUTION, "operation": "ANALYZE"})
            self.assertEqual(Path(checkpoint["cwd"]).resolve(), Path(checkpoint["scratch"]).resolve())
            self.assertFalse(Path(checkpoint["scratch"]).exists())
            self.assertFalse(checkpoint["receivedServiceToken"])
            self.assertFalse(checkpoint["receivedUnrelatedSecret"])
            self.assertTrue(checkpoint["receivedFigmaToken"])
            self.assertEqual(checkpoint["archiveIndex"], os.environ["SFDC_MANAGED_ARCHIVE_INDEX"])
            working_directories.add(checkpoint["scratch"])
        self.assertEqual(len(working_directories), 2)

    async def test_generation_accepts_artifacts_with_relative_bundle_paths(self):
        self.write_runner(GENERATION_SCRIPT)
        status, reply, _ = await self.request(GENERATION)
        self.assertEqual(status, 200)
        self.assertEqual(reply, {"status": "COMPLETED", "title": "결과", "summary": {"workflow": "order-flow"},
                                 "artifacts": [{"name": "lwc/bundle.js", "mediaType": "application/octet-stream",
                                                "base64": "ZmlnbWE="}]})
        with patch(f"{RUNNER_MODULE}._ARTIFACT_BYTES", 2):
            status, _, _ = await self.request(GENERATION)
        self.assertEqual(status, 502)
        self.write_runner(GENERATION_SCRIPT.replace("lwc/bundle.js", "lwc/../bundle.js"))
        status, _, _ = await self.request(GENERATION)
        self.assertEqual(status, 502)

    async def test_rejects_wrong_status_unusable_output_or_errors_without_worker_details(self):
        self.write_runner(ANALYSIS_SCRIPT)
        status, _, _ = await self.request(GENERATION)
        self.assertEqual(status, 502)
        self.write_runner('process.stderr.write("secret stderr"); process.stdout.write("secret stdout"); process.exitCode = 1;')
        status, reply, _ = await self.request(EXECUTION)
        self.assertEqual(status, 502)
        self.assertNotIn("secret", repr(reply))
        self.write_runner('process.stdout.write(Buffer.alloc(102400, 1));')
        with patch(f"{RUNNER_MODULE}._OUTPUT_BYTES", 4096):
            status, _, _ = await self.request(EXECUTION)
        self.assertEqual(status, 502)

    async def test_generation_preserves_empty_css_and_rejects_other_empty_artifacts(self):
        script = GENERATION_SCRIPT.replace("lwc/bundle.js", "lwc/bundle.css")
        script = script.replace("request.analysis.bytes", "''")
        self.write_runner(script)
        status, reply, _ = await self.request(GENERATION)
        self.assertEqual(status, 200)
        self.assertEqual(reply["artifacts"][0]["base64"], "")
        for name, media_type in (("lwc/bundle.js", "application/octet-stream"),
                                 ("lwc/bundle.html", "application/octet-stream"),
                                 ("source.png", "image/png"), ("lwc/bundle.css", "application/zip")):
            with self.subTest(name=name, media_type=media_type):
                self.write_runner(script.replace("lwc/bundle.css", name).replace("application/octet-stream", media_type))
                status, _, _ = await self.request(GENERATION)
                self.assertEqual(status, 502)

    async def test_process_timeout_and_caller_disconnect_release_resources(self):
        marker = Path(self.directory) / "started"
        self.write_runner("import { writeFileSync } from 'node:fs';\n"
                          f"writeFileSync({json.dumps(str(marker))}, process.env.SFDC_EXECUTION_SCRATCH);\n"
                          "setInterval(() => {}, 10000);\n")
        with patch.dict(os.environ, {"SFDC_MANAGED_EXECUTION_TIMEOUT_SECONDS": "1"}):
            status, _, _ = await self.request(EXECUTION)
        self.assertEqual(status, 504)
        self.assertFalse(Path(marker.read_text()).exists())
        marker.unlink()

        disconnect = asyncio.Event()
        pending = asyncio.create_task(self.request(EXECUTION, disconnect=disconnect))
        async with asyncio.timeout(5):
            while not marker.is_file():
                await asyncio.sleep(0.02)
        disconnect.set()
        status, _, _ = await asyncio.wait_for(pending, timeout=3)
        self.assertEqual(status, 499)
        self.assertFalse(Path(marker.read_text()).exists())
        self.write_runner(ANALYSIS_SCRIPT)
        self.assertEqual((await self.request(EXECUTION))[0], 200)

    async def test_source_discovery_preserves_only_known_fallback_codes_and_releases_slots(self):
        root = Path(self.directory)
        source_runner = root / 'nextbss/server/managed-source-discovery.mjs'
        source_runner.parent.mkdir(parents=True)
        source_runner.write_text('// Trusted source projection fixture')
        payload = {'sourceReference': 'https://www.figma.com/design/RealFile'}
        for code, expected_status, expected in (
            ('FIGMA_TOKEN_REQUIRED', 503, {'code': 'FIGMA_TOKEN_REQUIRED'}),
            ('FIGMA_RATE_LIMITED', 429, {'code': 'FIGMA_RATE_LIMITED'}),
            ('FIGMA_RATE_LIMIT', 429, {'code': 'FIGMA_RATE_LIMITED'}),
            ('PRIVATE_PATH_OR_TOKEN', 422, {'detail': 'The Figma archive could not be inspected.'}),
        ):
            with self.subTest(code=code):
                slots = Mock()
                slots.acquire.return_value = True
                with patch(f'{RUNNER_MODULE}._WORKER_SLOTS', slots), \
                     patch(f'{RUNNER_MODULE}.engine_root', return_value=root), \
                     patch(f'{RUNNER_MODULE}._runner_configuration', return_value=('node', self.runner, 90)), \
                     patch(f'{RUNNER_MODULE}._run_process', new=AsyncMock(return_value=json.dumps({'code': code, 'message': 'private-password-or-path'}).encode())):
                    status, reply, _ = await self.request(payload, path='/internal/v1/source-discovery')
                self.assertEqual(status, expected_status)
                self.assertEqual(reply, expected)
                slots.acquire.assert_called_once_with(blocking=False)
                slots.release.assert_called_once_with()

    async def test_source_discovery_authenticates_before_body_or_slot_and_bounds_body(self):
        slots = Mock()
        slots.acquire.return_value = True
        path = '/internal/v1/source-discovery'
        with patch(f'{RUNNER_MODULE}._WORKER_SLOTS', slots), patch(f'{RUNNER_MODULE}._run_process', new=AsyncMock()) as process:
            for token in (None, 'Bearer invalid'):
                status, _, read = await self.request(raw=b'untrusted body', authorization=token, path=path)
                self.assertEqual(status, 401)
                self.assertFalse(read)
            slots.acquire.assert_not_called()
            with patch(f'{RUNNER_MODULE}._REQUEST_BYTES', 8):
                self.assertEqual((await self.request(raw=b' ' * 9, path=path))[0], 413)
            slots.release.assert_called_once_with()
            process.assert_not_called()
        slots.reset_mock()
        slots.acquire.return_value = False
        with patch(f'{RUNNER_MODULE}._WORKER_SLOTS', slots):
            status, _, read = await self.request(raw=b'not read when busy', path=path)
        self.assertEqual(status, 503)
        self.assertFalse(read)
        slots.release.assert_not_called()

    async def start_source_process(self, disconnect):
        root = Path(self.directory)
        runner = root / 'nextbss/server/managed-source-discovery.mjs'
        runner.parent.mkdir(parents=True)
        marker = root / 'source-process.json'
        self.write_runner(ANALYSIS_SCRIPT)
        runner.write_text("import { writeFileSync } from 'node:fs';\n"
                          f"writeFileSync({json.dumps(str(marker))}, JSON.stringify({{pid: process.pid, scratch: process.env.SFDC_EXECUTION_SCRATCH}}));\n"
                          "setInterval(() => {}, 10000);\n")
        pending = asyncio.create_task(self.request({'sourceReference': 'https://www.figma.com/design/RealFile'},
                                                   disconnect=disconnect, path='/internal/v1/source-discovery'))
        async with asyncio.timeout(5):
            while not marker.is_file():
                await asyncio.sleep(0.02)
        return pending, json.loads(marker.read_text())

    def assert_source_process_reaped(self, state):
        self.assertFalse(Path(state['scratch']).exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(state['pid'], 0)

    async def test_source_disconnect_reaps_actual_node_and_releases_capacity(self):
        slots = BoundedSemaphore(1)
        disconnect = asyncio.Event()
        with patch(f'{RUNNER_MODULE}._WORKER_SLOTS', slots), \
             patch(f'{RUNNER_MODULE}.engine_root', return_value=Path(self.directory)):
            pending, state = await self.start_source_process(disconnect)
            disconnect.set()
            status, _, _ = await asyncio.wait_for(pending, timeout=3)
            self.assertEqual(status, 499)
            self.assert_source_process_reaped(state)
            with patch(f'{RUNNER_MODULE}._run_process', new=AsyncMock(return_value=b'{"result":{"pages":[]}}')):
                self.assertEqual((await self.request({'sourceReference': 'https://www.figma.com/design/RealFile'},
                                                    path='/internal/v1/source-discovery'))[0], 200)

    async def test_source_cancel_and_rejected_waiter_do_not_leak_or_cancel_other_work(self):
        slots = BoundedSemaphore(1)
        with patch(f'{RUNNER_MODULE}._WORKER_SLOTS', slots), \
             patch(f'{RUNNER_MODULE}.engine_root', return_value=Path(self.directory)):
            pending, state = await self.start_source_process(asyncio.Event())
            status, _, read = await self.request(raw=b'busy requests must not be read',
                                                path='/internal/v1/source-discovery')
            self.assertEqual(status, 503)
            self.assertFalse(read)
            self.assertFalse(pending.done())
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            self.assert_source_process_reaped(state)
            self.assertTrue(slots.acquire(blocking=False))
            slots.release()

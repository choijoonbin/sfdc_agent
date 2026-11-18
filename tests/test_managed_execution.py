import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile
from time import time
from threading import BoundedSemaphore
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from sfdc_agent.api import app


TOKEN = "managed-test-token"
RUNNER_MODULE = "sfdc_agent.managed_execution"
PATH = "/internal/v1/executions"
EXECUTION = {
    "jobId": "8b9ed4d2-ffb7-4fd5-9e14-1d4b1ec8b42d",
    "attempt": 1,
    "executionScope": {"tenantId": "aa000000-0000-4000-8000-000000000001", "workspaceId": "ad000000-0000-4000-8000-000000000001"},
    "timeoutSeconds": 10,
    "deadlineEpochSeconds": 0,
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
import { readFileSync, writeFileSync } from 'node:fs';
import { createHash, randomUUID } from 'node:crypto';
import { join } from 'node:path';
const request = JSON.parse(readFileSync(0, 'utf8'));
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const analysis = { id:randomUUID(), source:{fileKey:'FrameDocument',nodeId:'1:2'}, dimensions:{width:1,height:1},mappings:[],issues:[] };
const raw = Buffer.from(JSON.stringify({analysis,input:{request}}));
const checkpoint = { id:analysis.id,digest:hash(raw),bytes:raw.toString('base64'),
  request,cwd:process.cwd(),scratch:process.env.SFDC_EXECUTION_SCRATCH,
  receivedServiceToken:Boolean(process.env.SFDC_AGENT_EXECUTION_TOKEN), receivedUnrelatedSecret:Boolean(process.env.UNRELATED_PRIVATE_VALUE),
  receivedFigmaToken:Boolean(process.env.FIGMA_ACCESS_TOKEN),archiveIndex:process.env.SFDC_MANAGED_ARCHIVE_INDEX };
const view = Buffer.from(JSON.stringify(analysis));
writeFileSync(join(process.env.SFDC_ANALYSIS_OUTPUT,'public-view.json'),view);
const manifest = Buffer.from(JSON.stringify({protocolVersion:1,serializerVersion:1,jobId:request.jobId,attempt:request.attempt,
  ...request.executionScope,kind:request.kind,engine:request.engine,producerRuntimeSha256:'b'.repeat(64),
  analysisId:analysis.id,checkpointSha256:checkpoint.digest,source:{fileKey:new URL(request.sourceReference).pathname.split('/')[2]},
  view:{path:'public-view.json',sizeBytes:view.length,sha256:hash(view)},assets:[],unavailable:[]}));
writeFileSync(join(process.env.SFDC_ANALYSIS_OUTPUT,'manifest.json'),manifest);
process.stdout.write(JSON.stringify({ status: 'REVIEW_REQUIRED', title: '검토 화면', artifacts: [],
  summary: { title: '검토 화면' }, checkpoint,analysisView:{protocolVersion:1,manifestSha256:hash(manifest)}}));
"""
GENERATION_SCRIPT = """
import { readFileSync } from 'node:fs';
const request = JSON.parse(readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify({ status: 'COMPLETED', title: '결과',
  artifacts: [{ name: 'lwc/bundle.js', mediaType: 'application/octet-stream', base64: request.analysis.bytes }],
  summary: { workflow: request.workflowId } }));
"""


class ManagedExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, payload=None, *, authorization=f"Bearer {TOKEN}", raw=None, disconnect=None, path=PATH, fresh=True, extra_headers=()):
        headers = [(b"host", b"127.0.0.1:18765"), (b"content-type", b"application/json")]
        if authorization is not None:
            headers.append((b"authorization", authorization.encode()))
        headers.extend(extra_headers)
        if isinstance(payload, dict) and 'jobId' in payload:
            payload = {**payload}
            if fresh:
                previous = payload['jobId']
                payload['jobId'] = str(uuid4())
                if payload.get('analysisJobId') == previous:
                    payload['analysisJobId'] = payload['jobId']
            if payload.get('deadlineEpochSeconds') == 0:
                payload['deadlineEpochSeconds'] = int(time()) + payload['timeoutSeconds'] + 1
        self.last_request = payload
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
        response_body = b"".join(message.get("body", b"") for message in messages)
        document = json.loads(response_body) if response_body else None
        return messages[0]["status"], document, received

    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.runner = Path(self.directory) / "runner.mjs"
        self.runner.write_text(ANALYSIS_SCRIPT)
        (Path(self.directory) / 'managed-runtime.json').write_text(json.dumps({'sha256': 'b' * 64}))
        (Path(self.directory) / 'ingress').mkdir()
        self.enterContext(patch(f'{RUNNER_MODULE}.engine_root', return_value=Path(self.directory)))
        self.enterContext(patch.dict('sfdc_agent.managed_execution_progress._ATTEMPTS', {}, clear=True))
        self.enterContext(patch.dict(os.environ, {
            "SFDC_AGENT_EXECUTION_TOKEN": TOKEN,
            "SFDC_MANAGED_EXECUTION_RUNNER": str(self.runner),
            "SFDC_MANAGED_EXECUTION_NODE": shutil.which("node") or "node",
            "SFDC_MANAGED_EXECUTION_TIMEOUT_SECONDS": "10",
            "SFDC_AGENT_SCREEN_KIRO_MODE": "headless",
            "SFDC_MANAGED_RELEASE_INGRESS": str(Path(self.directory) / 'ingress'),
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
        with patch(f'{RUNNER_MODULE}._WORKER_SLOTS') as slots:
            status, _, received = await self.request(raw=b'not read', extra_headers=((b'origin', b'https://browser.invalid'),))
            self.assertEqual(status, 403)
            self.assertFalse(received)
            slots.acquire.assert_not_called()

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
            self.assertEqual(checkpoint["request"], {**self.last_request, "operation": "ANALYZE", 'inputMode': 'SOURCE'})
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
            status, _, _ = await self.request({**EXECUTION, 'timeoutSeconds': 1})
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

    async def test_expired_deadline_and_physical_limit_mismatch_do_not_start_a_worker(self):
        worker = AsyncMock()
        with patch(f'{RUNNER_MODULE}._run_process', new=worker):
            status, body, _ = await self.request({**EXECUTION, 'deadlineEpochSeconds': int(time()) - 1})
            self.assertEqual((status, body), (410, {'code': 'EXECUTION_DEADLINE_EXPIRED'}))
            status, body, _ = await self.request({**EXECUTION, 'timeoutSeconds': 11})
            self.assertEqual((status, body), (409, {'code': 'LIMIT_MISMATCH'}))
            status, _, _ = await self.request({**EXECUTION, 'deadlineEpochSeconds': int(time()) + 3600})
            self.assertEqual(status, 422)
        worker.assert_not_called()
        self.assertFalse((Path(self.directory) / 'ingress/analysis').exists())

    async def test_cancel_before_start_is_scope_pinned_and_attempt_numbers_are_int32(self):
        payload = {**EXECUTION, 'jobId': str(uuid4()), 'attempt': 101, 'deadlineEpochSeconds': int(time()) + 11}
        path = f"{PATH}/{payload['jobId']}/attempts/{payload['attempt']}/cancel"
        cancel = {key: payload[key] for key in ('executionScope', 'deadlineEpochSeconds')}
        self.assertEqual((await self.request(cancel, path=path))[0], 204)
        status, body, _ = await self.request(payload, fresh=False)
        self.assertEqual((status, body), (409, {'code': 'EXECUTION_CANCELLED'}))
        wrong = {**cancel, 'executionScope': {**cancel['executionScope'], 'tenantId': str(uuid4())}}
        self.assertEqual((await self.request(wrong, path=path))[0], 409)
        self.assertEqual((await self.request(cancel, path=path.replace('/101/', '/2147483648/')))[0], 422)
        self.assertFalse((Path(self.directory) / 'ingress/analysis').exists())
        self.write_runner(ANALYSIS_SCRIPT)
        self.assertEqual((await self.request({**payload, 'attempt': 2147483647}, fresh=False))[0], 200)

    async def start_execution_marker(self, payload, disconnect=None):
        marker = Path(self.directory) / (payload['jobId'] + '.json')
        self.write_runner("import {readFileSync,writeFileSync} from 'node:fs';\n"
            "import {join} from 'node:path';\nconst request=JSON.parse(readFileSync(0,'utf8'));\n"
            f"writeFileSync(join({json.dumps(self.directory)},request.jobId+'.json'),JSON.stringify({{pid:process.pid,scratch:process.env.SFDC_EXECUTION_SCRATCH}}));\n"
            "setInterval(()=>{},10000);\n")
        pending = asyncio.create_task(self.request(payload, fresh=False, disconnect=disconnect))
        async with asyncio.timeout(5):
            while not marker.is_file():
                await asyncio.sleep(0.02)
        return pending, json.loads(marker.read_text())

    async def test_private_cancel_reaps_actual_worker_and_releases_capacity(self):
        payload = {**GENERATION, 'jobId': str(uuid4()), 'deadlineEpochSeconds': int(time()) + 11}
        pending, state = await self.start_execution_marker(payload)
        try:
            body = {key: payload[key] for key in ('executionScope', 'deadlineEpochSeconds')}
            path = f"{PATH}/{payload['jobId']}/attempts/1/cancel"
            wrong = {**body, 'deadlineEpochSeconds': body['deadlineEpochSeconds'] + 1}
            self.assertEqual((await self.request(wrong, path=path))[0], 409)
            self.assertFalse(pending.done())
            self.assertEqual((await self.request(body, path=path))[0], 204)
            self.assertEqual((await asyncio.wait_for(pending, 3))[0], 499)
            self.assert_source_process_reaped(state)
            self.assertEqual((await self.request(body, path=path))[0], 204)
            self.write_runner(GENERATION_SCRIPT)
            self.assertEqual((await self.request(GENERATION))[0], 200)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_two_actual_children_make_third_explicit_busy_before_body_and_ingress(self):
        from sfdc_agent.managed_execution_progress import _CONTEXTS
        payloads = [{**GENERATION, 'jobId': str(uuid4()), 'attempt': number,
                     'deadlineEpochSeconds': int(time()) + 11} for number in (101, 102)]
        pending = []
        try:
            for payload in payloads:
                pending.append(await self.start_execution_marker(payload))
            self.assertEqual(len(_CONTEXTS), 2)
            status, body, received = await self.request(raw=b'busy request is never parsed')
            self.assertEqual((status, body, received), (503, {'code': 'EXECUTION_BUSY'}, False))
            self.assertEqual(len(_CONTEXTS), 2)
            self.assertFalse((Path(self.directory) / 'ingress/analysis').exists())
            for payload, (task, state) in zip(payloads, pending):
                body = {key: payload[key] for key in ('executionScope', 'deadlineEpochSeconds')}
                path = f"{PATH}/{payload['jobId']}/attempts/{payload['attempt']}/cancel"
                self.assertEqual((await self.request(body, path=path))[0], 204)
                self.assertEqual((await task)[0], 499)
                self.assert_source_process_reaped(state)
            self.write_runner(GENERATION_SCRIPT)
            self.assertEqual((await self.request(GENERATION))[0], 200)
        finally:
            for task, _ in pending:
                task.cancel()
            await asyncio.gather(*(task for task, _ in pending), return_exceptions=True)

    async def test_legal_worst_escaped_templates_and_checkpoint_fit_sixteen_mib(self):
        from sfdc_agent.managed_prompts import PromptBundle, checked_bundle, template_digest
        from sfdc_agent.prompt_templates import load_baseline_templates
        from sfdc_agent.managed_execution import ExecutionResult, _REQUEST_BYTES
        templates = {key: text + '\x01' * (131072 - len(text.encode()))
                     for key, text in load_baseline_templates().items()}
        bundle = PromptBundle(releaseId='prompts-v1', sha256=template_digest(templates), templates=templates)
        checked_bundle(bundle)
        payload = {**GENERATION, 'promptBundle': bundle.model_dump(mode='json'),
                   'analysis': {'data': 'x' * (3 * 1024 * 1024 - len('{"data":""}'))}}
        encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        self.assertGreater(len(encoded), 12 * 1024 * 1024)
        self.assertLess(len(encoded), _REQUEST_BYTES)
        result = ExecutionResult.model_validate({'status':'COMPLETED','title':'Fixture',
            'artifacts':[{'name':'lwc/a.js','mediaType':'application/octet-stream','base64':'YQ=='}]})
        with patch(f'{RUNNER_MODULE}._execute', new=AsyncMock(return_value=result)):
            self.assertEqual((await self.request(payload))[0], 200)
        # Exact body limit is accepted for parsing (then fails required fields),
        # one byte beyond it is rejected before validation or child creation.
        raw = b'{}' + b' ' * (_REQUEST_BYTES - 2)
        self.assertEqual((await self.request(raw=raw))[0], 422)
        self.assertEqual((await self.request(raw=raw + b' '))[0], 413)

    async def test_new_attempt_reaps_the_old_child_and_stale_cancel_cannot_stop_new_work(self):
        payload = {**GENERATION, 'jobId': str(uuid4()), 'attempt': 101, 'deadlineEpochSeconds': int(time()) + 11}
        old, old_state = await self.start_execution_marker(payload)
        latest = None
        try:
            (Path(self.directory) / (payload['jobId'] + '.json')).unlink()
            next_payload = {**payload, 'attempt': 102}
            latest, new_state = await self.start_execution_marker(next_payload)
            self.assertEqual((await old)[0], 499)
            self.assert_source_process_reaped(old_state)
            body = {key: payload[key] for key in ('executionScope', 'deadlineEpochSeconds')}
            path = f"{PATH}/{payload['jobId']}/attempts/101/cancel"
            self.assertEqual((await self.request(body, path=path))[0], 409)
            self.assertFalse(latest.done())
            self.assertEqual((await self.request(body, path=path.replace('/101/', '/102/')))[0], 204)
            self.assertEqual((await latest)[0], 499)
            self.assert_source_process_reaped(new_state)
        finally:
            for pending in (old, latest):
                if pending: pending.cancel()
            await asyncio.gather(*(item for item in (old, latest) if item), return_exceptions=True)

    async def test_shutdown_reaps_active_children_and_finishes_requests(self):
        from sfdc_agent.managed_execution_progress import execution_lifespan, _CONTEXTS
        payload = {**GENERATION, 'jobId': str(uuid4()), 'deadlineEpochSeconds': int(time()) + 11}
        async with execution_lifespan(app):
            pending, state = await self.start_execution_marker(payload)
        self.assertEqual((await pending)[0], 499)
        self.assert_source_process_reaped(state)
        self.assertFalse(_CONTEXTS)

    async def test_cancellation_during_process_creation_still_reaps_the_created_child(self):
        from sfdc_agent.managed_execution import _run_process
        real_spawn = asyncio.create_subprocess_exec
        created = asyncio.Event(); release = asyncio.Event(); children = []
        async def delayed_spawn(*args, **kwargs):
            process = await real_spawn(*args, **kwargs)
            children.append(process)
            created.set()
            await release.wait()
            return process
        self.write_runner('setInterval(()=>{},10000);')
        with patch(f'{RUNNER_MODULE}.asyncio.create_subprocess_exec', new=delayed_spawn):
            pending = asyncio.create_task(_run_process(os.environ['SFDC_MANAGED_EXECUTION_NODE'], self.runner, self.directory, 5, b'{}'))
            await asyncio.wait_for(created.wait(), 3)
            pending.cancel()
            await asyncio.sleep(0)
            self.assertFalse(pending.done(), 'Cancellation must wait until it can reap the spawn result')
            release.set()
            with self.assertRaises(asyncio.CancelledError): await pending
        self.assertIsNotNone(children[0].returncode)
        with self.assertRaises(ProcessLookupError): os.kill(children[0].pid, 0)

    async def test_simultaneous_consumer_disconnect_and_private_cancel_still_reap(self):
        payload = {**GENERATION, 'jobId': str(uuid4()), 'deadlineEpochSeconds': int(time()) + 11}
        disconnect = asyncio.Event()
        pending, state = await self.start_execution_marker(payload, disconnect)
        body = {key: payload[key] for key in ('executionScope', 'deadlineEpochSeconds')}
        path = f"{PATH}/{payload['jobId']}/attempts/1/cancel"
        disconnect.set()
        self.assertEqual((await self.request(body, path=path))[0], 204)
        self.assertEqual((await pending)[0], 499)
        self.assert_source_process_reaped(state)

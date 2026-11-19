import asyncio
import base64
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from threading import BoundedSemaphore
import tempfile
from time import time
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from sfdc_agent.managed_source_revisions import RevisionRequest, create_source_revision, run_revision


class ManagedSourceRevisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.runner = self.root / 'nextbss/server/managed-source-revision.mjs'
        self.runner.parent.mkdir(parents=True)
        self.runner.write_text('// trusted isolated runner fixture')
        self.slots = BoundedSemaphore(1)
        self.enterContext(patch.dict(os.environ, {'SFDC_AGENT_EXECUTION_TOKEN': 'revision-service-fixture',
            'FIGMA_ACCESS_TOKEN': 'never-forward-rest', 'FIGMA_PLAN_ACCESS_TOKEN': 'never-forward-plan',
            'SFDC_MANAGED_FIGMA_SCOPE': 'invalid'}))
        self.enterContext(patch('sfdc_agent.managed_source_revisions.engine_root', return_value=self.root))
        self.enterContext(patch('sfdc_agent.managed_source_revisions._WORKER_SLOTS', self.slots))
        self.enterContext(patch('sfdc_agent.managed_source_revisions._runner_configuration', return_value=(shutil.which('node'), self.runner, 540)))
        self.library = {'schemaVersion': 1, 'family': 'NOVA_PUBLIC', 'libraryVersion': '1.0.0',
                        **{key: 'a' * 64 for key in ['descriptorSha256', 'contractSha256', 'moduleManifestSha256', 'tokenCssSha256']}}
        self.payload = {
            'revisionId': str(uuid4()), 'parentResultId': str(uuid4()), 'parentDigest': 'a' * 64,
            'adjustmentKind': 'SURFACE', 'producerKind': 'DETERMINISTIC_SOURCE_ADJUSTMENT', 'modelCalls': 0,
            'executionScope': {'tenantId': str(uuid4()), 'workspaceId': str(uuid4())}, 'kind': 'SCREEN', 'engine': 'NOVA',
            'sourceReference': 'https://www.figma.com/design/RealFixture?node-id=1-2&version-id=SavedV1',
            'pins': {'design': {'sourceId': 'nextbss-1.9-' + 'b' * 64, 'version': '1.9', 'manifestSha256': 'b' * 64},
                     'prompt': {'sourceId': 'prompts-nova-' + 'c' * 64, 'version': '1.9', 'manifestSha256': 'c' * 64},
                     'maxConcurrentJobs': 4, 'timeoutSeconds': 540},
            'view': {'source': {'fileKey': 'RealFixture', 'nodeId': '1:2', 'version': 'SavedV1'},
                     'standard': {'version': '1.9'}, 'dimensions': {'width': 100, 'height': 80},
                     'analysis': {'id': str(uuid4()), 'digest': 'd' * 64}},
            'metadata': {'version': 1, 'source': {'fileKey': 'RealFixture', 'nodeId': '1:2', 'version': 'SavedV1'},
                         'standard': {'version': '1.9'}, 'eligible': True, 'componentLibrary': self.library},
            'analysisViewId': str(uuid4()), 'analysisPayloadSha256': 'e' * 64, 'analysisView': {'source': {'nodeId': '1:2'}},
            'analysisCheckpoint': {'id': str(uuid4()), 'digest': sha256(b'{}').hexdigest(), 'bytes': base64.b64encode(b'{}').decode()},
            'artifacts': [self.artifact('lwc/nextBssFixture/nextBssFixture.css', b':host{display:block}', with_hash=True)],
        }

    @staticmethod
    def artifact(name, body, *, with_hash=False):
        return {'name': name, 'mediaType': 'application/octet-stream', 'base64': base64.b64encode(body).decode(),
                **({'sha256': sha256(body).hexdigest()} if with_hash else {})}

    def unchanged(self, **changes):
        return {**{key: self.payload[key] for key in ['revisionId', 'parentResultId', 'parentDigest', 'producerKind', 'modelCalls']},
                'status': 'UNCHANGED', 'ruleDigest': 'b' * 64, 'artifacts': [], **changes}

    def completed(self):
        view = {**self.payload['view'], 'id': self.payload['revisionId'], 'generator': 'nova-deterministic-runtime',
                'conversionMode': 'deterministic-adjustment', 'canApply': True, 'applyBlockers': []}
        receipt = {**{key: self.payload[key] for key in ['revisionId', 'parentResultId', 'parentDigest', 'adjustmentKind', 'analysisViewId', 'analysisPayloadSha256']},
                   'version': 1, 'source': self.payload['metadata']['source'], 'componentLibrary': self.library,
                   'compiled': True, 'evidence': {'checkpointSha256': self.payload['analysisCheckpoint']['digest']}, 'view': view}
        return self.unchanged(status='COMPLETED', receipt=receipt, artifacts=[
            self.artifact('managed-result-meta.json', json.dumps(self.payload['metadata']).encode()),
            self.artifact('managed-deployment.json', b'{}'), self.artifact('managed-preview/manifest.json', b'{}'),
        ])

    def request(self, value=None, *, raw=None, auth=True, extra=(), disconnect=None):
        body = raw if raw is not None else json.dumps(value or self.payload).encode()
        headers = [(b'authorization', b'Bearer revision-service-fixture')] if auth else []
        received = []
        async def receive():
            if not received:
                received.append(True)
                return {'type': 'http.request', 'body': body, 'more_body': False}
            if disconnect:
                await disconnect.wait()
                return {'type': 'http.disconnect'}
            await asyncio.Future()
        return Request({'type': 'http', 'method': 'POST', 'headers': [*headers, *extra]}, receive), received

    def assert_slot_free(self):
        self.assertTrue(self.slots.acquire(blocking=False)); self.slots.release()

    async def test_auth_body_origin_and_busy_fail_before_any_child(self):
        process = AsyncMock()
        with patch('sfdc_agent.managed_source_revisions._run_process', process):
            request, read = self.request(raw=b'x', auth=False)
            with self.assertRaises(HTTPException) as unauthenticated:
                await create_source_revision(request)
            self.assertEqual(unauthenticated.exception.status_code, 401); self.assertEqual(read, [])
            with self.assertRaises(HTTPException) as browser:
                await create_source_revision(self.request(extra=[(b'origin', b'https://browser.invalid')])[0])
            self.assertEqual(browser.exception.status_code, 403)
            with self.assertRaises(HTTPException) as size:
                await create_source_revision(self.request(raw=b'x' * (24 * 1024 * 1024 + 1))[0])
            self.assertEqual(size.exception.status_code, 413)
            self.slots.acquire()
            try:
                response = await create_source_revision(self.request()[0])
                self.assertEqual(response.status_code, 503)
                self.assertEqual(json.loads(response.body), {'code': 'EXECUTION_BUSY'})
            finally:
                self.slots.release()
            process.assert_not_awaited(); self.assert_slot_free()

    async def test_strict_request_preserves_full_nova_prompt_digest_and_rejects_tampering(self):
        self.assertEqual(RevisionRequest.model_validate(self.payload).pins.prompt.sourceId, 'prompts-nova-' + 'c' * 64)
        null_label = deepcopy(self.payload); null_label['pins']['design']['version'] = None
        null_label['pins']['prompt']['version'] = None
        self.assertIsNone(RevisionRequest.model_validate(null_label).pins.design.version)
        self.assertIsNone(RevisionRequest.model_validate(null_label).pins.prompt.version)
        for mutation in [lambda p: p.update(engine='KIRO'), lambda p: p.update(modelCalls=1),
                         lambda p: p.update(environment={'FIGMA_ACCESS_TOKEN': 'forged'}),
                         lambda p: p['analysisCheckpoint'].update(digest='0' * 64),
                         lambda p: p['artifacts'][0].update(sha256='0' * 64),
                         lambda p: p['artifacts'][0].update(name='../escape'),
                         lambda p: p['artifacts'].append(self.artifact('managed-original-view.json', b'{}', with_hash=True)),
                         lambda p: p.update(archive={**p['executionScope'], 'workspaceId': str(uuid4()), 'fileKey': 'RealFixture', 'sha256': 'a' * 64})]:
            value = deepcopy(self.payload); mutation(value)
            with self.assertRaises(ValidationError): RevisionRequest.model_validate(value)

    async def test_remote_and_local_inputs_never_forward_provider_or_model_credentials(self):
        for selection in ['REMOTE', 'LOCAL', 'HTTPS_ARCHIVE_MEASURED']:
            value = deepcopy(self.payload)
            if selection != 'REMOTE':
                value['archive'] = {**value['executionScope'], 'fileKey': 'RealFixture' if selection == 'HTTPS_ARCHIVE_MEASURED' else 'local' + 'b' * 32, 'sha256': 'c' * 64}
                value['sourceReference'] = 'https://www.figma.com/design/RealFixture?node-id=1-2&page-id=1-0&version-id=IgnoredRemoteVersion' if selection == 'HTTPS_ARCHIVE_MEASURED' else 'figma-local://' + value['archive']['fileKey'] + '/source?node-id=1-2&page-id=1-0'
            with patch('sfdc_agent.managed_source_revisions._run_process', AsyncMock(return_value=json.dumps(self.unchanged()).encode())) as child:
                result = await run_revision(RevisionRequest.model_validate(value))
            self.assertEqual(result.status, 'UNCHANGED')
            self.assertEqual(child.call_args.args[-1], {})
            self.assertEqual(child.call_args.args[3], 120)
            self.assertEqual(child.call_args.kwargs['output_limit'], 12 * 1024 * 1024)
            self.assertNotIn(b'never-forward', child.call_args.args[4])

    async def test_deadlines_are_bounded_before_process_start(self):
        process = AsyncMock(return_value=json.dumps(self.unchanged()).encode())
        with patch('sfdc_agent.managed_source_revisions._run_process', process):
            for deadline, expected in [(int(time()) - 1, 410), (int(time()) + 1000, 422)]:
                value = RevisionRequest.model_validate({**self.payload, 'deadlineEpochSeconds': deadline})
                with self.assertRaises(HTTPException) as failed: await run_revision(value)
                self.assertEqual(failed.exception.status_code, expected)
            process.assert_not_awaited()
            await run_revision(RevisionRequest.model_validate({**self.payload, 'deadlineEpochSeconds': int(time()) + 30}))
            self.assertLessEqual(process.call_args.args[3], 30)

    async def test_completed_receipt_requires_exact_parent_analysis_source_and_new_derived_identity(self):
        with patch('sfdc_agent.managed_source_revisions._run_process', AsyncMock(return_value=json.dumps(self.completed()).encode())):
            result = await create_source_revision(self.request()[0])
        self.assertEqual(result.status, 'COMPLETED'); self.assert_slot_free()
        for mutation in [lambda r: r.update(parentResultId=str(uuid4())), lambda r: r.update(modelCalls=1),
                         lambda r: r['receipt'].update(compiled=False),
                         lambda r: r['receipt']['view'].update(analysisId=str(uuid4())),
                         lambda r: r['receipt']['source'].update(nodeId='1:9'),
                         lambda r: r['artifacts'].append(self.artifact('managed-original-view.json', b'{}'))]:
            reply = deepcopy(self.completed()); mutation(reply)
            with patch('sfdc_agent.managed_source_revisions._run_process', AsyncMock(return_value=json.dumps(reply).encode())):
                with self.assertRaises(HTTPException) as failed: await create_source_revision(self.request()[0])
                self.assertEqual(failed.exception.status_code, 422)
            self.assert_slot_free()

    async def test_errors_return_only_known_safe_codes(self):
        for reply, expected in [({'code': 'SOURCE_REVISION_CHECKPOINT_CHANGED', 'detail': '/private/secret'}, 'SOURCE_REVISION_CHECKPOINT_CHANGED'),
                                ({'code': 'PRIVATE_SECRET_PATH', 'message': 'never-forward-rest'}, 'SOURCE_REVISION_FAILED')]:
            with patch('sfdc_agent.managed_source_revisions._run_process', AsyncMock(return_value=json.dumps(reply).encode())):
                result = await create_source_revision(self.request()[0])
            self.assertEqual(result.status_code, 422)
            self.assertEqual(json.loads(result.body), {'code': expected})
            self.assert_slot_free()

    async def test_disconnect_and_repeated_cancel_reap_child_before_releasing_shared_slot(self):
        pidfile = self.root / 'child.pid'
        self.runner.write_text("import {writeFileSync,readFileSync} from 'node:fs'; readFileSync(0); writeFileSync(" + json.dumps(str(pidfile)) + ",String(process.pid)); setInterval(()=>{},1000);")
        for cancellation in [False, True]:
            if pidfile.exists(): pidfile.unlink()
            disconnected = asyncio.Event()
            task = asyncio.create_task(create_source_revision(self.request(disconnect=disconnected)[0]))
            async with asyncio.timeout(5):
                while not pidfile.exists(): await asyncio.sleep(0.01)
            pid = int(pidfile.read_text())
            if cancellation:
                task.cancel(); await asyncio.sleep(0); task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
            else:
                disconnected.set()
                with self.assertRaises(HTTPException) as failed: await task
                self.assertEqual(failed.exception.status_code, 499)
            with self.assertRaises(ProcessLookupError): os.kill(pid, 0)
            self.assert_slot_free()

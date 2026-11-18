import asyncio
import json
import os
from pathlib import Path
import shutil
from threading import BoundedSemaphore
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from sfdc_agent.managed_source_reference import approve_source_reference


class ManagedSourceReferenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.runner = self.root / 'nextbss/server/managed-source-reference.mjs'
        self.runner.parent.mkdir(parents=True)
        self.runner.write_text('fixture')
        self.slots = BoundedSemaphore(1)
        self.enterContext(patch.dict(os.environ, {'SFDC_AGENT_EXECUTION_TOKEN': 'reference-fixture-token'}))
        self.enterContext(patch('sfdc_agent.managed_source_reference.engine_root', return_value=self.root))
        self.enterContext(patch('sfdc_agent.managed_source_reference._WORKER_SLOTS', self.slots))
        self.enterContext(patch('sfdc_agent.managed_source_reference._runner_configuration', return_value=(shutil.which('node'), self.runner, 60)))
        self.payload = {'reference': 'https://www.figma.com/design/File/NOVA?node-id=1-2&version-id=Fixed',
                        'executionScope': {'tenantId': 'aa000000-0000-4000-8000-000000000001', 'workspaceId': 'ad000000-0000-4000-8000-000000000001'}}

    def request(self, value=None, *, raw=None, auth=True, extra=(), disconnect=None):
        body = raw if raw is not None else json.dumps(value or self.payload).encode()
        headers = [(b'authorization', b'Bearer reference-fixture-token')] if auth else []
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
        self.assertTrue(self.slots.acquire(blocking=False))
        self.slots.release()

    async def test_auth_body_and_busy_boundaries_do_not_start_a_child(self):
        process = AsyncMock()
        with patch('sfdc_agent.managed_source_reference._run_process', process):
            request, received = self.request(auth=False, raw=b'x' * 8193)
            with self.assertRaises(HTTPException) as failure:
                await approve_source_reference(request)
            self.assertEqual(failure.exception.status_code, 401)
            self.assertEqual(received, [])
            with self.assertRaises(HTTPException) as browser:
                await approve_source_reference(self.request(extra=[(b'origin', b'https://browser.invalid')])[0])
            self.assertEqual(browser.exception.status_code, 403)
            with self.assertRaises(HTTPException) as oversized:
                await approve_source_reference(self.request(raw=b'x' * 8193)[0])
            self.assertEqual(oversized.exception.status_code, 413)
            self.slots.acquire()
            try:
                response = await approve_source_reference(self.request()[0])
                self.assertEqual(response.status_code, 503)
                self.assertEqual(json.loads(response.body), {'code': 'EXECUTION_BUSY'})
            finally:
                self.slots.release()
            process.assert_not_awaited()
            self.assert_slot_free()

    async def test_known_provider_errors_are_bounded_and_unknown_details_are_private(self):
        for output, status, public in [({'code': 'FIGMA_PLAN_TOKEN_REQUIRED'}, 503, {'code': 'FIGMA_PLAN_TOKEN_REQUIRED'}),
                ({'code': 'FIGMA_RATE_LIMIT', 'retryAfterSeconds': 10}, 429, {'code': 'FIGMA_RATE_LIMITED', 'retryAfterSeconds': 10}),
                ({'code': 'PRIVATE_PATH_SECRET', 'detail': '/private/secret'}, 422, {'code': 'FIGMA_REFERENCE_UNAVAILABLE'})]:
            with patch('sfdc_agent.managed_source_reference._run_process', AsyncMock(return_value=json.dumps(output).encode())):
                response = await approve_source_reference(self.request()[0])
            self.assertEqual(response.status_code, status)
            self.assertEqual(json.loads(response.body), public)
            self.assert_slot_free()

    async def test_large_original_png_envelope_uses_its_dedicated_bound(self):
        result = {'provenance': 'OFFICIAL_FIGMA_EXPORT', 'pngBase64': 'A' * (3 * 1024 * 1024 + 1)}
        process = AsyncMock(return_value=json.dumps({'result': result}).encode())
        with patch('sfdc_agent.managed_source_reference._run_process', process):
            self.assertEqual(await approve_source_reference(self.request()[0]), result)
        self.assertEqual(process.call_args.kwargs['output_limit'], 23 * 1024 * 1024)
        self.assert_slot_free()

    async def test_disconnect_and_repeated_cancel_reap_the_owned_child_before_releasing_slot(self):
        pidfile = self.root / 'child.pid'
        self.runner.write_text("import {writeFileSync,readFileSync} from 'node:fs'; readFileSync(0); writeFileSync(" + json.dumps(str(pidfile)) + ",String(process.pid)); setInterval(()=>{},1000);")
        disconnected = asyncio.Event()
        task = asyncio.create_task(approve_source_reference(self.request(disconnect=disconnected)[0]))
        async with asyncio.timeout(5):
            while not pidfile.exists():
                await asyncio.sleep(0.01)
        pid = int(pidfile.read_text())
        disconnected.set()
        with self.assertRaises(HTTPException) as failure:
            await task
        self.assertEqual(failure.exception.status_code, 499)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assert_slot_free()

        pidfile.unlink()
        task = asyncio.create_task(approve_source_reference(self.request()[0]))
        async with asyncio.timeout(5):
            while not pidfile.exists():
                await asyncio.sleep(0.01)
        pid = int(pidfile.read_text())
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assert_slot_free()

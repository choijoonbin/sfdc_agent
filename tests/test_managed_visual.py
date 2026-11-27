import asyncio
import base64
import json
import os
from hashlib import sha256
from pathlib import Path
import tempfile
from threading import BoundedSemaphore
import unittest
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from starlette.requests import Request
from sfdc_agent.managed_visual import compare, _capture


class ManagedVisualTests(unittest.IsolatedAsyncioTestCase):
    def request(self, value, headers=None):
        body = json.dumps(value).encode()
        sent = False
        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Future()
        return Request({"type": "http", "method": "POST", "headers": headers or []}, receive)

    async def test_service_auth_and_bound_result_hashes(self):
        payload = {"resultId": "f2d461f7-1db4-4239-9c13-cae58693a717", "referenceSha256": "a" * 64,
                   "previewSha256": "b" * 64}
        with patch.dict("os.environ", {"SFDC_AGENT_EXECUTION_TOKEN": "visual-token"}):
            with self.assertRaises(HTTPException) as missing:
                await compare(self.request(payload))
            self.assertEqual(missing.exception.status_code, 401)
            headers = [(b"authorization", b"Bearer visual-token")]
            with self.assertRaises(HTTPException) as browser:
                await compare(self.request(payload, [*headers, (b"origin", b"https://browser.invalid")]))
            self.assertEqual(browser.exception.status_code, 403)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                runner = root / "nextbss/server/managed-visual-capture.mjs"
                runner.parent.mkdir(parents=True)
                runner.write_text("fixture")
                output = {**payload, "report": {}, "captures": [
                    {"key": key, "base64": "cG5n", "sha256": sha256(b'png').hexdigest()} for key in ("source", "rendered", "difference")]}
                process = AsyncMock(return_value=json.dumps(output).encode())
                with patch("sfdc_agent.managed_visual.engine_root", return_value=root),                         patch("sfdc_agent.managed_visual._runner_configuration", return_value=("node", runner, 60)),                         patch("sfdc_agent.managed_visual._run_process", process):
                    self.assertEqual(await compare(self.request(payload, headers)), output)
                    self.assertEqual(json.loads(process.call_args.args[4]), payload)
                    self.assertEqual(process.call_args.kwargs['output_limit'], 68 * 1024 * 1024)
                    output["referenceSha256"] = "c" * 64
                    process.return_value = json.dumps(output).encode()
                    with self.assertRaises(HTTPException) as changed:
                        await compare(self.request(payload, headers))
                    self.assertEqual(changed.exception.status_code, 422)

    async def test_original_large_captures_and_unique_hashes_are_enforced(self):
        payload = {"resultId": "result", "referenceSha256": "a" * 64, "previewSha256": "b" * 64}
        data = b'p' * (2 * 1024 * 1024 + 1)
        output = {**payload, "report": {}, "captures": [{"key": key, "base64": base64.b64encode(data).decode(),
                  "sha256": sha256(data).hexdigest()} for key in ('source', 'rendered', 'difference')]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = root / 'nextbss/server/managed-visual-capture.mjs'
            runner.parent.mkdir(parents=True)
            runner.write_text('fixture')
            with patch('sfdc_agent.managed_visual.engine_root', return_value=root), \
                    patch('sfdc_agent.managed_visual._runner_configuration', return_value=('node', runner, 60)), \
                    patch('sfdc_agent.managed_visual._run_process', AsyncMock(return_value=json.dumps(output).encode())):
                self.assertEqual(len((await _capture(payload))['captures']), 3)
            output['captures'][2]['key'] = 'rendered'
            with patch('sfdc_agent.managed_visual.engine_root', return_value=root), \
                    patch('sfdc_agent.managed_visual._runner_configuration', return_value=('node', runner, 60)), \
                    patch('sfdc_agent.managed_visual._run_process', AsyncMock(return_value=json.dumps(output).encode())):
                with self.assertRaises(ValueError):
                    await _capture(payload)

    async def test_visual_body_busy_disconnect_and_cancel_release_the_same_bounded_slot(self):
        slots = BoundedSemaphore(1)
        payload = {'test': 'bounded input'}
        headers = [(b'authorization', b'Bearer visual-token')]
        with patch.dict('os.environ', {'SFDC_AGENT_EXECUTION_TOKEN': 'visual-token'}), \
                patch('sfdc_agent.managed_visual._WORKER_SLOTS', slots):
            slots.acquire()
            result = await compare(self.request(payload, headers))
            self.assertEqual(result.status_code, 503)
            self.assertEqual(json.loads(result.body), {'code': 'EXECUTION_BUSY'})
            slots.release()
            body = b'{}' + b' ' * (34 * 1024 * 1024 - 2)
            sent = False
            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {'type': 'http.request', 'body': body, 'more_body': False}
                await asyncio.Future()
            with patch('sfdc_agent.managed_visual._capture', AsyncMock(return_value={'bounded': True})):
                self.assertEqual(await compare(Request({'type': 'http', 'headers': headers}, receive)), {'bounded': True})
                sent = False
                body += b' '
                with self.assertRaises(HTTPException) as large:
                    await compare(Request({'type': 'http', 'headers': headers}, receive))
                self.assertEqual(large.exception.status_code, 413)
            started, stopped, disconnected = asyncio.Event(), asyncio.Event(), asyncio.Event()
            async def capture(_):
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    stopped.set()
            sent = False
            body = b'{}'
            async def disconnect_receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {'type': 'http.request', 'body': body, 'more_body': False}
                await disconnected.wait()
                return {'type': 'http.disconnect'}
            with patch('sfdc_agent.managed_visual._capture', capture):
                task = asyncio.create_task(compare(Request({'type': 'http', 'headers': headers}, disconnect_receive)))
                await started.wait()
                disconnected.set()
                with self.assertRaises(HTTPException) as gone:
                    await task
                self.assertEqual(gone.exception.status_code, 499)
                self.assertTrue(stopped.is_set())
            self.assertTrue(slots.acquire(blocking=False))
            slots.release()

    async def test_output_reader_default_and_dedicated_bounds_are_explicit(self):
        from sfdc_agent.managed_execution import _read_output, _run_process, IncompleteExecution
        stream = asyncio.StreamReader()
        stream.feed_data(b'x' * 10)
        stream.feed_eof()
        self.assertEqual(await _read_output(stream, 10), b'x' * 10)
        stream = asyncio.StreamReader()
        stream.feed_data(b'x' * 11)
        stream.feed_eof()
        with self.assertRaises(IncompleteExecution):
            await _read_output(stream, 10)
        with self.assertRaises(ValueError):
            await _run_process('not-started', Path('not-started'), '.', 1, b'', output_limit=68 * 1024 * 1024 + 1)


if __name__ == "__main__":
    unittest.main()

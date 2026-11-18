import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from starlette.requests import Request
from sfdc_agent.managed_visual import compare


class ManagedVisualTests(unittest.IsolatedAsyncioTestCase):
    def request(self, value, headers=None):
        body = json.dumps(value).encode()
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
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
                    {"key": key, "base64": "cG5n"} for key in ("source", "rendered", "difference")]}
                process = AsyncMock(return_value=json.dumps(output).encode())
                with patch("sfdc_agent.managed_visual.engine_root", return_value=root),                         patch("sfdc_agent.managed_execution._runner_configuration", return_value=("node", runner, 60)),                         patch("sfdc_agent.managed_execution._run_process", process):
                    self.assertEqual(await compare(self.request(payload, headers)), output)
                    self.assertEqual(json.loads(process.call_args.args[4]), payload)
                    output["referenceSha256"] = "c" * 64
                    process.return_value = json.dumps(output).encode()
                    with self.assertRaises(HTTPException) as changed:
                        await compare(self.request(payload, headers))
                    self.assertEqual(changed.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()

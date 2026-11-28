"""Synthetic process-boundary checks; no provider, image or private request data."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from sfdc_agent.managed_runtime import run_process


FLAG = 'SFDC_AGENT_SCREEN_IMAGE_INPUT'


class ScreenImageEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def observe_flag(self, trusted_value, requested_value):
        binary = shutil.which('node')
        if binary is None:
            self.skipTest('Node is required for the managed process boundary.')
        with tempfile.TemporaryDirectory() as directory:
            runner = Path(directory) / 'runner.mjs'
            runner.write_text(
                "let input='';for await (const chunk of process.stdin) input+=chunk;"
                'JSON.parse(input);'
                'const expected=' + json.dumps(trusted_value) + ';'
                'const actual=process.env.SFDC_AGENT_SCREEN_IMAGE_INPUT;'
                'if(expected===null?actual!==undefined:actual!==expected)process.exit(2);'
                'if(process.env.UNRELATED_PRIVATE_VALUE)process.exit(3);'
                "process.stdout.write(actual==='bound-png-v1'?'image-enabled':'text-only');"
            )
            payload = json.dumps({FLAG: requested_value, 'environment': {FLAG: requested_value}}).encode()
            with patch.dict(os.environ, {'UNRELATED_PRIVATE_VALUE': 'synthetic-not-forwarded'}):
                os.environ.pop(FLAG, None)
                if trusted_value is not None:
                    os.environ[FLAG] = trusted_value
                return await run_process(binary, runner, directory, 5, payload)

    async def test_server_owned_pilot_flag_survives_the_worker_allowlist(self):
        self.assertEqual(await self.observe_flag('bound-png-v1', 'request-cannot-disable'), b'image-enabled')

    async def test_request_cannot_enable_an_unset_screen_image_flag(self):
        self.assertEqual(await self.observe_flag(None, 'bound-png-v1'), b'text-only')

    async def test_request_cannot_replace_an_unrecognized_mode_with_the_pilot(self):
        self.assertEqual(await self.observe_flag('unapproved-mode', 'bound-png-v1'), b'text-only')


if __name__ == '__main__':
    unittest.main()

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from sfdc_agent.managed_runtime import run_process


class ConversionAttemptEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_owned_attempt_root_survives_the_worker_allowlist_and_request_cannot_replace_it(self):
        binary = shutil.which('node')
        if binary is None:
            self.skipTest('Node is required for the managed process boundary.')
        with tempfile.TemporaryDirectory() as directory:
            trusted_root = str(Path(directory) / 'server-owned-attempts')
            runner = Path(directory) / 'runner.mjs'
            runner.write_text(
                "let input='';for await (const chunk of process.stdin) input+=chunk;"
                'const request=JSON.parse(input);'
                'if(process.env.SFDC_CONVERSION_ATTEMPT_ROOT!==' + json.dumps(trusted_root) + ')process.exit(2);'
                "if(process.env.SFDC_CONVERSION_ATTEMPT_ROOT===request.conversionAttemptRoot)process.exit(3);"
                "if(process.env.UNRELATED_PRIVATE_VALUE)process.exit(4);"
                "process.stdout.write('server-owned-root');"
            )
            with patch.dict(os.environ, {'SFDC_CONVERSION_ATTEMPT_ROOT': trusted_root,
                                         'UNRELATED_PRIVATE_VALUE': 'must-not-reach-worker'}):
                result = await run_process(binary, runner, directory, 5,
                                           b'{"conversionAttemptRoot":"request-controlled-root"}')
            self.assertEqual(result, b'server-owned-root')


if __name__ == '__main__':
    unittest.main()

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException

from sfdc_agent.managed_design import DesignBuild, check_output_directory, output_directory, run_build
from sfdc_agent.managed_execution import IncompleteExecution


class ManagedDesignTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ingress = self.directory / "ingress"
        self.ingress.mkdir()
        engine = self.directory / "engine"
        runner = engine / "nextbss/server/managed-design-release.mjs"
        runner.parent.mkdir(parents=True)
        runner.write_text("// Trusted deployment adapter fixture\n")
        self.enterContext(patch.dict(os.environ, {
            "SFDC_MANAGED_ENGINE_ROOT": str(engine),
            "SFDC_MANAGED_RELEASE_INGRESS": str(self.ingress),
        }))
        self.enterContext(patch("sfdc_agent.managed_design._runner_configuration", return_value=("node", runner, 900)))
        self.payload = DesignBuild.model_validate({
            "buildId": str(uuid4()),
            "baseReleaseId": "nextbss-1.9-" + "a" * 64,
            "baseManifestSha256": "b" * 64,
            "designVersion": "1.9",
            "sources": {},
        })

    def response(self, **changes):
        result = {
            "status": "VALIDATED",
            "buildId": str(self.payload.buildId),
            "releaseId": "nextbss-1.9-" + "c" * 64,
            "manifestSha256": "d" * 64,
            "outputManifestSha256": "e" * 64,
            "checks": [{"label": "Compile verified", "status": "passed"}],
            "compatible": True,
            "reproduced": True,
        }
        return json.dumps({**result, **changes}).encode()

    async def test_retains_only_successfully_validated_service_builds(self):
        with patch("sfdc_agent.managed_design._run_process", new=AsyncMock(return_value=self.response())) as process:
            result = await run_build(self.payload)
        self.assertEqual(result.status, "VALIDATED")
        output = self.ingress / str(self.payload.buildId)
        self.assertTrue(output.is_dir())
        environment = process.call_args.args[-1]
        self.assertEqual(environment["SFDC_DESIGN_BUILD_OUTPUT"], str(output.resolve()))
        self.assertEqual(environment["SFDC_DESIGN_BUILD_ID"], str(self.payload.buildId))
        with self.assertRaises(HTTPException) as conflict:
            output_directory(self.payload.buildId)
        self.assertEqual(conflict.exception.status_code, 409)

    async def test_rejects_unverified_build_and_cleans_its_output(self):
        with patch("sfdc_agent.managed_design._run_process",
                   new=AsyncMock(return_value=self.response(checks=[{"label": "Compile", "status": "failed"}]))):
            with self.assertRaises(HTTPException) as invalid:
                await run_build(self.payload)
        self.assertEqual(invalid.exception.status_code, 502)
        self.assertFalse((self.ingress / str(self.payload.buildId)).exists())

    async def test_rejects_symlink_output_and_cleans_reserved_build(self):
        async def symlink_output(*args):
            folder = Path(args[-1]["SFDC_DESIGN_BUILD_OUTPUT"])
            (folder / "forged").symlink_to(self.directory)
            return self.response()

        with patch("sfdc_agent.managed_design._run_process", new=symlink_output):
            with self.assertRaises(HTTPException):
                await run_build(self.payload)
        self.assertFalse((self.ingress / str(self.payload.buildId)).exists())

    def test_output_guard_rejects_non_regular_entries(self):
        output = output_directory(self.payload.buildId)
        (output / "untrusted").symlink_to(self.directory)
        with self.assertRaises(IncompleteExecution):
            check_output_directory(output)

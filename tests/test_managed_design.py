import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from sfdc_agent.managed_design import DesignBuild, check_output_directory, output_directory, run_build
from sfdc_agent.managed_execution import IncompleteExecution
from sfdc_agent.managed_figma_scope import FigmaProviderScopeError


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

    def remote_payload(self):
        return {**self.payload.model_dump(mode="json"),
                "sources": {"library": {"fileKey": "ApprovedLibrary", "revision": "SavedVersion"}},
                "executionScope": {"tenantId": "aa000000-0000-4000-8000-000000000001",
                                   "workspaceId": "ad000000-0000-4000-8000-000000000001"}}

    async def test_remote_build_requires_scope_before_creating_output_or_child(self):
        remote = self.remote_payload()
        with self.assertRaises(ValidationError):
            DesignBuild.model_validate({**remote, "executionScope": None})
        with patch.dict(os.environ, {"SFDC_MANAGED_FIGMA_SCOPE": str(uuid4()) + "/" + str(uuid4())}), \
                patch("sfdc_agent.managed_design._run_process", new=AsyncMock()) as process:
            with self.assertRaises(FigmaProviderScopeError) as forbidden:
                await run_build(DesignBuild.model_validate(remote))
            self.assertEqual(forbidden.exception.status_code, 403)
            process.assert_not_called()
        self.assertFalse((self.ingress / str(self.payload.buildId)).exists())

    async def test_only_allocated_remote_build_receives_access_token_without_plan_or_mcp(self):
        remote = self.remote_payload()
        scope = remote["executionScope"]
        with patch.dict(os.environ, {"SFDC_MANAGED_FIGMA_SCOPE": scope["tenantId"] + "/" + scope["workspaceId"],
                                    "FIGMA_ACCESS_TOKEN": "scoped-access-fixture", "FIGMA_PLAN_ACCESS_TOKEN": "plan-fixture",
                                    "FIGMA_MCP_TOKEN": "mcp-fixture"}), \
                patch("sfdc_agent.managed_design._run_process", new=AsyncMock(return_value=self.response())) as process:
            await run_build(DesignBuild.model_validate(remote))
        environment = process.call_args.args[-1]
        self.assertEqual(environment["FIGMA_ACCESS_TOKEN"], "scoped-access-fixture")
        self.assertNotIn("FIGMA_PLAN_ACCESS_TOKEN", environment)
        self.assertNotIn("FIGMA_MCP_TOKEN", environment)

    async def test_offline_rebuild_does_not_receive_any_global_figma_credentials(self):
        with patch.dict(os.environ, {"SFDC_MANAGED_FIGMA_SCOPE": "invalid", "FIGMA_ACCESS_TOKEN": "access-fixture",
                                    "FIGMA_PLAN_ACCESS_TOKEN": "plan-fixture"}), \
                patch("sfdc_agent.managed_design._run_process", new=AsyncMock(return_value=self.response())) as process:
            await run_build(self.payload)
        self.assertFalse(any(key.startswith("FIGMA_") for key in process.call_args.args[-1]))

    async def test_scope_revocation_discards_completed_remote_output(self):
        remote = self.remote_payload()
        scope = remote["executionScope"]
        async def revoked(*args):
            os.environ["SFDC_MANAGED_FIGMA_SCOPE"] = str(uuid4()) + "/" + str(uuid4())
            return self.response()
        with patch.dict(os.environ, {"SFDC_MANAGED_FIGMA_SCOPE": scope["tenantId"] + "/" + scope["workspaceId"]}), \
                patch("sfdc_agent.managed_design._run_process", new=revoked):
            with self.assertRaises(FigmaProviderScopeError):
                await run_build(DesignBuild.model_validate(remote))
        self.assertFalse((self.ingress / str(self.payload.buildId)).exists())

    async def test_provider_rate_limit_is_typed_and_does_not_retain_build_output(self):
        checks = [{"label": "Figma 원본 조회", "status": "failed", "code": "FIGMA_RATE_LIMITED",
                   "httpStatus": 429, "retryAfterSeconds": 66568, "detail": "Wait for the provider."}]
        with patch("sfdc_agent.managed_design._run_process", new=AsyncMock(return_value=self.response(
                status="BLOCKED", checks=checks, compatible=False, reproduced=False))):
            result = await run_build(self.payload)
        self.assertEqual(result.checks[0].retryAfterSeconds, 66568)
        self.assertEqual(result.checks[0].httpStatus, 429)
        self.assertFalse((self.ingress / str(self.payload.buildId)).exists())

    async def test_provider_wait_longer_than_one_day_is_not_lost_or_retried(self):
        checks = [{"label": "Figma", "status": "failed", "code": "FIGMA_RATE_LIMITED", "retryAfterSeconds": 31 * 86400}]
        with patch("sfdc_agent.managed_design._run_process", new=AsyncMock(return_value=self.response(
                status="BLOCKED", checks=checks, compatible=False, reproduced=False))) as process:
            result = await run_build(self.payload)
        self.assertEqual(result.checks[0].retryAfterSeconds, 31 * 86400)
        self.assertEqual(process.await_count, 1)

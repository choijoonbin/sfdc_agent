import hashlib
import json
import os
import unittest
from subprocess import CompletedProcess
from unittest.mock import Mock, patch

from sfdc_agent.prompt_provider_eval import evaluate_prompt_provider
from sfdc_agent.prompt_registry import BASELINE_RELEASE, PromptRelease, runtime_release
from sfdc_agent.providers import ProviderError
from sfdc_agent.story_tasks import build_story_task


class PromptProviderEvaluationTests(unittest.TestCase):
    operation = "story.classification"
    fields = {"evidence": '{"headings":[],"screens":[],"links":[]}'}
    result = {"workflows": [], "unassigned": []}

    def test_model_transports_receive_identical_rendered_text(self):
        expected = build_story_task(self.operation, self.fields)
        openai_response = {
            "status": "completed",
            "output": [{
                "type": "message", "content": [{"type": "output_text", "text": json.dumps(self.result)}],
            }],
        }
        azure_configuration = {
            "BASE_URL": "https://gateway.invalid",
            "API_VERSION": "2024-10-21",
            "TOKEN_SCOPE": "api://test/.default",
            "TENANT_ID": "fake-tenant",
            "CLIENT_ID": "fake-client",
            "CLIENT_SECRET": "fake-client-secret",
            "SUBSCRIPTION_KEY": "fake-key",
            "CHAT_MODEL": "azure-test",
        }
        environment = {
            "OPENAI_API_KEY": "synthetic-openai-key",
            **{f"SFDC_AGENT_AZURE_{key}": value for key, value in azure_configuration.items()},
        }
        with (
            patch.dict(os.environ, environment),
            patch("sfdc_agent.providers.build_opener") as opener,
            patch("sfdc_agent.azure_provider._read_local_settings", return_value={}),
            patch("sfdc_agent.azure_provider._EntraToken.get", return_value="synthetic-auth"),
            patch("sfdc_agent.azure_provider._post") as azure_post,
            patch("sfdc_agent.providers.subprocess.run") as kiro_run,
        ):
            transport = opener.return_value.open
            transport.return_value.__enter__.return_value.read.return_value = json.dumps(openai_response).encode()
            azure_post.return_value = {
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(self.result)}}],
            }
            kiro_run.return_value = CompletedProcess([], 0, stdout=json.dumps({"text": json.dumps(self.result)}))
            results = [
                evaluate_prompt_provider(self.operation, self.fields, name, model=model, release_id=BASELINE_RELEASE)
                for name, model in (("openai", "test-model"), ("azure", None), ("kiro", None))
            ]
            openai_body = json.loads(transport.call_args.args[0].data)
            azure_body = json.loads(azure_post.call_args.args[0].data)
            kiro_body = json.loads(kiro_run.call_args.kwargs["input"])

        self.assertEqual(openai_body["input"], expected)
        self.assertEqual(openai_body["text"]["format"], {"type": "json_object"})
        self.assertEqual(azure_body["messages"], [{"role": "user", "content": expected}])
        self.assertEqual(kiro_body["prompt"], expected)
        for result, name in zip(results, ("openai", "azure", "kiro")):
            with self.subTest(provider=name):
                self.assertEqual(result["provider"], name)
                self.assertEqual(result["promptReleaseId"], BASELINE_RELEASE)
                self.assertEqual(result["promptVersion"], "v1")
                self.assertEqual(result["promptDigest"], hashlib.sha256(expected.encode()).hexdigest())
                self.assertEqual(result["promptBytes"], len(expected.encode()))
                self.assertEqual(result["result"], self.result)

    def test_explicit_release_and_edited_text_are_forwarded(self):
        baseline = runtime_release(BASELINE_RELEASE)
        release_id = "prompts-" + "1" * 32
        release = PromptRelease(release_id, {**baseline.versions, self.operation: "v2"}, {
            **baseline.templates, self.operation: "Additional guidance.\n" + baseline.templates[self.operation],
        })
        provider = Mock(name="provider")
        provider.name, provider.model = "openai", "selected-model"
        provider.complete_rendered.return_value = self.result
        with (
            patch("sfdc_agent.prompt_provider_eval.runtime_release", return_value=release) as load_release,
            patch("sfdc_agent.prompt_provider_eval.create_provider", return_value=provider) as create,
        ):
            result = evaluate_prompt_provider(self.operation, self.fields, "openai", model="selected-model",
                                              release_id=release_id, timeout=42)
        load_release.assert_called_once_with(release_id)
        create.assert_called_once_with("openai", model="selected-model", timeout=42)
        provider.complete_rendered.assert_called_once_with(
            "Additional guidance.\n" + build_story_task(self.operation, self.fields),
        )
        self.assertEqual(result["promptVersion"], "v2")
        self.assertEqual(result["promptReleaseId"], release_id)

    def test_tool_dependent_requests_and_invalid_evidence_stop_before_model_setup(self):
        cases = [
            ("screen.capture", {"fileKey": "Test"}, "PROMPT_EVALUATION_UNSUPPORTED"),
            ("screen.generate", {"capturedContext": "missing reviewedAnalysis"}, "PROMPT_EVALUATION_INVALID"),
            ("screen.repair", {"diagnostic": "retry without saved data"}, "PROMPT_EVALUATION_INVALID"),
            ("screen.plan.repair", {"diagnostic": "retry without saved data"}, "PROMPT_EVALUATION_INVALID"),
            ("story.classification", {"evidence": "test-invalid-sensitive-input"}, "PROMPT_EVALUATION_INVALID"),
        ]
        with patch("sfdc_agent.prompt_provider_eval.create_provider") as create:
            for operation, fields, code in cases:
                with self.subTest(operation=operation), self.assertRaises(ProviderError) as caught:
                    evaluate_prompt_provider(operation, fields, "azure", release_id=BASELINE_RELEASE)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("test-invalid-sensitive-input", str(caught.exception))
            create.assert_not_called()


if __name__ == "__main__":
    unittest.main()

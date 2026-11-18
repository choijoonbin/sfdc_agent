import asyncio
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from sfdc_agent.managed_prompt_observability import evaluate, private_value
from sfdc_agent.managed_prompts import PromptBundle, checked_bundle, execution_prompts, record_managed_trace, runner_token, template_digest
from sfdc_agent.prompt_run_values import capture_prompt_run
from sfdc_agent.prompt_templates import load_baseline_templates, PromptTemplate
from sfdc_agent.story_tasks import build_story_task


class ManagedPromptObservabilityTest(unittest.TestCase):
    def setUp(self):
        templates = load_baseline_templates()
        self.bundle = PromptBundle(releaseId="prompts-v1", sha256=template_digest(templates), templates=templates)

    def request(self, body=None, token=None, auth=True):
        headers = [(b"authorization", b"Bearer service-token")] if auth else []
        if token:
            headers.append((b"x-sfdc-managed-prompt", token.encode()))
        async def receive():
            return {"type": "http.request", "body": json.dumps(body or {}).encode(), "more_body": False}
        return Request({"type": "http", "method": "POST", "headers": headers}, receive)

    def test_records_actual_substitution_under_only_its_job_context_and_redacts_credentials(self):
        release = checked_bundle(self.bundle)
        with execution_prompts(self.bundle) as traces:
            request = self.request(token=runner_token())
            with capture_prompt_run("screen.analyze.local") as capture:
                text = PromptTemplate("screen.analyze.local", release.templates).render(
                    node_id="1:2", source_context='{"apiKey":"synthetic-private-value"}')
            record_managed_trace(request, capture, release, text, "headless")
            self.assertEqual(len(traces), 1)
            self.assertEqual(traces[0]["values"]["node_id"][0]["value"], "1:2")
            self.assertTrue(traces[0]["values"]["source_context"][0]["redacted"])
            self.assertNotIn("synthetic-private-value", json.dumps(traces))
            with execution_prompts(self.bundle) as other:
                self.assertEqual(other, [])
        record_managed_trace(request, capture, release, text, "headless")
        self.assertEqual(len(traces), 1)

    def test_evaluation_uses_supplied_immutable_bundle_and_returns_only_redacted_provider_response(self):
        payload = {"operation": "story.classification", "provider": "openai", "model": "test-model",
                   "fields": {"evidence": '{"headings":[],"screens":[],"links":[]}'},
                   "promptBundle": self.bundle.model_dump()}
        response = {"operation": "story.classification", "provider": "openai", "promptReleaseId": "prompts-v1",
                    "promptDigest": "a" * 64, "promptBytes": 100, "result": {"accessToken": "private-response", "workflows": []}}
        with patch.dict("os.environ", {"SFDC_AGENT_EXECUTION_TOKEN": "service-token"}), patch(
            "sfdc_agent.managed_prompt_observability.evaluate_prompt_provider", return_value=response) as provider:
            with self.assertRaises(HTTPException):
                asyncio.run(evaluate(self.request(payload, auth=False)))
            result = asyncio.run(evaluate(self.request(payload)))
            self.assertTrue(result["result"]["accessToken"]["redacted"])
            self.assertNotIn("private-response", json.dumps(result))
            self.assertEqual(provider.call_args.kwargs["templates_release"].templates, self.bundle.templates)
            payload["fields"]["apiKey"] = "private-request"
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(evaluate(self.request(payload)))
            self.assertEqual(caught.exception.status_code, 422)
            self.assertEqual(provider.call_count, 1)

    def test_nested_secret_keys_and_token_patterns_are_removed(self):
        data = {"items": [{"client-secret": "value"}], "message": "Bearer syntheticcredential0123456789", "ok": "layout"}
        safe = private_value(data)
        self.assertEqual(safe["ok"], "layout")
        self.assertTrue(safe["items"][0]["client-secret"]["redacted"])
        self.assertTrue(safe["message"]["redacted"])

    def test_managed_kiro_uses_the_configured_headless_transport_with_the_same_actual_render(self):
        fields = {"evidence": '{"headings":[],"screens":[],"links":[]}'}
        payload = {"operation": "story.classification", "provider": "kiro", "fields": fields,
                   "promptBundle": self.bundle.model_dump()}
        with patch.dict("os.environ", {"SFDC_AGENT_EXECUTION_TOKEN": "service-token", "SFDC_AGENT_STORY_KIRO_MODE": "headless"}), patch(
            "sfdc_agent.kiro_headless.read_key", return_value="ksk_test_only"), patch("sfdc_agent.kiro_headless.HeadlessKiroSession") as factory:
            factory.return_value.call = AsyncMock(return_value={"text": '{"workflows":[],"unassigned":[]}'})
            factory.return_value.close = AsyncMock()
            response = asyncio.run(evaluate(self.request(payload)))
            self.assertEqual(response["executionMode"], "headless")
            self.assertEqual(response["promptTrace"]["values"]["evidence"][0]["value"], fields["evidence"])
            self.assertEqual(factory.return_value.call.call_args.args[1]["text"], build_story_task("story.classification", fields))
            factory.return_value.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

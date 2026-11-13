import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from unittest.mock import MagicMock, patch

from sfdc_agent.contracts import ScreenPlan, StoryPlan
from sfdc_agent.pilot import propose
from sfdc_agent.providers import ProviderError, create_provider


class AzureProviderTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"SFDC_AGENT_ROOT": directory}, clear=True))
        self.transport = self.enterContext(patch("sfdc_agent.azure_provider.build_opener")).return_value.open
        self.env_file = Path(directory, ".env.azure.local")
        self.settings = {
            "BASE_URL": "https://aihub.invalid",
            "API_VERSION": "2024-10-21",
            "TOKEN_SCOPE": "api://synthetic-aihub/.default",
            "TOKEN_TIMEOUT_SECONDS": "30",
            "TENANT_ID": "11111111-1111-4111-8111-111111111111",
            "CLIENT_ID": "22222222-2222-4222-8222-222222222222",
            "CLIENT_SECRET": "synthetic-client-secret",
            "SUBSCRIPTION_KEY": "synthetic-subscription-key",
            "CHAT_MODEL": "gpt-test-deployment",
            "REQUEST_TIMEOUT_SECONDS": "120",
        }

    def write_settings(self):
        self.env_file.write_text("".join(f"SFDC_AGENT_AZURE_{key}={value}\n" for key, value in self.settings.items()))
        self.env_file.chmod(0o600)

    @staticmethod
    def response(payload):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        return response

    def token(self, value="synthetic-bearer"):
        return self.response({"access_token": value, "expires_in": 3600})

    def completion(self, content=None, finish="stop"):
        if content is None:
            content = '{"findings":[{"evidenceId":"field-1","explanation":"Review field."}]}'
        return self.response({"choices": [{"message": {"content": content}, "finish_reason": finish}]})

    def test_azure_uses_entra_and_gateway_key_and_reuses_token(self):
        self.write_settings()
        self.transport.side_effect = [self.token(), self.completion(), self.completion()]
        os.environ["SFDC_AGENT_AZURE_CHAT_MODEL"] = "configured-deployment"
        provider = create_provider("azure", timeout=42)
        self.transport.assert_not_called()

        evidence = {"nodeId": "12:34"}
        for _ in range(2):
            result = propose(provider, "Review", evidence, ScreenPlan)
            self.assertEqual(result["status"], "received")
            self.assertEqual(result["plan"]["findings"][0]["evidenceId"], "field-1")

        self.assertEqual(self.transport.call_count, 3)
        token_request = self.transport.call_args_list[0].args[0]
        self.assertEqual(token_request.get_method(), "POST")
        self.assertEqual(urlsplit(token_request.full_url).hostname, "login.microsoftonline.com")
        self.assertEqual(urlsplit(token_request.full_url).path,
                         f"/{self.settings['TENANT_ID']}/oauth2/v2.0/token")
        self.assertEqual(parse_qs(token_request.data.decode()), {
            "grant_type": ["client_credentials"], "client_id": [self.settings["CLIENT_ID"]],
            "client_secret": [self.settings["CLIENT_SECRET"]], "scope": [self.settings["TOKEN_SCOPE"]],
        })
        for call in self.transport.call_args_list[1:]:
            request = call.args[0]
            endpoint = urlsplit(request.full_url)
            self.assertEqual(endpoint.hostname, "aihub.invalid")
            self.assertEqual(endpoint.path, "/openai/deployments/configured-deployment/chat/completions")
            self.assertEqual(parse_qs(endpoint.query), {"api-version": [self.settings["API_VERSION"]]})
            self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-bearer")
            self.assertEqual(request.get_header("Api-key"), self.settings["SUBSCRIPTION_KEY"])
            body = json.loads(request.data)
            self.assertEqual(body["model"], "configured-deployment")
            self.assertIn("Review", body["messages"][0]["content"])
            self.assertEqual(json.loads(body["messages"][1]["content"]), evidence)
            self.assertGreater(body["max_completion_tokens"], 0)
            self.assertLessEqual(call.kwargs["timeout"], 42)

    def test_missing_credential_fails_before_network(self):
        self.settings.pop("SUBSCRIPTION_KEY")
        self.write_settings()
        with self.assertRaises(ProviderError) as caught:
            create_provider("azure").complete("Review", {}, ScreenPlan.model_json_schema())
        self.assertIn("SUBSCRIPTION_KEY", str(caught.exception))
        self.transport.assert_not_called()

    def test_short_lived_token_refreshes_before_expiration(self):
        self.write_settings()
        self.transport.side_effect = [
            self.response({"access_token": "synthetic-first", "expires_in": 30}),
            self.completion(),
            self.response({"access_token": "synthetic-refreshed", "expires_in": 30}),
            self.completion(),
        ]
        provider = create_provider("azure", model="explicit-deployment")
        with patch("sfdc_agent.azure_provider.monotonic", return_value=100):
            provider.complete("Review", {}, ScreenPlan.model_json_schema())
        with patch("sfdc_agent.azure_provider.monotonic", return_value=120):
            provider.complete("Review", {}, ScreenPlan.model_json_schema())
        headers = [call.args[0].get_header("Authorization") for call in self.transport.call_args_list]
        self.assertEqual(headers, [None, "Bearer synthetic-first", None, "Bearer synthetic-refreshed"])
        self.assertIn("/deployments/explicit-deployment/", self.transport.call_args.args[0].full_url)

    def test_http_gateway_error_does_not_leak_response_or_repeat_call(self):
        self.write_settings()
        self.transport.side_effect = [self.token(), HTTPError(
            "https://aihub.invalid", 403, "sensitive upstream detail", {}, io.BytesIO(b"sensitive error payload"),
        )]
        with self.assertRaises(ProviderError) as caught:
            create_provider("azure").complete("Review", {}, ScreenPlan.model_json_schema())
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertEqual(self.transport.call_count, 2)

    def test_content_filter_is_not_retried(self):
        self.write_settings()
        self.transport.side_effect = [self.token(), self.completion("", finish="content_filter")]
        with self.assertRaises(ProviderError):
            create_provider("azure").complete("Review", {}, ScreenPlan.model_json_schema())
        self.assertEqual(self.transport.call_count, 2)

    def test_proposal_rejects_content_outside_the_existing_contract(self):
        self.write_settings()
        self.transport.side_effect = [self.token(), self.completion('{"findings":[],"unexpected":true}')]
        result = propose(create_provider("azure"), "Review", {}, ScreenPlan)
        self.assertEqual(result, {"status": "failed", "code": "MODEL_PLAN_SCHEMA"})

    def test_story_plan_and_usage_logging(self):
        self.write_settings()
        self.transport.side_effect = [self.token(), self.response({
            "choices": [{"message": {"content": '{"transitions":[],"scenarios":[]}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 210, "completion_tokens": 15, "total_tokens": 225,
                      "prompt_tokens_details": {"cached_tokens": 80}},
        })]
        with self.assertLogs("sfdc_agent.azure_provider", level="INFO") as logs:
            result = propose(create_provider("azure"), "Review", "synthetic-story-evidence", StoryPlan)
        self.assertEqual(result["status"], "received")
        self.assertEqual(result["plan"]["scenarios"], [])
        self.assertIn("total_tokens=225 cached_tokens=80", logs.output[0])
        self.assertNotIn("synthetic", logs.output[0])

    def test_invalid_endpoint_fails_before_network(self):
        self.settings["BASE_URL"] = "http://aihub.invalid"
        self.write_settings()
        with self.assertRaises(ProviderError) as caught:
            create_provider("azure")
        self.assertEqual(caught.exception.code, "AZURE_CONFIG_INVALID")
        self.transport.assert_not_called()

    def test_default_kiro_provider_does_not_read_azure_settings(self):
        self.env_file.write_text("malformed line")
        self.assertEqual(create_provider("kiro").name, "kiro")
        self.transport.assert_not_called()

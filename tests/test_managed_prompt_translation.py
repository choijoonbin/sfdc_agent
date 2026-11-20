import asyncio
from hashlib import sha256
import json
import re
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from starlette.requests import Request

from sfdc_agent.managed_prompt_translation import (
    TranslationRequest,
    TranslationResponse,
    protect_prompt,
    restore_prompt,
    translate,
    translate_prompt,
)
from sfdc_agent.providers import ProviderError


SOURCE = """@@section main
Read {{source_context}} with get_design_context at https://example.test/design/1.
Keep `{\"mode\":\"strict\"}` and this object {\"rows\":[1,2]} unchanged.
```json
{\"output\": true}
```
"""


class FakeProvider:
    name = "fixture"
    model = "fixture-model"

    def __init__(self, mutate=None):
        self.mutate = mutate
        self.evidence = None

    def complete(self, task, evidence, schema):
        self.evidence = evidence
        body = evidence["body"].replace("Read ", "읽으세요 ").replace(" with ", " 그리고 ")
        if self.mutate:
            body = self.mutate(body)
        return {"targetLocale": "ko-KR", "body": body}


def payload(body=SOURCE, digest=None):
    return TranslationRequest(
        operation="screen.generate",
        sourceReleaseId=uuid4(),
        sourceSha256=digest or sha256(body.encode()).hexdigest(),
        sourceLocale="en",
        targetLocale="ko-KR",
        body=body,
    )


def request(value, auth=True, browser=False):
    headers = [(b"authorization", b"Bearer service-token")] if auth else []
    if browser:
        headers.append((b"origin", b"https://browser.example"))

    async def receive():
        return {"type": "http.request", "body": json.dumps(value).encode(), "more_body": False}

    return Request({"type": "http", "method": "POST", "headers": headers}, receive)


class ManagedPromptTranslationTest(unittest.TestCase):
    def test_masks_and_restores_prompt_syntax_before_returning_korean_reference(self):
        provider = FakeProvider()
        result = translate_prompt(payload(), provider)

        self.assertIn("읽으세요", result.body)
        for protected in ("@@section main", "{{source_context}}", "get_design_context",
                          "https://example.test/design/1.", '`{\"mode\":\"strict\"}`',
                          '{\"rows\":[1,2]}', '```json\n{\"output\": true}\n```'):
            self.assertIn(protected, result.body)
            self.assertNotIn(protected, provider.evidence["body"])
        self.assertNotIn("__NOVA_TRANSLATION_TOKEN_", result.body)
        self.assertEqual(result.translatorVersion, "nova-prompt-ko-KR-v1")
        self.assertEqual(result.glossaryVersion, "nova-prompt-glossary-v1")

    def test_rejects_changed_protected_tokens_and_changed_source_hash(self):
        def duplicate(body):
            token = re.search(r"__NOVA_TRANSLATION_TOKEN_[A-Za-z0-9_]+__", body).group()
            return body.replace(token, token + token, 1)

        with self.assertRaisesRegex(ProviderError, "protected prompt syntax"):
            translate_prompt(payload(), FakeProvider(duplicate))
        with self.assertRaisesRegex(ProviderError, "source hash changed"):
            translate_prompt(payload(digest="a" * 64), FakeProvider())

    def test_protection_roundtrip_requires_every_generated_token_once(self):
        masked, protected = protect_prompt(SOURCE)
        self.assertEqual(restore_prompt(masked, protected), SOURCE)
        first = next(iter(protected))
        with self.assertRaises(ProviderError):
            restore_prompt(masked.replace(first, "", 1), protected)
        first, second = list(protected)[:2]
        swapped = masked.replace(first, "__SWAP__", 1).replace(second, first, 1).replace("__SWAP__", second, 1)
        with self.assertRaisesRegex(ProviderError, "reordered"):
            restore_prompt(swapped, protected)

    def test_service_endpoint_requires_bearer_and_rejects_browser_origin(self):
        raw = payload().model_dump(mode="json")
        response = TranslationResponse(
            sourceSha256=raw["sourceSha256"], targetLocale="ko-KR", body="번역",
            translatorVersion="v1", glossaryVersion="g1", provider="fixture", model=None,
        )
        with patch.dict("os.environ", {"SFDC_AGENT_EXECUTION_TOKEN": "service-token"}), patch(
            "sfdc_agent.managed_prompt_translation.translate_prompt", return_value=response,
        ):
            with self.assertRaises(HTTPException) as unauthenticated:
                asyncio.run(translate(request(raw, auth=False)))
            self.assertEqual(unauthenticated.exception.status_code, 401)
            with self.assertRaises(HTTPException) as browser:
                asyncio.run(translate(request(raw, browser=True)))
            self.assertEqual(browser.exception.status_code, 403)
            self.assertEqual(asyncio.run(translate(request(raw))).body, "번역")


if __name__ == "__main__":
    unittest.main()

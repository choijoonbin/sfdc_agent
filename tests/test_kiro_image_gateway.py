import asyncio
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from starlette.requests import Request

from sfdc_agent.api import app
from sfdc_agent.image_input import ImageInput
from sfdc_agent.kiro_gateway import SessionError, kiro_lifespan, prompt_session, start_session
from sfdc_agent.provider_capabilities import provider_receipt
from image_input_fixtures import image_payload


def request(payload, *, trace=False):
    headers = [(b"host", b"127.0.0.1:8766"), (b"x-sfdc-agent", b"1"), (b"x-sfdc-kiro-purpose", b"screen")]
    if trace:
        headers.append((b"x-sfdc-prompt-trace-owner", b"a" * 64))
    sent = False
    async def receive():
        nonlocal sent
        if sent:
            await asyncio.Future()
        sent = True
        return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}
    return Request({"type": "http", "method": "POST", "headers": headers, "app": app}, receive=receive)


class SessionFixture:
    execution_mode = "headless"
    purpose = "screen"
    closed = False
    expiry = float("inf")

    def __init__(self, *, deliver=True):
        self.deliver, self.calls = deliver, []
        self.provider_receipt = provider_receipt("kiro")

    async def call(self, method, params):
        self.calls.append(params)
        if image := params.get("image"):
            if self.deliver:
                self.provider_receipt["imageInput"] = {"status": "read-verified", "reference": image.receipt(), "completedReads": 1}
        return {"text": "{}", "stopReason": "end_turn", "toolCalls": []}

    async def close(self):
        self.closed = True


class GatewayImageTests(unittest.IsolatedAsyncioTestCase):
    async def open(self, session):
        with patch("sfdc_agent.kiro_gateway.read_key", return_value="ksk_synthetic_fixture"), \
                patch("sfdc_agent.kiro_gateway.HeadlessKiroSession", return_value=session):
            return await start_session(request({"fileKey": "FixtureFile"}))

    async def test_only_validated_bound_bytes_reach_adapter_and_never_prompt_trace(self):
        async with kiro_lifespan(app):
            session = SessionFixture()
            opened = await self.open(session)
            self.assertEqual(opened["imageInputContractVersion"], 1)
            payload = image_payload()
            reply = await prompt_session(request(payload, trace=True), opened["id"])
            params = session.calls[0]
            self.assertIsInstance(params["image"], ImageInput)
            self.assertIsNone(params["png"])
            self.assertNotIn(payload["png"], params["text"])
            self.assertNotIn(payload["png"], json.dumps(reply))
            self.assertNotEqual(reply["inputDigest"], reply["promptDigest"])
            self.assertEqual(reply["imageBytes"], payload["imageBinding"]["bytes"])
            trace = app.state.prompt_run_values.get("a" * 64, "screen.generate", "reviewed_analysis")
            self.assertNotIn(payload["png"], json.dumps(trace))

    async def test_generation_and_repair_reuse_same_source_binding(self):
        async with kiro_lifespan(app):
            session = SessionFixture()
            opened = await self.open(session)
            payload = image_payload()
            await prompt_session(request(payload), opened["id"])
            repair = {**payload, "operation": "screen.repair", "fields": {
                "local": False, "diagnostic": "fixture", "previousDraft": "{}", "evidence": payload["fields"]}}
            await prompt_session(request(repair), opened["id"])
            self.assertEqual(session.calls[0]["image"], session.calls[1]["image"])

    async def test_mismatch_missing_binding_and_other_operation_fail_before_adapter(self):
        async with kiro_lifespan(app):
            session = SessionFixture()
            opened = await self.open(session)
            value = image_payload()
            wrong_digest = copy.deepcopy(value)
            wrong_digest["imageBinding"]["analysisSourceDigest"] = "b" * 64
            no_binding = {**value}
            no_binding.pop("imageBinding")
            review = {**value, "operation": "screen.review-plan"}
            for payload in (wrong_digest, no_binding, review):
                with self.subTest(operation=payload["operation"]), self.assertRaises(SessionError) as error:
                    await prompt_session(request(payload), opened["id"])
                self.assertEqual(error.exception.code, "KIRO_TASK_INVALID")
            self.assertEqual(session.calls, [])

    async def test_managed_attempt_node_mismatch_fails_even_for_a_locally_captured_source(self):
        async with kiro_lifespan(app):
            session = SessionFixture()
            opened = await self.open(session)
            session.managed_execution_context = SimpleNamespace(purpose="screen", file_key=None,
                node_id="56:78", remote=False, alive=lambda: True)
            with self.assertRaises(SessionError) as error:
                await prompt_session(request(image_payload()), opened["id"])
            self.assertEqual(error.exception.code, "KIRO_TASK_INVALID")
            self.assertEqual(session.calls, [])

    async def test_text_answer_without_delivery_receipt_is_not_successful_image_generation(self):
        async with kiro_lifespan(app):
            session = SessionFixture(deliver=False)
            opened = await self.open(session)
            with self.assertRaises(SessionError) as error:
                await prompt_session(request(image_payload()), opened["id"])
            self.assertEqual(error.exception.code, "KIRO_HEADLESS_INCOMPLETE")
            self.assertEqual(len(session.calls), 1)


if __name__ == "__main__":
    unittest.main()

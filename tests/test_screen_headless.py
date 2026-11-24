import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException, Request

from sfdc_agent.api import app, health
from sfdc_agent.kiro_gateway import end_session, kiro_lifespan, prompt_session, start_session
from sfdc_agent.kiro_headless import SessionError


REST_EVIDENCE = {
    "fileKey": "FixtureFile", "nodeId": "29876:15045", "name": "nextBssScreen29876x15045",
    "local": True, "sourceKind": "figma-rest", "layoutDimensions": {"width": 1380, "height": 1716},
    "version": "1.9", "tokenDeclarations": "--nb-color-t-blue-600: #3617ce;", "typographyGroups": [],
    "componentVariants": "Button: Filled, Ghost", "sourceContext": {
        "nodes": [{"id": "29876:15045", "box": [0, 0, 1380, 1716]}],
        "actions": [{"kind": "search", "nodeId": "29876:15050"}],
    },
}
REVIEWED_EVIDENCE = {**REST_EVIDENCE, "reviewedAnalysis": {"id": "fixture-analysis", "digest": "a" * 64}}
TEST_KEY = "ksk_screen_synthetic_fixture"


def request(body=None, purpose=None):
    headers = [(b"host", b"127.0.0.1:8765"), (b"x-sfdc-agent", b"1")]
    if purpose is not None:
        headers.append((b"x-sfdc-kiro-purpose", purpose.encode()))
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            await asyncio.Future()
        sent = True
        return {"type": "http.request", "body": json.dumps(body or {}).encode(), "more_body": False}

    return Request({"type": "http", "method": "POST", "headers": headers, "app": app}, receive=receive)


class ScreenHeadlessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.key = self.enterContext(patch("sfdc_agent.kiro_gateway.read_key", return_value=TEST_KEY))
        self.sessions = []
        self.headless = self.enterContext(patch("sfdc_agent.kiro_gateway.HeadlessKiroSession", side_effect=self.new_session))

    def new_session(self, key, *, purpose):
        self.assertEqual(key, TEST_KEY)
        self.session = Mock(execution_mode="headless", closed=False, purpose=purpose)
        self.session.call = AsyncMock(return_value={"text": "{}", "stopReason": "end_turn", "toolCalls": []})
        self.session.close = AsyncMock()
        self.sessions.append(self.session)
        return self.session

    async def test_screen_and_story_use_the_key_only_headless_transport(self):
        async with kiro_lifespan(app):
            screen = await start_session(request({"readFigma": False}, purpose="screen"))
            self.assertEqual(screen["executionMode"], "headless")
            self.assertEqual(health()["screenKiroMode"], "headless")
            self.assertEqual(health()["storyKiroMode"], "headless")
            await end_session(request(), screen["id"])
            story = await start_session(request({"readFigma": False}, purpose="story"))
            self.assertEqual(story["executionMode"], "headless")
            self.assertEqual(self.session.purpose, "story")
            await end_session(request(), story["id"])
            self.assertEqual([session.purpose for session in self.sessions], ["screen", "story"])
            self.assertEqual(self.key.call_count, 2)

    async def test_screen_headless_does_not_open_mcp_or_accept_untrusted_operations(self):
        async with kiro_lifespan(app):
            with self.assertRaises(HTTPException) as disallowed_mcp:
                await start_session(request({"fileKey": "FixtureFile", "readFigma": True}, purpose="screen"))
            self.assertEqual(disallowed_mcp.exception.status_code, 422)
            self.key.assert_not_called()
            session = await start_session(request({"fileKey": "FixtureFile", "readFigma": False}, purpose="screen"))
            self.assertEqual(session["executionMode"], "headless")
            self.assertEqual(self.session.purpose, "screen")
            for operation, fields in (
                ("screen.capture", {"fileKey": "FixtureFile", "nodeId": "29876:15045", "maxDimension": 640}),
                ("story.analysis", {"evidence": "{}", "categories": "{}", "maxFacts": 1}),
                ("screen.generate", REST_EVIDENCE),
                ("screen.repair", {"local": True, "diagnostic": "missing stateless evidence"}),
                ("screen.review-plan", {**REST_EVIDENCE, "sourceContext": None}),
            ):
                with self.subTest(operation=operation):
                    with self.assertRaises(SessionError) as invalid:
                        await prompt_session(request({"operation": operation, "fields": fields}), session["id"])
                    self.assertEqual(invalid.exception.code, "KIRO_TASK_INVALID")
            with self.assertRaises(SessionError):
                await prompt_session(request({"operation": "screen.review-plan", "text": "unstructured request"}), session["id"])
            self.assertFalse(any(call.args[0] == "prompt" for call in self.session.call.await_args_list))

    async def test_rest_analysis_generation_and_repair_are_independent_prompts(self):
        async with kiro_lifespan(app):
            session = await start_session(request({"readFigma": False}, purpose="screen"))
            turns = (
                ("screen.review-plan", REST_EVIDENCE, 'figmaRestSnapshot'),
                ("screen.generate", REVIEWED_EVIDENCE, 'NOVA Figma REST source JSON'),
                ("screen.repair", {"local": True, "diagnostic": "Invalid action event", "evidence": REVIEWED_EVIDENCE,
                                   "previousDraft": '{"title":"broken draft"}'}, 'Previous draft JSON text'),
            )
            for operation, fields, marker in turns:
                result = await prompt_session(request({"operation": operation, "fields": fields}), session["id"])
                self.assertEqual(result["executionMode"], "headless")
                prompt = self.session.call.await_args.args[1]["text"]
                self.assertIn(marker, prompt)
                self.assertIn('"id":"29876:15045","box":[0,0,1380,1716]', prompt)
                self.assertNotIn("get_screenshot", prompt)
                if operation in {"screen.generate", "screen.repair"}:
                    self.assertIn('"id":"fixture-analysis"', prompt)
                if operation == "screen.repair":
                    self.assertIn("Invalid action event", prompt)
                    self.assertIn("broken draft", prompt)

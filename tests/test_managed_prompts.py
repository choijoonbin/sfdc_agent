import asyncio
import json
import unittest

from fastapi import HTTPException
from starlette.requests import Request
from unittest.mock import patch

from sfdc_agent.api import app
from sfdc_agent.managed_prompts import (
    PromptBundle, checked_bundle, execution_prompts, runner_token, session_prompt_release, template_digest, validate,
)
from sfdc_agent.prompt_templates import load_baseline_templates, PromptTemplate


class ManagedPromptTest(unittest.TestCase):
    def bundle(self, marker):
        templates = load_baseline_templates()
        templates["screen.generate"] += "\n" + marker
        return PromptBundle(releaseId="prompts-" + marker * 32, sha256=template_digest(templates), templates=templates)

    def request(self, token=None):
        headers = [(b"x-sfdc-managed-prompt", token.encode())] if token else []
        return Request({"type": "http", "headers": headers})

    def test_execution_snapshots_remain_separate_and_expire_without_changing_baseline(self):
        first, second = self.bundle("a"), self.bundle("b")
        baseline = load_baseline_templates()["screen.generate"]
        self.assertNotEqual(first.sha256, second.sha256)
        with execution_prompts(first):
            token = runner_token()
            release = session_prompt_release(self.request(token), first.releaseId)
            self.assertEqual(release.templates["screen.generate"], first.templates["screen.generate"])
            self.assertEqual(PromptTemplate("screen.generate", release.templates).operation, "screen.generate")
            with execution_prompts(second):
                other = runner_token()
                self.assertNotEqual(token, other)
                self.assertEqual(session_prompt_release(self.request(other), second.releaseId).id, second.releaseId)
                with self.assertRaises(HTTPException):
                    session_prompt_release(self.request(other), first.releaseId)
            self.assertEqual(runner_token(), token)
        self.assertIsNone(runner_token())
        with self.assertRaises(HTTPException):
            session_prompt_release(self.request(token), first.releaseId)
        self.assertEqual(load_baseline_templates()["screen.generate"], baseline)

    def test_rejects_changed_digest_and_unknown_variables(self):
        bundle = self.bundle("a")
        invalid = bundle.model_copy(update={"sha256": "f" * 64})
        with self.assertRaises(ValueError):
            checked_bundle(invalid)
        templates = dict(bundle.templates)
        templates["screen.generate"] += "{{unknown_variable}}"
        with self.assertRaises(ValueError):
            checked_bundle(bundle.model_copy(update={"templates": templates, "sha256": template_digest(templates)}))

    def test_service_validation_uses_actual_render_builders_and_blocks_browser_calls(self):
        def request(payload, headers):
            async def receive():
                return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}
            return Request({"type": "http", "method": "POST", "headers": headers}, receive)

        with patch.dict("os.environ", {"SFDC_AGENT_EXECUTION_TOKEN": "token"}):
            body = load_baseline_templates()["story.generate"]
            payload = {"operation": "story.generate", "templates": {"story.generate": body}}
            with self.assertRaises(HTTPException):
                asyncio.run(validate(request(payload, [])))
            headers = [(b"authorization", b"Bearer token")]
            response = asyncio.run(validate(request(payload, headers)))
            self.assertTrue(response["valid"])
            self.assertTrue(response["cases"])
            self.assertIn("preview", response)
            with self.assertRaises(HTTPException):
                asyncio.run(validate(request(payload, [*headers, (b"origin", b"https://browser.example")])))
            payload["templates"]["story.generate"] = body + "{{unexpected}}"
            self.assertFalse(asyncio.run(validate(request(payload, headers)))["valid"])


if __name__ == "__main__":
    unittest.main()

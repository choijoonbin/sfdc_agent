import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi import Request

from sfdc_agent.api import app
from sfdc_agent.kiro_gateway import kiro_lifespan, prompt_session, start_session
from sfdc_agent.prompt_api import save_draft
from sfdc_agent.prompt_registry import BASELINE_RELEASE, PROMPTS, PromptRegistry, RegistryError
from sfdc_agent.prompt_templates import load_baseline_templates
from sfdc_agent.prompt_validation import preview_operation


OPERATION = "story.classification"
ACTOR = "salesforce:00DFixture:005Fixture"
CHANGE = "\nPreserve explicitly named workflow boundaries."


def request(body=None, headers=None):
    data = json.dumps(body or {}).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            await asyncio.Future()
        sent = True
        return {"type": "http.request", "body": data, "more_body": False}

    return Request({"type": "http", "method": "POST", "app": app,
                    "headers": [(b"x-sfdc-agent", b"1"), (b"x-sfdc-kiro-purpose", b"story"),
                                *(headers or [])]}, receive=receive)


class PromptRegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(temporary) / "prompts.sqlite3"
        self.registry = PromptRegistry(self.path)

    def test_all_operations_preview_and_only_usable_catalog_is_exposed(self):
        catalog = self.registry.catalog()
        self.assertEqual(catalog["activeReleaseId"], BASELINE_RELEASE)
        self.assertFalse(self.path.exists())
        self.assertEqual(len(catalog["operations"]), 12)
        self.assertEqual(sum(item["group"] == "Screen" for item in catalog["operations"]), 9)
        self.assertNotIn("screen.analyze.local", [item["id"] for item in catalog["operations"]])
        templates = load_baseline_templates()
        for operation in PROMPTS:
            with self.subTest(operation=operation):
                preview, cases = preview_operation(operation, templates)
                self.assertTrue(preview and cases)
                self.assertEqual(self.registry.detail(operation)["active"]["templates"][operation], templates[operation])

    def test_variable_detail_examples_cover_all_operations(self):
        for operation in PROMPTS:
            with self.subTest(operation=operation):
                meta = self.registry.detail(operation)["operation"]
                self.assertEqual(tuple(meta["variableDetails"]), meta["variables"])
                for detail in meta["variableDetails"].values():
                    self.assertTrue(detail["description"].strip() and detail["valueType"].strip())
                    json.dumps(detail["example"], allow_nan=False)

    def test_variable_examples_distinguish_input_from_assembled_prompt_parts(self):
        def details(operation):
            return self.registry.detail(operation)["operation"]["variableDetails"]

        review = details("screen.review-plan")
        self.assertEqual(review["version"]["example"], "1.9")
        self.assertEqual(review["component_variants"]["inputField"], "componentVariants")
        self.assertIn("\n", review["component_variants"]["example"])
        self.assertNotIn("inputField", review["source"])
        self.assertIn("figmaContext", review["source"]["example"])
        self.assertIn("nodes", details("screen.plan")["source"]["example"])

        analyze = details("screen.analyze")["arguments"]["example"]
        metadata = details("screen.metadata")["arguments"]["example"]
        self.assertEqual(set(metadata), {"fileKey", "nodeId"})
        self.assertIs(analyze["excludeScreenshot"], False)
        self.assertEqual(analyze["clientFrameworks"], "LWC")

        generate = details("screen.generate")
        self.assertIsInstance(generate["reviewed_analysis"]["example"]["mappings"], list)
        self.assertEqual(generate["history_tables"]["example"][0]["empty"]["box"], [24, 128, 160, 48])

        story = details("story.generate")
        self.assertEqual(story["reviewed"]["inputField"], "input")
        self.assertEqual(set(story["diagnostic"]["example"]), {"code", "message"})
        self.assertEqual(story["diagnostic"]["exampleLabel"], "직렬화 전 JSON 예시")
        self.assertIsInstance(details("screen.repair")["diagnostic"]["example"], str)
        self.assertIsInstance(details("screen.repair")["previous_draft"]["example"], str)

    def test_save_validate_activate_restore_and_keep_immutable_releases(self):
        baseline = self.registry.detail(OPERATION)["active"]["templates"]
        with self.assertRaises(RegistryError) as invalid:
            self.registry.save_draft(OPERATION, {OPERATION: "bad {{incorrect}}"}, None, BASELINE_RELEASE, "test", ACTOR)
        self.assertEqual(invalid.exception.code, "PROMPT_TEMPLATE_INVALID")
        altered = {OPERATION: baseline[OPERATION] + CHANGE}
        draft = self.registry.save_draft(OPERATION, altered, None, BASELINE_RELEASE, "test", ACTOR)["draft"]
        self.assertEqual(draft["version"], "v2")
        self.assertEqual(self.registry.release().id, BASELINE_RELEASE)
        with self.assertRaises(RegistryError) as conflict:
            self.registry.save_draft(OPERATION, altered, None, BASELINE_RELEASE, "test", ACTOR)
        self.assertEqual(conflict.exception.code, "PROMPT_DRAFT_CHANGED")
        with self.assertRaises(RegistryError) as unvalidated:
            self.registry.promote(OPERATION, "v2", BASELINE_RELEASE, ACTOR)
        self.assertEqual(unvalidated.exception.code, "PROMPT_VALIDATION_REQUIRED")
        verification = self.registry.validate(OPERATION, "v2")
        self.assertIn(CHANGE, verification["preview"][OPERATION])
        published = self.registry.promote(OPERATION, "v2", BASELINE_RELEASE, ACTOR)
        second_release = published["activeReleaseId"]
        self.assertEqual(self.registry.release(second_release).templates[OPERATION], altered[OPERATION])
        self.assertEqual(self.registry.release(BASELINE_RELEASE).templates[OPERATION], baseline[OPERATION])
        self.assertIsNone(published["draft"])
        self.assertEqual([entry["version"] for entry in published["history"]], ["v2", "v1"])
        with self.assertRaises(RegistryError) as stale:
            self.registry.promote(OPERATION, "v1", BASELINE_RELEASE, ACTOR, restore=True)
        self.assertEqual(stale.exception.code, "PROMPT_RELEASE_CHANGED")
        restored = PromptRegistry(self.path).promote(OPERATION, "v1", second_release, ACTOR, restore=True)
        self.assertNotEqual(restored["activeReleaseId"], second_release)
        self.assertEqual(self.registry.release().templates[OPERATION], baseline[OPERATION])
        self.assertEqual(self.registry.release(second_release).templates[OPERATION], altered[OPERATION])

    def test_initial_open_before_first_transaction_can_still_resolve_baseline(self):
        self.path.touch()
        self.assertEqual(self.registry.release().id, BASELINE_RELEASE)
        self.assertEqual(self.registry.catalog()["activeReleaseId"], BASELINE_RELEASE)
        self.assertIsNone(self.registry.detail(OPERATION)["draft"])
        with self.assertRaises(RegistryError) as unknown:
            self.registry.release("prompts-" + "a" * 32)
        self.assertEqual(unknown.exception.code, "PROMPT_RELEASE_NOT_FOUND")


class PromptApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(temporary) / "prompts.sqlite3"
        self.enterContext(patch.dict(os.environ, {"SFDC_AGENT_PROMPT_DB": str(self.path),
                                                  "SFDC_AGENT_PROMPT_ADMIN_TOKEN": "fixture-token"}))

    async def test_mutations_need_server_credential_and_do_not_accept_browser_directly(self):
        original = load_baseline_templates()[OPERATION]
        body = {"operation": OPERATION, "actor": ACTOR, "templates": {OPERATION: original + CHANGE},
                "expectedReleaseId": BASELINE_RELEASE, "expectedVersion": None, "reason": "fixture change"}
        forbidden = await save_draft(request(body))
        self.assertEqual(forbidden.status_code, 403)
        credential = (b"x-sfdc-prompt-admin", b"fixture-token")
        browser = await save_draft(request(body, [credential, (b"origin", b"https://localhost:4175")]))
        self.assertEqual(browser.status_code, 403)
        self.assertFalse(self.path.exists())
        allowed = await save_draft(request(body, [credential]))
        self.assertEqual(allowed["draft"]["templates"][OPERATION], original + CHANGE)

    async def test_existing_and_reopened_session_keep_previous_templates(self):
        registry = PromptRegistry(self.path)
        old_body = load_baseline_templates()[OPERATION]
        call_body = {"operation": OPERATION, "fields": {"evidence": "{}"}}

        def fake_session():
            session = Mock(execution_mode="headless", closed=False, purpose="story")
            session.call = AsyncMock(return_value={"text": "{}", "stopReason": "end_turn"})
            session.close = AsyncMock()
            return session

        async with kiro_lifespan(app):
            with patch("sfdc_agent.kiro_gateway.read_key", return_value="ksk_prompt_registry_fixture"), patch(
                    "sfdc_agent.kiro_gateway.HeadlessKiroSession",
                    side_effect=[fake_session() for _ in range(3)]):
                original = await start_session(request())
                first = await prompt_session(request(call_body), original["id"])
                registry.save_draft(OPERATION, {OPERATION: old_body + CHANGE}, None, BASELINE_RELEASE, "fixture", ACTOR)
                registry.validate(OPERATION, "v2")
                new_release = registry.promote(OPERATION, "v2", BASELINE_RELEASE, ACTOR)["activeReleaseId"]
                same_session = await prompt_session(request(call_body), original["id"])
                reopened = await start_session(request({"promptReleaseId": original["promptReleaseId"]}))
                same_release = await prompt_session(request(call_body), reopened["id"])
                latest = await start_session(request())
                changed = await prompt_session(request(call_body), latest["id"])
                self.assertEqual(first, same_session)
                self.assertEqual(first, same_release)
                self.assertEqual(changed["promptVersion"], "v2")
                self.assertEqual(changed["promptReleaseId"], new_release)
                text = app.state.kiro_sessions[latest["id"]].call.await_args.args[1]["text"]
                self.assertIn(CHANGE, text)
                self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), changed["promptDigest"])
                self.assertNotEqual(first["promptDigest"], changed["promptDigest"])

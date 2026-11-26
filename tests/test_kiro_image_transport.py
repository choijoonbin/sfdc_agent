import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from textwrap import dedent
import unittest
from unittest.mock import patch

from sfdc_agent.kiro_headless import HeadlessKiroSession, SessionError, parse_output
from sfdc_agent.kiro_image_guard import guard_image_read
from sfdc_agent.kiro_image_transport import AGENT_NAME, prepare_image_workspace, verify_image_delivery
from image_input_fixtures import event, stream, terminal, validated_image


class ImageAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.transport = prepare_image_workspace(Path(self.directory), validated_image())

    def hook_event(self, phase="PreToolUse", **changes):
        return {"hook_event_name": phase, "tool_name": "read_file", "cwd": str(self.transport.workspace),
                "session_id": "fixture-session", "tool_input": {"path": str(self.transport.workspace / "reference.png")}, **changes}

    def authorize(self):
        guard_image_read(self.transport.manifest, self.hook_event())
        guard_image_read(self.transport.manifest, self.hook_event("PostToolUse"))

    def response(self):
        return stream(event("tool_call", toolCallId="one-image", title="human text is not authorization"),
                      event("tool_call_update", toolCallId="one-image", status="completed"), terminal())

    def test_profile_exposes_only_one_read_file_without_global_trust(self):
        profile = json.loads((self.transport.workspace / ".kiro" / "agents" / f"{AGENT_NAME}.json").read_text())
        self.assertEqual(profile["tools"], ["read_file"])
        self.assertFalse(profile["includeMcpJson"])
        self.assertFalse(profile["includePowers"])
        rules = profile["permissions"]["rules"]
        self.assertEqual([item for item in rules if item["effect"] == "allow"], [
            {"capability": "fs_read", "match": [str(self.transport.workspace / "reference.png")], "effect": "allow"}])
        self.assertIn({"capability": "fs_read", "exclude": [str(self.transport.workspace / "reference.png")], "effect": "deny"}, rules)
        for capability in ("fs_write", "shell", "mcp", "web_fetch", "web_search", "subagent"):
            self.assertIn({"capability": capability, "effect": "deny"}, rules)
        self.assertTrue(all(hook.get("matcher") is None for hook in profile["hooks"]))

    def test_guard_denies_wrong_tool_path_workspace_repeats_and_post_without_pre(self):
        for changes in ({"tool_name": "execute_bash"}, {"tool_input": {"path": "/etc/passwd"}},
                        {"cwd": "/tmp"}, {"session_id": ""}, {"hook_event_name": "PostToolUse"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                guard_image_read(self.transport.manifest, self.hook_event(**changes))
        guard_image_read(self.transport.manifest, self.hook_event())
        with self.assertRaises(FileExistsError):
            guard_image_read(self.transport.manifest, self.hook_event())

    def test_guard_detects_tampering_and_symlink_replacement(self):
        image = self.transport.workspace / "reference.png"
        image.chmod(0o600)
        image.write_bytes(b"different bytes")
        image.chmod(0o400)
        with self.assertRaises(ValueError):
            guard_image_read(self.transport.manifest, self.hook_event())
        image.unlink()
        image.symlink_to("/etc/passwd")
        with self.assertRaises(ValueError):
            guard_image_read(self.transport.manifest, self.hook_event())

    def test_complete_read_requires_stream_and_matching_private_pre_post_receipts(self):
        with self.assertRaises(OSError):
            verify_image_delivery(self.response(), self.transport)
        self.authorize()
        filtered, receipt = verify_image_delivery(self.response(), self.transport)
        self.assertEqual(parse_output(filtered)["toolCalls"], [])
        self.assertEqual(receipt["status"], "read-verified")
        self.assertEqual(receipt["completedReads"], 1)
        self.assertEqual(receipt["reference"], self.transport.image.receipt())
        self.assertEqual(receipt["modelInputResolution"], "unreported")
        self.assertNotIn(self.directory, json.dumps(receipt))
        self.assertNotIn("image/png", filtered.decode())

    def test_missing_failed_duplicate_and_cross_session_reads_never_become_delivered(self):
        self.authorize()
        responses = [stream(terminal()), stream(event("tool_call", toolCallId="one-image"), terminal()),
                     stream(event("tool_call", toolCallId="one-image"),
                            event("tool_call_update", toolCallId="one-image", status="failed"), terminal()),
                     self.response() + stream(event("tool_call", toolCallId="second-image")),
                     self.response().replace(b"fixture-session", b"different-session")]
        for output in responses:
            with self.subTest(output=len(output)), self.assertRaises(ValueError):
                verify_image_delivery(output, self.transport)


KEY = "ksk_synthetic_image_fixture_only"


class HeadlessImageTests(unittest.IsolatedAsyncioTestCase):
    def fake_cli(self, *, skip_read=False, wait=False):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        executable = directory / "fixture-cli"
        executable.write_text(f"#!{sys.executable}\n" + dedent(f'''\
            import hashlib, json, os, shlex, subprocess, sys, time
            from pathlib import Path
            assert os.environ['KIRO_API_KEY'] == {KEY!r}
            if sys.argv[1] == 'whoami':
                print('Authenticated with API key')
                sys.exit(0)
            assert '--trust-tools=' in sys.argv and '--trust-all-tools' not in sys.argv
            name = sys.argv[sys.argv.index('--agent') + 1]
            profile = json.loads((Path.cwd()/'.kiro'/'agents'/(name+'.json')).read_text())
            prompt = sys.stdin.read()
            events = []
            if profile['tools']:
                assert profile['tools'] == ['read_file']
                assert profile['includeMcpJson'] is False and profile['includePowers'] is False
                reference = Path.cwd()/'reference.png'
                assert str(reference) in prompt and 'fixture domain request' in prompt
                Path(sys.argv[0]).with_name('image-workspace').write_text(str(Path.cwd().parent))
                Path(sys.argv[0]).with_name('image-pid').write_text(str(os.getpid()))
                if {wait!r}:
                    time.sleep(60)
                if not {skip_read!r}:
                    for hook in profile['hooks']:
                        event = {{'hook_event_name':hook['trigger'],'tool_name':'read_file',
                                 'cwd':str(Path.cwd()),'session_id':'fixture-session',
                                 'tool_input':{{'path':str(reference)}}}}
                        result = subprocess.run(shlex.split(hook['action']['command']),
                                                input=json.dumps(event).encode(),capture_output=True)
                        assert result.returncode == 0 and result.stdout == b''
                    events = [{{'type':'sessionUpdate','data':{{'sessionId':'fixture-session','update':{{
                        'sessionUpdate':'tool_call','toolCallId':'one-image','title':'irrelevant title'}}}}}},
                        {{'type':'sessionUpdate','data':{{'sessionId':'fixture-session','update':{{
                        'sessionUpdate':'tool_call_update','toolCallId':'one-image','status':'completed'}}}}}}]
            else:
                assert profile['tools'] == []
            events.append({{'type':'runFinished','data':{{'sessionId':'fixture-session','status':'success',
                'stopReason':'end_turn','finalText':'{{}}','finalTextTruncated':False}}}})
            for event in events: print(json.dumps(event))
            '''))
        executable.chmod(0o700)
        self.enterContext(patch.dict(os.environ, {"NOVA_KIRO_CLI": str(executable), "HOME": str(directory)}, clear=True))
        return directory

    async def test_image_delivery_is_verified_and_next_text_turn_remains_tool_free(self):
        directory = self.fake_cli()
        session = HeadlessKiroSession(KEY, purpose="screen")
        try:
            image = validated_image()
            result = await session.call("prompt", {"text": "fixture domain request", "image": image}, timeout=8)
            self.assertEqual(result["text"], "{}")
            self.assertEqual(result["toolCalls"], [{"name": "read_file", "kind": "image", "status": "completed", "sha256": image.binding.sha256}])
            receipt = session.provider_receipt
            self.assertTrue(receipt["capabilities"]["images"])
            self.assertEqual(receipt["imageInput"]["status"], "read-verified")
            self.assertNotIn(str(directory), json.dumps(receipt["imageInput"]))
            self.assertFalse(Path((directory / "image-workspace").read_text()).exists())
            result = await session.call("prompt", {"text": "fixture domain request"}, timeout=8)
            self.assertEqual(result["toolCalls"], [])
            self.assertFalse(session.provider_receipt["capabilities"]["images"])
            self.assertNotIn("imageInput", session.provider_receipt)
        finally:
            await session.close()

    async def test_cli_silently_skipping_image_is_an_error_and_never_a_text_fallback(self):
        directory = self.fake_cli(skip_read=True)
        session = HeadlessKiroSession(KEY, purpose="screen")
        try:
            with self.assertRaises(SessionError) as error:
                await session.call("prompt", {"text": "fixture domain request", "image": validated_image()}, timeout=8)
            self.assertEqual(error.exception.code, "KIRO_HEADLESS_INCOMPLETE")
            self.assertNotIn("imageInput", session.provider_receipt)
            self.assertFalse(session.provider_receipt["capabilities"]["images"])
            self.assertFalse(Path((directory / "image-workspace").read_text()).exists())
        finally:
            await session.close()

    async def test_cancellation_removes_private_image_and_terminates_cli(self):
        directory = self.fake_cli(wait=True)
        session = HeadlessKiroSession(KEY, purpose="screen")
        turn = asyncio.create_task(session.call("prompt", {"text": "fixture domain request", "image": validated_image()}))
        try:
            async with asyncio.timeout(6):
                while not (directory / "image-pid").is_file():
                    await asyncio.sleep(0.02)
            pid = int((directory / "image-pid").read_text())
            private = Path((directory / "image-workspace").read_text())
        finally:
            await session.close()
        with self.assertRaises(SessionError) as error:
            await turn
        self.assertEqual(error.exception.code, "KIRO_ABORTED")
        self.assertFalse(private.exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_story_and_unvalidated_raw_png_cannot_enable_tools(self):
        session = HeadlessKiroSession(KEY, purpose="story")
        try:
            for params in ({"text": "fixture", "image": validated_image()}, {"text": "fixture", "png": "unvalidated"}):
                with self.assertRaises(SessionError) as error:
                    await session.call("prompt", params)
                self.assertEqual(error.exception.code, "KIRO_TASK_INVALID")
        finally:
            await session.close()


if __name__ == "__main__":
    unittest.main()

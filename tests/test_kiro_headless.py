import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent
from unittest.mock import AsyncMock, patch

from starlette.exceptions import HTTPException
from starlette.requests import Request

from sfdc_agent.api import app
from sfdc_agent.kiro_gateway import (
    KiroSession, end_session, kiro_lifespan, prompt_session, start_session,
)
from sfdc_agent.kiro_headless import HeadlessKiroSession, SessionError, parse_output, read_key


TEST_KEY = "ksk_synthetic_fixture_only"


def stream(*events):
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events).encode() + b"\n"


def update(kind, **fields):
    return {"type": "sessionUpdate", "data": {"sessionId": "sess_test",
            "update": {"sessionUpdate": kind, **fields}}}


def completed(**fields):
    return {"type": "runFinished", "data": {"sessionId": "sess_test", "status": "success",
            "stopReason": "end_turn", "finalText": "{}", "finalTextTruncated": False, **fields}}


class KeyTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"SFDC_AGENT_ROOT": directory}, clear=True))
        self.settings = Path(directory) / ".env.kiro.local"

    def write_key(self, permissions=0o600):
        self.settings.write_text(f"# Local Kiro key\nKIRO_API_KEY={TEST_KEY}\n", encoding="utf-8")
        self.settings.chmod(permissions)

    def test_reads_owner_only_file_with_environment_override(self):
        self.write_key()
        self.assertEqual(read_key(), TEST_KEY)
        with patch.dict(os.environ, {"KIRO_API_KEY": "ksk_synthetic_override"}):
            self.assertEqual(read_key(), "ksk_synthetic_override")

    def test_rejects_missing_or_world_readable_key(self):
        with self.assertRaises(SessionError) as missing:
            read_key()
        self.assertEqual(missing.exception.code, "KIRO_KEY_MISSING")
        self.write_key(0o644)
        with self.assertRaises(SessionError) as error:
            read_key()
        self.assertEqual(error.exception.code, "KIRO_KEY_INVALID")
        self.assertNotIn(TEST_KEY, str(error.exception))


class StreamTests(unittest.TestCase):
    def test_uses_completed_model_text_without_duplicating_streamed_chunks(self):
        text = '{"reason":"상담 이력"}'
        result = parse_output(stream(
            update("agent_message_chunk", content={"type": "text", "text": text[:9]}),
            update("agent_message_chunk", content={"type": "text", "text": text[9:]}),
            completed(finalText=text),
        ))
        self.assertEqual(result, {"text": text, "stopReason": "end_turn", "toolCalls": []})

    def test_rejects_invalid_or_incomplete_stream_without_exposing_model_text(self):
        for code, body in (
            ("KIRO_HEADLESS_INVALID", b"malformed JSONL\n"),
            ("KIRO_HEADLESS_INCOMPLETE", stream(update("agent_message_chunk", content={"type": "text", "text": TEST_KEY}))),
            ("KIRO_HEADLESS_INCOMPLETE", stream(completed(finalText=TEST_KEY, finalTextTruncated=True))),
            ("KIRO_HEADLESS_TOOL_CALL", stream(update("tool_call", toolCallId="fixture-call", title="fixture-tool"), completed())),
        ):
            with self.subTest(code=code):
                with self.assertRaises(SessionError) as error:
                    parse_output(body)
                self.assertEqual(error.exception.code, code)
                self.assertNotIn(TEST_KEY, str(error.exception))

    def test_recovers_truncated_summary_from_streamed_chunks(self):
        prefix, suffix = '{"reason":', '"상담 이력"}'
        result = parse_output(stream(
            update("agent_message_chunk", content={"type": "text", "text": prefix}),
            update("agent_message_chunk", content={"type": "text", "text": suffix}),
            completed(finalText=prefix, finalTextTruncated=True),
        ))
        self.assertEqual(result["text"], prefix + suffix)


class HeadlessProcessTests(unittest.IsolatedAsyncioTestCase):
    def fake_cli(self, source):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        executable = Path(directory) / "fake-kiro"
        executable.write_text(f"#!{sys.executable}\n" + dedent(source), encoding="utf-8")
        executable.chmod(0o700)
        self.enterContext(patch.dict(os.environ, {
            "NOVA_KIRO_CLI": str(executable), "HOME": directory,
            "PRIVATE_TEST_ENV": "unrelated-synthetic-credential",
        }, clear=True))
        return Path(directory)

    async def test_launches_isolated_cli_with_key_in_env_and_prompt_in_stdin(self):
        output = stream(completed(finalText='{"ok":true}')).decode()
        self.fake_cli(f"""\
            import os
            import sys
            from pathlib import Path

            assert os.environ.get('KIRO_API_KEY') == {TEST_KEY!r}
            assert os.environ['HOME'] != str(Path(sys.argv[0]).parent)
            assert os.path.isdir(os.environ['HOME'])
            assert os.environ.get('PRIVATE_TEST_ENV') is None
            if sys.argv[1] == 'whoami':
                assert sys.argv[2:] == ['--format', 'plain']
                print('**Authenticated with API key**')
                sys.exit(0)
            assert sys.argv[1] == 'chat'
            assert '--no-interactive' in sys.argv
            assert '--v3' in sys.argv or 'v3' in sys.argv
            assert 'stream-json' in sys.argv or '--output-format=stream-json' in sys.argv
            assert '--trust-all-tools' not in sys.argv
            assert all({TEST_KEY!r} not in arg and 'fixture instruction' not in arg for arg in sys.argv)
            assert sys.stdin.read() == 'fixture instruction'
            print({output!r}, end='')
            """)
        session = HeadlessKiroSession(TEST_KEY)
        try:
            result = await session.call("prompt", {"text": "fixture instruction", "png": None}, timeout=5)
        finally:
            await session.close()
        self.assertEqual(result, {"text": '{"ok":true}', "stopReason": "end_turn", "toolCalls": []})

    async def test_refuses_cli_using_browser_authentication_before_prompt(self):
        directory = self.fake_cli("""\
            import sys
            from pathlib import Path

            if sys.argv[1] == 'whoami':
                print('Authenticated with browser')
            else:
                Path(sys.argv[0]).with_name('model-started').touch()
            """)
        session = HeadlessKiroSession(TEST_KEY)
        try:
            with self.assertRaises(SessionError):
                await session.call("prompt", {"text": "fixture instruction", "png": None}, timeout=5)
        finally:
            await session.close()
        self.assertFalse((directory / "model-started").exists())

    async def test_closing_a_running_session_terminates_the_cli_process(self):
        directory = self.fake_cli("""\
            import os
            import sys
            import time
            from pathlib import Path

            if sys.argv[1] == 'whoami':
                print('Authenticated with API key')
            else:
                sys.stdin.read()
                Path(sys.argv[0]).with_name('model-pid').write_text(str(os.getpid()))
                time.sleep(60)
            """)
        session = HeadlessKiroSession(TEST_KEY)
        turn = asyncio.create_task(session.call("prompt", {"text": "fixture instruction", "png": None}))
        marker = directory / "model-pid"
        try:
            async with asyncio.timeout(5):
                while not marker.exists() or not (pid := marker.read_text()).isdigit():
                    await asyncio.sleep(0.02)
        finally:
            await session.close()
        with self.assertRaises(SessionError) as stopped:
            await turn
        self.assertEqual(stopped.exception.code, "KIRO_ABORTED")
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid), 0)


def request(payload=None, *, story=False):
    headers = [(b"host", b"127.0.0.1:8765"), (b"x-sfdc-agent", b"1")]
    if story:
        headers.append((b"x-sfdc-kiro-purpose", b"story"))
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            await asyncio.Future()
        sent = True
        return {"type": "http.request", "body": json.dumps(payload or {}).encode(), "more_body": False}

    return Request({"type": "http", "method": "POST", "headers": headers, "app": app}, receive=receive)


class GatewayRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_opt_in_is_only_for_story_and_preserves_the_prompt_contract(self):
        mode = {"SFDC_AGENT_STORY_KIRO_MODE": "headless"}
        with patch.dict(os.environ, mode, clear=True), \
                patch("sfdc_agent.kiro_gateway.read_key", return_value=TEST_KEY) as read, \
                patch("sfdc_agent.kiro_gateway.HeadlessKiroSession") as launch, \
                patch.object(KiroSession, "start") as start_acp:
            async with kiro_lifespan(app):
                session = launch.return_value
                session.closed = False
                session.execution_mode = "headless"
                session.purpose = "story"
                session.call = AsyncMock(return_value={"text": "{}", "stopReason": "end_turn", "toolCalls": []})
                session.close = AsyncMock()
                opened = await start_session(request({"readFigma": False}, story=True))
                launch.assert_called_once_with(TEST_KEY, purpose="story")
                read.assert_called_once()
                start_acp.assert_not_awaited()

                fields = {"evidence": '{"scope":"상담 이력"}', "categories": "{}", "maxFacts": 1}
                result = await prompt_session(request({"operation": "story.analysis", "fields": fields}), opened["id"])
                call = next(call for call in session.call.await_args_list if call.args[0] == "prompt")
                prompt = call.args[1]["text"].encode()
                self.assertTrue(prompt.rstrip().endswith(fields["evidence"].encode()))
                self.assertEqual(result["promptDigest"], hashlib.sha256(prompt).hexdigest())
                self.assertEqual(result["promptBytes"], len(prompt))
                self.assertEqual(result["executionMode"], "headless")
                with self.assertRaises(SessionError) as invalid:
                    await prompt_session(request({"operation": "screen.analyze", "fields": {
                        "fileKey": "FixtureScreen", "nodeIds": ["3:4"],
                    }}), opened["id"])
                self.assertEqual(invalid.exception.code, "KIRO_TASK_INVALID")
                self.assertEqual(sum(call.args[0] == "prompt" for call in session.call.await_args_list), 1)
                await end_session(request(), opened["id"])
                session.close.assert_awaited_once()

                acp = start_acp.return_value
                acp.call = AsyncMock()
                acp.close = AsyncMock()
                await start_session(request({"fileKey": "FixtureScreen", "readFigma": True}))
                start_acp.assert_awaited_once()
                launch.assert_called_once()

    async def test_default_story_keeps_acp_and_explicit_story_mcp_is_invalid(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("sfdc_agent.kiro_gateway.read_key") as read, \
                patch("sfdc_agent.kiro_gateway.HeadlessKiroSession") as launch, \
                patch.object(KiroSession, "start") as start_acp:
            acp = start_acp.return_value
            acp.call = AsyncMock()
            acp.close = AsyncMock()
            async with kiro_lifespan(app):
                await start_session(request({"readFigma": False}, story=True))
                with self.assertRaises(HTTPException) as error:
                    await start_session(request({"fileKey": "FixtureScreen", "readFigma": True}, story=True))
                self.assertEqual(error.exception.status_code, 422)
                start_acp.assert_awaited_once()
                launch.assert_not_called()
                read.assert_not_called()

    async def test_explicit_headless_story_missing_key_does_not_use_acp(self):
        mode = {"SFDC_AGENT_STORY_KIRO_MODE": "headless"}
        with patch.dict(os.environ, mode, clear=True), \
                patch("sfdc_agent.kiro_gateway.read_key", side_effect=SessionError("KIRO_KEY_MISSING", "Missing")), \
                patch("sfdc_agent.kiro_gateway.HeadlessKiroSession") as launch, \
                patch.object(KiroSession, "start") as start_acp:
            async with kiro_lifespan(app):
                with self.assertRaises(SessionError) as error:
                    await start_session(request({"readFigma": False}, story=True))
                self.assertEqual(error.exception.code, "KIRO_KEY_MISSING")
                self.assertEqual(app.state.kiro_sessions, {})
                launch.assert_not_called()
                start_acp.assert_not_awaited()

import asyncio
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import sys
import tempfile
from textwrap import dedent
import unittest
from unittest.mock import AsyncMock, patch

from sfdc_agent import kiro_headless
from sfdc_agent.kiro_headless import HeadlessKiroSession, SessionError, _cleanup_workspace
from sfdc_agent.kiro_processes import ProcessCleanupError, _snapshot


KEY = 'ksk_synthetic_lifecycle_fixture'
ANSWER = '{"fixture":"completed"}'


class HeadlessLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def fake_cli(self, *, inherited_stdout=False, hang=False, child_on_identity=False):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        executable = directory / 'fake-kiro'
        executable.write_text(f'#!{sys.executable}\n' + dedent(f'''\
            import json, os, shutil, subprocess, sys, time
            from pathlib import Path
            marker = Path(sys.argv[0]).parent
            home = Path(os.environ['HOME'])
            marker.joinpath('workspace').write_text(str(home))
            identity = sys.argv[1] == 'whoami'
            if identity == {child_on_identity!r}:
                support = home / 'kiro-data' / 'node'
                support.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile('/bin/sh', support)
                support.chmod(0o700)
                writer = "trap '' TERM; while :; do mkdir -p .kiro/session; echo writing >> .kiro/session/messages; sleep 0.01; done"
                child = subprocess.Popen([str(support), '-c', writer], cwd=home,
                    start_new_session=True, stdin=subprocess.DEVNULL,
                    stdout=None if {inherited_stdout!r} else subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                marker.joinpath('child-pid').write_text(str(child.pid))
            if identity:
                print('Authenticated with API key')
            else:
                sys.stdin.read()
                marker.joinpath('parent-pid').write_text(str(os.getpid()))
                if {hang!r}:
                    time.sleep(60)
                print(json.dumps({{'type':'runFinished','data':{{'status':'success','stopReason':'end_turn',
                    'finalText':{ANSWER!r},'finalTextTruncated':False}}}}), flush=True)
            '''))
        executable.chmod(0o700)
        self.enterContext(patch.dict(os.environ, {'NOVA_KIRO_CLI': str(executable), 'HOME': str(directory)}, clear=True))
        return directory

    async def wait_marker(self, path):
        async with asyncio.timeout(5):
            while not path.exists() or not path.read_text().strip():
                await asyncio.sleep(0.02)
        return path.read_text().strip()

    def assert_not_running(self, pid):
        self.assertFalse(any(row.pid == int(pid) and not row.state.startswith('Z') for row in _snapshot()))

    async def test_success_stops_detached_writer_and_inherited_stdout_without_losing_answer(self):
        directory = self.fake_cli(inherited_stdout=True)
        sibling = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(60)',
            cwd=directory, start_new_session=True)
        session = HeadlessKiroSession(KEY)
        try:
            result = await session.call('prompt', {'text': 'synthetic fixture'}, timeout=5)
            self.assertEqual(result['text'], ANSWER)
            self.assertEqual(result['stopReason'], 'end_turn')
            self.assert_not_running((directory / 'child-pid').read_text())
            self.assertFalse(Path((directory / 'workspace').read_text()).exists())
            self.assertIsNone(sibling.returncode, 'An unrelated sibling must never be signalled')
        finally:
            await session.close()
            sibling.kill()
            await sibling.wait()

    async def test_identity_command_also_reaps_its_detached_support_process(self):
        directory = self.fake_cli(child_on_identity=True)
        session = HeadlessKiroSession(KEY)
        try:
            self.assertEqual((await session.call('prompt', {'text': 'synthetic fixture'}, timeout=5))['text'], ANSWER)
            self.assert_not_running((directory / 'child-pid').read_text())
            self.assertFalse(Path((directory / 'workspace').read_text()).exists())
        finally:
            await session.close()

    async def test_timeout_retains_primary_code_and_kills_detached_term_ignoring_writer(self):
        directory = self.fake_cli(hang=True, inherited_stdout=True)
        session = HeadlessKiroSession(KEY)
        try:
            with self.assertRaises(SessionError) as failed:
                await session.call('prompt', {'text': 'synthetic fixture'}, timeout=1.5)
            self.assertEqual(failed.exception.code, 'KIRO_TIMEOUT')
            self.assert_not_running((directory / 'parent-pid').read_text())
            self.assert_not_running((directory / 'child-pid').read_text())
            self.assertFalse(Path((directory / 'workspace').read_text()).exists())
        finally:
            await session.close()

    async def test_close_cancels_running_parent_and_detached_writer_before_workspace_cleanup(self):
        directory = self.fake_cli(hang=True, inherited_stdout=True)
        session = HeadlessKiroSession(KEY)
        turn = asyncio.create_task(session.call('prompt', {'text': 'synthetic fixture'}, timeout=10))
        await self.wait_marker(directory / 'parent-pid')
        await session.close()
        with self.assertRaises(SessionError) as stopped:
            await turn
        self.assertEqual(stopped.exception.code, 'KIRO_ABORTED')
        self.assert_not_running((directory / 'parent-pid').read_text())
        self.assert_not_running((directory / 'child-pid').read_text())
        self.assertFalse(Path((directory / 'workspace').read_text()).exists())

    async def test_workspace_enotempty_race_is_bounded_and_retried(self):
        with tempfile.TemporaryDirectory() as parent:
            home = Path(parent) / 'owned'
            home.mkdir()
            original = shutil.rmtree
            calls = []
            def racing_cleanup(path):
                calls.append(path)
                if len(calls) < 3:
                    raise OSError(errno.ENOTEMPTY, 'synthetic concurrent directory flush')
                return original(path)
            with patch('sfdc_agent.kiro_headless.shutil.rmtree', side_effect=racing_cleanup):
                await _cleanup_workspace(home)
            self.assertEqual(len(calls), 3)
            self.assertFalse(home.exists())

    async def test_workspace_cleanup_failure_cannot_mask_original_timeout(self):
        directory = self.fake_cli(hang=True)
        session = HeadlessKiroSession(KEY)
        try:
            with patch('sfdc_agent.kiro_headless._cleanup_workspace', AsyncMock(side_effect=OSError(errno.ENOTEMPTY, 'fixture'))):
                with self.assertRaises(SessionError) as failed:
                    await session.call('prompt', {'text': 'synthetic fixture'}, timeout=1.5)
            self.assertEqual(failed.exception.code, 'KIRO_TIMEOUT')
            self.assertIn('KIRO_WORKSPACE_CLEANUP_FAILED', failed.exception.__notes__)
            self.assert_not_running((directory / 'child-pid').read_text())
        finally:
            await session.close()
            shutil.rmtree((directory / 'workspace').read_text())

    async def test_completed_answer_has_private_recovery_evidence_if_workspace_cleanup_cannot_finish(self):
        directory = self.fake_cli()
        session = HeadlessKiroSession(KEY)
        try:
            with patch('sfdc_agent.kiro_headless._cleanup_workspace', AsyncMock(side_effect=OSError(errno.ENOTEMPTY, 'fixture'))):
                result = await session.call('prompt', {'text': 'synthetic fixture'}, timeout=5)
            self.assertEqual(result['text'], ANSWER)
            home = Path((directory / 'workspace').read_text())
            evidence = home / 'completed-response.json'
            self.assertEqual(stat.S_IMODE(evidence.stat().st_mode), 0o600)
            result = json.loads(evidence.read_text())
            self.assertEqual(result['validation'], 'CLI_STREAM_ONLY', 'This is not a validated Story checkpoint')
            self.assertEqual(result['response']['text'], ANSWER)
            self.assertEqual(result['textSha256'], hashlib.sha256(ANSWER.encode()).hexdigest())
            self.assertNotIn(KEY, evidence.read_text())
            self.assert_not_running((directory / 'child-pid').read_text())
        finally:
            await session.close()
            shutil.rmtree((directory / 'workspace').read_text())

    async def test_cancellation_during_spawn_waits_for_owned_child_then_reaps_it(self):
        directory = self.fake_cli(child_on_identity=True)
        started = asyncio.Event()
        release = asyncio.Event()
        original = asyncio.create_subprocess_exec
        async def delayed_creation(*args, **kwargs):
            process = await original(*args, **kwargs)
            started.set()
            await release.wait()
            return process
        session = HeadlessKiroSession(KEY)
        with patch('sfdc_agent.kiro_headless.asyncio.create_subprocess_exec', side_effect=delayed_creation):
            turn = asyncio.create_task(session.call('prompt', {'text': 'synthetic fixture'}, timeout=10))
            await started.wait()
            await self.wait_marker(directory / 'child-pid')
            asyncio.get_running_loop().call_later(0.05, release.set)
            await session.close()
            with self.assertRaises(SessionError) as failed:
                await turn
            self.assertEqual(failed.exception.code, 'KIRO_ABORTED')
        self.assert_not_running((directory / 'child-pid').read_text())
        self.assertFalse(Path((directory / 'workspace').read_text()).exists())
        self.assertFalse((directory / 'parent-pid').exists(), 'Cancelled identity startup must not begin a model turn')

    async def test_double_cancellation_during_cancelled_io_join_still_reaps_and_cleans(self):
        directory = self.fake_cli(hang=True, inherited_stdout=True)
        joining = asyncio.Event()
        release = asyncio.Event()
        original = kiro_headless._read_output
        async def delayed_cancel(*args, **kwargs):
            try:
                return await original(*args, **kwargs)
            except asyncio.CancelledError:
                joining.set()
                await release.wait()
                raise
        session = HeadlessKiroSession(KEY)
        try:
            with patch('sfdc_agent.kiro_headless._read_output', side_effect=delayed_cancel):
                turn = asyncio.create_task(session.call('prompt', {'text': 'synthetic fixture'}, timeout=10))
                await self.wait_marker(directory / 'parent-pid')
                turn.cancel()
                await joining.wait()
                # The protected cleanup must join the cancelled reader even if its
                # caller is cancelled again after the process tree has stopped.
                async with asyncio.timeout(5):
                    while any(row.pid == int((directory / 'child-pid').read_text()) and not row.state.startswith('Z') for row in _snapshot()):
                        await asyncio.sleep(0.03)
                turn.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await turn
            self.assert_not_running((directory / 'parent-pid').read_text())
            self.assert_not_running((directory / 'child-pid').read_text())
            self.assertFalse(Path((directory / 'workspace').read_text()).exists())
        finally:
            release.set()
            await session.close()

    async def test_late_cancel_during_cleanup_does_not_replace_established_timeout(self):
        directory = self.fake_cli(hang=True, inherited_stdout=True)
        joining = asyncio.Event()
        release = asyncio.Event()
        original = kiro_headless._read_output
        async def delayed_cancel(*args, **kwargs):
            try:
                return await original(*args, **kwargs)
            except asyncio.CancelledError:
                joining.set()
                await release.wait()
                raise
        session = HeadlessKiroSession(KEY)
        try:
            with patch('sfdc_agent.kiro_headless._read_output', side_effect=delayed_cancel):
                turn = asyncio.create_task(session.call('prompt', {'text': 'synthetic fixture'}, timeout=1.5))
                await joining.wait()
                turn.cancel()
                release.set()
                with self.assertRaises(SessionError) as failed:
                    await turn
            self.assertEqual(failed.exception.code, 'KIRO_TIMEOUT')
            self.assert_not_running((directory / 'child-pid').read_text())
            self.assertFalse(Path((directory / 'workspace').read_text()).exists())
        finally:
            release.set()
            await session.close()

    async def test_unproven_process_quiescence_cannot_publish_a_completed_response(self):
        directory = self.fake_cli(inherited_stdout=True)
        session = HeadlessKiroSession(KEY)
        original = kiro_headless.OwnedProcesses.quiesce
        calls = 0
        async def fail_chat_quiescence(owned):
            nonlocal calls
            calls += 1
            if calls <= 2:  # Both identity command observations may finish normally.
                return await original(owned)
            raise ProcessCleanupError('synthetic ownership metadata failure')
        try:
            with patch('sfdc_agent.kiro_headless.OwnedProcesses.quiesce', fail_chat_quiescence):
                with self.assertRaises(SessionError) as failed:
                    await session.call('prompt', {'text': 'synthetic fixture'}, timeout=5)
            self.assertEqual(failed.exception.code, 'KIRO_PROCESS_CLEANUP_FAILED')
            home = Path((directory / 'workspace').read_text())
            self.assertTrue(home.exists(), 'Unproven live ownership must not start deleting its workspace')
            self.assertFalse((home / 'completed-response.json').exists())
        finally:
            # Only this synthetic fixture's freshly recorded isolated process group.
            pid = int((directory / 'child-pid').read_text())
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await session.close()
            shutil.rmtree((directory / 'workspace').read_text())

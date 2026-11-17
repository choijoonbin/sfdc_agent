import asyncio
import hashlib
import json
import unittest
from unittest.mock import patch

from sfdc_agent.api import app
from sfdc_agent.kiro_gateway import KiroSession, SessionError, kiro_lifespan, reap_kiro_sessions


BASE_PATH = '/internal/kiro/sessions'
CANDIDATE_PATH = '/internal/kiro/design-standards/candidate'
PNG_HEADERS = ((b'x-sfdc-kiro-purpose', b'figma-png'),)


def candidate_payload():
    return {
        'designVersion': '2.0', 'baseReleaseId': 'nextbss-1.9-' + 'a' * 64,
        'sources': {'guide': {'fileKey': 'FileA', 'revision': '1234567890123'},
                    'library': {'fileKey': 'FileB', 'sha256': 'b' * 64}},
        'evidence': [{'source': 'guide', 'nodeId': '1:2', 'title': 'Typography', 'text': 'Pretendard'}],
    }


class StubSession:
    execution_mode = 'acp'

    def __init__(self):
        self.closed = False
        self.events = [{'type': 'figma-auth-required'}]
        self.updated = asyncio.Event()
        self.reader = asyncio.get_running_loop().create_future()
        self.expiry = float('inf')
        self.calls = []
        self.pending = {}
        self.figma_connected = False
        self.png_prompt_active = False

    def touch(self):
        self.expiry = float('inf')

    async def call(self, method, params=None, timeout=None):
        self.calls.append((method, params))
        if method == 'authorize':
            return 'https://www.figma.com/oauth/authorize'
        if method == 'prompt':
            return {'text': '{}', 'stopReason': 'end_turn', 'toolCalls': []}
        return None

    async def close(self):
        self.closed = True
        self.reader.cancel()


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, method, path, payload=None, *, trusted=True, extra_headers=(), disconnect=False, query=b''):
        headers = [(b'host', b'127.0.0.1:8765')]
        if trusted:
            headers.append((b'x-sfdc-agent', b'1'))
        headers.extend(extra_headers)
        body = b'' if payload is None else json.dumps(payload).encode()
        messages = []

        request_sent = False

        async def receive():
            nonlocal request_sent
            if request_sent:
                if disconnect:
                    return {'type': 'http.disconnect'}
                await asyncio.Future()
            request_sent = True
            return {'type': 'http.request', 'body': body, 'more_body': False}

        async def send(message):
            messages.append(message)

        await app({
            'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
            'scheme': 'http', 'method': method, 'path': path, 'root_path': '',
            'query_string': query, 'headers': headers, 'server': ('127.0.0.1', 8765),
            'client': ('127.0.0.1', 41000),
        }, receive, send)
        body = b''.join(message.get('body', b'') for message in messages)
        return messages[0]['status'], json.loads(body) if body else None

    async def test_design_candidate_returns_only_reviewable_cited_proposals(self):
        data = candidate_payload()
        proposal = {'summary': '서체 기준의 변경을 확인합니다.', 'suggestions': [{
            'source': 'guide', 'nodeId': '1:2', 'title': '서체', 'note': '기존 토큰과 확인하세요.',
        }]}
        session = StubSession()
        async with kiro_lifespan(app):
            with patch('sfdc_agent.design_candidate.read_key', return_value='test-key'), patch(
                'sfdc_agent.design_candidate.HeadlessKiroSession', return_value=session
            ) as launch, patch.object(session, 'call', return_value={'text': json.dumps(proposal)}) as call:
                status, reply = await self.request('POST', CANDIDATE_PATH, data)
        self.assertEqual(status, 200)
        launch.assert_called_once_with('test-key', purpose='design-standards')
        self.assertEqual(call.call_args.args[0], 'prompt')
        prompt = call.call_args.args[1]['text']
        self.assertIn('"text": "Pretendard"', prompt)
        self.assertEqual(reply, {**proposal, 'promptVersion': 'design-candidate-v1',
                                'promptDigest': hashlib.sha256(prompt.encode()).hexdigest()})
        self.assertTrue(session.closed)

    async def test_design_candidate_rejects_untrusted_or_unpinned_input_before_launch(self):
        data = candidate_payload()
        unpinned = {**data, 'sources': {**data['sources'], 'guide': {'fileKey': 'FileA'}}}
        async with kiro_lifespan(app):
            with patch('sfdc_agent.design_candidate.read_key') as key:
                self.assertEqual((await self.request('POST', CANDIDATE_PATH, data, trusted=False))[0], 403)
                self.assertEqual((await self.request(
                    'POST', CANDIDATE_PATH, data, extra_headers=((b'origin', b'https://localhost:4175'),)
                ))[0], 403)
                self.assertEqual((await self.request('POST', CANDIDATE_PATH, unpinned))[0], 422)
                self.assertEqual((await self.request(
                    'POST', CANDIDATE_PATH, {**data, 'evidence': [{**data['evidence'][0], 'text': 'a' * 500000}]}
                ))[0], 413)
                key.assert_not_called()

    async def test_design_candidate_drops_outputs_with_unknown_citations_or_extra_mutations(self):
        candidate = candidate_payload()
        proposal = {'summary': '서체', 'suggestions': [{
            'source': 'library', 'nodeId': '1:2', 'title': '서체', 'note': '검토',
        }]}
        async with kiro_lifespan(app):
            for answer in (proposal, {**proposal, 'suggestions': [], 'activate': True}):
                with self.subTest(answer=answer):
                    session = StubSession()
                    with patch('sfdc_agent.design_candidate.read_key', return_value='test-key'), patch(
                        'sfdc_agent.design_candidate.HeadlessKiroSession', return_value=session
                    ), patch.object(session, 'call', return_value={'text': json.dumps(answer)}):
                        status, reply = await self.request('POST', CANDIDATE_PATH, candidate)
                    self.assertEqual(status, 502)
                    self.assertEqual(reply['error']['code'], 'KIRO_INVALID_PROPOSAL')
                    self.assertTrue(session.closed)

    async def test_session_preserves_oauth_and_builds_structured_screen_and_story_prompts(self):
        session = StubSession()
        async with kiro_lifespan(app):
            with patch.object(KiroSession, 'start', return_value=session):
                status, opened = await self.request('POST', BASE_PATH, {'fileKey': 'FileA', 'readFigma': True})
            self.assertEqual(status, 200)
            path = f'{BASE_PATH}/{opened["id"]}'
            self.assertEqual(session.calls[0], ('open', {'reference': {'fileKey': 'FileA'}, 'readFigma': True}))
            self.assertEqual(await self.request('GET', f'{path}/events'), (200, {'events': [{'type': 'figma-auth-required'}]}))
            self.assertEqual(await self.request('POST', f'{path}/authorize'), (200, {'result': 'https://www.figma.com/oauth/authorize'}))
            self.assertEqual(await self.request('POST', f'{path}/wait'), (200, {'result': None}))

            status, reply = await self.request('POST', f'{path}/prompt', {
                'operation': 'screen.analyze', 'fields': {'fileKey': 'FileA', 'nodeIds': ['3:4']},
            })
            screen_prompt = session.calls[-1][1]['text']
            self.assertEqual(status, 200)
            self.assertIn('get_design_context', screen_prompt)
            self.assertIn('"nodeId":"3:4"', screen_prompt)
            self.assertEqual(reply['promptDigest'], hashlib.sha256(screen_prompt.encode()).hexdigest())

            evidence = '{"scope":"번호이동"}'
            status, _ = await self.request('POST', f'{path}/prompt', {
                'operation': 'story.analysis', 'fields': {'evidence': evidence, 'categories': '{}', 'maxFacts': 1},
            })
            self.assertEqual(status, 200)
            self.assertTrue(session.calls[-1][1]['text'].endswith(evidence))

            digest = 'e' * 64
            reviewed = json.dumps({'specDigest': digest, 'bundleName': 'nextBssStoryTest'})
            status, reply = await self.request('POST', f'{path}/prompt', {
                'operation': 'story.generate', 'fields': {'input': reviewed},
            })
            story_prompt = session.calls[-1][1]['text']
            self.assertEqual(status, 200)
            self.assertIn('nextBssStoryTest.html', story_prompt)
            self.assertIn(digest, story_prompt)
            self.assertTrue(story_prompt.endswith(reviewed))
            self.assertEqual(reply['promptBytes'], len(story_prompt.encode()))

            self.assertEqual(await self.request('DELETE', path), (204, None))
            self.assertTrue(session.closed)
            status, error = await self.request('POST', f'{path}/wait')
            self.assertEqual(status, 502)
            self.assertEqual(error['error']['code'], 'KIRO_CLOSED')

    async def test_png_sessions_force_acp_and_reuse_only_connected_sessions_of_the_same_file(self):
        session = StubSession()
        different_file = StubSession()
        normal_session = StubSession()
        file_a = {'fileKey': 'FileA', 'readFigma': True}
        async with kiro_lifespan(app):
            with patch.object(KiroSession, 'start', side_effect=[session, normal_session, different_file]) as launch, patch(
                'sfdc_agent.kiro_gateway.kiro_mode', return_value='headless'
            ) as configured_mode:
                for payload in ({'fileKey': 'FileA'}, {'readFigma': True}):
                    status, _ = await self.request('POST', BASE_PATH, payload, extra_headers=PNG_HEADERS)
                    self.assertEqual(status, 422)
                launch.assert_not_awaited()
                status, first = await self.request('POST', BASE_PATH, file_a, extra_headers=PNG_HEADERS)
                self.assertEqual(status, 200)
                self.assertEqual(first['executionMode'], 'acp')
                status, busy = await self.request('POST', BASE_PATH, file_a, extra_headers=PNG_HEADERS)
                self.assertEqual(status, 502)
                self.assertEqual(busy['error']['code'], 'KIRO_BUSY')
                self.assertEqual(session.calls, [('open', {
                    'reference': {'fileKey': 'FileA'}, 'readFigma': True, 'figmaPng': True,
                })])
                session.figma_connected = True
                first_path = f'{BASE_PATH}/{first["id"]}'
                await self.request('GET', f'{first_path}/events')
                old_poll = asyncio.create_task(self.request('GET', f'{first_path}/events'))
                await asyncio.sleep(0)
                self.assertEqual(await self.request('DELETE', first_path), (204, None))
                self.assertFalse(session.closed)
                status, normal = await self.request('POST', BASE_PATH, file_a)
                self.assertEqual(status, 200)
                status, second = await self.request('POST', BASE_PATH, file_a, extra_headers=PNG_HEADERS)
                self.assertEqual(status, 200)
                self.assertNotEqual(second['id'], first['id'])
                self.assertNotEqual(second['id'], normal['id'])
                status, expired = await old_poll
                self.assertEqual(status, 502)
                self.assertEqual(expired['error']['code'], 'KIRO_CLOSED')
                second_path = f'{BASE_PATH}/{second["id"]}'
                self.assertEqual(await self.request('GET', f'{second_path}/events'),
                                 (200, {'events': [{'type': 'status', 'stage': 'figma-connected'}]}))
                status, different = await self.request('POST', BASE_PATH, {**file_a, 'fileKey': 'filea'},
                                                       extra_headers=PNG_HEADERS)
                self.assertEqual(status, 200)
                self.assertEqual(await self.request('DELETE', f'{BASE_PATH}/{different["id"]}'), (204, None))
                self.assertTrue(different_file.closed)
                self.assertEqual(await self.request('DELETE', second_path), (204, None))
                session.expiry = 0
                await reap_kiro_sessions(app)
                self.assertTrue(session.closed)
                configured_mode.assert_not_called()
        self.assertEqual(launch.await_count, 3)

    async def test_png_session_prevents_duplicate_open_and_prompt_while_requests_are_running(self):
        session = StubSession()
        launching, launch_allowed = asyncio.Event(), asyncio.Event()
        prompting, prompt_allowed = asyncio.Event(), asyncio.Event()
        file_a = {'fileKey': 'FileA', 'readFigma': True}

        async def launch():
            launching.set()
            await launch_allowed.wait()
            return session

        call_stub = session.call

        async def call(method, params=None, timeout=None):
            if method == 'prompt':
                prompting.set()
                await prompt_allowed.wait()
            return await call_stub(method, params, timeout)

        async with kiro_lifespan(app):
            with patch.object(KiroSession, 'start', side_effect=launch), patch.object(session, 'call', side_effect=call):
                start = asyncio.create_task(self.request('POST', BASE_PATH, file_a, extra_headers=PNG_HEADERS))
                await launching.wait()
                status, busy = await self.request('POST', BASE_PATH, file_a, extra_headers=PNG_HEADERS)
                self.assertEqual((status, busy['error']['code']), (502, 'KIRO_BUSY'))
                launch_allowed.set()
                status, opened = await start
                self.assertEqual(status, 200)
                session.figma_connected = True
                path = f'{BASE_PATH}/{opened["id"]}'
                prompt = asyncio.create_task(self.request('POST', f'{path}/prompt', {'text': 'Export a PNG.'}))
                await prompting.wait()
                status, busy = await self.request('POST', f'{path}/prompt', {'text': 'Export a second PNG.'})
                self.assertEqual((status, busy['error']['code']), (502, 'KIRO_BUSY'))
                self.assertEqual(await self.request('DELETE', path), (204, None))
                self.assertTrue(session.closed)
                self.assertNotIn('FileA', app.state.kiro_png_idle)
                prompt_allowed.set()
                await prompt

    async def test_png_session_closes_after_connection_failure_even_if_previously_connected(self):
        session = StubSession()
        replacement = StubSession()
        file_a = {'fileKey': 'FileA', 'readFigma': True}
        async with kiro_lifespan(app):
            with patch.object(KiroSession, 'start', side_effect=[session, replacement]) as launch:
                status, opened = await self.request('POST', BASE_PATH, file_a, extra_headers=PNG_HEADERS)
                self.assertEqual(status, 200)
                session.figma_connected = True
                path = f'{BASE_PATH}/{opened["id"]}'
                with patch.object(session, 'call', side_effect=SessionError('KIRO_FIGMA_UNAVAILABLE', 'Unavailable.')):
                    status, failure = await self.request('POST', f'{path}/wait')
                self.assertEqual((status, failure['error']['code']), (502, 'KIRO_FIGMA_UNAVAILABLE'))
                self.assertTrue(session.closed)
                self.assertNotIn('FileA', app.state.kiro_png_idle)
                status, _ = await self.request('POST', BASE_PATH, file_a, extra_headers=PNG_HEADERS)
                self.assertEqual(status, 200)
                self.assertEqual(launch.await_count, 2)

    async def test_invalid_requests_do_not_reach_kiro(self):
        session = StubSession()
        async with kiro_lifespan(app):
            with patch.object(KiroSession, 'start', return_value=session) as launch:
                status, _ = await self.request('POST', BASE_PATH, {}, trusted=False)
                self.assertEqual(status, 403)
                status, _ = await self.request('POST', BASE_PATH, {}, extra_headers=((b'origin', b'https://localhost:4175'),))
                self.assertEqual(status, 403)
                status, _ = await self.request('POST', BASE_PATH, {'readFigma': True})
                self.assertEqual(status, 422)
                launch.assert_not_awaited()
                status, opened = await self.request('POST', BASE_PATH, {})
            self.assertEqual(status, 200)
            path = f'{BASE_PATH}/{opened["id"]}/prompt'
            for payload in (
                {'operation': 'screen.analyze', 'fields': {'fileKey': 'FileA', 'nodeIds': ['invalid']}},
                {'operation': 'story.generate', 'fields': {'input': '{}'}},
                {'operation': 'arbitrary', 'text': 'irrelevant'},
                {'operation': 'screen.plan', 'text': 'invalid image', 'png': 'bm90LXBuZw=='},
            ):
                with self.subTest(payload=payload):
                    status, error = await self.request('POST', path, payload)
                    self.assertEqual(status, 502)
                    self.assertEqual(error['error']['code'], 'KIRO_TASK_INVALID')
            self.assertEqual([method for method, _ in session.calls], ['open'])
        self.assertTrue(session.closed)

    async def test_run_values_match_sent_prompt_and_are_scoped_to_the_prompt_request_owner(self):
        story_run = '/internal/prompts/story.analysis/runs'
        screen_run = '/internal/prompts/screen.capture/runs'
        alice = ((b'x-sfdc-prompt-trace-owner', b'a' * 64),)
        bob = ((b'x-sfdc-prompt-trace-owner', b'b' * 64),)
        session = StubSession()
        async with kiro_lifespan(app):
            with patch.object(KiroSession, 'start', return_value=session):
                _, opened = await self.request('POST', BASE_PATH, {})
            path = f'{BASE_PATH}/{opened["id"]}/prompt'
            original = '{\n  "scope": "번호이동"\n}'
            story = {'operation': 'story.analysis',
                     'fields': {'evidence': original, 'categories': '{}', 'maxFacts': 1}}

            self.assertEqual(await self.request('GET', story_run, extra_headers=alice, query=b'variable=evidence'),
                             (200, {'run': None}))
            _, reply = await self.request('POST', path, story, extra_headers=alice)
            self.assertTrue(session.calls[-1][1]['text'].endswith(original))
            self.assertNotIn('values', reply)
            status, response = await self.request('GET', story_run, extra_headers=alice, query=b'variable=evidence')
            self.assertEqual(status, 200)
            run = response['run']
            self.assertEqual(run['values'], [{'section': 'main', 'value': original}])
            self.assertEqual(run['promptReleaseId'], reply['promptReleaseId'])
            self.assertEqual(run['promptVersion'], reply['promptVersion'])
            self.assertEqual(run['executionMode'], 'acp')
            self.assertEqual(await self.request('GET', story_run, extra_headers=bob, query=b'variable=evidence'),
                             (200, {'run': None}))
            for headers, trusted in (((), True), (alice, False), ((*alice, (b'origin', b'https://localhost:4175')), True)):
                status, _ = await self.request('GET', story_run, trusted=trusted, extra_headers=headers, query=b'variable=evidence')
                self.assertEqual(status, 403)
            self.assertEqual((await self.request('GET', story_run, extra_headers=alice, query=b'variable=unknown'))[0], 404)

            second = {**story, 'fields': {**story['fields'], 'evidence': '{"password":"fixture-secret"}'}}
            self.assertEqual((await self.request('POST', path, second, extra_headers=bob))[0], 200)
            _, isolated = await self.request('GET', story_run, extra_headers=alice, query=b'variable=evidence')
            _, protected = await self.request('GET', story_run, extra_headers=bob, query=b'variable=evidence')
            self.assertEqual(isolated['run'], run)
            self.assertEqual(protected['run']['values'], [{'section': 'main', 'redacted': True}])

            invalid = {**story, 'fields': {**story['fields'], 'maxFacts': 0}}
            self.assertEqual((await self.request('POST', path, invalid, extra_headers=alice))[0], 502)
            with patch.object(session, 'call', side_effect=SessionError('KIRO_UNAVAILABLE', 'Unavailable')):
                self.assertEqual((await self.request('POST', path, second, extra_headers=alice))[0], 502)
            _, unchanged = await self.request('GET', story_run, extra_headers=alice, query=b'variable=evidence')
            self.assertEqual(unchanged['run'], run)

            capture = {'operation': 'screen.capture', 'fields': {'fileKey': 'FileA', 'nodeId': '1:2', 'maxDimension': 640}}
            self.assertEqual((await self.request('POST', path, capture, extra_headers=alice))[0], 200)
            _, reference = await self.request('GET', screen_run, extra_headers=alice, query=b'variable=reference')
            _, screenshot = await self.request('GET', screen_run, extra_headers=alice, query=b'variable=screenshot')
            self.assertEqual(reference['run']['id'], screenshot['run']['id'])
            self.assertEqual(reference['run']['values'], [])
            self.assertEqual(json.loads(screenshot['run']['values'][0]['value'])['nodeId'], '1:2')

    async def test_failed_open_releases_the_session(self):
        session = StubSession()
        async with kiro_lifespan(app):
            with patch.object(KiroSession, 'start', return_value=session), patch.object(
                session, 'call', side_effect=SessionError('KIRO_UNAVAILABLE', 'Unavailable'),
            ):
                status, error = await self.request('POST', BASE_PATH, {})
            self.assertEqual(status, 502)
            self.assertEqual(error['error']['code'], 'KIRO_UNAVAILABLE')
            self.assertTrue(session.closed)
            self.assertEqual(app.state.kiro_sessions, {})


    async def test_disconnected_caller_releases_the_opening_session(self):
        session = StubSession()
        async with kiro_lifespan(app):
            with patch.object(KiroSession, 'start', return_value=session):
                status, error = await self.request('POST', BASE_PATH, {}, disconnect=True)
            self.assertEqual(status, 502)
            self.assertEqual(error['error']['code'], 'KIRO_ABORTED')
            self.assertTrue(session.closed)
            self.assertEqual(app.state.kiro_sessions, {})

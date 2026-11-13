import asyncio
import hashlib
import json
import unittest
from unittest.mock import patch

from sfdc_agent.api import app
from sfdc_agent.kiro_gateway import KiroSession, SessionError, kiro_lifespan


BASE_PATH = '/internal/kiro/sessions'


class StubSession:
    execution_mode = 'acp'

    def __init__(self):
        self.closed = False
        self.events = [{'type': 'figma-auth-required'}]
        self.updated = asyncio.Event()
        self.reader = asyncio.get_running_loop().create_future()
        self.expiry = float('inf')
        self.calls = []

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
    async def request(self, method, path, payload=None, *, trusted=True, extra_headers=(), disconnect=False):
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
            'query_string': b'', 'headers': headers, 'server': ('127.0.0.1', 8765),
            'client': ('127.0.0.1', 41000),
        }, receive, send)
        body = b''.join(message.get('body', b'') for message in messages)
        return messages[0]['status'], json.loads(body) if body else None

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

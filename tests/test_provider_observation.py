import json
import unittest
import test_kiro_headless as fixtures

from sfdc_agent.provider_observation import StreamObservation, reported_usage
from sfdc_agent.kiro_headless import HeadlessKiroSession


class ProviderObservationTests(unittest.TestCase):
    def test_first_content_is_distinct_from_transport_metadata_and_text_is_not_retained(self):
        now = [5.0]
        probe = StreamObservation(clock=lambda: now[0])
        now[0] = 5.1
        probe.feed(b'{"type":"sessionUpdate","data":{"update":{"sessionUpdate":"status"}}}\n')
        now[0] = 5.4
        line = json.dumps({'type':'sessionUpdate','data':{'update':{'sessionUpdate':'agent_message_chunk',
            'content':{'type':'text','text':'private source model text'}}}}).encode()+b'\n'
        probe.feed(line[:20]);probe.feed(line[20:])
        self.assertEqual(probe.receipt(), {'firstStreamByteMs':100,'firstContentMs':400,'firstContentStatus':'observed'})
        self.assertEqual(probe._line, b'')
        self.assertNotIn('private source', json.dumps(probe.receipt()))

    def test_oversized_lines_do_not_grow_memory_and_absent_content_stays_unobserved(self):
        probe = StreamObservation()
        probe.feed(b'private' * 10000)
        self.assertLessEqual(len(probe._line), probe.MAX_LINE)
        probe.feed(b'\n')
        self.assertIsNone(probe.receipt()['firstContentMs'])
        self.assertEqual(probe.receipt()['firstContentStatus'], 'unobserved')

    def test_usage_is_only_explicit_terminal_metadata_not_an_estimate(self):
        terminal=lambda usage: json.dumps({'type':'runFinished','data':{'usage':usage,'finalText':'private'}}).encode()
        self.assertEqual(reported_usage(terminal({'inputTokens':120,'outputTokens':42})),
            {'inputTokens':120,'outputTokens':42,'usageStatus':'reported'})
        self.assertEqual(reported_usage(terminal({'inputTokens':True,'outputTokens':-1})),
            {'inputTokens':None,'outputTokens':None,'usageStatus':'unreported'})
        self.assertEqual(reported_usage(b'{"type":"runFinished","data":{"finalText":"private"}}')['usageStatus'], 'unreported')


class ActualCliObservationTests(unittest.IsolatedAsyncioTestCase):
    fake_cli = fixtures.HeadlessProcessTests.fake_cli

    async def test_actual_stream_reports_auth_content_usage_and_cleanup_without_private_payload(self):
        first = json.dumps({'type':'sessionUpdate','data':{'update':{'sessionUpdate':'agent_message_chunk',
            'content':{'type':'text','text':'{}'}}}})
        finished = json.dumps(fixtures.completed(usage={'inputTokens':100,'outputTokens':8},modelId='reported-fixture-model'))
        self.fake_cli(f'''
            import sys,time
            if sys.argv[1]=='whoami':
                print('Authenticated with API key');sys.exit(0)
            sys.stdin.read()
            print('{{"type":"runStarted","data":{{}}}}',flush=True)
            time.sleep(0.03)
            print({first!r},flush=True)
            print({finished!r},flush=True)
        ''')
        session = HeadlessKiroSession(fixtures.TEST_KEY)
        try:
            result = await session.call('prompt',{'text':'private source prompt'},timeout=5)
            self.assertEqual(result['text'],'{}')
            receipt = session.provider_receipt
            timing = receipt['performance']
            for key in ['workspacePrepareMs','authenticationMs','modelProcessMs','parseMs','cleanupMs','totalMs']:
                self.assertIsInstance(timing[key],int)
                self.assertGreaterEqual(timing[key],0)
            self.assertIsNotNone(timing['firstContentMs'])
            self.assertLessEqual(timing['firstStreamByteMs'],timing['firstContentMs'])
            self.assertEqual(timing['inputTokens'],100)
            self.assertEqual(timing['outputTokens'],8)
            self.assertEqual(receipt['reportedModel'],'reported-fixture-model')
            self.assertNotIn('private source prompt',json.dumps(receipt))
        finally:
            await session.close()

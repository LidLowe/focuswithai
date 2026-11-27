
import asyncio
import base64
import csv
import io
import json
import math
import time
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from activity_context import Frame
from context_stream import ContextStream
from gemini_live import REPORT, GeminiLiveTransport
from live_audio import LiveAudio
from live_session import LiveSession
from panel import Panel
from performance import FrameTrace, Latencies, Recording, PerformanceProfiler, SECOND, TurnTrace, clock
from test_gemini_live import ASSESSMENT
from test_live_session import until
from voicepack import VoicePackNotifier


class ProfilerStatsTests(unittest.TestCase):
    def test_moments_and_percentiles(self):
        samples = Latencies()
        for ns in (1, 2, 3, 4, 10):
            samples.add(ns * 1_000_000)
        summary = samples.values()
        self.assertEqual(summary['count'], 5)
        self.assertEqual(summary['min'], 1)
        self.assertEqual(summary['max'], 10)
        self.assertEqual(summary['mean'], 4)
        self.assertAlmostEqual(summary['stddev'], math.sqrt(10))
        self.assertLessEqual(3, summary['p50'])
        self.assertLessEqual(summary['p50'], 3.15)
        self.assertEqual([summary[k] for k in ('p90', 'p95', 'p99')], [10, 10, 10])
        even = Latencies()
        even.add(1_000_000)
        even.add(9_000_000)
        self.assertAlmostEqual(even.values()['p50'], 5, delta=.05)

    def test_bounded_rows_and_correct_window_and_sprint_aggregation(self):
        record = Recording({}, 'fixture', capacity=4, export_limit=6)
        with record._lock:
            for _ in range(5000):
                record.tally('frames_captured')
        record.stop()
        self.assertGreater(record.dropped, 0)
        self.assertLessEqual(len(record.rows), 6)
        now = record.started + 1000 * SECOND
        for sprint_id, offset in [('old', 970), ('a', 400), ('a', 230), ('b', 10)]:
            ts = now - offset * SECOND
            record._add((ts, 'sprint', 'sprint', None, {'sprint_id': sprint_id}))
            record._add((ts, 'latency', 'resize', 1000, {'sprint_id': sprint_id}))
            record._add((ts, 'count', 'frames_sent', 1, {'sprint_id': sprint_id}))
        record._prune(now)
        data = record._snapshot(now)
        self.assertEqual(data['records_retained'], 6)
        self.assertGreater(data['records_evicted'], 0)
        self.assertEqual([data['windows'][k]['metrics']['resize']['count']
                          for k in ('1m', '5m', '15m', 'sprint', 'recording')], [1, 2, 3, 1, 4])
        self.assertNotIn('jpeg', data['windows']['1m']['metrics'])
        self.assertEqual(data['windows']['recording']['counters']['frames_sent'], 4)


    def test_incomplete_boundaries_and_previous_recording_frames_are_not_samples(self):
        record = Recording({}, 'fixture')
        event = SimpleNamespace(stamp=SimpleNamespace(sprint_id='sprint', generation=3, observation=2), sequence=42)
        previous = FrameTrace('previous-recording', 71, None, clock())
        trace = TurnTrace(record, event, previous)
        try:
            started = clock()
            trace.mark('first_response', started)  # A network reply may race local backpressure.
            trace.send_complete(started + 1)
            trace.assessment('classifications_ok', at=started + 10)
            trace.tokens({'totalTokenCount': 13})
            trace.finish(final=False)
        finally:
            record.stop()
        exported = json.loads(record.export('json'))
        scope = exported['summary']['windows']['recording']
        self.assertIn('classification', scope['metrics'])
        self.assertNotIn('first_response', scope['metrics'])
        self.assertNotIn('capture_to_classification', scope['metrics'])
        self.assertNotIn('totalTokenCount', scope['counters'])
        self.assertEqual(exported['summary']['usage']['fields']['totalTokenCount'], 13)
        self.assertFalse(exported['summary']['usage']['final'])
        self.assertTrue(all(row['frame_id'] is None for row in exported['measurements']))


class ProfilerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def make_profiler(self):
        profiler = PerformanceProfiler({'capture_quality': 90, 'google_assistant_api_key': 'SECRET_DO_NOT_EXPORT',
                                        'activity_classification_prompt': 'PROFILE_PROMPT'}, 'gemini-3.8-live')
        self.addAsyncCleanup(asyncio.to_thread, profiler.control, 'stop')
        return profiler

    async def test_dashboard_controls_downloads_and_disabled_state(self):
        profiler = self.make_profiler()
        runtime = SimpleNamespace(voicepack=VoicePackNotifier({}), focus=None, profiler=profiler)
        self.addAsyncCleanup(runtime.voicepack.close)
        headers = {'X-Panel': '1'}
        async with TestClient(TestServer(Panel(runtime).app)) as client:
            self.assertIsNone(profiler.recording)  # No thread until start.
            self.assertFalse((await (await client.get('/api/performance')).json())['running'])
            self.assertEqual((await client.get('/api/performance/export/json')).status, 400)
            response = await client.post('/api/performance', json={'action': 'start'}, headers=headers)
            payload = await response.json()
            self.assertTrue(payload['running'])
            record = profiler.active
            record.tally('classifications_ok', scope=record.scope())
            response = await client.post('/api/performance', json={'action': 'stop'}, headers=headers)
            payload = await response.json()
            self.assertFalse(payload['running'])
            self.assertTrue(record._thread is not None and not record._thread.is_alive())
            self.assertEqual(payload['windows']['recording']['counters']['classifications_ok'], 1)
            record.tally('frames_sent')  # Stopped record drops new calls.
            export = await client.get('/api/performance/export/json')
            self.assertIn('attachment', export.headers['Content-Disposition'])
            text = await export.text()
            self.assertNotIn('PROFILE_PROMPT', text)
            self.assertNotIn('SECRET_DO_NOT_EXPORT', text)
            self.assertNotIn('frames_sent', json.loads(text)['summary']['windows']['recording']['counters'])
            export = await client.get('/api/performance/export/csv')
            rows = list(csv.DictReader(io.StringIO(await export.text())))
            self.assertEqual(rows[0]['kind'], 'metadata')
            self.assertEqual(rows[-1]['metric'], 'classifications_ok')
            response = await client.post('/api/performance', json={'action': 'reset'}, headers=headers)
            self.assertNotIn('recording_id', await response.json())
            self.assertEqual((await client.get('/api/performance/export/invalid')).status, 400)
            await client.post('/api/performance', json={'action': 'start'}, headers=headers)
            old = profiler.active
            reset = await client.post('/api/performance', json={'action': 'reset'}, headers=headers)
            replacement = await reset.json()
            self.assertTrue(replacement['running'])
            self.assertNotEqual(replacement['recording_id'], old.id)
            self.assertFalse(old._thread.is_alive())

    async def test_replaced_pending_frame_is_counted_with_its_original_identity(self):
        profiler = self.make_profiler()
        profiler.control('start')
        record = profiler.active
        stream = ContextStream(profiler)
        stream.start('sprint-fixture', {})
        facts = {'application': 'fixture.exe', 'window_title': 'fixture', 'browser': None, 'media': None,
                 'user_apps': [], 'activity_seconds': 1}
        for frame_id in range(1, 5):
            trace = FrameTrace(record.id, frame_id, record.sprint_key)
            picture = Frame(b'jpeg' + bytes([frame_id]), b'', 640, 480, time.monotonic(), profile=trace)
            stream.observe(facts, picture, 'window', time.monotonic(), 4)
        self.assertEqual(stream.pending['SCREEN_FRAME'].data.profile.frame_id, 4)
        await asyncio.to_thread(profiler.control, 'stop')
        export = json.loads(profiler.export('json'))
        dropped = [row['frame_id'] for row in export['measurements'] if row['metric'] == 'frames_dropped']
        self.assertEqual(dropped, [1, 2, 3])

    async def test_stale_turn_is_never_attributed_to_new_frame_and_usage_is_not_double_counted(self):
        from unittest.mock import patch

        profiler = self.make_profiler()
        profiler.control('start')
        record = profiler.active
        record.set_sprint('sprint-fixture')
        first_turn = asyncio.Event()
        release_old = asyncio.Event()
        terminal = asyncio.Event()
        exit_server = asyncio.Event()
        bytes_in = 0
        bytes_out = 0

        async def endpoint(request):
            nonlocal bytes_in, bytes_out
            ws = web.WebSocketResponse()
            await ws.prepare(request)

            async def recv():
                nonlocal bytes_out
                text = await ws.receive_str()
                bytes_out += len(text.encode('utf-8'))
                return json.loads(text)

            async def send(body):
                nonlocal bytes_in
                text = json.dumps(body, ensure_ascii=False)
                bytes_in += len(text.encode('utf-8'))
                await ws.send_str(text)

            async def report(id):
                await send({'toolCall': {'functionCalls': [{'id': id, 'name': REPORT, 'args': {
                    'assessment': ASSESSMENT, 'commentary_kind': 'NEXT_STEP', 'commentary_text': 'Часть ответа',
                }}]}})
                await recv()

            await recv()
            await send({'setupComplete': {}})
            await recv()
            first_turn.set()
            await release_old.wait()
            await report('old')
            await send({'serverContent': {'turnComplete': True}})
            await recv()
            await send({'usageMetadata': {'promptTokenCount': 6, 'totalTokenCount': 7}})
            await report('new')
            await send({'usageMetadata': {'promptTokenCount': 6, 'totalTokenCount': 8},
                        'serverContent': {'modelTurn': {'parts': [{'inlineData': {'mimeType': 'audio/pcm',
                        'data': base64.b64encode(bytes(80)).decode()}}]}}})
            await send({'usageMetadata': {'promptTokenCount': 6, 'responseTokenCount': 4, 'totalTokenCount': 10},
                        'serverContent': {'turnComplete': True}})
            terminal.set()
            await exit_server.wait()
            return ws

        class Player:
            busy = False

            @asynccontextmanager
            async def sequence(self):
                yield

        class Sink:
            def __init__(self, rate, volume):
                pass

            async def write(self, pcm):
                await asyncio.sleep(.02)

            async def drain(self):
                pass

            def stop(self):
                pass

        audio = LiveAudio(Player(), sink_factory=Sink)
        self.addAsyncCleanup(audio.close)

        async def prelude():
            await asyncio.sleep(.015)

        def output(msg):
            response = (msg.stamp, msg.turn)
            if msg.kind == 'COMMENTARY':
                audio.start(response, 24000, lambda: True, prelude, profile=msg.profile)
            if msg.kind == 'AUDIO':
                audio.feed(response, msg.data)
            if msg.kind == 'END':
                audio.end(response)

        upstream = web.Application()
        upstream.router.add_get('/ws', endpoint)
        config = {'activity_allow_remote': True, 'activity_send_remote_images': True, 'live_proactive': True,
                  'google_assistant_api_key': 'SECRET_DO_NOT_EXPORT'}
        transport = GeminiLiveTransport(config, profiler=profiler)
        session = LiveSession(config, output, audio.cancel, lambda: transport, profiler=profiler)
        facts = {'application': 'profile-private.exe', 'window_title': 'PERSONAL_TITLE',
                 'browser': None, 'media': None, 'user_apps': [], 'activity_seconds': 5}

        def frame(id):
            tracer = FrameTrace(record.id, id, record.sprint_key, clock() - 10_000_000)
            tracer.finish('jpeg', tracer.started + 1_000_000)
            return Frame(b'SCREEN_BYTES_PRIVATE', b'', 640, 480, time.monotonic(), profile=tracer)

        async with TestServer(upstream) as server:
            with patch('gemini_live.ENDPOINT', str(server.make_url('/ws'))):
                try:
                    session.start('sprint-fixture', {'goal': 'PERSONAL_GOAL'})
                    session.observe(facts, frame(11), 'old', time.monotonic())
                    await asyncio.wait_for(first_turn.wait(), 2)
                    session.observe(facts, frame(22), 'new', time.monotonic())
                    await until(lambda: transport.stale)
                    await asyncio.sleep(.04)
                    release_old.set()
                    await asyncio.wait_for(terminal.wait(), 2)
                    await until(lambda: transport.profile_turn.points.get('playback'))
                    self.assertIsNotNone(session.result)
                finally:
                    release_old.set()
                    exit_server.set()
                    await session.close()
        await asyncio.to_thread(profiler.control, 'stop')
        raw = profiler.export('json')
        data = json.loads(raw)
        for sensitive in ('PERSONAL_TITLE', 'PERSONAL_GOAL', 'SECRET_DO_NOT_EXPORT', 'Часть ответа',
                          'SCREEN_BYTES_PRIVATE', base64.b64encode(b'SCREEN_BYTES_PRIVATE').decode()):
            self.assertNotIn(sensitive, raw)
        windows = data['summary']['windows']
        counters = windows['recording']['counters']
        self.assertEqual(counters['frames_sent'], 2)
        self.assertEqual(counters['classifications_stale'], 1)
        self.assertEqual(counters['classifications_ok'], 1)
        self.assertEqual(counters['token_turns'], 1)
        self.assertEqual(counters['totalTokenCount'], 10)
        self.assertEqual(counters['ws_in_bytes'], bytes_in)
        self.assertEqual(counters['ws_out_bytes'], bytes_out)
        measurements = data['measurements']
        for metric in ('classification', 'capture_to_classification', 'playback', 'audio_local', 'first_audio', 'tool_to_audio'):
            result = [row for row in measurements if row['metric'] == metric]
            self.assertEqual(len(result), 1, metric)
            self.assertEqual(result[0]['frame_id'], 22, metric)
        queued = [row for row in measurements if row['metric'] == 'jpeg_to_queue']
        self.assertEqual([row['frame_id'] for row in queued], [11, 22])
        previous = [row for row in measurements if row['metric'] == 'previous_turn']
        self.assertEqual(len(previous), 1)
        self.assertEqual(previous[0]['frame_id'], 22)
        self.assertGreaterEqual(previous[0]['value'], 30)

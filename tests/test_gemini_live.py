import asyncio
import base64
import io
import json
import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from context_stream import ContextStream
from activity_context import ContextAggregator, Frame, Privacy, sprint_context
from live_session import LivePolicyClose, LiveSession, LiveSetupError
from panel import Panel, probe_gemini_live
from focus_meter import Foreground
from sprints import SprintManager
from test_live_session import until
from gemini_live import DEFAULTS, GeminiLiveTransport, RATE, REPORT


ASSESSMENT = {
    'observed_activity': 'Synthetic work', 'classification': 'ON_TASK',
    'relevance': .9, 'confidence': .9, 'reason': 'The exercise matches.',
    'recommended_action': 'NONE', 'suggested_next_step': None,
}


class GeminiTransportTests(unittest.IsolatedAsyncioTestCase):
    def test_transport_requires_permission_and_key(self):
        config = {}
        with self.assertRaises(LiveSetupError):
            GeminiLiveTransport(config)
        config['activity_allow_remote'] = True
        with self.assertRaises(LiveSetupError):
            GeminiLiveTransport(config)
        config['google_assistant_api_key'] = 'example'
        self.assertFalse(GeminiLiveTransport(config).allow_images)

    async def test_context_refresh_restores_current_sprint_and_latest_observations(self):
        config = {'activity_allow_remote': True, 'google_assistant_api_key': 'fixture',
                  'activity_send_remote_images': True}
        transport = GeminiLiveTransport(config)
        transport._send_json = AsyncMock()
        stream = ContextStream()
        sprint = {'goal': 'Read the circuit notes', 'context': 'Keep the entire sprint instruction'}
        stream.start('fixture', sprint)
        await transport.send(stream.event('SPRINT_STARTED', sprint))
        await transport.send(stream.event('FOREGROUND_CHANGED', ('notes.exe', 'Circuit notes')))
        await transport.send(stream.event('RECENT_ACTIVITY', {'recent_classifications': ['ON_TASK']}))

        async def send_turn(frame=None):
            await transport.send(replace(stream.event('ACTIVITY_DURATION', 3), current_frame=frame))
            transport.ready.set()
            return transport._send_json.call_args.args[0]['clientContent']['turns'][0]['parts']

        first = json.loads((await send_turn())[0]['text'])
        self.assertEqual(first['context_updates']['SPRINT_STARTED'], sprint)
        await transport.send(stream.event('SPRINT_STARTED', sprint))
        second = json.loads((await send_turn())[0]['text'])
        self.assertNotIn('SPRINT_STARTED', second['context_updates'])
        transport.refresh_context = True
        transport.current_image_available = True
        refreshed = await send_turn()
        updates = json.loads(refreshed[0]['text'])
        self.assertEqual(updates['context_updates']['SPRINT_STARTED'], sprint)
        self.assertEqual(updates['context_updates']['FOREGROUND_CHANGED'], ['notes.exe', 'Circuit notes'])
        self.assertEqual(updates['context_updates']['RECENT_ACTIVITY'], {'recent_classifications': ['ON_TASK']})
        self.assertFalse(updates['current_image_available'])
        self.assertEqual(len(refreshed), 1)

        transport.refresh_context = True
        current = Frame(b'current JPEG', b'fingerprint', 1280, 720, time.monotonic())
        restored = await send_turn(current)
        self.assertTrue(json.loads(restored[0]['text'])['current_image_available'])
        self.assertEqual(base64.b64decode(restored[1]['inlineData']['data']), current.data)

    def test_context_budget_checks_order_type_and_preserves_large_instructions(self):
        config = {'activity_allow_remote': True, 'google_assistant_api_key': 'fixture'}
        for trigger, target in [(8192, 8192), (16384, True), (131072, 8192)]:
            with self.subTest(trigger=trigger, target=target), self.assertRaises(LiveSetupError):
                GeminiLiveTransport(dict(config, gemini_live_trigger_tokens=trigger,
                                         gemini_live_target_tokens=target))
        transport = GeminiLiveTransport(config)
        transport.instruction_budget = 8000
        with self.assertRaisesRegex(LiveSetupError, '9152'):
            transport.check_budget(0)

    async def test_probe_uses_only_setup_and_masks_google_error(self):
        messages = []
        async def endpoint(request):
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            messages.append(await socket.receive_json())
            await socket.send_json({'error': {'status': 'PERMISSION_DENIED', 'message': 'private fixture'}})
            await socket.close()
            return socket
        upstream = web.Application()
        upstream.router.add_get('/ws', endpoint)
        dashboard = web.Application()
        dashboard.router.add_post('/probe', Panel().test_gemini_live)
        config = {'activity_allow_remote': True, 'google_assistant_api_key': 'fixture-key'}
        async with TestServer(upstream) as server, TestClient(TestServer(dashboard)) as client:
            with patch('gemini_live.ENDPOINT', str(server.make_url('/ws'))), patch('panel.load_config', return_value=config):
                response = await client.post('/probe', json={})
                self.assertEqual(response.status, 200)
                result = await response.json()
                error = result['stages']['connection']['detail']
                self.assertEqual(result['stages']['classification']['status'], 'skipped')
                self.assertIn('PERMISSION_DENIED', error)
                self.assertNotIn('private fixture', error)
                self.assertEqual([set(msg) for msg in messages], [{'setup'}])

    async def test_websocket_setup_assessment_speech_and_interrupt(self):
        requests = []
        audio = bytes(RATE // 4)

        async def endpoint(request):
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            requests.append(request.headers.get('x-goog-api-key'))
            requests.append(await socket.receive_json())
            await socket.send_json({'setupComplete': {}})
            for _ in range(2):
                requests.append(await socket.receive_json())
                await socket.send_json({'toolCall': {'functionCalls': [{
                    'id': 'fixture', 'name': REPORT,
                    'args': {'assessment': ASSESSMENT, 'commentary_kind': 'NEXT_STEP',
                             'commentary_text': 'Проверь первый пункт.'},
                }]}})
                ack = await socket.receive_json()
                requests.append(ack)
                await socket.send_json({'serverContent': {'modelTurn': {'parts': [
                    {'inlineData': {'mimeType': 'audio/pcm;rate=24000',
                                    'data': base64.b64encode(audio).decode()}},
                ]}, 'turnComplete': True}, 'usageMetadata': {'totalTokenCount': 25}})
            await socket.close()
            return socket

        app = web.Application()
        app.router.add_get('/live', endpoint)
        async with TestServer(app) as server:
            adapter = GeminiLiveTransport({'activity_allow_remote': True, 'google_assistant_api_key': 'fixture-key'})
            self.addAsyncCleanup(adapter.close)
            with patch('gemini_live.ENDPOINT', str(server.make_url('/live'))):
                await adapter.connect('test instructions')
            stream = ContextStream()
            stream.start('sprint-test', {'goal': 'Synthetic work'})
            await adapter.send(stream.event('SPRINT_STARTED', {'goal': 'Synthetic work'}))
            await adapter.send(stream.event('SCREEN_FRAME', object()))
            await adapter.send(stream.event('COMMENTARY_REQUEST', 'NEXT_STEP'))
            turn = stream.event('ACTIVITY_DURATION', 8)
            await adapter.send(turn)
            outputs = [await adapter.receive() for _ in range(6)]
            self.assertEqual([event.kind for event in outputs],
                             ['ASSESSMENT', 'COMMENTARY', 'AUDIO', 'AUDIO', 'USAGE', 'END'])
            self.assertEqual(b''.join(event.data for event in outputs[2:4]), audio)
            self.assertEqual(outputs[1].data['rate'], RATE)
            self.assertTrue(all(event.stamp == turn.stamp and event.turn == turn.sequence for event in outputs))

            adapter.allow_images = True
            await adapter.send(stream.event('SCREEN_FRAME', Frame(b'fixture', b'', 1, 1, 0)))
            await adapter.send(stream.event('ACTIVITY_DURATION', 18))
            await adapter.interrupt()
            with self.assertRaisesRegex(ConnectionError, 'WebSocket Gemini закрыт'):
                await adapter.receive()

        self.assertEqual(len(requests[2]['clientContent']['turns'][0]['parts']), 1)
        blob = requests[4]['clientContent']['turns'][0]['parts'][1]['inlineData']
        self.assertEqual(base64.b64decode(blob['data']), b'fixture')
        self.assertEqual(requests[0], 'fixture-key')
        self.assertEqual(requests[1]['setup']['model'], 'models/' + DEFAULTS['gemini_live_model'])
        turn_text = requests[2]['clientContent']['turns'][0]['parts'][0]['text']
        self.assertNotIn('SCREEN_FRAME', turn_text)
        self.assertEqual(json.loads(turn_text)['requested_commentary'], 'NEXT_STEP')
        self.assertTrue(requests[3]['toolResponse']['functionResponses'][0]['response']['speak'])
        self.assertFalse(requests[5]['toolResponse']['functionResponses'][0]['response']['speak'])

    async def test_extended_thinking_waits_for_idle_and_uses_selected_voice(self):
        setups = []
        async def endpoint(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            setups.append((await ws.receive_json())['setup'])
            await ws.send_json({'setupComplete': {}})
            await ws.receive_json()
            await ws.send_json({'serverContent': {'turnComplete': True, 'interactionStatus': 'IN_PROGRESS'}})
            await ws.send_json({'toolCall': {'functionCalls': [{
                'id': 'fixture', 'name': REPORT,
                'args': {'assessment': ASSESSMENT, 'commentary_kind': 'DEVIATION', 'commentary_text': 'К цели.'},
            }]}})
            self.assertTrue((await ws.receive_json())['toolResponse']['functionResponses'][0]['response']['speak'])
            await ws.send_json({'serverContent': {'interactionStatus': 'IDLE'}})
            await ws.close()
            return ws

        app = web.Application()
        app.router.add_get('/live', endpoint)
        model = 'gemini-3.8-live-extended-thinking'
        adapter = GeminiLiveTransport({'gemini_live_model': model, 'gemini_live_voice': 'Orus',
                                       'activity_allow_remote': True, 'google_assistant_api_key': 'fixture',
                                       'live_proactive': True})
        async with TestServer(app) as server:
            self.addAsyncCleanup(adapter.close)
            with patch('gemini_live.ENDPOINT', str(server.make_url('/live'))):
                await adapter.connect('Test instructions.')
            stream = ContextStream()
            stream.start('test', ASSESSMENT)
            await adapter.send(stream.event('ACTIVITY_DURATION', 10))
            try:
                self.assertEqual([(await asyncio.wait_for(adapter.receive(), 2)).kind for _ in range(3)],
                                 ['ASSESSMENT', 'COMMENTARY', 'END'])
            finally:
                await adapter.close()
        self.assertEqual(setups[0]['model'], 'models/' + model)
        self.assertEqual(setups[0]['tools'][0]['functionDeclarations'][0]['behavior'], 'NON_BLOCKING')
        self.assertEqual(setups[0]['generationConfig']['speechConfig']['voiceConfig']['prebuiltVoiceConfig']['voiceName'],
                         'Orus')


    async def test_goaway_reconnects_without_replaying_blocked_context(self):
        handshakes = []
        requests = []

        async def endpoint(request):
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.receive_json()
            handshakes.append(socket)
            await socket.send_json({'setupComplete': {}})
            async for message in socket:
                wire = json.loads(message.data)
                if 'clientContent' in wire:
                    requests.append(json.loads(wire['clientContent']['turns'][0]['parts'][0]['text']))
                    if len(handshakes) == 1:
                        await socket.send_json({'goAway': {'timeLeft': '3s'}})
                    else:
                        await socket.send_json({'toolCall': {'functionCalls': [{
                            'id': 'fixture', 'name': REPORT, 'args': {
                                'assessment': ASSESSMENT, 'commentary_kind': 'NONE', 'commentary_text': '',
                            },
                        }]}})
                else:
                    await socket.send_json({'serverContent': {'turnComplete': True}})
            return socket

        upstream = web.Application()
        upstream.router.add_get('/ws', endpoint)
        config = {'activity_allow_remote': True, 'google_assistant_api_key': 'fixture'}
        session = LiveSession(config, lambda message: None, lambda: None, lambda: GeminiLiveTransport(config))
        goal = {'goal': 'Synthetic exercise'}
        old = {'application': 'before.exe', 'window_title': 'Previous work', 'activity_seconds': 4,
               'browser': None, 'media': None, 'user_apps': []}
        async with TestServer(upstream) as server:
            with patch('gemini_live.ENDPOINT', str(server.make_url('/ws'))):
                try:
                    session.start('sprint', goal)
                    session.observe(old, None, 'before', time.monotonic())
                    await until(lambda: session.connection_state == 'RECONNECTING')
                    self.assertIn('GoAway', session.error)
                    session.hold('browser_tab_not_allowed')
                    await until(lambda: session.connection_state == 'CONNECTED')
                    self.assertEqual(len(requests), 1)
                    session.observe({**old, 'application': 'after.exe', 'window_title': 'Current work'},
                                    None, 'after', time.monotonic())
                    await until(lambda: session.result is not None)
                    self.assertEqual(len(handshakes), 2)
                    self.assertEqual(requests[-1]['context_updates']['FOREGROUND_CHANGED'][0], 'after.exe')
                    self.assertNotIn('before.exe', json.dumps(requests[-1]))
                finally:
                    await session.close()

    async def test_policy_close_before_setup_is_a_configuration_error(self):
        async def endpoint(request):
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.receive_json()
            await socket.close(code=1008, message=b'Fixture private details')
            return socket

        upstream = web.Application()
        upstream.router.add_get('/ws', endpoint)
        config = {'activity_allow_remote': True, 'google_assistant_api_key': 'fixture'}
        async with TestServer(upstream) as server:
            with patch('gemini_live.ENDPOINT', str(server.make_url('/ws'))):
                result = await probe_gemini_live(config, 'connection')
        detail = result['stages']['connection']['detail']
        self.assertIn('до setupComplete', detail)
        self.assertNotIn('Fixture private details', detail)
        self.assertFalse(result['ok'])

    async def test_established_policy_close_waits_for_a_fresh_allowed_observation(self):
        handshakes = []
        expire = asyncio.Event()

        async def endpoint(request):
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.receive_json()
            handshakes.append(socket)
            await socket.send_json({'setupComplete': {}})
            if len(handshakes) == 1:
                await expire.wait()
                await socket.close(code=1008, message=b'Session timed out due to inactivity. Private fixture.')
                return socket
            async for message in socket:
                wire = json.loads(message.data)
                if 'clientContent' in wire:
                    await socket.send_json({'toolCall': {'functionCalls': [{
                        'id': 'fixture', 'name': REPORT, 'args': {
                            'assessment': ASSESSMENT, 'commentary_kind': 'NONE', 'commentary_text': '',
                        },
                    }]}})
                else:
                    await socket.send_json({'serverContent': {'turnComplete': True}})
            return socket

        upstream = web.Application()
        upstream.router.add_get('/ws', endpoint)
        config = {'activity_allow_remote': True, 'google_assistant_api_key': 'fixture'}
        session = LiveSession(config, lambda message: None, lambda: None, lambda: GeminiLiveTransport(config))
        allowed = {'application': 'editor.exe', 'window_title': 'Synthetic exercise', 'activity_seconds': 4,
                   'browser': None, 'media': None, 'user_apps': []}
        async with TestServer(upstream) as server:
            with patch('gemini_live.ENDPOINT', str(server.make_url('/ws'))):
                try:
                    session.start('sprint', {'goal': 'Synthetic exercise'})
                    await until(lambda: session.connection_state == 'CONNECTED')
                    session.hold('pause', paused=True)
                    expire.set()
                    await until(lambda: session.connection_state == 'RECONNECTING')
                    self.assertIn('бездействия', session.error)
                    self.assertNotIn('Private fixture', session.error)
                    self.assertEqual(session.analysis_state, 'PAUSED')
                    await asyncio.sleep(1.1)
                    self.assertEqual(len(handshakes), 1)
                    session.observe(allowed, None, 'new window', time.monotonic())
                    await until(lambda: session.result is not None)
                    self.assertEqual(session.connection_state, 'CONNECTED')
                    self.assertEqual(len(handshakes), 2)
                finally:
                    expire.set()
                    await session.close()

    def test_policy_close_distinguishes_access_errors_from_unclassified_runtime_closes(self):
        transport = GeminiLiveTransport({'activity_allow_remote': True, 'google_assistant_api_key': 'fixture'})
        transport.setup_complete = True
        access = transport._policy_close('Permission denied. private fixture')
        unknown = transport._policy_close('Private fixture')
        self.assertIsInstance(access, LiveSetupError)
        self.assertIsInstance(unknown, LivePolicyClose)
        self.assertFalse(unknown.expected_expiry)
        self.assertNotIn('Private fixture', str(unknown))

    async def test_probe_exercises_distinct_connection_classification_and_audio_stages(self):
        turns = []

        async def endpoint(request):
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.receive_json()
            await socket.send_json({'setupComplete': {}})
            async for message in socket:
                wire = json.loads(message.data)
                if 'clientContent' in wire:
                    parts = wire['clientContent']['turns'][0]['parts']
                    turn = json.loads(parts[0]['text'])
                    turns.append(turn)
                    await socket.send_json({'toolCall': {'functionCalls': [{
                        'id': 'fixture', 'name': REPORT, 'args': {
                            'assessment': ASSESSMENT, 'commentary_kind': turn['requested_commentary'] or 'NONE',
                            'commentary_text': 'Synthetic reply.',
                        },
                    }]}})
                else:
                    reply = wire['toolResponse']['functionResponses'][0]['response']
                    parts = [{'inlineData': {'mimeType': 'audio/pcm;rate=24000', 'data': 'AAAAAA=='}}] if reply['speak'] else []
                    await socket.send_json({'serverContent': {'modelTurn': {'parts': parts}, 'turnComplete': True}})
            return socket

        upstream = web.Application()
        upstream.router.add_get('/ws', endpoint)
        config = {'activity_allow_remote': True, 'google_assistant_api_key': 'fixture'}
        async with TestServer(upstream) as server:
            with patch('gemini_live.ENDPOINT', str(server.make_url('/ws'))):
                for index, stage in enumerate(('connection', 'classification', 'audio')):
                    result = await probe_gemini_live(config, stage)
                    self.assertTrue(result['ok'], result)
                    self.assertEqual(len(turns), index)
                    self.assertEqual(result['stages'][stage]['status'], 'ok')
        self.assertIsNone(turns[0]['requested_commentary'])
        self.assertEqual(turns[1]['requested_commentary'], 'NEXT_STEP')


class SprintWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def run_sprint(self, config, on_complete=None, *, chrome_image=None):
        # Use the normal Dashboard route, Runtime and focus tick. All foreground data are fixtures.
        with (patch('config_store.load_config', return_value={}), patch('panel.capture_output'),
              patch('logging.basicConfig'), patch('signal.signal')):
            import main

        ide = Foreground(1, 2, 'Synthetic programming exercise', r'C:\Work\example-editor.exe')
        browser = Foreground(2, 3, 'Synthetic programming exercise - Google Chrome', r'C:\Chrome\chrome.exe')
        protected_tab = Foreground(browser.handle, browser.pid, 'Sign in - Google Chrome', browser.executable)
        private = Foreground(3, 4, 'Excluded application fixture', 'secret-example.exe')
        foreground = ide
        config = {**config, 'activity_excluded_apps': [private.executable], 'focus_meter_enabled': True}
        if chrome_image:
            config['activity_send_remote_images'] = True
        with tempfile.TemporaryDirectory() as tmp:
            store = SprintManager(Path(tmp) / 'sprints.json')
            with (patch.object(main, 'config', config), patch.object(main, 'SprintManager', return_value=store),
                  patch.object(main, 'wake_event', asyncio.Event()), patch.object(main, 'shutdown_flag', False)):
                runtime = main.Runtime()
                runtime.start_focus()
                runtime.startup_running = True
                tracker = runtime.focus
                acquisition = tracker.acquisition

                def capture(window, dimensions, quality):
                    assert window in (ide, browser)
                    return Frame(chrome_image, b'fingerprint', 320, 160, time.monotonic()), None

                def capture_browser(window, dimensions, quality, allow_address):
                    assert window == browser and allow_address('https://docs.example.org/exercise') is None
                    return capture(window, dimensions, quality)

                acquisition.windows = SimpleNamespace(
                    foreground=lambda: foreground, user_apps=lambda: [], capture_window=capture,
                    capture_browser_window=capture_browser,
                )
                tracker.monitor.get_window = lambda: foreground
                tracker.monitor.get_idle = lambda: 0
                session = tracker.live.session
                app = Panel(runtime).app
                try:
                    async with TestClient(TestServer(app)) as client:
                        async def command(action, **values):
                            response = await client.post('/api/sprints', json={'action': action, **values},
                                                         headers={'X-Panel': '1'})
                            self.assertEqual(response.status, 200, await response.text())
                            return await response.json()

                        async def control(action):
                            response = await client.post('/api/control', json={'action': action}, headers={'X-Panel': '1'})
                            self.assertEqual(response.status, 200, await response.text())

                        async def check_ide(*, expect_frame=False):
                            nonlocal foreground
                            previous = session.last_success
                            foreground = ide
                            if expect_frame:
                                await asyncio.sleep(max(0, acquisition.capture_not_before - time.monotonic()))
                            tracker.next_sample = 0
                            tracker._tick(time.monotonic())
                            if expect_frame:
                                await acquisition.capture_job
                            acquisition.context.entered -= 3
                            tracker._tick(time.monotonic())
                            await until(lambda: session.result and session.last_success != previous,
                                        timeout=session.response_timeout)
                            self.assertEqual(session.analysis_state, 'READY')
                            self.assertEqual(session.result.classification, 'ON_TASK')

                        # Start while Dashboard is foregrounded on an unapproved browser tab.
                        foreground = browser
                        created = await command('quick_start', goal='Edit the example function in the synthetic programming exercise.')
                        sprint_id = created['item']['id']
                        tracker._tick(time.monotonic())
                        await until(lambda: session.connection_state == 'CONNECTED', timeout=13)
                        connected_at = session.connected
                        self.assertEqual(session.analysis_state, 'WAITING_FOR_CONTEXT')
                        self.assertEqual(session.sent, 0)
                        acquisition.context.entered -= 3
                        tracker._tick(time.monotonic())
                        await until(lambda: session.result is not None, timeout=session.response_timeout)
                        self.assertEqual(session.frames, 0)
                        self.assertEqual(session.connected, connected_at)
                        await check_ide()
                        for excluded in (protected_tab, private):
                            foreground = excluded
                            tracker.next_sample = 0
                            tracker._tick(time.monotonic())
                            sent = session.sent
                            await asyncio.sleep(.05)
                            self.assertEqual(session.sent, sent)
                            self.assertEqual(tracker.state.value, 'UNCERTAIN')
                            self.assertIsNone(session.result)
                            self.assertEqual(session.connected, connected_at)
                            await check_ide()
                        if chrome_image:
                            acquisition.has_capture = True
                            await control('focus_capture_start')
                            foreground = browser
                            tracker.next_sample = 0
                            tracker._tick(time.monotonic())
                            await acquisition.capture_job
                            acquisition.context.entered -= 3
                            tracker.live.allow_images = False
                            previous = session.last_success
                            tracker._tick(time.monotonic())
                            await until(lambda: session.result and session.last_success != previous,
                                        timeout=session.response_timeout)
                            self.assertEqual(session.analysis_state, 'READY')
                            self.assertIsNone(acquisition.context.browser)
                            self.assertEqual(session.frames, 0)
                            self.assertEqual(acquisition.frames_captured, 1)
                            self.assertIn('image_preview', tracker.context_preview(include_image=True))

                            previous = session.last_success
                            acquisition.context.entered -= 3
                            tracker.live.allow_images = True
                            tracker._tick(time.monotonic())
                            await until(lambda: session.result and session.last_success != previous,
                                        timeout=session.response_timeout)
                            self.assertGreater(session.frames, 0)
                            self.assertEqual(session.connected, connected_at)
                            self.assertEqual(session.stream.latest['FOREGROUND_CHANGED'], ('chrome.exe', ''))
                            self.assertIsNone(session.stream.latest['BROWSER_CONTEXT_CHANGED'])

                            foreground = protected_tab
                            tracker.next_sample = 0
                            tracker._tick(time.monotonic())
                            self.assertEqual(session.analysis_state, 'WAITING_FOR_CONTEXT')
                            self.assertEqual(session.connected, connected_at)
                            self.assertIsNone(acquisition.context.frame)
                            self.assertIsNone(session.stream.latest.get('SCREEN_FRAME'))
                            self.assertEqual(acquisition.capture_sprint, sprint_id)
                            await check_ide(expect_frame=True)
                            self.assertEqual(session.frames, 2)
                            self.assertIsNotNone(session.last_frame_sent)
                            await control('focus_capture_stop')
                            self.assertIsNone(acquisition.context.frame)
                            self.assertEqual(session.connection_state, 'CONNECTED')
                        await command('pause', id=sprint_id)
                        self.assertEqual(session.analysis_state, 'PAUSED')
                        self.assertEqual(session.connected, connected_at)
                        await command('resume', id=sprint_id)
                        await check_ide()
                        await command('break', id=sprint_id)
                        self.assertEqual(session.analysis_state, 'PAUSED')
                        self.assertEqual(session.connected, connected_at)
                        await command('resume', id=sprint_id)
                        await check_ide()
                        await command('complete', id=sprint_id)
                        self.assertEqual(session.connection_state, 'DISCONNECTED')
                        if on_complete:
                            await on_complete()
                finally:
                    await session.close()
                    await tracker.live.audio.close()
                    await runtime.voicepack.close()
                    tracker.acquisition.suspend()
                    if tracker.intervention:
                        tracker.intervention.guard.close()
                    if tracker.hud:
                        tracker.hud.close()

    async def test_window_switches_pause_and_break_use_one_websocket(self):
        requests = []
        handshakes = 0
        closed = asyncio.Event()

        async def endpoint(request):
            nonlocal handshakes
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            handshakes += 1
            await socket.receive_json()
            await socket.send_json({'setupComplete': {}})
            async for message in socket:
                wire = json.loads(message.data)
                if 'clientContent' in wire:
                    requests.append(wire)
                    await socket.send_json({'toolCall': {'functionCalls': [{
                        'id': 'fixture', 'name': REPORT, 'args': {
                            'assessment': ASSESSMENT, 'commentary_kind': 'NONE', 'commentary_text': '',
                        },
                    }]}})
                else:
                    await socket.send_json({'serverContent': {'turnComplete': True}})
            closed.set()
            return socket

        async def verify_close():
            await asyncio.wait_for(closed.wait(), 2)

        upstream = web.Application()
        upstream.router.add_get('/ws', endpoint)
        async with TestServer(upstream) as server:
            with patch('gemini_live.ENDPOINT', str(server.make_url('/ws'))):
                await self.run_sprint({'activity_allow_remote': True, 'google_assistant_api_key': 'fixture'},
                                      on_complete=verify_close, chrome_image=b'fixture JPEG')
        self.assertEqual(handshakes, 1)
        self.assertEqual(len(requests), 9)
        images = [part['inlineData'] for wire in requests for turn in wire['clientContent']['turns']
                  for part in turn['parts'] if 'inlineData' in part]
        self.assertEqual(images, [{'mimeType': 'image/jpeg', 'data': base64.b64encode(b'fixture JPEG').decode()}] * 2)
        wire_text = json.dumps(requests)
        self.assertNotIn('Synthetic programming exercise - Google Chrome', wire_text)
        self.assertNotIn('Sign in - Google Chrome', wire_text)
        self.assertNotIn('Excluded application fixture', wire_text)
        self.assertNotIn('secret-example.exe', wire_text)

    async def test_websocket_carries_full_sprint_fields_and_separate_prompts(self):
        fields = {key: (key + '\n' + 'и' * 8192)[:8188] + 'TAIL'
                  for key in ('goal', 'description', 'expected_outcome', 'context')}
        preferences = 'Classify by the sprint evidence.\n' + 'Ж' * 8000 + ' CRITERIA_TAIL'
        style = 'Use plain language.\n' + 'a' * 8000 + ' VOICE_TAIL'
        config = {'activity_allow_remote': True, 'google_assistant_api_key': 'fixture',
                  'activity_classification_prompt': preferences, 'gemini_live_prompt': style,
                  'gemini_live_trigger_tokens': 65536, 'gemini_live_target_tokens': 49152}
        received = {}
        completed = asyncio.Event()

        async def endpoint(request):
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            setup = (await socket.receive_json())['setup']
            received['instructions'] = setup['systemInstruction']['parts'][0]['text']
            await socket.send_json({'setupComplete': {}})
            turn = (await socket.receive_json())['clientContent']['turns'][0]['parts'][0]['text']
            received['context'] = json.loads(turn)['context_updates']['SPRINT_STARTED']
            await socket.send_json({'toolCall': {'functionCalls': [{
                'id': 'fixture', 'name': REPORT, 'args': {
                    'assessment': ASSESSMENT, 'commentary_kind': 'NONE', 'commentary_text': '',
                },
            }]}})
            await socket.receive_json()
            await socket.send_json({'serverContent': {'turnComplete': True}})
            completed.set()
            async for _ in socket:
                pass
            return socket

        upstream = web.Application()
        upstream.router.add_get('/ws', endpoint)
        with tempfile.TemporaryDirectory() as tmp:
            store = SprintManager(Path(tmp) / 'sprints.json')
            sprint = store.command('quick_start', fields)
            now = time.monotonic()
            session = LiveSession(config, lambda _: None, lambda: None, lambda: GeminiLiveTransport(config))
            async with TestServer(upstream) as server:
                with patch('gemini_live.ENDPOINT', str(server.make_url('/ws'))):
                    try:
                        session.start(sprint['id'], sprint_context(store.llm_context))
                        session.observe({'application': 'editor.exe', 'window_title': 'Fixture', 'browser': None,
                                         'media': None, 'user_apps': [], 'activity_seconds': 4}, None, 'editor', now)
                        await asyncio.wait_for(completed.wait(), 3)
                        await until(lambda: session.result is not None)
                    finally:
                        await session.close()
        self.assertEqual(received['context'], json.loads(store.llm_context))
        lines = received['instructions'].splitlines()
        for prefix, value in (('Classification Prompt ', preferences), ('Voice Style ', style)):
            index = next(i for i, line in enumerate(lines) if line.startswith(prefix))
            self.assertEqual(json.loads(lines[index + 1]), value)
        self.assertIn('output protocol and safety rules are mandatory', received['instructions'])

    def real_config(self):
        from config_store import GEMINI_KEY, read_secret
        path = Path('config.json')
        config = json.loads(path.read_text(encoding='utf-8-sig'))
        if sys.platform == 'win32':
            config[GEMINI_KEY] = read_secret(path, GEMINI_KEY) or config.get(GEMINI_KEY)
        self.assertTrue(config.get(GEMINI_KEY), 'A saved Gemini API key is required.')
        self.assertTrue(config.get('activity_allow_remote'), 'Text-context permission is required.')
        # aiohttp may return before Windows has finished closing the TLS transport.
        self.addAsyncCleanup(asyncio.sleep, .25)
        return config

    @unittest.skipUnless(os.environ.get('FOCUSWITHAI_TEST_GOOGLE') == '1', 'opt-in Google API test')
    async def test_real_gemini_sprint_and_native_audio(self):
        config = self.real_config()
        from PIL import Image, ImageDraw
        image = Image.new('RGB', (320, 160), 'white')
        ImageDraw.Draw(image).multiline_text((10, 10), 'Synthetic programming exercise\n\ndef example_function():\n    return 42',
                                            fill='black')
        output = io.BytesIO()
        image.save(output, format='JPEG')
        await self.run_sprint(config, chrome_image=output.getvalue())
        audio = await probe_gemini_live(config, 'audio')
        self.assertTrue(audio['ok'], audio['stages'])

    @unittest.skipUnless(os.environ.get('FOCUSWITHAI_TEST_GOOGLE') == '1', 'opt-in Google visual-understanding test')
    async def test_real_gemini_reads_random_number_from_jpeg(self):
        import secrets
        from PIL import Image, ImageDraw, ImageFont

        config = self.real_config()
        marker = f'{secrets.randbelow(100_000_000):08d}'
        image = Image.new('RGB', (800, 300), 'white')
        drawing = ImageDraw.Draw(image)
        drawing.text((40, 35), 'CALIBRATION NUMBER', font=ImageFont.load_default(size=36), fill='black')
        drawing.text((40, 100), marker, font=ImageFont.load_default(size=110), fill='black')
        output = io.BytesIO()
        image.save(output, format='JPEG', quality=90)
        frame = Frame(output.getvalue(), b'', image.width, image.height, time.monotonic())

        config = {**config, 'activity_send_remote_images': True, 'live_proactive': False,
                  'activity_classification_prompt': 'For this synthetic calibration, put the eight digits you see in the attached '
                                       'image into observed_activity exactly, using only digits. If no digits are visible, use UNKNOWN.'}
        session = LiveSession(config, lambda _: None, lambda: None, lambda: GeminiLiveTransport(config))
        goal = {'goal': 'Read the calibration number from the current image.'}
        try:
            session.start('visual-fixture', goal)
            session.observe({'application': 'synthetic-editor.exe', 'window_title': 'Visual calibration',
                             'activity_seconds': 3, 'browser': None, 'media': None, 'user_apps': []},
                            frame, 'visual-fixture', time.monotonic())
            await until(lambda: session.result is not None or session.error, timeout=60)
            self.assertIsNotNone(session.result, session.error)
            self.assertEqual(session.frames, 1)
            self.assertEqual(session.result.observed_activity, marker)
        finally:
            await session.close()


    async def classify_synthetic_window(self, config, sprint, screen, seconds):
        from PIL import Image, ImageDraw, ImageFont

        title, lines = screen
        image = Image.new('RGB', (1120, 590), '#f7f8fc')
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 1120, 88), fill='#202c48')
        draw.text((34, 24), title, fill='white', font=ImageFont.load_default(size=34))
        font = ImageFont.load_default(size=25)
        for i, line in enumerate(lines):
            draw.text((48, 125 + 57 * i), line, fill='#182238', font=font)
        output = io.BytesIO()
        image.save(output, format='JPEG', quality=90)
        config = {**config, 'activity_send_remote_images': True, 'live_proactive': False}
        session = LiveSession(config, lambda _: None, lambda: None, lambda: GeminiLiveTransport(config))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = SprintManager(Path(tmp) / 'sprints.json')
                created = store.command('quick_start', sprint)
                now = time.monotonic()
                source = ContextAggregator(Privacy({}))
                source.sample(now - seconds, Foreground(title='Fixture tab', executable='chrome.exe'))
                facts, _ = source.snapshot(now, store.llm_context, 0)
                self.assertEqual(facts['activity_seconds'], seconds)
                session.start(created['id'], facts['sprint'])
                session.observe(facts, Frame(output.getvalue(), b'', *image.size, now), 'fixture-window', now)
                await until(lambda: session.result is not None or session.error, timeout=60)
                self.assertIsNotNone(session.result, session.error)
                self.assertEqual(session.frames, 1)
                return session.result
        finally:
            await session.close()

    @unittest.skipUnless(os.environ.get('FOCUSWITHAI_TEST_GOOGLE') == '1', 'opt-in Google classification test')
    async def test_real_gemini_distinguishes_study_tool_setup_and_dashboard_use(self):
        config = {**self.real_config(), 'activity_classification_prompt': '',
                  'gemini_live_prompt': 'Use a calm, technical tone in spoken commentary.'}
        study = {'goal': 'Study RC, RL and RLC circuits.',
                 'description': 'Read the theory, then simulate the circuit responses in LTspice.',
                 'expected_outcome': 'Explain time constants, damping and resonance.'}
        development = {'goal': 'Develop FocusWithAI and fix its Dashboard settings form.',
                       'context': 'Debug the JavaScript form and confirm the saved resolution is restored.'}
        cases = [
            ('ON_TASK', study, 90, ('Electrical engineering lesson: RC, RL and RLC circuits', [
                'RC step response and the charge time constant tau = R C',
                'RL current rise: the time constant is L / R',
                'RLC natural response: damping and resonant frequency',
                'Example 4: compare overdamped and underdamped step responses.',
            ])),
            ('INDIRECTLY_RELATED', study, 75, ('LTspice — simulation tool setup', [
                'Preferences | Waveforms | Solver configuration',
                'Prepare LTspice for the upcoming RC, RL and RLC circuit lab.',
                'Setting transient analysis defaults and adding component libraries.',
                'No schematic run or simulation output yet.',
            ])),
            ('UNCERTAIN', study, 8, ('FocusWithAI — current sprint Dashboard', [
                'Goal: Study RC, RL and RLC circuits.',
                'Status: active        Elapsed: 12:15',
                'Pause        Take a break        Complete',
            ])),
            ('OFF_TASK', study, 240, ('FocusWithAI — Dashboard settings', [
                'Active sprint: Study RC, RL and RLC circuits.',
                'Editing settings: HUD opacity 72%, window corner bottom right.',
                'Startup options and audio pack preferences are open.',
                'Four changes pending.      Save changes        Preview chime',
            ])),
            ('ON_TASK', development, 115, ('DevTools — Debugging FocusWithAI Dashboard', [
                'panel_web/app.js: Populate settings form from saved config.',
                'Breakpoint: capture preset reset after reload.',
                'Regression test: saving Full HD must preserve the chosen resolution.',
                'Inspecting event handler and configuration API response.',
            ])),
        ]
        for index, (expected, sprint, seconds, screen) in enumerate(cases):
            with self.subTest(screen=screen[0]):
                result = await self.classify_synthetic_window(config, sprint, screen, seconds)
                print(f'{screen[0]} / {seconds}s: {result.classification.value} — {result.reason}', flush=True)
                self.assertEqual(result.classification, expected)
                if index == 3:
                    self.assertLessEqual(result.relevance, .40)

    @unittest.skipUnless(os.environ.get('FOCUSWITHAI_TEST_GOOGLE') == '1', 'opt-in Google long-context test')
    async def test_real_gemini_reads_tails_of_long_prompt_and_all_sprint_fields(self):
        import secrets

        def long_text(start, tail):
            padding = 'Reserved area for ordinary notes about the same lab exercise. '
            return start + (padding * 160)[:8192 - len(start) - len(tail)] + tail

        field_names = ('goal', 'description', 'expected_outcome', 'context')
        codes = {key: f'{prefix}{secrets.randbelow(10**7):07d}'
                 for key, prefix in zip((*field_names, 'prompt'), ('G', 'D', 'O', 'C', 'P'))}
        sprint = {key: long_text(
            'Review the visible calibration registry entry for the current lab ticket.\n',
            f'\n{key} verification identifier: {codes[key]}.',
        ) for key in field_names}
        preferences = long_text(
            'For a calibration registry observation, the task is direct progress. Explain your decision in reason '
            'with one short Russian sentence citing the four verification identifiers at the ends of the goal, '
            'description, expected_outcome, and context, and your own policy identifier at the end of this prompt. '
            'Use all five exact identifiers as evidence for this calibration.\n',
            f'\nPolicy identifier: {codes["prompt"]}.',
        )
        self.assertTrue(all(len(value) == 8192 for value in (*sprint.values(), preferences)))
        result = await self.classify_synthetic_window(
            {**self.real_config(), 'activity_classification_prompt': preferences,
             'gemini_live_trigger_tokens': 65536, 'gemini_live_target_tokens': 49152,
             'gemini_live_prompt': 'Spoken commentary should use a calm tone and omit numbers.'},
            sprint, ('Local QA — calibration registry', [
                'Ticket: calibrate the current activity review',
                'Viewing the registry entry required for the current lab.',
                'Review the corresponding verification identifiers for this ticket.',
            ]), 90,
        )
        self.assertEqual(result.classification, 'ON_TASK')
        self.assertTrue(all(code in result.reason for code in codes.values()), result.reason)

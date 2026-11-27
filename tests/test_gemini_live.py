import asyncio
import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from context_stream import ContextStream
from activity_context import Frame
from live_session import LiveSetupError
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
        from live_session import LiveSession
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

        ide = Foreground(1, 2, 'Synthetic programming exercise', 'example-editor.exe')
        browser = Foreground(2, 3, 'Synthetic programming exercise - Google Chrome', r'C:\Chrome\chrome.exe')
        protected_tab = Foreground(browser.handle, browser.pid, 'Sign in - Google Chrome', browser.executable)
        private = Foreground(3, 4, 'Excluded application fixture', 'secret-example.exe')
        foreground = ide
        config = {**config, 'activity_excluded_apps': [private.executable], 'focus_meter_enabled': True}
        if chrome_image:
            config.update(capture_enabled=True, capture_apps=[browser.executable], activity_send_remote_images=True)
        with tempfile.TemporaryDirectory() as tmp:
            store = SprintManager(Path(tmp) / 'sprints.json')
            with (patch.object(main, 'config', config), patch.object(main, 'SprintManager', return_value=store),
                  patch.object(main, 'wake_event', asyncio.Event()), patch.object(main, 'shutdown_flag', False)):
                runtime = main.Runtime()
                runtime.start_focus()
                runtime.startup_running = True
                tracker = runtime.focus
                acquisition = tracker.acquisition

                def capture(window, dimensions, quality, allow_address):
                    assert window == browser and allow_address('https://docs.example.org/exercise')
                    return Frame(chrome_image, b'fingerprint', 320, 160, time.monotonic())

                acquisition.windows = SimpleNamespace(
                    foreground=lambda: foreground, user_apps=lambda: [], capture_window=capture,
                    capture_choices=lambda privacy: [
                        {'id': '2-3', 'handle': browser.handle, 'pid': browser.pid},
                    ],
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

                        async def check_ide():
                            nonlocal foreground
                            previous = session.last_success
                            foreground = ide
                            tracker.next_sample = 0
                            tracker._tick(time.monotonic())
                            tracker.acquisition.context.entered -= 3
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
                        await check_ide()
                        for excluded in (browser, private):
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
                            acquisition.has_capture = acquisition.enabled = True
                            acquisition.arm('2-3')
                            acquisition.next_capture = 0
                            foreground = browser
                            tracker.next_sample = 0
                            tracker._tick(time.monotonic())
                            await acquisition.capture_job
                            acquisition.context.entered -= 3
                            tracker.live.allow_images = False
                            tracker._tick(time.monotonic())
                            self.assertEqual(session.analysis_state, 'WAITING_FOR_CONTEXT')
                            self.assertIsNone(acquisition.context.browser)

                            previous = session.last_success
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
                            self.assertTrue(acquisition.armed)
                            await check_ide()
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
        self.assertEqual(len(requests), 7)
        images = [part['inlineData'] for wire in requests for turn in wire['clientContent']['turns']
                  for part in turn['parts'] if 'inlineData' in part]
        self.assertEqual(images, [{'mimeType': 'image/jpeg', 'data': base64.b64encode(b'fixture JPEG').decode()}])
        wire_text = json.dumps(requests)
        self.assertNotIn('Synthetic programming exercise - Google Chrome', wire_text)
        self.assertNotIn('Sign in - Google Chrome', wire_text)
        self.assertNotIn('Excluded application fixture', wire_text)
        self.assertNotIn('secret-example.exe', wire_text)

    @unittest.skipUnless(os.environ.get('FOCUSWITHAI_TEST_GOOGLE') == '1', 'opt-in Google API test')
    async def test_real_gemini_sprint_and_native_audio(self):
        from config_store import load_config
        config = load_config()
        self.assertTrue(config.get('google_assistant_api_key'), 'A saved Gemini API key is required.')
        self.assertTrue(config.get('activity_allow_remote'), 'Text-context permission is required.')
        import io
        from PIL import Image, ImageDraw
        image = Image.new('RGB', (320, 160), 'white')
        ImageDraw.Draw(image).multiline_text((10, 10), 'Synthetic programming exercise\n\ndef example_function():\n    return 42',
                                            fill='black')
        output = io.BytesIO()
        image.save(output, format='JPEG')
        await self.run_sprint(config, chrome_image=output.getvalue())
        audio = await probe_gemini_live(config, 'audio')
        self.assertTrue(audio['ok'], audio['stages'])

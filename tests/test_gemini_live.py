import asyncio
import base64
import json
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from context_stream import ContextStream
from activity_context import Frame
from live_session import LiveSetupError
from panel import Panel
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
                self.assertEqual(response.status, 400)
                error = (await response.json())['error']
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
            with self.assertRaisesRegex(ConnectionError, 'gemini_socket_closed'):
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

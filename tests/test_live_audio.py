import asyncio
import os
import unittest
from unittest.mock import AsyncMock, Mock

os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
from live_audio import LiveAudio
from voicepack import ClipPlayer


class AudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_pcm_plays_on_shared_channel_and_cancels(self):
        coordinator = ClipPlayer()
        audio = LiveAudio(coordinator, .25)
        self.assertTrue(audio.start('a', 16000, lambda: True, AsyncMock()))
        self.assertFalse(audio.start('b', 16000, lambda: True, AsyncMock()))
        self.assertFalse(audio.feed('b', b'xx'))
        self.assertTrue(audio.feed('a', bytes(16000)))
        await asyncio.sleep(.02)
        self.assertTrue(coordinator.busy)
        await audio.close()
        self.assertFalse(coordinator.busy)
        self.assertEqual(audio.buffered, 0)

    async def test_overflow_and_invalid_chunks_abort_whole_response(self):
        audio = LiveAudio(ClipPlayer())
        for payloads in ([b'!'], [bytes(8000)] * 5, [bytes(2)] * 97):
            self.assertTrue(audio.start('a', 8000, lambda: True, AsyncMock()))
            for data in payloads:
                accepted = audio.feed('a', data)
            self.assertFalse(accepted)
            await audio.close()
            self.assertFalse(audio.feed('a', b'xx'))
            self.assertEqual(audio.buffered, 0)
        self.assertEqual(audio.dropped, 3)

    async def test_native_burst_waits_for_playback_capacity(self):
        sink = Mock(write=AsyncMock(), drain=AsyncMock())
        audio = LiveAudio(ClipPlayer(), sink_factory=lambda rate, level: sink)
        audio.start('burst', 8000, lambda: True, AsyncMock())
        for _ in range(8):
            self.assertTrue(await asyncio.wait_for(audio.feed_stream('burst', bytes(8000)), 1))
            self.assertLessEqual(audio.buffered, 32000)
        audio.end('burst')
        await asyncio.wait_for(audio.task, 1)
        self.assertEqual(sink.write.await_count, 8)
        self.assertEqual(audio.dropped, 0)

    async def test_local_tts_only_uses_admitted_text_and_shared_player(self):
        from tts import Speaker
        text = 'Следующий шаг для тестового спринта'
        local = Mock(name='local')
        local.name = 'sapi'
        local.synthesize = AsyncMock(return_value=(b'test fixture', 'wav'))
        network = Mock()
        network.name = 'edge'
        network.synthesize = AsyncMock()
        speaker = Speaker({'tts_engines': []})
        speaker.engines = [network, local]
        player = ClipPlayer()
        player.play = AsyncMock()
        audio = LiveAudio(player)
        audio.start_speech('request', lambda: speaker.synthesize(text, local_only=True),
                           lambda: True, AsyncMock(), Mock())
        self.assertFalse(audio.feed('request', bytes(200)))
        await audio.task
        local.synthesize.assert_awaited_once_with(text)
        network.synthesize.assert_not_awaited()
        player.play.assert_awaited_once()
        self.assertFalse(player.busy)

    async def test_cancel_during_alarm_delay_and_native_drain(self):
        alarm = asyncio.Event()
        sink = Mock(write=AsyncMock(), drain=AsyncMock())
        audio = LiveAudio(ClipPlayer(), sink_factory=lambda rate, level: sink)
        audio.start('old', 24000, lambda: True, alarm.wait)
        audio.feed('old', bytes(400))
        await asyncio.sleep(.01)
        await audio.close()
        sink.write.assert_not_awaited()
        audio.start('new', 24000, lambda: True, AsyncMock())
        audio.feed('new', bytes(400))
        audio.end('new')
        await asyncio.wait_for(audio.task, 1)
        sink.write.assert_awaited_once_with(bytes(400))
        sink.drain.assert_awaited_once()


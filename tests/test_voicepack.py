import asyncio
import io
import json
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
os.environ.setdefault('PYGAME_HIDE_SUPPORT_PROMPT', '1')

from voicepack import ClipPlayer, Event, VoicePackCatalog, VoicePackEngine, VoicePackNotifier


def wav_bytes():
    content = io.BytesIO()
    with wave.open(content, 'wb') as sample:
        sample.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        sample.writeframes(b'\0\0' * 640)
    return content.getvalue()


class VoicePackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        pack = self.root / 'demo'
        (pack / 'distraction').mkdir(parents=True)
        (pack / 'manifest.json').write_text('{"version":1,"name":"Demo"}', encoding='utf-8')
        self.clips = pack / 'distraction'
        self.catalog = VoicePackCatalog(self.root)

    async def test_randomization_ignores_corrupt_file_and_avoids_immediate_repeat(self):
        (self.clips / 'bad.wav').write_bytes(b'not audio')
        (self.clips / 'first.wav').write_bytes(wav_bytes())
        (self.clips / 'second.WAV').write_bytes(wav_bytes())
        (self.clips / 'notes.txt').write_text('notes', encoding='utf-8')
        engine = VoicePackEngine(self.catalog, ClipPlayer())
        with patch('voicepack.random.shuffle', side_effect=lambda clips: None):
            self.assertTrue(await engine.play_event('demo', Event.DISTRACTION, 0.65))
            self.assertEqual(engine.last_clip.name, 'first.wav')
            self.assertTrue(await engine.play_event('demo', Event.DISTRACTION, 0.65))
            self.assertEqual(engine.last_clip.name, 'second.WAV')
        self.assertFalse(await engine.play_event('demo', Event.IDLE, 0.65))

    async def test_preview_uses_only_catalogued_files(self):
        (self.clips / 'good.wav').write_bytes(wav_bytes())
        engine = VoicePackEngine(self.catalog, ClipPlayer())
        await engine.preview('demo', Event.DISTRACTION, 'good.wav', 0.5)
        with self.assertRaises(ValueError):
            await engine.preview('demo', Event.DISTRACTION, '../good.wav', 0.5)
        with self.assertRaises(ValueError):
            await engine.preview('../demo', Event.DISTRACTION, 'good.wav', 0.5)
        self.assertEqual(self.catalog.list_packs()[0]['clips']['DISTRACTION'], ['good.wav'])

    async def test_audio_is_serialized_and_cancelled_playback_releases_channel(self):
        player = ClipPlayer()
        starts = []
        first = asyncio.create_task(player.play(io.BytesIO(wav_bytes()), 0.5, lambda: starts.append(1)))
        second = asyncio.create_task(player.play(io.BytesIO(wav_bytes()), 0.5, lambda: starts.append(2)))
        while not starts:
            await asyncio.sleep(0.005)
        self.assertEqual(starts, [1])
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(second, 1)
        self.assertEqual(starts, [1, 2])

    async def test_pack_playback_has_no_overlap_and_honors_cooldown(self):
        (self.clips / 'good.wav').write_bytes(wav_bytes())
        notifier = VoicePackNotifier({'voicepack_id': 'demo', 'notification_mode': 'voicepack'})
        notifier.engine = VoicePackEngine(self.catalog, notifier.player)
        self.assertTrue(notifier.notify(Event.DISTRACTION))
        self.assertFalse(notifier.notify(Event.TASK_COMPLETED))
        await notifier._task
        self.assertEqual(notifier.engine.last_clip.name, 'good.wav')
        self.assertFalse(notifier.notify(Event.DISTRACTION))

    async def test_missing_clip_is_silent(self):
        notifier = VoicePackNotifier({'voicepack_id': 'demo', 'notification_mode': 'voicepack'})
        notifier.engine = VoicePackEngine(self.catalog, notifier.player)
        self.assertTrue(notifier.notify(Event.IDLE))
        await notifier._task
        self.assertIsNone(notifier.engine.last_clip)
        self.assertEqual(notifier.last_notice, float('-inf'))

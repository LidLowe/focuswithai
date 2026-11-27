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

from voicepack import (Activity, ClipPlayer, Event, VoicePackCatalog, VoicePackEngine,
                       VoicePackNotifier, parse_decision)


def wav_bytes():
    content = io.BytesIO()
    with wave.open(content, 'wb') as sample:
        sample.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        sample.writeframes(b'\0\0' * 640)
    return content.getvalue()


class ActivityTests(unittest.TestCase):
    def test_distraction_recovery_escalation_and_idle(self):
        state = Activity({'reminder_interval': 5, 'punishment_interval': 10,
                          'voicepack_repeat_interval': 20, 'voicepack_idle_seconds': 25})
        self.assertIsNone(state.observe(0, False, 0))
        self.assertEqual(state.observe(5, False, 0), (Event.DISTRACTION, 5))
        self.assertEqual(state.observe(10, False, 0), (Event.PERSISTENT_DISTRACTION, 10))
        self.assertIsNone(state.observe(29, False, 0))
        self.assertEqual(state.observe(30, False, 0), (Event.PERSISTENT_DISTRACTION, 30))
        self.assertEqual(state.observe(31, True, 0), (Event.RETURN_TO_WORK, 0))
        self.assertFalse(state.is_current(Event.PERSISTENT_DISTRACTION))
        self.assertIsNone(state.observe(40, False, 0))
        self.assertEqual(state.observe(45, False, 0), (Event.REPEATED_DISTRACTION, 5))
        self.assertEqual(state.observe(65, False, 25), (Event.IDLE, 25))
        self.assertIsNone(state.observe(70, False, 30))
        self.assertEqual(state.observe(71, True, 0), (Event.RETURN_TO_WORK, 0))
        state.reset_block()
        self.assertIsNone(state.observe(100, False, None))
        self.assertEqual(state.observe(105, False, None), (Event.DISTRACTION, 5))

    def test_structured_schema_rejects_unsupported_events_and_bad_types(self):
        self.assertEqual(parse_decision('```json\n{"event":"DISTRACTION","reason":"Окно вне списка."}\n```',
                                        ['DISTRACTION']), ('DISTRACTION', 'Окно вне списка.'))
        for invalid in ('Oops', '{"event":"TASK_COMPLETED","reason":"x"}',
                        '{"event":"DISTRACTION","reason":[]}', '{"event":"DISTRACTION","reason":" "}',
                        '{"event":"DISTRACTION","reason":"x","extra":0}'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_decision(invalid, ['DISTRACTION'])


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

    async def test_structured_decision_fallback_no_overlap_and_cooldown(self):
        client = AsyncMock()
        client.chat.return_value = '{"event":"REPEATED_DISTRACTION","reason":"Снова открылось другое окно."}'
        speaker = AsyncMock()
        speaker.synthesize.return_value = wav_bytes(), 'wav'
        notifier = VoicePackNotifier({'voicepack_id': 'demo'}, client, speaker)
        notifier.engine = VoicePackEngine(self.catalog, ClipPlayer())
        evidence = {'observation': 'Окно вне списка.', 'distractions_in_block': 2}
        self.assertTrue(notifier.notify(Event.PERSISTENT_DISTRACTION, evidence))
        self.assertFalse(notifier.notify(Event.TASK_COMPLETED, evidence))
        await notifier._task
        speaker.synthesize.assert_awaited_once()
        self.assertEqual(notifier.decision['event'], 'REPEATED_DISTRACTION')
        self.assertFalse(notifier.notify(Event.TASK_COMPLETED, evidence))
        facts = json.loads(client.chat.await_args.args[0][0]['content'])
        self.assertNotIn('demo', str(facts))
        client.chat.return_value = 'not JSON'
        self.assertEqual((await notifier.classify(Event.SESSION_END, evidence, True))['source'], 'tracker')

    async def test_voicepack_uses_audio_without_synthesis_and_honors_none(self):
        (self.clips / 'good.wav').write_bytes(wav_bytes())
        client = AsyncMock()
        speaker = AsyncMock()
        notifier = VoicePackNotifier({'voicepack_id': 'demo'}, client, speaker)
        notifier.engine = VoicePackEngine(self.catalog, ClipPlayer())
        evidence = {'observation': 'Окно вне списка.'}
        client.chat.return_value = '{"event":"NONE","reason":"Окно нужно для задачи."}'
        notifier.notify(Event.DISTRACTION, evidence)
        await notifier._task
        self.assertIsNone(notifier.engine.last_clip)
        notifier.notify(Event.DISTRACTION, evidence, use_ai=False)
        await notifier._task
        self.assertEqual(notifier.engine.last_clip.name, 'good.wav')
        speaker.synthesize.assert_not_awaited()

    async def test_missing_clip_is_silent_when_tts_fallback_is_disabled(self):
        speaker = AsyncMock()
        notifier = VoicePackNotifier({'voicepack_id': 'demo', 'voicepack_tts_fallback': False}, AsyncMock(), speaker)
        notifier.engine = VoicePackEngine(self.catalog, ClipPlayer())
        self.assertTrue(notifier.notify(Event.IDLE, {'observation': 'Нет ввода.'}, use_ai=False))
        await notifier._task
        speaker.synthesize.assert_not_awaited()
        self.assertEqual(notifier.decision['source'], 'tracker')

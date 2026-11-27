import asyncio
import ctypes
import io
import os
import sys
import tempfile
import time
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
os.environ.setdefault('PYGAME_HIDE_SUPPORT_PROMPT', '1')

from audio_import import MpqSource, STORM_DLL, import_starcraft, save_clip
from voicepack import Activity, ClipPlayer, Event, PlaybackStopped, VoicePackCatalog, VoicePackNotifier


def clip_bytes(value=1000, frames=800):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as sample:
        sample.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        sample.writeframes(value.to_bytes(2, 'little', signed=True) * frames)
    return buffer.getvalue()


class AudioRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_five_modes_keep_order_and_the_default_delay(self):
        expected = {'alarm': ['alarm'], 'voicepack': ['voice'], 'tts': ['tts'],
                    'alarm_voicepack': ['alarm', 'voice'], 'alarm_tts': ['alarm', 'tts']}
        for mode, order in expected.items():
            with self.subTest(mode=mode):
                calls = []

                def marker(kind):
                    async def record(*_args):
                        calls.append((kind, time.monotonic()))
                        return True
                    return record

                speech = AsyncMock()
                speech.synthesize.return_value = clip_bytes(), 'wav'
                router = VoicePackNotifier({'notification_mode': mode, 'alarm_volume': 0.2,
                    'voicepack_volume': 0.6, 'alarm_pack_id': 'demo', 'voicepack_id': 'terran',
                    'audio_event_mappings': {'alarm': {'demo': {'TASK_COMPLETED': 'attention/ping.wav'}}}},
                    AsyncMock(), speech)
                router.alarms.play_event = AsyncMock(side_effect=marker('alarm'))
                router.engine.play_event = AsyncMock(side_effect=marker('voice'))
                router.player.play = AsyncMock(side_effect=marker('tts'))
                self.assertTrue(router.notify(Event.TASK_COMPLETED, {'observation': 'Готово'}, use_ai=False))
                await router._task
                self.assertEqual([kind for kind, _ in calls], order)
                if order[0] == 'alarm':
                    self.assertEqual(router.alarms.play_event.await_args.args[2], 0.2)
                    self.assertEqual(router.alarms.play_event.await_args.args[4], 'attention/ping.wav')
                if order[-1] == 'voice':
                    self.assertEqual(router.engine.play_event.await_args.args[2], 0.6)
                if mode in ('alarm_voicepack', 'alarm_tts'):
                    self.assertGreaterEqual(calls[1][1] - calls[0][1], 0.28)
                if 'tts' not in order:
                    speech.synthesize.assert_not_awaited()

    async def test_stop_during_delay_cancels_the_voice_stage(self):
        router = VoicePackNotifier({'notification_mode':'alarm_voicepack', 'alarm_voice_delay_ms': 5000},
                                   AsyncMock(), AsyncMock())
        started = asyncio.Event()

        async def effect(*_args):
            started.set()
            return True

        router.alarms.play_event = AsyncMock(side_effect=effect)
        router.engine.play_event = AsyncMock(return_value=True)
        router.notify(Event.ATTENTION, {'observation': 'Внимание'}, use_ai=False)
        task = router._task
        await asyncio.wait_for(started.wait(), 1)
        router.stop_audio()
        with self.assertRaises(asyncio.CancelledError):
            await task
        router.engine.play_event.assert_not_awaited()

    async def test_stop_discards_queued_previews(self):
        player = ClipPlayer()
        started = asyncio.Event()
        data = clip_bytes(frames=4000)
        first = asyncio.create_task(player.play(io.BytesIO(data), 0.2, started.set))
        await asyncio.wait_for(started.wait(), 1)
        second = asyncio.create_task(player.play(io.BytesIO(data), 0.2))
        await asyncio.sleep(0)
        player.stop()
        await asyncio.wait_for(first, 0.25)
        with self.assertRaises(PlaybackStopped):
            await second

    async def test_disabled_event_skips_the_model_and_audio(self):
        model = AsyncMock()
        router = VoicePackNotifier({'notification_mode': 'alarm', 'notification_disabled_events': ['ATTENTION']},
                                   model, AsyncMock())
        self.assertFalse(router.notify(Event.ATTENTION, {'observation': 'Внимание'}))
        model.chat.assert_not_awaited()

    def test_long_distraction_has_a_finite_repeat_budget(self):
        activity = Activity({'reminder_interval': 1, 'punishment_interval': 2,
                             'voicepack_repeat_interval': 5, 'voicepack_max_repeats': 1})
        activity.observe(0, False, None)
        self.assertEqual(activity.observe(1, False, None)[0], Event.DISTRACTION)
        self.assertEqual(activity.observe(2, False, None)[0], Event.PERSISTENT_DISTRACTION)
        self.assertEqual(activity.observe(7, False, None)[0], Event.PERSISTENT_DISTRACTION)
        self.assertIsNone(activity.observe(100, False, None))


class ImportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for family, pack in [('alarms','starcraft'), ('voices','terran'), ('voices','zerg')]:
            directory = self.root / family / pack
            directory.mkdir(parents=True)
            (directory/'manifest.json').write_text('{"version":1,"name":"Fixture"}', encoding='utf-8')
        self.alarms = VoicePackCatalog(self.root/'alarms')
        self.voices = VoicePackCatalog(self.root/'voices')

    def test_zip_aliases_validation_and_idempotent_import(self):
        source = self.root/'fixture.zip'
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('release/Interface/countdown.wav', clip_bytes())
            archive.writestr('release/sounds/buzz.wav', clip_bytes())
            archive.writestr('release/advisor/TAdUpd02.wav', clip_bytes())
            archive.writestr('release/sounds/youwin.wav', b'damaged sample')
        first = import_starcraft([source], self.alarms, self.voices)
        self.assertEqual([item['status'] for item in first['imported']], ['added'] * 3)
        self.assertTrue(any(item['source'] == 'misc/youwin.wav' for item in first['invalid']))
        self.assertIn('misc/transmission.wav', first['missing'])
        second = import_starcraft([source], self.alarms, self.voices)
        self.assertEqual([item['status'] for item in second['imported']], ['existing'] * 3)
        self.assertEqual(self.alarms.resolve_clip('starcraft', 'session_start/countdown.wav').read_bytes(), clip_bytes())

    def test_manual_import_matches_catalog_and_never_overwrites(self):
        args = (self.voices, 'zerg', Event.ATTENTION)
        self.assertEqual(save_clip(*args, 'clip.wav', clip_bytes())['file'], 'clip.wav')
        self.assertEqual(save_clip(*args, 'clip.wav', clip_bytes(800))['file'], 'clip-2.wav')
        self.assertEqual(self.voices.resolve_clip('zerg', 'attention/clip.wav').read_bytes(), clip_bytes())
        for name in ('..\\evil.wav', '../evil.wav', 'aux.wav'):
            with self.assertRaises(ValueError):
                save_clip(*args, name, clip_bytes())

    def test_import_from_an_extracted_game_directory(self):
        game = self.root/'Game'
        audio = game/'Sound'/'Zerg'/'Advisor'
        audio.mkdir(parents=True)
        (audio/'ZAdUpd02.wav').write_bytes(clip_bytes())
        report = import_starcraft([game], self.alarms, self.voices)
        self.assertEqual({item['event'] for item in report['imported']}, {'TASK_COMPLETED', 'SESSION_END'})

    @unittest.skipUnless(sys.platform == 'win32' and STORM_DLL.is_file(), 'optional Unicode StormLib.dll')
    def test_reads_a_real_mpq_with_a_unicode_windows_path(self):
        source = MpqSource(STORM_DLL)
        dll = source.dll
        uint, handle = ctypes.c_uint32, ctypes.c_void_p
        dll.SFileCreateArchive.argtypes = (ctypes.c_wchar_p, uint, uint, ctypes.POINTER(handle))
        dll.SFileCreateArchive.restype = ctypes.c_bool
        dll.SFileAddFileEx.argtypes = (handle, ctypes.c_wchar_p, ctypes.c_char_p, uint, uint, uint)
        dll.SFileAddFileEx.restype = ctypes.c_bool
        path = self.root/'Сигнал.wav'
        path.write_bytes(clip_bytes())
        archive = self.root/'Мой архив.mpq'
        result = handle()
        self.assertTrue(dll.SFileCreateArchive(str(archive), 0x100000, 16, ctypes.byref(result)))
        try:
            self.assertTrue(dll.SFileAddFileEx(result, str(path), b'sound\\misc\\buzz.wav', 0x200, 2, 2))
        finally:
            dll.SFileCloseArchive(result)
        report = import_starcraft([archive], self.alarms, self.voices)
        self.assertFalse(report['errors'], report)
        self.assertEqual([item['event'] for item in report['imported']], ['DISTRACTION'])

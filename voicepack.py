"""Local voice packs and event-driven notifications for the focus tracker."""

import asyncio
import io
import json
import logging
import math
import random
import re
import time
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path

import pygame


class Event(str, Enum):
    SESSION_START = 'SESSION_START'
    DISTRACTION = 'DISTRACTION'
    PERSISTENT_DISTRACTION = 'PERSISTENT_DISTRACTION'
    REPEATED_DISTRACTION = 'REPEATED_DISTRACTION'
    RETURN_TO_WORK = 'RETURN_TO_WORK'
    TASK_COMPLETED = 'TASK_COMPLETED'
    SESSION_END = 'SESSION_END'
    IDLE = 'IDLE'
    ATTENTION = 'ATTENTION'
    FOCUS_MODE_ENABLED = 'FOCUS_MODE_ENABLED'
    FOCUS_WARNING = 'FOCUS_WARNING'
    FOCUS_CRITICAL = 'FOCUS_CRITICAL'


CLIP_ALIASES = {Event.FOCUS_WARNING: Event.PERSISTENT_DISTRACTION,
                Event.FOCUS_CRITICAL: Event.REPEATED_DISTRACTION}
DEFAULTS = {
    'voicepack_id': '',
    'voicepack_volume': 0.65,
    'voicepack_min_interval': 15,
    'voicepack_repeat_interval': 120,
    'voicepack_max_repeats': 2,
    'voicepack_idle_seconds': 180,
    'notification_mode': 'live',
    'notification_disabled_events': [],
    'alarm_pack_id': '',
    'alarm_volume': 0.40,
    'alarm_voice_delay_ms': 300,
    'audio_event_mappings': {},
}
PROJECT_ROOT = Path(__file__).resolve().parent
PACK_ROOT = PROJECT_ROOT / 'voicepacks'
VOICE_ROOT = PROJECT_ROOT / 'assets' / 'voicepacks'
ALARM_ROOT = PROJECT_ROOT / 'assets' / 'alarms'
PACK_ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z')
FORMATS = {'.wav', '.mp3', '.ogg'}
MAX_CLIP_BYTES = 16 * 1024 * 1024
MAX_CLIP_SECONDS = 30
MODES = ('alarm', 'voicepack', 'live', 'alarm_voicepack', 'alarm_live')


def notification_mode(config):
    mode = config.get('notification_mode')
    if mode in MODES:
        return mode
    return DEFAULTS['notification_mode']


def configured_volume(config, key):
    try:
        return volume(config.get(key, DEFAULTS[key]))
    except (TypeError, ValueError):
        return DEFAULTS[key]


def volume(value):
    level = float(value)
    if not math.isfinite(level):
        raise ValueError('Громкость должна быть конечным числом')
    return min(1.0, max(0.0, level))


def _seconds(config, key, default, minimum):
    try:
        value = float(config.get(key, default))
    except (ValueError, TypeError):
        value = default
    return max(minimum, value if math.isfinite(value) else default)


class VoicePackCatalog:
    def __init__(self, root=None):
        self.roots = (Path(root).resolve(),) if root is not None else (VOICE_ROOT.resolve(), PACK_ROOT.resolve())

    @staticmethod
    def _pack(root, pack_id):
        if not isinstance(pack_id, str) or not PACK_ID.fullmatch(pack_id):
            raise ValueError('Недопустимое имя папки голосового пакета')
        folder = root / pack_id
        manifest = folder / 'manifest.json'
        if folder.resolve() != folder or manifest.resolve() != manifest:
            raise ValueError('Папка и manifest.json пакета должны быть обычными файлами и каталогами')
        data = json.loads(manifest.read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1:
            raise ValueError('Требуется manifest.json версии 1')
        name = data.get('name')
        if not isinstance(name, str) or not name.strip():
            raise ValueError('Укажи имя пакета в manifest.json')
        return folder, name.strip()[:100]

    def locations(self, pack_id):
        locations = []
        for root in self.roots:
            try:
                locations.append(self._pack(root, pack_id))
            except FileNotFoundError:
                continue
        if not locations:
            raise FileNotFoundError(f'Пакет {pack_id!r} не найден')
        return locations

    @staticmethod
    def _clips(folder, event):
        directory = folder / event.value.lower()
        if directory.resolve() != directory or not directory.is_dir():
            return []
        clips = []
        for path in directory.iterdir():
            if path.suffix.lower() not in FORMATS:
                continue
            try:
                if path.resolve() == path and path.is_file() and 0 < path.stat().st_size <= MAX_CLIP_BYTES:
                    clips.append(path)
            except (OSError, RuntimeError):
                continue  # Файл мог исчезнуть во время сканирования.
        return sorted(clips, key=lambda path: path.name.casefold())

    def clips(self, pack_id, event):
        return self._merged_clips(self.locations(pack_id), event)

    def _merged_clips(self, locations, event):
        unique = {}
        for folder, _ in locations:
            for clip in self._clips(folder, event):
                unique.setdefault(clip.name.casefold(), clip)
        return sorted(unique.values(), key=lambda clip: clip.name.casefold())

    def resolve_clip(self, pack_id, reference):
        category, _, filename = reference.partition('/')
        event = Event(category.upper())
        path = next((item for item in self.clips(pack_id, event) if item.name == filename), None)
        if path is None:
            raise ValueError('Аудиофайл не найден в выбранном пакете')
        return path

    def list_packs(self):
        ids = {}
        for root in self.roots:
            if root.is_dir():
                for path in sorted(root.iterdir()):
                    if path.is_dir() and PACK_ID.fullmatch(path.name):
                        ids.setdefault(path.name.casefold(), path.name)
        packs = []
        for pack_id in sorted(ids.values()):
            try:
                locations = self.locations(pack_id)
                clips = {event.value: [clip.name for clip in self._merged_clips(locations, event)] for event in Event}
                packs.append({'id': pack_id, 'name': locations[0][1], 'clips': clips})
            except (OSError, ValueError, RuntimeError):
                continue
        return packs


class ClipPlayer:
    """One audio channel at a time for pack clips, TTS fallback and previews."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._owner = None
        self._owner_generation = 0
        self._channel = None
        self._generation = 0

    @property
    def busy(self):
        return self._lock.locked()

    @asynccontextmanager
    async def sequence(self):
        task = asyncio.current_task()
        if task is self._owner:
            if self._owner_generation != self._generation:
                raise PlaybackStopped()
            yield
            return
        generation = self._generation
        async with self._lock:
            if generation != self._generation:
                raise PlaybackStopped()
            self._owner = task
            self._owner_generation = generation
            try:
                yield
            finally:
                self._owner = None

    def stop(self):
        self._generation += 1
        if self._channel is not None:
            self._channel.stop()

    @staticmethod
    def _decode(source, limit=None):
        if isinstance(source, Path):
            if source.resolve(strict=True) != source:
                raise ValueError('Аудиофайл должен находиться в папке пакета')
            # Закрываем файл сами, в том числе если SDL отклонит повреждённый клип.
            with source.open('rb') as file:
                data = file.read(MAX_CLIP_BYTES + 1)
            if not data or len(data) > MAX_CLIP_BYTES:
                raise ValueError('Файл пустой или больше 16 МиБ')
            source = io.BytesIO(data)
            limit = MAX_CLIP_SECONDS
        sound = pygame.mixer.Sound(file=source)
        if sound.get_length() <= 0 or (limit is not None and sound.get_length() > limit):
            raise ValueError('Длительность аудиофайла должна быть от 0 до 30 секунд')
        return sound

    @classmethod
    def _prepare(cls, source, level):
        sound = cls._decode(source)
        if pygame.mixer.get_init()[1] == -16:
            samples = memoryview(sound.get_raw()).cast('h')
            peak = max(max(samples), -min(samples))
            if peak:
                level = min(level, 22000 / peak)
        sound.set_volume(level)
        return sound

    async def play(self, source, level, on_start=None):
        generation = self._generation
        async with self.sequence():
            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=48000, size=-16, channels=2, buffer=2048)
            sound = await asyncio.to_thread(self._prepare, source, volume(level))
            if generation != self._generation:
                raise PlaybackStopped()
            channel = sound.play(fade_ms=25)
            if channel is None:
                raise RuntimeError('Нет свободного звукового канала')
            self._channel = channel
            try:
                if on_start:
                    on_start()
                while channel.get_busy():
                    await asyncio.sleep(0.05)
            finally:
                channel.stop()
                self._channel = None


class PlaybackStopped(Exception):
    """The user interrupted the current sound or an already queued preview."""


class VoicePackEngine:
    def __init__(self, catalog, player):
        self.catalog = catalog
        self.player = player
        self.last_clip = None

    async def play_event(self, pack_id, event, level, on_start=None, override=None):
        try:
            if override:
                clips = [await asyncio.to_thread(self.catalog.resolve_clip, pack_id, override)]
            else:
                clips = await asyncio.to_thread(self.catalog.clips, pack_id, event)
                if not clips and event in CLIP_ALIASES:
                    clips = await asyncio.to_thread(self.catalog.clips, pack_id, CLIP_ALIASES[event])
        except (OSError, ValueError, RuntimeError) as error:
            logging.warning('[VoicePack] Пакет %s недоступен: %s', pack_id, error)
            return False
        random.shuffle(clips)
        if self.last_clip in clips:
            clips.remove(self.last_clip)
            clips.append(self.last_clip)
        for path in clips:
            try:
                await self.player.play(path, level, on_start)
            except (OSError, ValueError, RuntimeError, pygame.error) as error:
                logging.warning('[VoicePack] Не удалось воспроизвести %s: %s', path.name, error)
                continue
            self.last_clip = path
            return True
        return False

    async def preview(self, pack_id, event, filename, level):
        async with self.player.sequence():
            path = await asyncio.to_thread(self.catalog.resolve_clip, pack_id, f'{event.value.lower()}/{filename}')
            await self.player.play(path, level)


class AlarmEngine(VoicePackEngine):
    """Plays a separate effects pack using the shared clip playback channel."""

    def __init__(self, player, catalog=None):
        super().__init__(catalog or VoicePackCatalog(ALARM_ROOT), player)


class VoicePackNotifier:
    """Runs one pack notification, with no backlog of stale alerts."""

    def __init__(self, config, *, player=None):
        self.config = {**DEFAULTS, **config}
        self.mode = notification_mode(config)
        self.player = player or ClipPlayer()
        self.engine = VoicePackEngine(VoicePackCatalog(), self.player)
        self.alarms = AlarmEngine(self.player)
        self.interval = _seconds(config, 'voicepack_min_interval', DEFAULTS['voicepack_min_interval'], 5)
        self.level = configured_volume(config, 'voicepack_volume')
        self.alarm_level = configured_volume(config, 'alarm_volume')
        self.delay = min(10000, _seconds(config, 'alarm_voice_delay_ms', DEFAULTS['alarm_voice_delay_ms'], 0)) / 1000
        disabled = self.config['notification_disabled_events']
        self.disabled = {event for event in Event if isinstance(disabled, list) and event.value in disabled}
        self.last_notice = float('-inf')
        self._task = None

    def notify(self, event, mode_override=None, on_play=None):
        if self.mode in ('live', 'alarm_live'):
            return False  # The live coordinator admits both parts of a notification together.
        if event in self.disabled or (self._task is not None and not self._task.done()):
            return False
        if time.monotonic() - self.last_notice < self.interval:
            return False
        self._task = asyncio.create_task(
            self._deliver(event, mode_override or self.mode, on_play),
        )
        return True

    async def _deliver(self, event, mode, on_play=None):
        def played_clip():
            nonlocal on_play
            self._played()
            if on_play:
                on_play()
                on_play = None

        try:
            uses_pack = mode in ('voicepack', 'alarm_voicepack') and self.level > 0
            async with self.player.sequence():
                if mode.startswith('alarm') and self.alarm_level > 0:
                    alarm_id = self.config['alarm_pack_id']
                    played = await self.alarms.play_event(
                        alarm_id, event, self.alarm_level, played_clip,
                        self._mapping('alarm', alarm_id, event),
                    )
                    if played and uses_pack:
                        await asyncio.sleep(self.delay)

                if uses_pack:
                    pack_id = self.config['voicepack_id']
                    await self.engine.play_event(
                        pack_id, event, self.level, played_clip,
                        self._mapping('voice', pack_id, event),
                    )
        except PlaybackStopped:
            pass
        except Exception as error:
            logging.warning('[VoicePack] Ошибка уведомления: %s', error)

    async def live_alarm(self, event):
        if self.mode == 'alarm_live' and self.alarm_level and event not in self.disabled:
            pack = self.config['alarm_pack_id']
            if await self.alarms.play_event(pack, event, self.alarm_level, override=self._mapping('alarm', pack, event)):
                await asyncio.sleep(self.delay)

    def _mapping(self, kind, pack_id, event):
        value = self.config['audio_event_mappings']
        for key in (kind, pack_id, event.value):
            value = value.get(key) if isinstance(value, dict) and isinstance(key, str) else None
        return value if isinstance(value, str) else None

    def _played(self):
        self.last_notice = time.monotonic()

    def cancel(self):
        if self._task:
            if not self._task.done():
                self._task.cancel()
            self._task = None

    def stop_audio(self):
        self.cancel()
        self.player.stop()

    async def close(self):
        if self._task:
            task = self._task
            self.cancel()
            await asyncio.gather(task, return_exceptions=True)

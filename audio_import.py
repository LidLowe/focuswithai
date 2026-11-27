"""Import audio from user-selected files, ZIPs, or classic local MPQ archives."""

import argparse
import ctypes
import io
import json
import math
import os
import re
import sys
import wave
import zipfile
from pathlib import Path, PurePosixPath

import pygame

from voicepack import (ALARM_ROOT, FORMATS, MAX_CLIP_BYTES, MAX_CLIP_SECONDS, PROJECT_ROOT, VOICE_ROOT,
                       ClipPlayer, Event, VoicePackCatalog)


STORM_DLL = PROJECT_ROOT / '.runtime' / 'stormlib' / 'StormLib.dll'
MPQ_ORDER = {'stardat.mpq': 0, 'starcraft.mpq': 0, 'broodat.mpq': 1, 'broodwar.mpq': 1, 'patch_rt.mpq': 2}
ALARM_CANDIDATES = {
    Event.DISTRACTION: 'misc/buzz.wav',
    Event.REPEATED_DISTRACTION: 'misc/perror.wav',
    Event.PERSISTENT_DISTRACTION: 'misc/perror.wav',
    Event.ATTENTION: 'misc/transmission.wav',
    Event.SESSION_START: 'glue/countdown.wav',
    Event.FOCUS_MODE_ENABLED: 'glue/swishlock.wav',
    Event.TASK_COMPLETED: 'glue/scorefillend.wav',
    Event.SESSION_END: 'misc/youwin.wav',
}
VOICE_CANDIDATES = {
    'terran': {
        Event.DISTRACTION: ('tadupd00.wav',),
        Event.ATTENTION: ('tadupd00.wav',),
        Event.REPEATED_DISTRACTION: ('tadupd01.wav',),
        Event.PERSISTENT_DISTRACTION: ('tadupd01.wav',),
        Event.TASK_COMPLETED: ('tadupd02.wav', 'tadupd06.wav'),
        Event.SESSION_END: ('tadupd06.wav',),
    },
    'zerg': {
        Event.DISTRACTION: ('zadupd00.wav',),
        Event.ATTENTION: ('zadupd00.wav',),
        Event.REPEATED_DISTRACTION: ('zadupd01.wav',),
        Event.PERSISTENT_DISTRACTION: ('zadupd01.wav',),
        Event.TASK_COMPLETED: ('zadupd02.wav',),
        Event.SESSION_END: ('zadupd02.wav',),
    },
}


def _candidates():
    for event, member in ALARM_CANDIDATES.items():
        folder, filename = member.split('/')
        alias = 'interface' if folder == 'glue' else 'sounds'
        yield 'alarm', 'starcraft', event, member, f'{alias}/{filename}'
    for pack, groups in VOICE_CANDIDATES.items():
        for event, files in groups.items():
            for filename in files:
                yield 'voice', pack, event, f'{pack}/advisor/{filename}', f'advisor/{filename}'


def read_clip(stream):
    data = stream.read(MAX_CLIP_BYTES + 1)
    if not 0 < len(data) <= MAX_CLIP_BYTES:
        raise ValueError('Аудиофайл пуст или превышает 16 МиБ')
    return data


def save_clip(catalog, pack, event, filename, data):
    if (not isinstance(filename, str) or len(filename) > 140 or not filename
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', filename) or filename.endswith((' ', '.'))
            or re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', filename, re.I)
            or Path(filename).suffix.lower() not in FORMATS):
        raise ValueError('Укажи допустимое имя файла WAV, MP3 или OGG')
    if not 0 < len(data) <= MAX_CLIP_BYTES:
        raise ValueError('Аудиофайл пуст или превышает 16 МиБ')
    folder, _ = catalog.locations(pack)[0]
    directory = folder / event.value.lower()
    if directory.resolve() != directory:
        raise ValueError('Папка категории должна быть обычным каталогом')
    if not pygame.mixer.get_init():
        pygame.mixer.init(frequency=48000, size=-16, channels=2, buffer=2048)
    sound = ClipPlayer._decode(io.BytesIO(data), limit=MAX_CLIP_SECONDS)
    duration = round(sound.get_length(), 3)
    del sound
    directory.mkdir(exist_ok=True)
    source_name = Path(filename)
    for number in range(1000):
        target = directory / (filename if number == 0 else f'{source_name.stem}-{number + 1}{source_name.suffix}')
        if target.resolve() != target:
            continue
        try:
            output = target.open('xb')
        except FileExistsError:
            if target.is_file() and target.stat().st_size == len(data) and target.read_bytes() == data:
                return {'file': target.name, 'seconds': duration, 'status': 'existing'}
            continue
        try:
            with output:
                output.write(data)
        except OSError:
            target.unlink(missing_ok=True)
            raise
        return {'file': target.name, 'seconds': duration, 'status': 'added'}
    raise ValueError('Слишком много файлов с таким именем')


def _normal(name):
    return name.replace('\\', '/').strip('/').lower()


def _match(names, canonical, alias):
    for candidate in (canonical, alias):
        matches = [name for name in names if name == candidate or name.endswith('/' + candidate)]
        if len(matches) == 1:
            return matches[0]
    basename = PurePosixPath(canonical).name
    matches = [name for name in names if name == basename or name.endswith('/' + basename)]
    return matches[0] if len(matches) == 1 else None


def _members(source, candidates):
    if isinstance(source, zipfile.ZipFile):
        members = {_normal(info.filename): info for info in source.infolist()
                   if not info.is_dir() and PurePosixPath(info.filename).suffix.lower() in FORMATS}
        for canonical, alias in candidates:
            name = _match(members, canonical, alias)
            if name and 0 < members[name].file_size <= MAX_CLIP_BYTES:
                with source.open(members[name]) as stream:
                    yield canonical, read_clip(stream)
        return
    files = {_normal(path.relative_to(source).as_posix()): path for path in source.rglob('*')
             if path.suffix.lower() in FORMATS and path.is_file() and path.resolve() == path}
    for canonical, alias in candidates:
        name = _match(files, canonical, alias)
        if name and 0 < files[name].stat().st_size <= MAX_CLIP_BYTES:
            with files[name].open('rb') as stream:
                yield canonical, read_clip(stream)


class MpqSource:
    """Read-only access to named classic assets through a user-local StormLib."""

    def __init__(self, library):
        if sys.platform != 'win32' or not library.is_file():
            raise ValueError('Для прямого импорта MPQ на Windows нужен StormLib.dll; можно также использовать ZIP или папку')
        self.dll = ctypes.WinDLL(str(library.resolve()), use_last_error=True)
        handle, dword = ctypes.c_void_p, ctypes.c_uint32
        pointer = ctypes.POINTER
        for name, result, args in (
            ('SFileOpenArchive', ctypes.c_bool, (ctypes.c_wchar_p, dword, dword, pointer(handle))),
            ('SFileOpenFileEx', ctypes.c_bool, (handle, ctypes.c_char_p, dword, pointer(handle))),
            ('SFileGetFileSize', dword, (handle, pointer(dword))),
            ('SFileReadFile', ctypes.c_bool, (handle, ctypes.c_void_p, dword, pointer(dword), ctypes.c_void_p)),
            ('SFileCloseFile', ctypes.c_bool, (handle,)),
            ('SFileCloseArchive', ctypes.c_bool, (handle,)),
        ):
            function = getattr(self.dll, name)
            function.restype, function.argtypes = result, args

    def _read(self, archive, member):
        handle = ctypes.c_void_p()
        if not self.dll.SFileOpenFileEx(archive, member.encode('ascii'), 0, ctypes.byref(handle)):
            return None
        try:
            high = ctypes.c_uint32()
            size = self.dll.SFileGetFileSize(handle, ctypes.byref(high))
            if high.value or not 0 < size <= MAX_CLIP_BYTES:
                return None
            buffer = ctypes.create_string_buffer(size)
            read = ctypes.c_uint32()
            if self.dll.SFileReadFile(handle, buffer, size, ctypes.byref(read), None) and read.value == size:
                return buffer.raw
        finally:
            self.dll.SFileCloseFile(handle)
        return None

    def members(self, source, candidates):
        archive = ctypes.c_void_p()
        if not self.dll.SFileOpenArchive(str(source), 0, 0x100, ctypes.byref(archive)):
            raise OSError(ctypes.get_last_error(), f'Не удалось открыть MPQ: {source.name}')
        try:
            for canonical, alias in candidates:
                names = (canonical, 'sound/' + canonical, alias, 'sound/' + alias)
                for name in names:
                    data = self._read(archive, name.replace('/', '\\'))
                    if data:
                        yield canonical, data
                        break
        finally:
            self.dll.SFileCloseArchive(archive)


def import_starcraft(paths, alarm_catalog, voice_catalog, stormlib=STORM_DLL):
    specifications = list(_candidates())
    candidates = list(dict.fromkeys((member, alias) for _, _, _, member, alias in specifications))
    found, errors = {}, []
    reader = None
    for source in paths:
        try:
            source = Path(source).expanduser().resolve(strict=True)
            archives = []
            if source.is_dir():
                archives = sorted((path for path in source.iterdir() if path.is_file() and path.suffix.lower() == '.mpq'),
                                  key=lambda path: (MPQ_ORDER.get(path.name.lower(), 1), path.name.lower()))
        except OSError as error:
            errors.append({'source': str(source), 'error': str(error)})
            continue
        for item in (source, *archives):
            try:
                if item.is_dir():
                    found.update(_members(item, candidates))
                elif item.suffix.lower() == '.zip':
                    with zipfile.ZipFile(item) as content:
                        found.update(_members(content, candidates))
                elif item.suffix.lower() == '.mpq':
                    reader = reader or MpqSource(Path(stormlib))
                    found.update(reader.members(item, candidates))
                else:
                    raise ValueError('Поддерживаются каталоги, ZIP и классические MPQ')
            except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
                errors.append({'source': item.name, 'error': str(error)})
    imported, invalid = [], []
    for kind, pack, event, member, _ in specifications:
        if member not in found:
            continue
        catalog = alarm_catalog if kind == 'alarm' else voice_catalog
        try:
            result = save_clip(catalog, pack, event, PurePosixPath(member).name, found[member])
            imported.append({'pack': pack, 'event': event.value, 'source': member, **result})
        except (OSError, ValueError, pygame.error) as error:
            invalid.append({'source': member, 'error': str(error)})
    return {'imported': imported, 'missing': [name for name, _ in candidates if name not in found],
            'invalid': invalid, 'errors': errors}


def generate_demo(root):
    root = Path(root).resolve()
    pack = root / 'demo'
    manifest = pack / 'manifest.json'
    if pack.resolve() != pack or manifest.resolve() != manifest:
        raise ValueError('Демонстрационный пакет должен находиться в обычной папке')
    pack.mkdir(parents=True, exist_ok=True)
    if not manifest.exists():
        manifest.write_text('{"version": 1, "name": "Demo · synthetic cues"}\n', encoding='utf-8')
    catalog = VoicePackCatalog(root)
    for index, event in enumerate(Event):
        buffer = io.BytesIO()
        rate, length = 16000, 3200
        frequency = 330 + 43 * index
        with wave.open(buffer, 'wb') as wav:
            wav.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
            for i in range(length):
                envelope = min(1, i / 320, (length - i) / 640)
                sample = int(4200 * envelope * math.sin(2 * math.pi * frequency * i / rate))
                wav.writeframesraw(sample.to_bytes(2, 'little', signed=True))
        save_clip(catalog, 'demo', event, f'demo-{event.value.lower()}.wav', buffer.getvalue())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sources', nargs='*', help='Local extracted directories, ZIPs or classic MPQs')
    parser.add_argument('--stormlib', type=Path, default=STORM_DLL, help='Path to the Unicode StormLib.dll')
    parser.add_argument('--demo', action='store_true', help='Generate original soft demo beeps')
    args = parser.parse_args()
    if not args.demo and not args.sources:
        parser.error('specify a local source or --demo')
    os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
    if args.demo:
        generate_demo(ALARM_ROOT)
    if args.sources:
        report = import_starcraft(args.sources, VoicePackCatalog(ALARM_ROOT), VoicePackCatalog(VOICE_ROOT), args.stormlib)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report['imported'] else 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

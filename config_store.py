"""Versioned application configuration and per-user credentials."""

import hashlib
import json
import os
import sys
from pathlib import Path


GEMINI_KEY = 'google_assistant_api_key'
SECRET_KEYS = (GEMINI_KEY,)
VERSION = 2
DEFAULT_PORT = 8765


def defaults():
    from activity_context import CONTEXT_DEFAULTS
    from focus_meter import FOCUS_DEFAULTS
    from gemini_live import DEFAULTS as GEMINI_DEFAULTS
    from startup_manager import STARTUP_DEFAULTS
    from voicepack import DEFAULTS as AUDIO_DEFAULTS

    return {
        'schema_version': VERSION, 'panel_port': DEFAULT_PORT, 'debug_level': 0,
        'discord_presence': False, 'audio_frequency': 48000, 'audio_buffer': 2048,
        **CONTEXT_DEFAULTS, **FOCUS_DEFAULTS, **GEMINI_DEFAULTS, **STARTUP_DEFAULTS, **AUDIO_DEFAULTS,
    }


def target(path, key):
    ident = hashlib.sha256(str(path.resolve()).casefold().encode()).hexdigest()[:20]
    return f'FocusWithAI/{ident}/{key}'


def read_secret(path, key):
    import win32cred
    import pywintypes

    try:
        blob = win32cred.CredRead(target(path, key), win32cred.CRED_TYPE_GENERIC)['CredentialBlob']
    except pywintypes.error as error:
        if error.winerror == 1168:
            return ''
        raise
    return blob.decode('utf-16-le') if isinstance(blob, bytes) else blob


def store_secret(path, key, value):
    import win32cred
    import pywintypes

    if not isinstance(value, str) or len(value.encode('utf-16-le')) > 2400:
        raise ValueError('Некорректный API-ключ.')
    if value:
        win32cred.CredWrite({'Type': win32cred.CRED_TYPE_GENERIC, 'TargetName': target(path, key),
                            'CredentialBlob': value, 'Persist': win32cred.CRED_PERSIST_LOCAL_MACHINE,
                            'UserName': 'FocusWithAI'}, 0)
    else:
        try:
            win32cred.CredDelete(target(path, key), win32cred.CRED_TYPE_GENERIC, 0)
        except pywintypes.error as error:
            if error.winerror != 1168:
                raise


def public_config(data):
    return {key: value for key, value in data.items() if key not in {*SECRET_KEYS, 'api_key', 'openai_api_key'}}


def write_disk(path, data):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as output:
        json.dump(data, output, indent=4, ensure_ascii=False)
        output.write('\n')
    os.replace(temporary, path)


def migrate(path, original):
    """Preserve the pre-consolidation settings without copying credentials to disk."""
    backup = path.with_name(path.stem + '.pre-gemini-live.json')
    if not backup.exists():
        write_disk(backup, public_config(original))

    known = defaults()
    data = {key: value for key, value in original.items() if key in known}
    data['schema_version'] = VERSION
    if original.get('activity_provider_mode') != 'gemini_live':
        data['activity_allow_remote'] = False
        data['activity_send_remote_images'] = False
    if 'gemini_live_prompt' not in data:
        if original.get('activity_system_prompt'):
            data['gemini_live_prompt'] = original['activity_system_prompt']
        elif original.get('live_style') == 'concise':
            data['gemini_live_prompt'] = 'Кратко и спокойно.'
    mode = original.get('notification_mode')
    if mode == 'alarm_tts':
        data['notification_mode'] = 'alarm_live'
    elif not mode and original.get('voicepack_enabled'):
        data['notification_mode'] = 'voicepack'
    elif mode in ('tts', ''):
        data['notification_mode'] = 'live'
    return data


def load_config(path='config.json'):
    path = Path(path)
    try:
        original = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return {}
    version = original.get('schema_version', 0) if isinstance(original, dict) else None
    if type(version) is not int or not 0 <= version <= VERSION:
        raise ValueError('Неподдерживаемая версия config.json.')
    data = migrate(path, original) if version < VERSION else original
    if sys.platform == 'win32':
        if data.get(GEMINI_KEY):
            store_secret(path, GEMINI_KEY, data[GEMINI_KEY])
        data = public_config(data)
    if original != data:
        write_disk(path, data)
    if sys.platform == 'win32':
        data[GEMINI_KEY] = read_secret(path, GEMINI_KEY)
    return data


def save_config(data, path='config.json'):
    path = Path(path)
    data = {**data, 'schema_version': VERSION}
    known = defaults()
    if data.keys() - known.keys():
        raise ValueError('Неизвестные настройки FocusWithAI.')
    if sys.platform == 'win32':
        store_secret(path, GEMINI_KEY, data.get(GEMINI_KEY, ''))
        data = public_config(data)
    write_disk(path, data)

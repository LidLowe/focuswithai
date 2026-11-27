"""Versioned application configuration and per-user credentials."""

import hashlib
import json
import os
import sys
from pathlib import Path


SECRET_KEYS = ('api_key', 'openai_api_key', 'google_assistant_api_key')
VERSION = 1


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
    return {key: value for key, value in data.items() if key not in SECRET_KEYS}


def write_disk(path, data):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as output:
        json.dump(data, output, indent=4, ensure_ascii=False)
        output.write('\n')
    os.replace(temporary, path)


def load_config(path='config.json'):
    path = Path(path)
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict) or type(data.get('schema_version', 0)) is not int or not 0 <= data.get('schema_version', 0) <= VERSION:
        raise ValueError('Неподдерживаемая версия config.json.')
    before = dict(data)
    data['schema_version'] = VERSION
    if sys.platform == 'win32':
        for key in SECRET_KEYS:
            if key in data:
                value = data.pop(key)
                if value:
                    store_secret(path, key, value)
        if before != data:
            write_disk(path, data)
        return {**data, **{key: read_secret(path, key) for key in SECRET_KEYS}}
    if before != data:
        write_disk(path, data)
    return data


def save_config(data, path='config.json'):
    path = Path(path)
    data = {**data, 'schema_version': VERSION}
    if sys.platform == 'win32':
        for key in SECRET_KEYS:
            store_secret(path, key, data.get(key, ''))
        data = public_config(data)
    write_disk(path, data)

"""Local FocusWithAI Dashboard and control API."""

import asyncio
import copy
import json
import logging
import os
import re
import sys
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

from aiohttp import web
from audio_import import save_clip
from config_store import DEFAULT_PORT, SECRET_KEYS, VERSION, defaults, load_config, public_config, save_config
from gemini_live import MODELS, VOICES, GeminiLiveTransport, validate_preferences
from live_session import LiveSetupError
from startup_manager import StartupTask
from voicepack import (AlarmEngine, ClipPlayer, DEFAULTS as VOICEPACK_DEFAULTS, Event, MAX_CLIP_BYTES,
                       PlaybackStopped, VoicePackCatalog, VoicePackEngine)

CONFIG_FILE = 'config.json'
BACKUP_DIR = 'backups'
BACKUPS_KEPT = 10
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'panel_web')


# Ключи API в панель не отдаются, вместо них приходит маска
MASK = '••••••••'



# ---------- захват вывода для просмотра логов в панели ----------

class LogBuffer:
    LEVEL_RE = re.compile(r"\[(ERROR|WARNING|WARN|INFO|DEBUG)\b")

    def __init__(self, size: int = 500):
        self.lines: deque = deque(maxlen=size)
        self.seq = 0
        self._lock = threading.Lock()

    def add(self, text: str):
        with self._lock:
            for line in text.splitlines():
                if not line.strip():
                    continue
                match = self.LEVEL_RE.search(line)
                level = match.group(1) if match else 'INFO'
                self.seq += 1
                self.lines.append((self.seq, time.time(), 'WARNING' if level == 'WARN' else level, line))

    def since(self, seq: int) -> List[Dict[str, Any]]:
        with self._lock:
            return [{'seq': s, 'ts': ts, 'level': lv, 'text': tx}
                    for s, ts, lv, tx in self.lines if s > seq]


class _Tee:
    """Пишет в исходный поток и копирует строки в LogBuffer"""

    def __init__(self, stream, buffer: LogBuffer):
        self.stream = stream
        self.buffer = buffer
        self.partial = ''

    def write(self, text):
        try:
            self.stream.write(text)
        except Exception:
            pass
        self.partial += text
        if '\n' in self.partial:
            done, self.partial = self.partial.rsplit('\n', 1)
            self.buffer.add(done)
        return len(text)

    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self.stream, name)


LOGS = LogBuffer()


def capture_output():
    """Вызывается из main.py до настройки logging"""
    # Когда вывод идёт не в консоль (терминал IDE, перенаправление в файл), print()
    # копится в буфере и строки появляются с большой задержкой или только при выходе
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass
    if sys.stdout is not None and not isinstance(sys.stdout, _Tee):
        sys.stdout = _Tee(sys.stdout, LOGS)
    if sys.stderr is not None and not isinstance(sys.stderr, _Tee):
        sys.stderr = _Tee(sys.stderr, LOGS)


# ---------- файлы ----------

def read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_text_atomic(path: str, text: str):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    os.replace(tmp, path)


def write_json_atomic(path: str, data: Any, indent: int = 4):
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=indent) + '\n')


def mask_config(config: Dict[str, Any]) -> Dict[str, Any]:
    masked = copy.deepcopy(config)
    for key in SECRET_KEYS:
        if masked.get(key):
            masked[key] = MASK
    return masked


def unmask(draft: Dict[str, Any], stored: Dict[str, Any]) -> Dict[str, Any]:
    """Черновик из панели с масками вместо ключей -> настоящий конфиг"""
    config = copy.deepcopy(draft)
    for key in SECRET_KEYS:
        if config.get(key) == MASK:
            config[key] = stored.get(key, '')
    return config


def set_path(data: Dict[str, Any], path: str, value: Any):
    parts = path.split('.')
    for part in parts[:-1]:
        if not isinstance(data.get(part), dict):
            data[part] = {}
        data = data[part]
    data[parts[-1]] = value


def del_path(data: Dict[str, Any], path: str):
    parts = path.split('.')
    for part in parts[:-1]:
        data = data.get(part)
        if not isinstance(data, dict):
            return
    data.pop(parts[-1], None)


def backup_config():
    """Копия config.json перед каждой записью, хранятся последние BACKUPS_KEPT"""
    if not os.path.exists(CONFIG_FILE):
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    path = os.path.join(BACKUP_DIR, f'config-{stamp}.json')
    n = 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(BACKUP_DIR, f'config-{stamp}-{n}.json')
    write_json_atomic(path, public_config(read_json(CONFIG_FILE, {}) or {}))
    for old in list_backups()[BACKUPS_KEPT:]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old['name']))
        except OSError:
            pass


def list_backups() -> List[Dict[str, Any]]:
    if not os.path.isdir(BACKUP_DIR):
        return []
    items = [{'name': n, 'mtime': os.path.getmtime(os.path.join(BACKUP_DIR, n))}
             for n in os.listdir(BACKUP_DIR) if re.fullmatch(r'config-[\d-]+\.json', n)]
    return sorted(items, key=lambda b: b['mtime'], reverse=True)


def write_config(config: Dict[str, Any]):
    if type(config.get('schema_version', VERSION)) is not int or config.get('schema_version', VERSION) != VERSION:
        raise ValueError('Неподдерживаемая версия конфигурации.')
    validate_preferences(config)
    if os.path.exists(CONFIG_FILE):
        backup_config()
    save_config(config)


# ---------- сервер ----------

def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status,
                             dumps=lambda o: json.dumps(o, ensure_ascii=False))


def _handler(func):
    """Ошибки обработчика -> JSON {error}, чтобы панель могла их показать"""
    async def wrapper(self, request):
        try:
            return await func(self, request)
        except web.HTTPException:
            raise
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
            return _json({'error': str(e)}, 400)
        except Exception as e:
            logging.exception('[PANEL] Ошибка запроса')
            return _json({'error': f'{type(e).__name__}: {e}'}, 500)
    return wrapper


class Panel:
    def __init__(self, runtime=None):
        self.rt = runtime
        notifier = runtime.voicepack if runtime else None
        self._player = notifier.player if notifier else ClipPlayer()
        self._voicepacks = notifier.engine if notifier else VoicePackEngine(VoicePackCatalog(), self._player)
        self._alarms = notifier.alarms if notifier else AlarmEngine(self._player)
        self.app = web.Application(middlewares=[self._guard], client_max_size=20 * 1024 * 1024)
        r = self.app.router
        r.add_get('/', self.index)
        r.add_static('/static/', WEB_DIR)
        r.add_get('/api/status', self.status)
        r.add_get('/api/activity/windows', self.capture_choices)
        r.add_get('/api/activity/preview', self.context_preview)
        r.add_post('/api/activity/preview', self.context_preview)
        r.add_post('/api/activity/browser', self.browser_event)
        r.add_get('/api/sprints', self.get_sprints)
        r.add_post('/api/sprints', self.sprint_command)
        r.add_get('/api/startup', self.startup_status)
        r.add_post('/api/startup', self.set_startup)
        r.add_get('/api/config', self.get_config)
        r.add_put('/api/config', self.put_config)
        r.add_get('/api/audio/packs', self.audio_packs)
        r.add_post('/api/audio/preview', self.audio_preview)
        r.add_post('/api/audio/import', self.audio_import)
        r.add_post('/api/audio/stop', self.audio_stop)
        r.add_post('/api/test/gemini-live', self.test_gemini_live)
        r.add_post('/api/control', self.control)
        r.add_post('/api/process', self.process)
        r.add_get('/api/logs', self.logs)

    @web.middleware
    async def _guard(self, request, handler):
        # Только localhost: чужой сайт не достучится через DNS rebinding,
        # а изменения без заголовка X-Panel не пройдут без CORS-разрешения
        host = request.host.rsplit(':', 1)[0] if not request.host.startswith('[') else request.host.split(']')[0] + ']'
        if host not in ('127.0.0.1', 'localhost', '[::1]'):
            return web.Response(status=403, text='forbidden host')
        if request.method not in ('GET', 'HEAD') and request.headers.get('X-Panel') != '1':
            return web.Response(status=403, text='missing X-Panel header')
        response = await handler(request)
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    def _live(self):
        if not self.rt:
            raise ValueError('Трекер не запущен: запусти start.bat или python main.py')
        return self.rt

    @_handler
    async def startup_status(self, request):
        if sys.platform != 'win32':
            return _json({'supported': False})
        return _json(await asyncio.to_thread(StartupTask().status))

    @_handler
    async def set_startup(self, request):
        enabled = (await self._body(request)).get('enabled')
        if sys.platform != 'win32' or type(enabled) is not bool:
            raise ValueError('Задание автозапуска можно включить или выключить в Windows.')
        return _json(await asyncio.to_thread(StartupTask().set_enabled, enabled))

    async def _body(self, request) -> Dict[str, Any]:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError('Ожидается JSON-объект')
        return data

    def _draft(self, body: Dict[str, Any]) -> Dict[str, Any]:
        stored = load_config()
        draft = body.get('draft')
        return unmask(draft, stored) if isinstance(draft, dict) else stored

    # --- страницы ---

    async def index(self, request):
        with open(os.path.join(WEB_DIR, 'index.html'), 'r', encoding='utf-8') as f:
            html = f.read()
        version = str(int(max(os.path.getmtime(os.path.join(WEB_DIR, n))
                              for n in ('app.js', 'app.css'))))
        return web.Response(text=html.replace('{{v}}', version), content_type='text/html',
                            headers={'Cache-Control': 'no-store'})

    # --- состояние ---

    @_handler
    async def status(self, request):
        data = {'live': self.rt is not None, 'pid': os.getpid(), 'now': time.time(),
                'config_mtime': os.path.getmtime(CONFIG_FILE) if os.path.exists(CONFIG_FILE) else None}
        if self.rt:
            data.update(self.rt.state())
        return _json(data)

    @_handler
    async def control(self, request):
        body = await self._body(request)
        self._live().control(body.get('action'), body.get('value'))
        return _json({'ok': True})

    @_handler
    async def capture_choices(self, request):
        focus = self._live().focus
        if not focus or not focus.phase().active or not focus.acquisition.enabled:
            return _json({'windows': []})
        acq = focus.acquisition
        return _json({'windows': await asyncio.to_thread(acq.windows.capture_choices, acq.privacy)})

    @_handler
    async def context_preview(self, request):
        focus = self._live().focus
        return _json({'preview': focus.context_preview(request.method == 'POST') if focus else None})

    @_handler
    async def browser_event(self, request):
        if not (request.headers.get('Origin', '').startswith('chrome-extension://')
                and request.headers.get('X-FocusWithAI-Browser') == '1'):
            return web.Response(status=403)
        focus = self._live().focus
        data = await self._body(request)
        active = bool(focus and focus.phase().active and focus.acquisition.privacy.domains)
        if active and not data.get('check'):
            focus.acquisition.browser_event(data, time.monotonic())
        return _json({'ok': True, 'active': active})

    @_handler
    async def get_sprints(self, request):
        return _json(self._live().sprints.snapshot())

    @_handler
    async def sprint_command(self, request):
        rt = self._live()
        body = await self._body(request)
        action = body.get('action')
        if not isinstance(action, str):
            raise ValueError('Укажи действие со спринтом.')
        if not rt.startup_running:
            raise ValueError('Подожди, пока трекер завершит запуск.')
        item = rt.sprint_command(action, body)
        return _json({'ok': True, 'item': item, **rt.sprints.snapshot()})

    @_handler
    async def process(self, request):
        rt = self._live()
        action = (await self._body(request)).get('action')
        if action not in ('restart', 'stop'):
            raise ValueError('Неизвестное действие')
        # Сначала ответить панели, потом завершаться
        loop = asyncio.get_running_loop()
        loop.call_later(0.3, rt.request_restart if action == 'restart' else rt.request_stop)
        return _json({'ok': True})

    @_handler
    async def logs(self, request):
        after = int(request.query.get('after', 0))
        return _json({'live': self.rt is not None, 'lines': LOGS.since(after)})

    # --- конфиг ---

    def _config_payload(self, config: Dict[str, Any]) -> Dict[str, Any]:
        return {
            'config': mask_config(config),
            'defaults': defaults(),
            'models': MODELS, 'voices': VOICES,
            'mtime': os.path.getmtime(CONFIG_FILE) if os.path.exists(CONFIG_FILE) else None,
            'live': self.rt is not None,
        }

    @_handler
    async def get_config(self, request):
        return _json(self._config_payload(load_config()))

    @_handler
    async def put_config(self, request):
        body = await self._body(request)
        config = load_config()
        known = defaults()
        for path in [*(body.get('set') or {}), *(body.get('unset') or [])]:
            if path.split('.', 1)[0] not in known:
                raise ValueError('Неизвестная настройка FocusWithAI.')
        for path, value in (body.get('set') or {}).items():
            if path in SECRET_KEYS and value == MASK:
                continue
            set_path(config, path, value)
        for path in body.get('unset') or []:
            del_path(config, path)
        write_config(config)
        logging.info(f"[PANEL] config.json сохранён: {', '.join([*(body.get('set') or {}), *(body.get('unset') or [])])}")
        return _json(self._config_payload(config))

    @_handler
    async def audio_packs(self, request):
        alarms, voices = await asyncio.gather(asyncio.to_thread(self._alarms.catalog.list_packs),
                                             asyncio.to_thread(self._voicepacks.catalog.list_packs))
        return _json({'alarm': alarms, 'voice': voices, 'events': [event.value for event in Event]})

    def _pack_engine(self, kind):
        if kind == 'alarm':
            return self._alarms
        if kind == 'voice':
            return self._voicepacks
        raise ValueError('Выбери тип пакета: alarm или voice')

    @_handler
    async def audio_preview(self, request):
        body = await self._body(request)
        engine = self._pack_engine(body['kind'])
        default = 'alarm_volume' if body['kind'] == 'alarm' else 'voicepack_volume'
        try:
            await engine.preview(body['pack'], Event(body['event']), body['file'],
                                 body.get('volume', VOICEPACK_DEFAULTS[default]))
        except PlaybackStopped:
            return _json({'ok': True, 'stopped': True})
        except Exception as error:
            return _json({'ok': False, 'error': f'Не удалось прослушать файл: {error}'}, 400)
        return _json({'ok': True})

    @_handler
    async def audio_import(self, request):
        query = request.query
        engine = self._pack_engine(query['kind'])
        if request.content_length and request.content_length > MAX_CLIP_BYTES:
            raise ValueError('Аудиофайл превышает 16 МиБ')
        data = await request.read()
        try:
            imported = await asyncio.to_thread(save_clip, engine.catalog, query['pack'], Event(query['event']),
                                               query['filename'], data)
        except Exception as error:
            return _json({'ok': False, 'error': f'Не удалось импортировать файл: {error}'}, 400)
        return _json({'ok': True, **imported})

    @_handler
    async def audio_stop(self, request):
        if self.rt and self.rt.voicepack:
            self.rt.stop_audio()
        else:
            import pygame
            self._player.stop()
            if pygame.mixer.get_init():
                pygame.mixer.stop()
        return _json({'ok': True})

    @_handler
    async def test_gemini_live(self, request):
        transport = GeminiLiveTransport(self._draft(await self._body(request)))
        try:
            await asyncio.wait_for(transport.connect('Connection check.'), 12)
            return _json({'model': transport.model, 'ok': True})
        except LiveSetupError:
            raise
        except Exception as error:
            return _json({'error': f'Нет соединения с Gemini ({type(error).__name__}).'}, 502)
        finally:
            await transport.close()

async def start_panel(runtime, config: Dict[str, Any]) -> Optional[web.AppRunner]:
    """Запускает панель на 127.0.0.1. Возвращает runner для остановки или None"""
    port = int(config.get('panel_port', DEFAULT_PORT))
    runner = web.AppRunner(Panel(runtime).app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, '127.0.0.1', port).start()
    except OSError as e:
        logging.error(f"[PANEL] Веб-панель не запущена, порт {port} занят: {e}")
        await runner.cleanup()
        return None
    logging.info('[Dashboard] http://127.0.0.1:%s', port)
    return runner

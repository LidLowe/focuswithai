"""Single-instance Windows supervisor for the local model, backend, and tray."""

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import time
import webbrowser
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from startup_manager import ROOT, STARTUP_DEFAULTS, startup_delay
from config_store import load_config as load_settings
from live_backends import live_factory


RUNTIME = ROOT / '.runtime'
AI_PORT = 18080
MODEL_ID = 'qwen2.5-1.5b-instruct'
RESTART_CODE = 75
MAX_RETRIES = 2


def load_config(root):
    return {**STARTUP_DEFAULTS, **load_settings(root / 'config.json')}


def local_endpoint(config):
    if live_factory(config):
        return None
    provider = config.get('ai_provider', 'llama')
    if provider == 'openai':
        url = config.get('openai_base_url', '').rstrip('/')
        target = config.get('openai_model', '')
        key = config.get('openai_api_key', '')
    elif provider == 'llama':
        completion = config.get('llama_url', 'http://127.0.0.1:8080/completion').rstrip('/')
        url = completion.removesuffix('/completion') + '/v1'
        target = ''  # The raw completion provider uses the server's loaded model.
        key = config.get('api_key', '')
    else:
        return None
    parsed = urlsplit(url)
    if (parsed.scheme not in ('http', 'https') or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
            or parsed.username is not None or parsed.query or parsed.fragment):
        return None
    bundled = (config.get('activity_provider_mode') != 'lmstudio_stream'
               and parsed.scheme == 'http' and parsed.port == AI_PORT and parsed.path == '/v1' and parsed.hostname != '::1')
    return url, target, key, bundled


def model_is_available(data, wanted):
    if not isinstance(data, dict) or not isinstance(data.get('data'), list):
        return False
    ids = {entry['id'] for entry in data['data'] if isinstance(entry, dict) and isinstance(entry.get('id'), str)}
    return wanted in ids if wanted else bool(ids)


async def request(session, url, *, data=None, headers=None):
    try:
        async with session.request('GET' if data is None else 'POST', url, json=data,
                                   headers=headers, allow_redirects=False) as response:
            try:
                payload = await response.json(content_type=None)
            except (ValueError, aiohttp.ClientError):
                payload = None
            return response.status, payload
    except (OSError, aiohttp.ClientError, asyncio.TimeoutError):
        return 0, None


async def probe_external(session, config):
    if config.get('ai_provider') != 'openai' or not config.get('activity_allow_remote'):
        return 'внешний'
    base = config.get('openai_base_url', '').rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
        return 'ошибка URL'
    key = config.get('openai_api_key')
    headers = {'Authorization': f'Bearer {key}'} if key else None
    code, payload = await request(session, base + '/models', headers=headers)
    if code == 200:
        return 'готов' if model_is_available(payload, config.get('openai_model')) else 'модель не найдена'
    return {0: 'недоступен', 401: 'ошибка доступа', 403: 'ошибка доступа', 429: 'лимит API'}.get(code, 'ошибка API')


class ChildProcess:
    def __init__(self, handle, pid, job):
        self.handle, self.pid = handle, pid
        self.job = job
        self.code = None

    def wait(self, timeout=None):
        import win32event
        import win32process

        if self.code is not None:
            return self.code
        result = win32event.WaitForSingleObject(self.handle, win32event.INFINITE if timeout is None else int(timeout * 1000))
        if result == win32event.WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(self.pid, timeout)
        self.code = win32process.GetExitCodeProcess(self.handle)
        return self.code

    def poll(self):
        try:
            return self.wait(0)
        except subprocess.TimeoutExpired:
            return None

    @property
    def returncode(self):
        return self.poll()

    def terminate(self):
        import win32job

        win32job.TerminateJobObject(self.job, 1)

    kill = terminate

    def contains(self, pid):
        import win32api
        import win32job

        if self.poll() is not None:
            return False
        try:
            process = win32api.OpenProcess(0x1000, False, pid)
            try:
                return win32job.IsProcessInJob(process, self.job)
            finally:
                process.Close()
        except win32api.error:
            return False

    def close(self):
        self.job.Close()
        self.handle.Close()


class OwnedJob:
    """Owns a separate job for each spawned process tree."""

    def __init__(self):
        self.children = []

    @staticmethod
    def _job():
        import win32job

        handle = win32job.CreateJobObject(None, '')
        limits = win32job.QueryInformationJobObject(handle, win32job.JobObjectExtendedLimitInformation)
        limits['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(handle, win32job.JobObjectExtendedLimitInformation, limits)
        return handle

    def spawn(self, command, *, cwd, env, creationflags=0, stdout=None, stderr=None):
        import msvcrt
        import win32api
        import win32con
        import win32job
        import win32process

        for child in self.children[:]:
            if child.poll() is not None:
                child.close()
                self.children.remove(child)
        with ExitStack() as streams:
            info = win32process.STARTUPINFO()
            if stdout is not None:
                if stderr != subprocess.STDOUT:
                    raise ValueError('Owned process logs redirect both output streams together.')
                stdin = streams.enter_context(open(os.devnull, 'rb'))
                owner = win32api.GetCurrentProcess()

                def inherit(file):
                    handle = win32api.DuplicateHandle(owner, msvcrt.get_osfhandle(file.fileno()), owner,
                                                      0, True, win32con.DUPLICATE_SAME_ACCESS)
                    streams.callback(handle.Close)
                    return handle

                info.dwFlags |= win32con.STARTF_USESTDHANDLES
                info.hStdInput = inherit(stdin)
                info.hStdOutput = info.hStdError = inherit(stdout)
            flags = creationflags | win32con.CREATE_SUSPENDED | win32con.CREATE_UNICODE_ENVIRONMENT
            job = self._job()
            handle, thread, pid, _ = win32process.CreateProcess(command[0], subprocess.list2cmdline(command),
                None, None, stdout is not None, flags, env, str(cwd), info)
        process = ChildProcess(handle, pid, job)
        try:
            win32job.AssignProcessToJobObject(job, handle)
            # The virtual environment's redirector can spawn the real Python process immediately.
            win32process.ResumeThread(thread)
        except Exception:
            win32api.TerminateProcess(handle, 1)
            process.wait(timeout=3)
            process.close()
            raise
        finally:
            thread.Close()
        self.children.append(process)
        return process

    def close(self):
        for process in self.children:
            process.close()
        self.children.clear()


class StartupOrchestrator:
    def __init__(self, root, config, session, *, tray_factory, job, open_dashboard, delay):
        self.root = root
        self.runtime = root / '.runtime'
        self.config = config
        self.session = session
        self.job = job
        self.open_dashboard = open_dashboard
        self.delay = delay
        self.panel = f"http://127.0.0.1:{int(config.get('panel_port', 8765))}"
        self.endpoint = local_endpoint(config)
        self.exit = asyncio.Event()
        self.tray = tray_factory(self.command)
        self.backend = None
        self.llm = None
        self.llm_attempts = 0
        self.llm_health = 'проверка'
        self.backend_status = None
        self.restart_requested = False
        self.commands = set()

    async def wait(self, seconds):
        try:
            await asyncio.wait_for(self.exit.wait(), seconds)
        except asyncio.TimeoutError:
            pass
        return not self.exit.is_set()

    def command(self, name):
        if name == 'exit':
            self.exit.set()
            return
        task = asyncio.create_task(self._command(name))
        self.commands.add(task)
        task.add_done_callback(self.commands.discard)

    async def _command(self, name):
        try:
            if name in ('dashboard', 'settings'):
                await asyncio.to_thread(webbrowser.open, self.panel + ('/#/startup' if name == 'settings' else '/#/sprints'))
            elif name == 'restart':
                self.restart_requested = True
            elif name == 'resume' and not ((await self.status() or {}).get('sprint')):
                await asyncio.to_thread(webbrowser.open, self.panel + '/#/sprints')
            else:
                action = 'focus_hud' if name == 'hud' else name
                if not await self.send('control', {'action': action}):
                    logging.warning('[Startup] Backend did not accept %s', action)
        except Exception:
            logging.exception('[Startup] Tray action %s failed', name)

    async def _pump(self):
        while not self.exit.is_set():
            self.tray.pump()
            if not self.tray.added:
                self.tray.update(*self.tray.status)
            await self.wait(.2 if self.tray.added else 1)

    async def probe_llm(self):
        if self.config.get('activity_provider_mode') == 'gemini_live':
            if not self.config.get('google_assistant_api_key') or not self.config.get('activity_allow_remote'):
                return 'проверь настройки Gemini'
            status = ((self.backend_status or {}).get('focus') or {}).get('live_engine') or {}
            if status.get('state') == 'ERROR':
                return 'ошибка Gemini'
            return 'готов' if status.get('state') in ('READY', 'ANALYZING', 'SPEAKING') else 'внешний'
        if live_factory(self.config):
            return 'готов'
        if self.endpoint is None:
            return await probe_external(self.session, self.config)
        url, wanted, key, bundled = self.endpoint
        headers = {'Authorization': f'Bearer {key}'} if key else None
        if bundled:
            code, _ = await request(self.session, url.removesuffix('/v1') + '/health', headers=headers)
            if code not in (200, 404, 405):
                return 'загрузка' if code == 503 else 'недоступен' if not code else 'ошибка health'
        code, data = await request(self.session, url + '/models', headers=headers)
        if code in (401, 403):
            return 'ошибка доступа'
        if code == 200:
            return 'готов' if model_is_available(data, wanted) else 'модель не найдена'
        return 'недоступен' if not code else 'ошибка API'

    async def port_occupied(self):
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', AI_PORT), .5)
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return True

    def _launch_llm(self):
        server = self.runtime / 'llama' / 'llama-server.exe'
        model = self.runtime / 'models' / f'{MODEL_ID}-q4_k_m.gguf'
        if not server.is_file() or not model.is_file():
            raise FileNotFoundError('Локальные файлы llama.cpp или GGUF отсутствуют.')
        key = self.endpoint[2]
        if not key:
            raise ValueError('Для локальной сборки нужен сохранённый ключ API.')
        with (self.runtime / 'llama-server.log').open('ab') as log:
            return self.job.spawn([str(server), '-m', str(model), '--alias', MODEL_ID,
                '--host', '127.0.0.1', '--port', str(AI_PORT), '-ngl', '0', '-c', '2048', '-t', '4',
                '--parallel', '1', '--no-ui', '--cors-origins', self.panel, '--no-cors-credentials'],
                cwd=self.root, stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW,
                env={**os.environ, 'LLAMA_API_KEY': key})

    async def ensure_llm(self):
        self.llm_health = await self.probe_llm()
        if self.llm_health in ('готов', 'внешний') or not self.endpoint:
            return
        _, wanted, _, bundled = self.endpoint
        if not bundled or not self.config['startup_llm_auto'] or (wanted and wanted != MODEL_ID):
            return
        if await self.port_occupied():
            # A healthy external server may have started just before this supervisor.
            if self.llm_health == 'загрузка':
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline and await self.wait(.5):
                    self.llm_health = await self.probe_llm()
                    if self.llm_health != 'загрузка':
                        break
            return
        maximum = 1 + MAX_RETRIES if self.config['startup_recover'] else 1
        while self.llm_attempts < maximum and not self.exit.is_set():
            self.llm_attempts += 1
            if self.llm_attempts > 1 and not await self.wait(min(4, 2 ** (self.llm_attempts - 2))):
                return
            if await self.port_occupied():
                self.llm_health = await self.probe_llm()
                return
            try:
                self.llm = self._launch_llm()
            except Exception:
                self.llm_health = 'ошибка запуска'
                logging.exception('[Startup] Failed to start the bundled LLM')
                continue
            deadline = time.monotonic() + 75
            self.llm_health = 'загрузка'
            if not self.backend_status:
                self.tray.update('loading', 'загрузка модели', self.llm_health)
            while not self.exit.is_set() and self.llm.poll() is None and time.monotonic() < deadline:
                self.llm_health = await self.probe_llm()
                if self.llm_health == 'готов':
                    return
                if self.llm_health in ('ошибка доступа', 'модель не найдена') or not await self.wait(.5):
                    break
            logging.warning('[Startup] LLM failed its readiness check (attempt %d)', self.llm_attempts)
            await self.stop_child(self.llm)
            self.llm = None
        if self.llm_health in ('загрузка', 'недоступен'):
            self.llm_health = 'недоступен'

    def _launch_backend(self):
        hidden = self.config['startup_start_minimized']
        python = Path(sys.executable).with_name('pythonw.exe' if hidden else 'python.exe')
        options = {'creationflags': 0 if hidden else subprocess.CREATE_NEW_CONSOLE}
        env = {**os.environ, 'WORKREMINDER_CHILD': '1', 'FOCUSWITHAI_MANAGED': '1',
               'PYTHONUTF8': '1', 'PYTHONUNBUFFERED': '1'}
        if hidden:
            with (self.runtime / 'backend.log').open('ab') as log:
                return self.job.spawn([str(python), 'main.py'], cwd=self.root, env=env,
                                      stdout=log, stderr=subprocess.STDOUT, **options)
        return self.job.spawn([str(python), 'main.py'], cwd=self.root, env=env, **options)

    async def status(self):
        code, data = await request(self.session, self.panel + '/api/status')
        if (code == 200 and isinstance(data, dict) and data.get('live') is True and self.backend
                and type(data.get('pid')) is int and self.backend.contains(data['pid']) and data.get('startup_managed')):
            return data
        return None

    async def send(self, category, body):
        if not self.backend or self.backend.poll() is not None or not await self.status():
            return False
        code, response = await request(self.session, self.panel + '/api/' + category, data=body,
                                       headers={'X-Panel': '1'})
        return code == 200 and isinstance(response, dict) and response.get('ok') is True

    async def start_backend(self):
        self.backend_status = None
        try:
            self.backend = self._launch_backend()
        except Exception:
            logging.exception('[Startup] Failed to spawn the backend')
            return False
        deadline = time.monotonic() + 25
        released = False
        while not self.exit.is_set() and time.monotonic() < deadline and self.backend.poll() is None:
            state = await self.status()
            if state and state.get('startup_ready'):
                if state.get('startup_running'):
                    self.backend_status = state
                    return True
                if not released:
                    released = await self.send('control', {'action': 'startup_release'})
            if not await self.wait(.25):
                break
        logging.warning('[Startup] Backend failed its readiness check')
        await self.stop_child(self.backend)
        self.backend = None
        return False

    @staticmethod
    async def stop_child(process, grace=0):
        if process is None:
            return
        deadline = time.monotonic() + grace
        while process.poll() is None and time.monotonic() < deadline:
            await asyncio.sleep(.15)
        if process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 3)
            except subprocess.TimeoutExpired:
                process.kill()

    def _update_tray(self):
        state = self.backend_status
        if not state:
            self.tray.update('error' if self.backend is None else 'loading',
                             'backend недоступен' if self.backend is None else 'запуск backend', self.llm_health)
            return
        limited = self.llm_health not in ('готов', 'внешний')
        sprint = (state.get('sprint') or {}).get('state')
        label = {'BREAK': 'перерыв', 'PAUSED': 'возобнови спринт', 'ACTIVE': 'спринт идёт'}.get(sprint, 'начни спринт')
        self.tray.update('limited' if limited else 'ready' if sprint == 'ACTIVE' else 'paused', label, self.llm_health)

    async def reload_model_settings(self):
        updated = load_config(self.root)
        endpoint = local_endpoint(updated)
        changed = (self.endpoint != endpoint or
                   self.config.get('activity_provider_mode') != updated.get('activity_provider_mode'))
        self.config = updated
        if changed:
            if self.llm:
                await self.stop_child(self.llm)
                self.llm = None
            self.endpoint = endpoint
            self.llm_attempts = 0
            await self.ensure_llm()

    async def run(self):
        pump = asyncio.create_task(self._pump())
        llm_recovery = None
        failures = 0
        opened = False
        exhausted = False
        try:
            if self.delay and not await self.wait(self.delay):
                return 0
            await self.ensure_llm()
            next_probe = 0.0
            next_llm = 0.0
            while not self.exit.is_set():
                reconfigure = False
                if self.restart_requested:
                    self.restart_requested = False
                    if self.backend and self.backend.poll() is None:
                        released = await self.send('process', {'action': 'restart'})
                        await self.stop_child(self.backend, 6 if released else 0)
                    self.backend = None
                    failures, exhausted = 0, False
                    reconfigure = True
                elif self.backend and self.backend.poll() is not None:
                    code = self.backend.returncode
                    self.backend = None
                    self.backend_status = None
                    if code == 0:
                        break  # Explicit Exit from the web panel.
                    if code == RESTART_CODE:
                        failures = 0
                        reconfigure = True
                    else:
                        failures += 1
                        logging.warning('[Startup] Backend exited with code %d', code)
                        exhausted = failures > (MAX_RETRIES if self.config['startup_recover'] else 0)

                if reconfigure:
                    if llm_recovery:
                        llm_recovery.cancel()
                        await asyncio.gather(llm_recovery, return_exceptions=True)
                        llm_recovery = None
                    await self.reload_model_settings()
                if self.backend is None and not exhausted:
                    self.tray.update('loading', 'запуск backend', self.llm_health)
                    if failures and not await self.wait(min(4, 2 ** (failures - 1))):
                        break
                    if not await self.start_backend():
                        failures += 1
                        exhausted = failures > (MAX_RETRIES if self.config['startup_recover'] else 0)
                    elif self.open_dashboard and not opened and not self.exit.is_set():
                        self.command('dashboard')
                        opened = True

                if llm_recovery and llm_recovery.done():
                    await llm_recovery
                    llm_recovery = None
                if self.llm and self.llm.poll() is not None and llm_recovery is None:
                    self.llm = None
                    llm_recovery = asyncio.create_task(self.ensure_llm())
                now = time.monotonic()
                if now >= next_probe:
                    self.backend_status = await self.status()
                    next_probe = now + 2
                if now >= next_llm and llm_recovery is None:
                    self.llm_health = await self.probe_llm()
                    next_llm = now + 8
                self._update_tray()
                await self.wait(.25)
            return 0 if not exhausted else 1
        finally:
            self.exit.set()
            if llm_recovery:
                llm_recovery.cancel()
                await asyncio.gather(llm_recovery, return_exceptions=True)
            if self.commands:
                for task in self.commands:
                    task.cancel()
                await asyncio.gather(*self.commands, return_exceptions=True)
            graceful = await self.send('process', {'action': 'stop'})
            await self.stop_child(self.backend, 8 if graceful else 0)
            await self.stop_child(self.llm)
            await pump
            self.tray.close()


async def launch(config, args):
    from startup_tray import StartupTray

    panel = f"http://127.0.0.1:{int(config.get('panel_port', 8765))}"
    want_browser = not args.no_browser and (config['startup_open_dashboard'] if args.scheduled else not args.background)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.35)) as session:
        code, data = await request(session, panel + '/api/status')
        if code == 200 and isinstance(data, dict) and data.get('live') is True:
            if want_browser:
                await asyncio.to_thread(webbrowser.open, panel + '/#/sprints')
            return 0
        job = OwnedJob()
        try:
            return await StartupOrchestrator(ROOT, config, session, tray_factory=StartupTray, job=job,
                                             open_dashboard=want_browser,
                                             delay=startup_delay(config) if args.scheduled else 0).run()
        finally:
            job.close()


async def open_running_dashboard(config):
    panel = f"http://127.0.0.1:{int(config.get('panel_port', 8765))}"
    deadline = time.monotonic() + 30
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.35)) as session:
        while time.monotonic() < deadline:
            code, state = await request(session, panel + '/api/status')
            if (code == 200 and isinstance(state, dict) and state.get('live')
                    and (not state.get('startup_managed') or state.get('startup_running'))):
                await asyncio.to_thread(webbrowser.open, panel + '/#/sprints')
                return 0
            await asyncio.sleep(.5)
    logging.warning('[Startup] Existing instance did not make its dashboard ready')
    return 1


def main():
    import msvcrt

    parser = argparse.ArgumentParser()
    parser.add_argument('--scheduled', action='store_true')
    parser.add_argument('--background', action='store_true')
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    RUNTIME.mkdir(exist_ok=True)
    logging.basicConfig(filename=RUNTIME / 'startup.log', encoding='utf-8', level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    try:
        config = load_config(ROOT)
        with (RUNTIME / 'launch.lock').open('a+b') as lock:
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                if not (args.scheduled or args.background or args.no_browser):
                    return asyncio.run(open_running_dashboard(config))
                return 0
            return asyncio.run(launch(config, args))
    except KeyboardInterrupt:
        return 0
    except Exception:
        logging.exception('[Startup] Supervisor failed')
        return 1


if __name__ == '__main__':
    sys.exit(main())

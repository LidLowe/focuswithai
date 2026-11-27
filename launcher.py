"""Single-instance Windows supervisor for the application and tray."""

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

import aiohttp

from startup_manager import ROOT, STARTUP_DEFAULTS, startup_delay
from config_store import load_config as load_settings


RUNTIME = ROOT / '.runtime'
RESTART_CODE = 75
MAX_RETRIES = 2


def load_config(root):
    return {**STARTUP_DEFAULTS, **load_settings(root / 'config.json')}


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
        self.exit = asyncio.Event()
        self.tray = tray_factory(self.command)
        self.backend = None
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
                await asyncio.to_thread(webbrowser.open, self.panel + ('/#/startup' if name == 'settings' else '/#/current'))
            elif name == 'restart':
                self.restart_requested = True
            elif name == 'resume' and not ((await self.status() or {}).get('sprint')):
                await asyncio.to_thread(webbrowser.open, self.panel + '/#/current')
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

    def _launch_backend(self):
        hidden = self.config['startup_start_minimized']
        python = Path(sys.executable).with_name('pythonw.exe' if hidden else 'python.exe')
        options = {'creationflags': 0 if hidden else subprocess.CREATE_NEW_CONSOLE}
        env = {**os.environ, 'FOCUSWITHAI_MANAGED': '1',
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
                             'Dashboard недоступен' if self.backend is None else 'запуск Dashboard', '—')
            return
        live = ((state.get('focus') or {}).get('live_engine') or {}).get('connection_state', 'DISCONNECTED')
        sprint = (state.get('sprint') or {}).get('state')
        label = {'BREAK': 'перерыв', 'PAUSED': 'возобнови спринт', 'ACTIVE': 'спринт идёт'}.get(sprint, 'начни спринт')
        icon = 'limited' if live == 'ERROR' else 'ready' if sprint == 'ACTIVE' else 'paused'
        self.tray.update(icon, label, live)

    async def run(self):
        pump = asyncio.create_task(self._pump())
        failures = 0
        opened = False
        exhausted = False
        try:
            if self.delay and not await self.wait(self.delay):
                return 0
            next_probe = 0.0
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
                    self.config = load_config(self.root)
                if self.backend is None and not exhausted:
                    self.tray.update('loading', 'запуск Dashboard', '—')
                    if failures and not await self.wait(min(4, 2 ** (failures - 1))):
                        break
                    if not await self.start_backend():
                        failures += 1
                        exhausted = failures > (MAX_RETRIES if self.config['startup_recover'] else 0)
                    elif self.open_dashboard and not opened and not self.exit.is_set():
                        self.command('dashboard')
                        opened = True

                now = time.monotonic()
                if now >= next_probe:
                    self.backend_status = await self.status()
                    next_probe = now + 2
                self._update_tray()
                await self.wait(.25)
            return 0 if not exhausted else 1
        finally:
            self.exit.set()
            if self.commands:
                for task in self.commands:
                    task.cancel()
                await asyncio.gather(*self.commands, return_exceptions=True)
            graceful = await self.send('process', {'action': 'stop'})
            await self.stop_child(self.backend, 8 if graceful else 0)
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
                await asyncio.to_thread(webbrowser.open, panel + '/#/current')
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
                await asyncio.to_thread(webbrowser.open, panel + '/#/current')
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

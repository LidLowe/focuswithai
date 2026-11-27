"""Per-user Windows logon task and startup preferences."""

import hashlib
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STARTUP_DEFAULTS = {
    'startup_open_dashboard': False,
    'startup_start_minimized': True,
    'startup_delay_seconds': 8,
    'startup_recover': True,
}
TASK_DESCRIPTION = 'FocusWithAI interactive startup manager'
TASK_NOT_FOUND = 0x80070002
INTERACTIVE_TOKEN = 3


def startup_delay(config):
    try:
        return max(0, min(180, int(config.get('startup_delay_seconds', 8))))
    except (TypeError, ValueError, OverflowError):
        return 8


def _com(operation, *args):
    if sys.platform != 'win32':
        raise ValueError('Автозапуск через Планировщик доступен в Windows.')
    import pythoncom

    pythoncom.CoInitialize()
    try:
        return operation(*args)
    finally:
        pythoncom.CoUninitialize()


class StartupTask:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.name = 'FocusWithAI-' + hashlib.sha256(str(self.root).casefold().encode()).hexdigest()[:10]

    def _connect(self):
        if sys.platform != 'win32':
            raise ValueError('Автозапуск через Планировщик доступен в Windows.')
        import win32com.client

        service = win32com.client.Dispatch('Schedule.Service')
        service.Connect()
        return service, service.GetFolder('\\')

    def _owned(self, folder):
        import pywintypes

        try:
            task = folder.GetTask(self.name)
        except pywintypes.com_error as error:
            codes = [error.hresult]
            if error.excepinfo and isinstance(error.excepinfo[5], int):
                codes.append(error.excepinfo[5])
            if any(code & 0xFFFFFFFF == TASK_NOT_FOUND for code in codes):
                return None
            raise
        definition = task.Definition
        if (definition.RegistrationInfo.Description != TASK_DESCRIPTION or definition.Actions.Count != 1
                or Path(definition.Actions.Item(1).WorkingDirectory).resolve() != self.root):
            raise ValueError('Имя задания занято другим приложением.')
        return task

    def _legacy_link(self):
        import win32com.client

        shell = win32com.client.Dispatch('WScript.Shell')
        path = Path(shell.SpecialFolders('Startup')) / 'FocusWithAI.lnk'
        if not path.is_file():
            return None
        shortcut = shell.CreateShortcut(str(path))
        target = self.root / '.venv' / 'Scripts' / 'pythonw.exe'
        script = self.root / '.runtime' / 'launch.py'
        if (shortcut.TargetPath and Path(shortcut.TargetPath).resolve() == target
                and shortcut.Arguments.strip() == f'"{script}" --background'):
            return path
        return None

    def _archive_legacy(self, path):
        if path:
            folder = self.root / '.runtime'
            folder.mkdir(exist_ok=True)
            destination = folder / 'legacy-startup.lnk'
            number = 2
            while destination.exists():
                destination = folder / f'legacy-startup-{number}.lnk'
                number += 1
            shutil.move(path, destination)

    def status(self):
        return _com(self._status)

    def _status(self):
        _, folder = self._connect()
        task = self._owned(folder)
        return {'supported': True, 'enabled': bool(task and task.Enabled),
                'legacy': self._legacy_link() is not None, 'task': self.name}

    def set_enabled(self, enabled):
        return _com(self._set_enabled, enabled)

    def _set_enabled(self, enabled):
        import win32api
        import win32security

        service, folder = self._connect()
        prior = self._owned(folder)
        legacy = self._legacy_link()
        if enabled:
            pythonw = self.root / '.venv' / 'Scripts' / 'pythonw.exe'
            script = self.root / 'launcher.py'
            if not pythonw.is_file() or not script.is_file():
                raise ValueError('Для автозапуска нужны launcher.py и локальное окружение .venv.')
            token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
            try:
                sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
                user = win32security.ConvertSidToStringSid(sid)
            finally:
                token.Close()
            task = service.NewTask(0)
            task.RegistrationInfo.Description = TASK_DESCRIPTION
            task.Principal.UserId = user
            task.Principal.LogonType = INTERACTIVE_TOKEN
            task.Principal.RunLevel = 0  # Least privilege.
            task.Settings.Enabled = True
            task.Settings.ExecutionTimeLimit = 'PT0S'
            task.Settings.MultipleInstances = 2  # IgnoreNew.
            task.Settings.DisallowStartIfOnBatteries = False
            task.Settings.StopIfGoingOnBatteries = False
            task.Triggers.Create(9).UserId = user  # TASK_TRIGGER_LOGON.
            action = task.Actions.Create(0)  # TASK_ACTION_EXEC.
            action.Path = str(pythonw)
            action.Arguments = f'"{script}" --scheduled'
            action.WorkingDirectory = str(self.root)
            folder.RegisterTaskDefinition(self.name, task, 6, None, None, INTERACTIVE_TOKEN)  # CreateOrUpdate.
            try:
                self._archive_legacy(legacy)
            except OSError:
                if not prior:
                    folder.DeleteTask(self.name, 0)
                raise
        else:
            self._archive_legacy(legacy)
            if prior:
                prior.Enabled = False
        task = self._owned(folder)
        return {'supported': True, 'enabled': bool(task and task.Enabled), 'legacy': False, 'task': self.name}

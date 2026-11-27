"""Windows tray for the startup supervisor, independent of the backend process."""

import logging

import win32api
import win32con
import win32gui


MESSAGE = win32con.WM_APP + 1
MENU = (
    ('dashboard', 'Open Dashboard'), ('hud', 'Show / Hide HUD'),
    ('resume', 'Start Monitoring'), ('pause', 'Pause Monitoring'), ('start_break', 'Start Break'),
    ('restart', 'Restart Backend'), ('settings', 'Settings'), ('exit', 'Exit'),
)


class StartupTray:
    def __init__(self, command):
        self.command = command
        self.ready = False
        self.added = False
        self.status = ('loading', 'запуск', 'проверка')
        self.taskbar_message = win32gui.RegisterWindowMessage('TaskbarCreated')
        self.instance = win32api.GetModuleHandle(None)
        self.name = 'FocusWithAIStartupTray'
        window = win32gui.WNDCLASS()
        window.hInstance = self.instance
        window.lpszClassName = self.name
        window.lpfnWndProc = self._message
        win32gui.RegisterClass(window)
        try:
            self.handle = win32gui.CreateWindow(self.name, '', 0, 0, 0, 0, 0, 0, 0, self.instance, None)
        except Exception:
            win32gui.UnregisterClass(self.name, self.instance)
            raise
        self.update(*self.status)

    def update(self, state, backend, live):
        ready = state in ('ready', 'limited', 'paused')
        value = state, backend, live
        self.ready = ready
        if self.added and self.status == value:
            return
        self.status = value
        icons = {'ready': win32con.IDI_INFORMATION, 'limited': win32con.IDI_WARNING,
                 'error': win32con.IDI_ERROR, 'paused': win32con.IDI_APPLICATION, 'loading': win32con.IDI_APPLICATION}
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        title = f'FocusWithAI · {backend} · Gemini: {live}'[:63]
        data = (self.handle, 0, flags, MESSAGE, win32gui.LoadIcon(0, icons[state]), title)
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY if self.added else win32gui.NIM_ADD, data)
            self.added = True
        except win32gui.error:
            self.added = False  # Explorer may still be starting after sign-in.

    def _menu(self):
        popup = win32gui.CreatePopupMenu()
        try:
            for index, (key, title) in enumerate(MENU, 1):
                if key in ('hud', 'restart', 'exit'):
                    win32gui.AppendMenu(popup, win32con.MF_SEPARATOR, 0, '')
                flags = win32con.MF_STRING
                if key in ('hud', 'resume', 'pause', 'start_break') and not self.ready:
                    flags |= win32con.MF_GRAYED
                win32gui.AppendMenu(popup, flags, index, title)
            x, y = win32gui.GetCursorPos()
            win32gui.SetForegroundWindow(self.handle)
            selected = win32gui.TrackPopupMenu(popup, win32con.TPM_RETURNCMD | win32con.TPM_RIGHTBUTTON,
                                              x, y, 0, self.handle, None)
            win32gui.PostMessage(self.handle, win32con.WM_NULL, 0, 0)
            if selected:
                self.command(MENU[selected - 1][0])
        finally:
            win32gui.DestroyMenu(popup)

    def _message(self, hwnd, msg, wparam, lparam):
        try:
            if msg == MESSAGE:
                if lparam in (win32con.WM_RBUTTONUP, win32con.WM_CONTEXTMENU):
                    self._menu()
                elif lparam == win32con.WM_LBUTTONDBLCLK:
                    self.command('dashboard')
                return 0
            if msg == self.taskbar_message:
                self.added = False
                self.update(*self.status)
                return 0
            if msg == win32con.WM_CLOSE:
                self.command('exit')
                return 0
        except Exception:
            logging.exception('[Startup] Tray handler failed')
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    @staticmethod
    def pump():
        win32gui.PumpWaitingMessages()

    def close(self):
        if self.added:
            try:
                win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.handle, 0))
            except win32gui.error:
                pass
        win32gui.DestroyWindow(self.handle)
        win32gui.UnregisterClass(self.name, self.instance)

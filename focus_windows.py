"""Windows foreground sampling and local, reversible foreground restrictions."""

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

import win32api
import win32con
import win32gui
import win32process

from focus_meter import Foreground


BROWSERS = {'chrome.exe', 'msedge.exe'}
PROTECTED_EXES = BROWSERS | {
    'firefox.exe', 'brave.exe', 'opera.exe', 'vivaldi.exe', 'python.exe', 'pythonw.exe',
    'explorer.exe', 'taskmgr.exe', 'dwm.exe', 'winlogon.exe',
    'windowsterminal.exe', 'cmd.exe', 'powershell.exe', 'pwsh.exe',
    'searchhost.exe', 'shellexperiencehost.exe', 'startmenuexperiencehost.exe',
    'systemsettings.exe', 'applicationframehost.exe', 'securityhealthsystray.exe',
}
SYSTEM_ROOT = Path(os.environ.get('SystemRoot', r'C:\Windows')).resolve()
APP_ROOT = Path(__file__).resolve().parent

_image_name = ctypes.windll.kernel32.QueryFullProcessImageNameW
_image_name.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
_image_name.restype = wintypes.BOOL
_minimize = ctypes.windll.user32.ShowWindowAsync
_minimize.argtypes = (wintypes.HWND, ctypes.c_int)
_minimize.restype = wintypes.BOOL


def foreground():
    handle = win32gui.GetForegroundWindow()
    if not handle:
        return Foreground()
    try:
        _, pid = win32process.GetWindowThreadProcessId(handle)
        title = win32gui.GetWindowText(handle)
    except win32gui.error:
        return Foreground()
    return Foreground(handle, pid, title, executable_for_pid(pid))


def executable_for_pid(pid):
    path = ''
    try:
        process = win32api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        try:
            length = wintypes.DWORD(2048)
            buffer = ctypes.create_unicode_buffer(length.value)
            if _image_name(int(process), 0, buffer, ctypes.byref(length)):
                path = buffer.value
        finally:
            process.Close()
    except win32api.error:
        pass
    return path


def app_path(value):
    if not isinstance(value, str):
        return None
    path = Path(value.strip().strip('"'))
    if not path.is_absolute() or not path.drive or path.drive.startswith('\\') or path.suffix.lower() != '.exe':
        return None
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return None
    if (resolved.name.lower() in PROTECTED_EXES or resolved.is_relative_to(SYSTEM_ROOT)
            or resolved.is_relative_to(APP_ROOT)):
        return None
    return os.path.normcase(str(resolved))


def minimize_if_still_foreground(expected):
    current = foreground()
    if current == expected and current.handle and current.executable and not win32gui.IsIconic(current.handle):
        return bool(_minimize(current.handle, win32con.SW_MINIMIZE))
    return False


def browser_address(expected):
    """Read Chrome/Edge's own toolbar, never an input field in the page."""
    if not expected.handle or Path(expected.executable).name.lower() not in BROWSERS:
        return ''
    import comtypes
    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.uia_element_info import UIAElementInfo

    comtypes.CoInitializeEx(0)  # COINIT_MULTITHREADED, the default for pywinauto UIA.
    try:
        if foreground() != expected:
            return ''
        root = UIAElementInfo(expected.handle)
        # Search only browser chrome, skipping page documents and tab contents.
        nodes = [root]
        names = {'address and search bar', 'search or enter web address', 'address bar',
                 'адресная строка', 'адресная строка и строка поиска', 'адрес и поисковый запрос'}
        for _ in range(12):
            children = []
            for node in nodes[:96]:
                if node.control_type == 'Edit' and not node.element.CurrentIsOffscreen and (
                    node.automation_id == 'addressEditBox' or node.class_name == 'OmniboxViewViews'
                    or (node.name or '').casefold() in names
                    or (node.element.CurrentAcceleratorKey or '').casefold() == 'ctrl+l'
                ):
                    if node.element.CurrentHasKeyboardFocus:
                        return ''
                    return UIAWrapper(node).iface_value.CurrentValue or ''
                if node.control_type in ('Window', 'Pane', 'ToolBar', 'Group', 'Custom'):
                    children.extend(node.children())
            nodes = children
            if not nodes:
                break
    finally:
        comtypes.CoUninitialize()
    return ''

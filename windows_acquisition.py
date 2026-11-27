"""Window-only capture, Windows app inventory and GSMTC metadata."""

import ctypes
import io
import ntpath
import os
import time
from ctypes import wintypes

import win32gui
import win32process
from pywintypes import error as WinError

from activity_context import BROWSERS, Frame, PRIVATE_TEXT, clean
from focus_windows import browser_address, foreground


_affinity = ctypes.windll.user32.GetWindowDisplayAffinity
_affinity.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))


def set_capture_exclusion(hwnd):
    api = ctypes.windll.user32.SetWindowDisplayAffinity
    api.argtypes = (wintypes.HWND, wintypes.DWORD)
    api(hwnd, 0x11)


def sensitive_controls(hwnd):
    import comtypes
    comtypes.CoInitializeEx(0)
    try:
        import comtypes.client
        import comtypes.gen

        definitions = comtypes.client.GetModule('UIAutomationCore.dll')
        interfaces = comtypes.gen.UIAutomationClient
        automation = comtypes.client.CreateObject(interfaces.CUIAutomation, interface=interfaces.IUIAutomation)
        focused = automation.GetFocusedElement()
        if not focused or focused.CurrentIsPassword:
            return True
        root = automation.ElementFromHandle(hwnd)
        property_id = definitions.UIA_IsPasswordPropertyId
        condition = automation.CreatePropertyCondition(property_id, True)
        return bool(root.FindFirst(4, condition))
    finally:
        comtypes.CoUninitialize()


def capture_window(window, dimensions, quality, browser_check):
    from PIL import ImageGrab

    affinity = wintypes.DWORD()
    if foreground() != window or win32gui.IsIconic(window.handle):
        return None, 'foreground_changed'
    if _affinity(window.handle, ctypes.byref(affinity)) and affinity.value:
        return None, 'protected_window'

    browser = ntpath.basename(window.executable).casefold() in BROWSERS
    if browser:
        address = browser_address(window, for_capture=True)
        if not browser_check(address):
            return None, 'browser_unverified'
    if sensitive_controls(window.handle):
        return None, 'sensitive_controls'
    left, top, right, bottom = win32gui.GetWindowRect(window.handle)
    if not 0 < (right - left) * (bottom - top) <= 20_000_000:
        return None, 'invalid_dimensions'
    image = ImageGrab.grab(window=window.handle)
    if foreground() != window:
        return None, 'foreground_changed'
    # Tabs can navigate without changing the native title or window handle.
    if browser and browser_address(window, for_capture=True) != address:
        return None, 'browser_changed'
    image.thumbnail(dimensions)
    if image.width < 40 or image.height < 40:
        return None, 'invalid_dimensions'
    gray = image.convert('L').resize((48, 32))
    low, high = gray.getextrema()
    if high - low < 8:
        return None, 'blank_frame'
    output = io.BytesIO()
    image.convert('RGB').save(output, 'JPEG', quality=quality)
    return Frame(output.getvalue(), gray.tobytes(), image.width, image.height, time.monotonic()), None


def user_apps():
    """Return visible taskbar-style window PIDs and names, with no window text."""
    found = {}

    def add(hwnd, unused):
        if len(found) >= 64:
            return True
        try:
            if not win32gui.IsWindowVisible(hwnd) or win32gui.GetWindow(hwnd, 4):
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid in (0, os.getpid()) or pid in found:
                return True
            from focus_windows import executable_for_pid
            path = executable_for_pid(pid)
            if path:
                found[pid] = ntpath.basename(path)
        except (OSError, WinError):
            pass
        return True

    win32gui.EnumWindows(add, None)
    return sorted(set(found.values()), key=str.casefold)


class MediaMonitor:
    def __init__(self):
        self.manager = None

    async def sample(self):
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager

        if self.manager is None:
            self.manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
        session = self.manager.get_current_session()
        if not session:
            return None
        player = session.source_app_user_model_id
        info = session.get_playback_info()
        props = await session.try_get_media_properties_async()
        # Browser media can include private tabs. Keep playback state only.
        private_player = any(token in player.casefold() for token in ('chrome', 'edge', 'firefox', 'brave', 'browser', 'opera', 'vivaldi'))
        title = props.title if props and not private_player else ''
        artist = props.artist if props and not private_player else ''
        if PRIVATE_TEXT.search(title + ' ' + artist):
            return None
        return {'player': clean(player, 100), 'state': info.playback_status.name if info else 'UNKNOWN',
                'title': clean(title), 'artist': clean(artist)}

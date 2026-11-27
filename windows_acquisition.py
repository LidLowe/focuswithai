"""Window-only capture, Windows app inventory and GSMTC metadata."""

import ctypes
import io
import ntpath
import os
import time
from _ctypes import COMError
from contextlib import contextmanager, suppress
from ctypes import wintypes
from dataclasses import replace
from hashlib import blake2s

import win32gui
import win32process
import win32ui
from pywintypes import error as WinError

from activity_context import Frame, Privacy, PRIVATE_TEXT, clean
from focus_windows import foreground


_affinity = ctypes.windll.user32.GetWindowDisplayAffinity
_affinity.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_print_window = ctypes.windll.user32.PrintWindow
_print_window.argtypes = (wintypes.HWND, wintypes.HDC, wintypes.UINT)
_print_window.restype = wintypes.BOOL
_thread_dpi = ctypes.windll.user32.SetThreadDpiAwarenessContext
_thread_dpi.argtypes = (ctypes.c_void_p,)
_thread_dpi.restype = ctypes.c_void_p
PW_RENDERFULLCONTENT = 2
PER_MONITOR_AWARE_V2 = -4


def set_capture_exclusion(hwnd):
    api = ctypes.windll.user32.SetWindowDisplayAffinity
    api.argtypes = (wintypes.HWND, wintypes.DWORD)
    api(hwnd, 0x11)


@contextmanager
def window_automation(hwnd):
    import comtypes
    from comtypes.client import CreateObject, GetModule

    initialized = False
    try:
        comtypes.CoInitializeEx(0)
        initialized = True
    except OSError as error:
        # UI Automation can also run on an existing STA.
        if getattr(error, 'winerror', None) != -2147417850:  # RPC_E_CHANGED_MODE
            raise
    try:
        api = GetModule('UIAutomationCore.dll')
        automation = CreateObject(api.CUIAutomation, interface=api.IUIAutomation)
        yield automation, automation.ElementFromHandle(hwnd), api
    finally:
        if initialized:
            comtypes.CoUninitialize()


def sensitive_controls(automation, root, api):
    focused = automation.GetFocusedElement()
    if not focused or not root or focused.CurrentIsPassword:
        return True
    password = automation.CreatePropertyCondition(api.UIA_IsPasswordPropertyId, True)
    return bool(root.FindFirst(4, password))


def browser_page(automation, root, api, check_address, *, wake_document=False):
    """Inspect Chromium's own tab/profile controls and renderer, without requiring an omnibox value."""
    def find(property_id, value):
        return root.FindFirst(4, automation.CreatePropertyCondition(property_id, value))

    browser = find(api.UIA_ClassNamePropertyId, 'BrowserRootView')
    avatar = find(api.UIA_ClassNamePropertyId, 'AvatarToolbarButton')
    if not browser or not avatar or not avatar.CurrentName:
        return None, 'browser_mode_unverified'
    if Privacy.sensitive_page(' '.join((browser.CurrentName or '', avatar.CurrentName))):
        return None, 'private_or_sensitive_window'

    location = find(api.UIA_ClassNamePropertyId, 'LocationIconView')
    regular_site = ('view site information', 'сведения о сайте', 'информация о сайте')
    if not location or (location.CurrentName or '').casefold() not in regular_site:
        return None, 'browser_mode_unverified'

    tab_type = automation.CreatePropertyCondition(api.UIA_ControlTypePropertyId, api.UIA_TabItemControlTypeId)
    selected = automation.CreatePropertyCondition(api.UIA_SelectionItemIsSelectedPropertyId, True)
    tab = root.FindFirst(4, automation.CreateAndCondition(tab_type, selected))
    document = find(api.UIA_ControlTypePropertyId, api.UIA_DocumentControlTypeId)
    if tab and not document and wake_document:
        # Chrome initially exposes only its toolbar. Hit-testing can request the
        # web accessibility tree without changing focus, flags or browser settings.
        rect = browser.CurrentBoundingRectangle
        automation.ElementFromPoint(wintypes.POINT((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2))
        document = find(api.UIA_ControlTypePropertyId, api.UIA_DocumentControlTypeId)
    if not tab or not document or document.CurrentIsOffscreen:
        return None, 'browser_document_unavailable'
    if Privacy.sensitive_page(' '.join((tab.CurrentName or '', document.CurrentName or ''))):
        return None, 'sensitive_page'

    address = ''
    omnibox = find(api.UIA_ClassNamePropertyId, 'OmniboxViewViews')
    if omnibox and omnibox.CurrentHasKeyboardFocus:
        return None, 'address_bar_focused'
    if omnibox and not omnibox.CurrentIsOffscreen:
        with suppress(COMError):
            value = omnibox.GetCurrentPropertyValue(api.UIA_ValueValuePropertyId)
            if isinstance(value, str):
                address = value
    reason = check_address(address)
    if reason:
        return None, reason
    identity = blake2s(address.encode(), digest_size=16).digest()
    return (tuple(tab.GetRuntimeId()), tuple(document.GetRuntimeId()), identity), None


def window_image(hwnd, width, height):
    from PIL import Image

    window_dc = win32gui.GetWindowDC(hwnd)
    if not window_dc:
        raise OSError('GetWindowDC failed')
    bitmap = None
    try:
        source = win32ui.CreateDCFromHandle(window_dc)
        target = source.CreateCompatibleDC()
        try:
            bitmap = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(source, width, height)
            target.SelectObject(bitmap)
            # Capture the window's rendered surface, including accelerated Chrome
            # content. Reading its window DC directly can return a blank bitmap.
            if not _print_window(hwnd, target.GetSafeHdc(), PW_RENDERFULLCONTENT):
                raise OSError('PrintWindow failed')
            info = bitmap.GetInfo()
            if info['bmBitsPixel'] != 32:
                raise OSError('Unsupported window bitmap format')
            return Image.frombytes('RGB', (width, height), bitmap.GetBitmapBits(True), 'raw',
                                   'BGRX', info['bmWidthBytes'], 1)
        finally:
            target.DeleteDC()
    finally:
        try:
            if bitmap and bitmap.GetHandle():
                win32gui.DeleteObject(bitmap.GetHandle())
        finally:
            win32gui.ReleaseDC(hwnd, window_dc)


def capture_window(window, dimensions, quality):
    """Capture the actual foreground HWND using only the Windows graphics API."""
    affinity = wintypes.DWORD()
    if foreground() != window or win32gui.IsIconic(window.handle):
        return None, 'foreground_changed'
    if _affinity(window.handle, ctypes.byref(affinity)) and affinity.value:
        return None, 'protected_window'

    previous_dpi = _thread_dpi(PER_MONITOR_AWARE_V2)
    try:
        left, top, right, bottom = win32gui.GetWindowRect(window.handle)
        width, height = right - left, bottom - top
        if min(width, height) < 40 or width * height > 20_000_000:
            return None, 'invalid_dimensions'
        image = window_image(window.handle, width, height)
    except (OSError, WinError, win32ui.error):
        return None, 'capture_failed'
    finally:
        if previous_dpi:
            _thread_dpi(previous_dpi)
    if foreground() != window:
        return None, 'foreground_changed'
    if _affinity(window.handle, ctypes.byref(affinity)) and affinity.value:
        return None, 'protected_window'

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


def capture_browser_window(window, dimensions, quality, browser_check):
    """Apply browser privacy checks before accepting an ordinary window screenshot."""
    if foreground() != window:
        return None, 'foreground_changed'
    try:
        with window_automation(window.handle) as (automation, root, api):
            if not root:
                return None, 'uia_unavailable'
            page, reason = browser_page(automation, root, api, browser_check, wake_document=True)
            if reason:
                return None, reason
            if sensitive_controls(automation, root, api):
                return None, 'sensitive_controls'

            frame, reason = capture_window(window, dimensions, quality)
            if not frame:
                return None, reason

            after, reason = browser_page(automation, root, api, browser_check)
            if reason:
                return None, reason
            if after != page:
                return None, 'browser_changed'
            if sensitive_controls(automation, root, api):
                return None, 'sensitive_controls'
            if foreground() != window:
                return None, 'foreground_changed'
            return replace(frame, source_key=page), None
    except (COMError, OSError, ImportError, WinError):
        return None, 'uia_unavailable'


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

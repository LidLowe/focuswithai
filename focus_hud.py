"""Small native Windows overlay drawn only when its content changes."""

import logging
import time
from itertools import count

import win32api
import win32con
import win32gui
import win32ui

from focus_meter import numeric


COLORS = {'NORMAL': '#72DC96', 'ATTENTION': '#F0CD6D', 'WARNING': '#FF9C57', 'CRITICAL': '#FF666D',
          'NEUTRAL': '#A9B8C3'}
ACTIVITY = {'ON_TASK': 'В работе', 'UNCERTAIN': 'Уточняю активность', 'OFF_TASK': 'Отвлечение',
            'IDLE': 'Бездействие', 'BREAK': 'Перерыв', 'PAUSED': 'На паузе'}
SPRINT_STATES = {'ACTIVE': 'Активен', 'PAUSED': 'На паузе', 'BREAK': 'Перерыв'}
_IDS = count()
HOTKEY_ID = 1
TOPMOST_REFRESH_SECONDS = 1.0


def rgb(color):
    value = color.lstrip('#')
    return win32api.RGB(*(int(value[i:i + 2], 16) for i in (0, 2, 4)))


def monitor_work_area(foreground):
    # The foreground window or monitor can vanish between the two Win32 calls.
    for monitor in (lambda: win32api.MonitorFromWindow(foreground, win32con.MONITOR_DEFAULTTOPRIMARY),
                    lambda: win32api.MonitorFromPoint((0, 0), win32con.MONITOR_DEFAULTTOPRIMARY)):
        try:
            return win32api.GetMonitorInfo(monitor())['Work']
        except win32api.error:
            continue
    return None


class NativeCard:
    def __init__(self, draw, dispatch):
        self.draw = draw
        self.dispatch = dispatch
        self.buttons = []
        self.fonts = {}
        self.frame = None
        self.content = None
        self.visible = False
        self.instance = win32api.GetModuleHandle(None)
        window = win32gui.WNDCLASS()
        window.hInstance = self.instance
        window.lpszClassName = self.name = f'FocusWithAIOverlay{next(_IDS)}'
        window.lpfnWndProc = self._message
        win32gui.RegisterClass(window)
        try:
            self.handle = win32gui.CreateWindowEx(
                win32con.WS_EX_LAYERED | win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW,
                self.name, 'FocusWithAI', win32con.WS_POPUP, 0, 0, 1, 1, 0, 0, self.instance, None,
            )
        except Exception:
            win32gui.UnregisterClass(self.name, self.instance)
            raise

    def show(self, frame, content, *, opacity, click_through, raise_window=False):
        geometry = frame, opacity, click_through
        style = win32gui.GetWindowLong(self.handle, win32con.GWL_EXSTYLE)
        expected = style | win32con.WS_EX_LAYERED | win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW
        expected = expected | win32con.WS_EX_TRANSPARENT if click_through else expected & ~win32con.WS_EX_TRANSPARENT
        if style != expected:
            win32gui.SetWindowLong(self.handle, win32con.GWL_EXSTYLE, expected)
        if not self.visible or self.frame != geometry or style != expected:
            win32gui.SetLayeredWindowAttributes(self.handle, 0, round(opacity * 255), win32con.LWA_ALPHA)
        iconic = win32gui.IsIconic(self.handle)
        hidden = not win32gui.IsWindowVisible(self.handle)
        if iconic:
            win32gui.ShowWindow(self.handle, win32con.SW_SHOWNOACTIVATE)
        needs_frame = self.frame != geometry or iconic or hidden
        if needs_frame or raise_window or style != expected or not style & win32con.WS_EX_TOPMOST:
            flags = win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW | win32con.SWP_NOOWNERZORDER
            if style != expected:
                flags |= win32con.SWP_FRAMECHANGED
            if not needs_frame:
                flags |= win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
            win32gui.SetWindowPos(self.handle, win32con.HWND_TOPMOST, *frame, flags)
        self.frame, self.visible = geometry, True
        if self.content != content or iconic or hidden:
            self.content = content
            win32gui.InvalidateRect(self.handle, None, False)

    def hide(self):
        if self.visible:
            win32gui.ShowWindow(self.handle, win32con.SW_HIDE)
            self.visible = False

    def _message(self, handle, message, wparam, lparam):
        try:
            if message == win32con.WM_MOUSEACTIVATE:
                return win32con.MA_NOACTIVATE
            if message == win32con.WM_HOTKEY and wparam == HOTKEY_ID:
                self.dispatch('disable')
                return 0
            if message == win32con.WM_LBUTTONUP:
                x, y = lparam & 65535, (lparam >> 16) & 65535
                for left, top, right, bottom, action in self.buttons:
                    if left <= x < right and top <= y < bottom:
                        self.dispatch(action)
                        break
                return 0
            if message == win32con.WM_PAINT:
                self._paint(handle)
                return 0
        except Exception:
            logging.exception('[HUD] Window callback failed')
        return win32gui.DefWindowProc(handle, message, wparam, lparam)

    def _paint(self, handle):
        dc, state = win32gui.BeginPaint(handle)
        try:
            if self.content is None:
                return
            _, _, width, height = win32gui.GetClientRect(handle)
            memory = win32gui.CreateCompatibleDC(dc)
            bitmap = win32gui.CreateCompatibleBitmap(dc, width, height)
            original = win32gui.SelectObject(memory, bitmap)
            try:
                self.buttons.clear()
                self.draw(self, memory, width, height, self.content)
                win32gui.BitBlt(dc, 0, 0, width, height, memory, 0, 0, win32con.SRCCOPY)
            finally:
                win32gui.SelectObject(memory, original)
                win32gui.DeleteObject(bitmap)
                win32gui.DeleteDC(memory)
        finally:
            win32gui.EndPaint(handle, state)

    def fill(self, dc, rect, color):
        brush = win32gui.CreateSolidBrush(rgb(color))
        try:
            win32gui.FillRect(dc, rect, brush)
        finally:
            win32gui.DeleteObject(brush)

    def text(self, dc, value, rect, color, size, bold=False, align=0):
        key = size, bold
        if key not in self.fonts:
            self.fonts[key] = win32ui.CreateFont({'name': 'Segoe UI', 'height': -size, 'weight': 650 if bold else 400})
        font = self.fonts[key].GetSafeHandle()
        original = win32gui.SelectObject(dc, font)
        try:
            win32gui.SetBkMode(dc, win32con.TRANSPARENT)
            win32gui.SetTextColor(dc, rgb(color))
            win32gui.DrawText(dc, value, -1, rect, win32con.DT_SINGLELINE | win32con.DT_VCENTER
                              | win32con.DT_END_ELLIPSIS | win32con.DT_NOPREFIX | align)
        finally:
            win32gui.SelectObject(dc, original)

    def close(self):
        win32gui.DestroyWindow(self.handle)
        win32gui.UnregisterClass(self.name, self.instance)
        self.fonts.clear()


class FocusHUD:
    """Two non-activating cards and an emergency shortcut on the same UI thread."""

    def __init__(self, config, dispatch):
        self.visible = bool(config.get('focus_hud_enabled', False))
        self.corner = config.get('focus_hud_corner', 'top_right')
        if self.corner not in ('top_left', 'top_right', 'bottom_left', 'bottom_right'):
            self.corner = 'top_right'
        self.width = int(numeric(config, 'focus_hud_width', 210, 620))
        self.height = int(numeric(config, 'focus_hud_height', 88, 240))
        self.opacity = numeric(config, 'focus_hud_opacity', 0.25, 1)
        self.click_through = bool(config.get('focus_hud_click_through', True))
        self.show_task = bool(config.get('focus_hud_show_task', True))
        self.show_time = bool(config.get('focus_hud_show_sprint_time', True))
        self.show_state = bool(config.get('focus_hud_show_sprint_state', True))
        self.show_distraction = bool(config.get('focus_hud_show_distraction', True))
        self.card = NativeCard(self._draw_meter, dispatch)
        from windows_acquisition import set_capture_exclusion
        set_capture_exclusion(self.card.handle)
        self.prompt = None
        self.dispatch = dispatch
        self.last_work_area = None
        self.last_foreground = None
        self.next_raise = 0.0
        try:
            win32gui.RegisterHotKey(self.card.handle, HOTKEY_ID, win32con.MOD_CONTROL | win32con.MOD_SHIFT | 0x4000,
                                   win32con.VK_F9)
            self.hotkey = True
        except win32gui.error:
            self.hotkey = False
            logging.warning('[HUD] Ctrl+Shift+F9 is unavailable; disable remains available in the panel.')

    @staticmethod
    def _frame(work, corner, width, height):
        left, top, right, bottom = work
        width, height = min(width, right - left - 24), min(height, bottom - top - 24)
        x = left + 12 if corner.endswith('left') else right - width - 12
        y = top + 12 if corner.startswith('top') else bottom - height - 12
        return x, y, width, height

    def update(self, state, warning):
        win32gui.PumpWaitingMessages()
        recording = state['sources']['armed']
        if not (self.visible or warning or recording):
            self.card.hide()
            if self.prompt:
                self.prompt.hide()
            return
        foreground = win32gui.GetForegroundWindow()
        work = monitor_work_area(foreground) or self.last_work_area
        if work is None:
            return  # The desktop is temporarily unavailable (for example, during a display change).
        self.last_work_area = work
        now = time.monotonic()
        raise_window = foreground != self.last_foreground or now >= self.next_raise
        if raise_window:
            self.last_foreground = foreground
            self.next_raise = now + TOPMOST_REFRESH_SECONDS
        if self.visible or recording:
            sprint = state['sprint']
            timer = ''
            label = 'Начни спринт в панели'
            if sprint:
                label = SPRINT_STATES.get(sprint['state'], '') if self.show_state else ''
                if sprint['recovered'] and self.show_state:
                    label = 'Возобнови спринт'
                if self.show_time:
                    seconds = sprint['break_remaining'] if sprint['state'] == 'BREAK' else sprint['remaining']
                    if seconds is None:
                        seconds = sprint['seconds']
                    timer = f'{seconds // 60:02d}:{seconds % 60:02d}'
            show_meter = self.show_distraction and state['meter_enabled'] and bool(sprint)
            activity = ACTIVITY.get(state['activity'], '')
            live = state['live_engine']
            result = live['result']
            if result and result['classification'] == 'INDIRECTLY_RELATED' and state['activity'] == 'ON_TASK':
                activity = 'Связано с целью'
            health = 'Gemini · ' + live['connection_state']
            last_success = live['last_success']
            stamp = time.strftime('%H:%M:%S', time.localtime(last_success)) if last_success else ''
            footer = 'Захват окна · Ctrl+Shift+F9' if recording else ' '.join(filter(None, (health, stamp)))
            content = (state['score'], state['level'], label, activity,
                       state['task'] if self.show_task else '', timer, show_meter, footer, recording)
            self.card.show(self._frame(work, self.corner, self.width, self.height), content,
                           opacity=self.opacity, click_through=self.click_through, raise_window=raise_window)
        else:
            self.card.hide()
        if warning:
            if self.prompt is None:
                self.prompt = NativeCard(self._draw_warning, self.dispatch)
                from windows_acquisition import set_capture_exclusion
                set_capture_exclusion(self.prompt.handle)
            side = 'bottom' if self.corner.startswith('top') else 'top'
            corner = f'{side}_{self.corner.split("_")[1]}'
            self.prompt.show(self._frame(work, corner, 390, 118 if warning[2] else 80), warning,
                             opacity=0.96, click_through=not warning[2], raise_window=raise_window)
        elif self.prompt:
            self.prompt.hide()

    def _draw_meter(self, card, dc, width, height, content):
        score, level, state, activity, task, timer, show_meter, footer, recording = content
        accent = COLORS[level]
        card.fill(dc, (0, 0, width, height), '#33424D')
        card.fill(dc, (1, 1, width - 1, height - 1), '#101921')
        card.fill(dc, (1, 1, width - 1, 3), '#FF9C57' if recording else '#C39A59')
        top = 10
        if show_meter:
            indicator = activity if activity not in ('Перерыв', 'На паузе') else ''
            card.text(dc, indicator or 'Отвлечение', (13, 10, width - 70, 29), '#B0C3CE', 14, True)
            card.text(dc, f'{score}%', (width - 74, 6, width - 13, 32), accent, 22, True, win32con.DT_RIGHT)
            card.fill(dc, (13, 33, width - 13, 41), '#263541')
            bar = int((width - 26) * score / 100)
            if bar:
                card.fill(dc, (13, 33, 13 + bar, 41), accent)
            top = 45
        elif activity and activity not in ('Перерыв', 'На паузе'):
            card.text(dc, activity, (13, top, width - 13, top + 18), '#B0C3CE', 14, True)
            top += 19
        if state or timer:
            card.text(dc, state, (13, top, width - (86 if timer else 13), top + 19), '#E2E9ED', 14, True)
            if timer:
                card.text(dc, timer, (width - 82, top, width - 13, top + 19), '#E2E9ED', 14, True, win32con.DT_RIGHT)
            top += 19
        if task and top + 28 <= height:
            card.text(dc, task, (13, top, width - 13, top + 16), '#A9B8C3', 13)
        card.text(dc, footer, (13, height - 22, width - 13, height - 4),
                  '#FFBD8B' if recording else '#A9B8C3', 11)

    def _draw_warning(self, card, dc, width, height, content):
        headline, detail, actions = content
        card.fill(dc, (0, 0, width, height), '#672D34')
        card.fill(dc, (1, 1, width - 1, height - 1), '#21171B')
        card.fill(dc, (1, 1, width - 1, 3), '#FF666D')
        card.text(dc, headline, (14, 9, width - 14, 34), '#FF8F95', 17, True)
        card.text(dc, detail, (14, 38, width - 14, 62), '#EBDDE0', 14)
        for index, (label, action) in enumerate(actions):
            step = (width - 24) // len(actions)
            left = 12 + index * step
            rect = left, height - 43, left + step - 5, height - 12
            card.fill(dc, rect, '#493039')
            card.text(dc, label, rect, '#FFF0F0', 12, True, win32con.DT_CENTER)
            card.buttons.append((*rect, action))

    def close(self):
        if self.hotkey:
            win32gui.UnregisterHotKey(self.card.handle, HOTKEY_ID)
        if self.prompt:
            self.prompt.close()
        self.card.close()

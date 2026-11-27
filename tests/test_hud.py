import sys
import unittest
from unittest.mock import Mock, patch

if sys.platform == 'win32':
    import win32con
    import win32gui
    from focus_hud import NativeCard, monitor_work_area
    from startup_tray import MENU, StartupTray


@unittest.skipUnless(sys.platform == 'win32', 'Windows overlay APIs')
class HudTests(unittest.TestCase):
    def test_missing_foreground_monitor_uses_primary_work_area(self):
        area = (0, 0, 1920, 1040)
        with patch('focus_hud.win32api.MonitorFromWindow', return_value=0), \
                patch('focus_hud.win32api.MonitorFromPoint', return_value=1), \
                patch('focus_hud.win32api.GetMonitorInfo',
                      side_effect=[win32gui.error(1461, 'GetMonitorInfo', 'Invalid monitor'), {'Work': area}]):
            self.assertEqual(monitor_work_area(99), area)

    def test_native_hidden_minimized_and_demoted_card_recovers(self):
        card = NativeCard(Mock(), Mock())
        frame = (12, 12, 280, 100)
        maintenance = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE
        try:
            card.show(frame, 'meter', opacity=.8, click_through=True)
            win32gui.ShowWindow(card.handle, win32con.SW_HIDE)
            card.show(frame, 'meter', opacity=.8, click_through=True)
            self.assertTrue(win32gui.IsWindowVisible(card.handle))
            win32gui.ShowWindow(card.handle, win32con.SW_SHOWMINNOACTIVE)
            self.assertTrue(win32gui.IsIconic(card.handle))
            card.show(frame, 'meter', opacity=.8, click_through=True)
            self.assertFalse(win32gui.IsIconic(card.handle))
            win32gui.SetWindowPos(card.handle, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, maintenance)
            card.show(frame, 'meter', opacity=.8, click_through=True)
            style = win32gui.GetWindowLong(card.handle, win32con.GWL_EXSTYLE)
            flags = win32con.WS_EX_TOPMOST | win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TRANSPARENT
            self.assertEqual(style & flags, flags)
            card.hide()
            self.assertFalse(win32gui.IsWindowVisible(card.handle))
        finally:
            card.close()

    def test_tray_menu_builds_and_dispatches_hud_action(self):
        card = NativeCard(Mock(), Mock())
        tray = StartupTray.__new__(StartupTray)
        tray.handle, tray.ready, tray.command = card.handle, True, Mock()
        try:
            index = next(i for i, (name, _) in enumerate(MENU, 1) if name == 'hud')
            with (patch('startup_tray.win32gui.TrackPopupMenu', return_value=index),
                  patch('startup_tray.win32gui.GetCursorPos', return_value=(12, 12)),
                  patch('startup_tray.win32gui.SetForegroundWindow')):
                tray._menu()
            tray.command.assert_called_once_with('hud')
        finally:
            card.close()

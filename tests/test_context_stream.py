import sys
import time
import unittest
from unittest.mock import Mock, patch

from acquisition import Acquisition
from focus_meter import Foreground

from activity_context import Frame
from context_stream import ContextStream


def facts(title='Synthetic editor'):
    return dict(application='editor.exe', window_title=title, browser=None, media=None, user_apps=[],
                activity_seconds=3, sprint={'goal': 'Synthetic exercise'})


class AcquisitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_explicit_window_and_allowed_app_names(self):
        path = r'C:\Work\editor.exe'
        windows = Mock()
        windows.capture_choices.return_value = [dict(id='8-2', handle=8, pid=2)]
        windows.user_apps.return_value = ['editor.exe', 'demo.exe', 'signal.exe']
        source = Acquisition({'capture_enabled': True, 'capture_apps': [path],
                              'context_allowed_apps': ['editor.exe', 'signal.exe']}, windows)
        source.has_capture = source.active = True
        with self.assertRaises(ValueError):
            source.arm('7-2')
        source.arm('8-2')
        now = time.monotonic() + 3
        source.sample(now, Foreground(7, 2, 'other', path))
        await source.apps_job
        windows.capture_window.assert_not_called()
        source.sample(now + 1, Foreground(8, 2, 'editor', path))
        await source.capture_job
        windows.capture_window.assert_called_once()
        self.assertEqual(source.context.user_apps, ['editor.exe'])
        source.suspend()
        self.assertFalse(source.armed)
        self.assertFalse(source.context.user_apps)


    async def test_unchanged_chrome_frame_stays_valid_after_recheck(self):
        chrome = Foreground(4, 5, 'Synthetic exercise - Google Chrome', r'C:\Chrome\chrome.exe')
        windows = Mock()
        windows.capture_choices.return_value = [dict(id='4-5', handle=4, pid=5)]
        windows.user_apps.return_value = []
        now = time.monotonic()
        first = Frame(b'jpeg', bytes(100), 80, 80, now - 60)
        refreshed = Frame(b'jpeg', bytes(100), 80, 80, now)
        windows.capture_window.return_value = refreshed
        source = Acquisition({'capture_enabled': True, 'capture_apps': [chrome.executable]}, windows)
        source.has_capture = source.active = True
        source.arm('4-5')
        source.context.sample(now, chrome)
        source.context.frame = first
        source.next_capture = 0
        source.sample(now, chrome)
        await source.capture_job
        source.sample(now + .1, chrome)
        self.assertIs(source.context.current_frame(now + .1), first)
        self.assertTrue(source.browser_context_available(now + .1, send_images=True))
        self.assertFalse(source.browser_context_available(now + .1, send_images=False))
        source.suspend()


class ChromeCaptureTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32', 'uses the Windows accessibility adapter')
    def test_chrome_address_is_unavailable_for_incognito_and_unknown_window_names(self):
        from focus_windows import browser_address

        chrome = Foreground(5, 6, 'Work fixture - Google Chrome', r'C:\Chrome\chrome.exe')
        root = Mock(control_type='Window')
        address = Mock(control_type='Edit', automation_id='addressEditBox')
        address.element.CurrentIsOffscreen = False
        address.element.CurrentHasKeyboardFocus = False
        root.children.return_value = [address]
        with (patch('focus_windows.foreground', return_value=chrome),
              patch('comtypes.CoInitializeEx'), patch('comtypes.CoUninitialize'),
              patch('pywinauto.uia_element_info.UIAElementInfo', return_value=root),
              patch('pywinauto.controls.uiawrapper.UIAWrapper') as wrapper):
            wrapper.return_value.iface_value.CurrentValue = 'docs.example.org/work'
            for title in ('', chrome.title + ' (Incognito)', chrome.title + ' (Инкогнито)'):
                root.name = title
                self.assertEqual(browser_address(chrome, for_capture=True), '')
            wrapper.assert_not_called()
            root.name = chrome.title
            self.assertEqual(browser_address(chrome, for_capture=True), 'docs.example.org/work')
            address.element.CurrentHasKeyboardFocus = True
            self.assertEqual(browser_address(chrome, for_capture=True), '')

    @unittest.skipUnless(sys.platform == 'win32', 'uses the Windows capture adapter')
    def test_browser_capture_checks_privacy_before_and_after_the_image(self):
        from PIL import Image
        from activity_context import Privacy
        from windows_acquisition import capture_window

        chrome = Foreground(5, 6, 'Work fixture - Google Chrome', r'C:\Chrome\chrome.exe')
        privacy = Privacy({'browser_context_domains': ['docs.example.org']})
        image = Image.new('RGB', (80, 80), 'white')
        image.paste((0, 0, 0), (0, 0, 40, 40))
        safe = 'https://docs.example.org/reference'
        login = 'https://docs.example.org/login'

        with (patch('windows_acquisition.foreground', return_value=chrome),
              patch('windows_acquisition.win32gui.IsIconic', return_value=False),
              patch('windows_acquisition.win32gui.GetWindowRect', return_value=(0, 0, 80, 80)),
              patch('windows_acquisition._affinity', return_value=False),
              patch('windows_acquisition.sensitive_controls', return_value=False) as sensitive,
              patch('windows_acquisition.browser_address') as read_address,
              patch('PIL.ImageGrab.grab', return_value=image) as grab):
            for address in ('', login):
                read_address.return_value = address
                self.assertIsNone(capture_window(chrome, (80, 80), 72, privacy.browser_capture_allowed))
                grab.assert_not_called()

            read_address.side_effect = [safe, login]
            self.assertIsNone(capture_window(chrome, (80, 80), 72, privacy.browser_capture_allowed))
            grab.assert_called_once_with(window=chrome.handle)
            grab.reset_mock()

            read_address.side_effect = [safe, safe]
            frame = capture_window(chrome, (80, 80), 72, privacy.browser_capture_allowed)
            self.assertTrue(frame.data.startswith(b'\xff\xd8'))
            grab.assert_called_once_with(window=chrome.handle)

            grab.reset_mock()
            sensitive.return_value = True
            self.assertIsNone(capture_window(chrome, (80, 80), 72, privacy.browser_capture_allowed))
            grab.assert_not_called()


class StreamTests(unittest.TestCase):
    def test_delta_latest_frame_and_replay(self):
        stream = ContextStream()
        stream.start('abc', facts()['sprint'])
        for i in range(100):
            frame = Frame(bytes([i]), bytes([i]), 2, 2, i)
            stream.observe(facts(), frame, 'window', 20, 10)
        self.assertLessEqual(len(stream.pending), 8)
        self.assertIs(stream.pending['SCREEN_FRAME'].data, frame)
        self.assertEqual(stream.pending['ACTIVITY_DURATION'].stamp, stream.stamp)
        stream.pending.clear()
        stream.observe(facts(), frame, 'window', 25, 10)
        self.assertFalse(stream.pending)
        stream.observe(facts(), frame, 'window', 30, 10)
        self.assertEqual(list(stream.pending), ['RECENT_ACTIVITY', 'ACTIVITY_DURATION'])
        stream.replay()
        self.assertIn('SPRINT_STARTED', stream.pending)
        self.assertEqual(stream.preview()['SCREEN_FRAME']['bytes'], 1)
        self.assertNotIn(frame.data, stream.preview().values())

    def test_new_frame_coalesces_without_invalidating_an_in_flight_assessment(self):
        stream = ContextStream()
        stream.start('sprint', {})
        stream.observe(facts(), Frame(b'a', b'a', 1, 1, 20), 'same window', 20, 8)
        stamp = stream.stamp
        stream.observe(facts(), Frame(b'b', b'b', 1, 1, 21), 'same window', 21, 8)
        self.assertEqual(stream.stamp, stamp)
        self.assertEqual(stream.pending['SCREEN_FRAME'].data.data, b'b')
        self.assertEqual(next(reversed(stream.pending)), 'ACTIVITY_DURATION')
        stream.block()
        self.assertFalse(stream.pending)
        self.assertNotIn('SCREEN_FRAME', stream.latest)
        self.assertNotEqual(stream.stamp, stamp)

    def test_recent_switches_and_warnings_travel_before_the_analysis(self):
        stream = ContextStream()
        stream.start('sprint', {})
        facts_with_history = {**facts(), 'recent_switches': [{'app': 'editor.exe', 'seconds': 9}],
                              'recent_warnings': [{'kind': 'DEVIATION', 'seconds_ago': 24}]}
        stream.observe(facts_with_history, None, 'window', 20, 8)
        recent, turn = list(stream.pending.values())[-2:]
        self.assertEqual(recent.kind, 'RECENT_ACTIVITY')
        self.assertEqual(turn.kind, 'ACTIVITY_DURATION')
        self.assertEqual(recent.stamp, turn.stamp)
        self.assertEqual(recent.data['recent_warnings'], facts_with_history['recent_warnings'])
        stream.replay()
        self.assertNotIn('RECENT_ACTIVITY', stream.pending)

    def test_activity_and_generation_invalidation(self):
        stream = ContextStream()
        stream.start('abc', {})
        stream.observe(facts(), None, 1, 10, 5)
        before = stream.stamp
        stream.observe({**facts(), 'browser': {'domain': 'example.org', 'title': 'draft'}}, None, 1, 12, 5)
        self.assertGreater(stream.stamp.observation, before.observation)
        stream.start('xyz', {}, 'SPRINT_UPDATED')
        self.assertGreater(stream.stamp.generation, before.generation)
        self.assertEqual(list(stream.pending), ['SPRINT_UPDATED'])
        stream.clear()
        self.assertIsNone(stream.stamp)
        self.assertFalse(stream.preview())

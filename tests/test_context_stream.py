import sys
import time
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from acquisition import Acquisition
from focus_meter import Foreground

from activity_context import Frame
from context_stream import ContextStream


def facts(title='Synthetic editor'):
    return dict(application='editor.exe', window_title=title, browser=None, media=None, user_apps=[],
                activity_seconds=3, sprint={'goal': 'Synthetic exercise'})


class AcquisitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_capture_follows_foreground_and_discards_a_late_frame(self):
        import threading
        ide = Foreground(1, 2, 'Synthetic editor', r'C:\Work\editor.exe')
        chrome = Foreground(2, 3, 'Synthetic page - Google Chrome', r'C:\Chrome\chrome.exe')
        excluded = Foreground(3, 4, 'Private window', r'C:\Work\excluded.exe')
        windows = Mock()
        windows.user_apps.return_value = ['editor.exe', 'demo.exe']
        release = threading.Event()

        def capture(window, dimensions, quality, check):
            if window == ide:
                release.wait(2)
            return Frame(window.executable.encode(), bytes(100), 80, 80, time.monotonic()), None

        windows.capture_window.side_effect = capture
        source = Acquisition({'activity_excluded_apps': ['excluded.exe'],
                              'context_allowed_apps': ['editor.exe']}, windows)
        source.has_capture = True
        now = time.monotonic()
        source.sample(now, ide)
        self.assertIsNone(source.capture_job)
        source.start_capture('sprint')
        source.sample(now, ide)
        old_job = source.capture_job
        source.sample(now + .1, chrome)
        release.set()
        await old_job
        source.sample(now + .2, chrome)
        self.assertIsNone(source.context.frame)
        await source.capture_job
        source.sample(now + .3, chrome)
        self.assertEqual(source.context.frame.data, chrome.executable.encode())
        self.assertEqual(source.frames_captured, 1)
        self.assertEqual(source.context.user_apps, ['editor.exe'])
        self.assertEqual(windows.capture_window.call_args.args[1:3], ((1280, 720), 80))
        source.sample(now + .4, excluded)
        self.assertIsNone(source.context.frame)
        self.assertIsNone(source.capture_job)
        self.assertEqual(source.capture_skip, 'window_excluded')
        source.stop_capture()
        source.sample(now + 4, ide)
        self.assertIsNone(source.capture_job)

    async def test_unchanged_frame_stays_valid_and_slows_captures(self):
        chrome = Foreground(4, 5, 'Synthetic exercise - Google Chrome', r'C:\Chrome\chrome.exe')
        windows = Mock()
        windows.user_apps.return_value = []
        now = time.monotonic()
        first = Frame(b'jpeg', bytes(100), 80, 80, now - 60)
        refreshed = Frame(b'jpeg', bytes(100), 80, 80, now)
        windows.capture_window.return_value = refreshed, None
        source = Acquisition({}, windows)
        source.has_capture = True
        source.start_capture('sprint')
        source.context.sample(now, chrome)
        source.context.frame = first
        source.sample(now, chrome)
        await source.capture_job
        source.sample(now + .1, chrome)
        self.assertIs(source.context.current_frame(now + .1), first)
        self.assertEqual(source.next_capture, now + .1 + 4)
        self.assertEqual(source.capture_skip, 'unchanged')
        source.suspend()


class ChromeCaptureTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32', 'uses the Windows accessibility adapter')
    def test_chrome_without_url_uses_verified_tab_and_privacy_controls(self):
        from activity_context import Privacy
        from windows_acquisition import browser_page

        api = SimpleNamespace(UIA_ClassNamePropertyId=1, UIA_ControlTypePropertyId=2,
                              UIA_SelectionItemIsSelectedPropertyId=3, UIA_TabItemControlTypeId=4,
                              UIA_DocumentControlTypeId=5, UIA_ValueValuePropertyId=6)
        browser = Mock(CurrentName='Work fixture', GetRuntimeId=Mock(return_value=[1]))
        avatar = Mock(CurrentName='You')
        tab = Mock(CurrentName='Work fixture', GetRuntimeId=Mock(return_value=[2]))
        document = Mock(CurrentName='Work fixture', CurrentIsOffscreen=False,
                        GetRuntimeId=Mock(return_value=[3]))
        nodes = {(1, 'BrowserRootView'): browser, (1, 'AvatarToolbarButton'): avatar,
                 (1, 'LocationIconView'): Mock(CurrentName='View site information'),
                 ((2, 4), (3, True)): tab, (2, 5): document}
        automation = Mock(CreatePropertyCondition=lambda *args: args,
                          CreateAndCondition=lambda *args: args)
        root = Mock(FindFirst=lambda _, key: nodes.get(key))
        privacy = Privacy({})

        def probe(context=privacy):
            return browser_page(automation, root, api, context.browser_capture_reason)

        first, reason = probe()
        self.assertIsNone(reason)
        self.assertIsNotNone(first)
        self.assertEqual(probe(Privacy({'browser_context_domains': ['example.org']}))[1], 'url_unavailable')
        tab.GetRuntimeId.return_value = [4]
        self.assertNotEqual(probe()[0], first)
        avatar.CurrentName = 'Incognito'
        self.assertEqual(probe(), (None, 'private_or_sensitive_window'))
        avatar.CurrentName = 'You'
        document.CurrentName = 'Sign in'
        self.assertEqual(probe(), (None, 'sensitive_page'))
        document.CurrentName = 'Work fixture'
        nodes.pop((2, 5))
        self.assertEqual(probe(), (None, 'browser_document_unavailable'))

    @unittest.skipUnless(sys.platform == 'win32', 'uses the Windows capture adapter')
    def test_browser_capture_checks_privacy_before_and_after_the_image(self):
        from PIL import Image
        from activity_context import Privacy
        from windows_acquisition import capture_window

        chrome = Foreground(5, 6, 'Work fixture - Google Chrome', r'C:\Chrome\chrome.exe')
        privacy = Privacy({})
        image = Image.new('RGB', (80, 80), 'white')
        image.paste((0, 0, 0), (0, 0, 40, 40))
        safe = ((1,), (2,))

        with (patch('windows_acquisition.foreground', return_value=chrome),
              patch('windows_acquisition.win32gui.IsIconic', return_value=False),
              patch('windows_acquisition.win32gui.GetWindowRect', return_value=(0, 0, 80, 80)),
              patch('windows_acquisition._affinity', return_value=False),
              patch('windows_acquisition.window_automation', return_value=nullcontext((Mock(), Mock(), Mock()))),
              patch('windows_acquisition.sensitive_controls', return_value=False) as sensitive,
              patch('windows_acquisition.browser_page') as read_page,
              patch('windows_acquisition.window_image', return_value=image) as grab):
            def capture():
                return capture_window(chrome, (80, 80), 72, privacy.browser_capture_reason)

            for reason in ('browser_mode_unverified', 'sensitive_page'):
                read_page.return_value = None, reason
                self.assertEqual(capture(), (None, reason))
                grab.assert_not_called()

            read_page.side_effect = [(safe, None), (None, 'sensitive_page')]
            self.assertEqual(capture(), (None, 'sensitive_page'))
            grab.assert_called_once_with(chrome.handle, 80, 80)
            grab.reset_mock()

            read_page.side_effect = [(safe, None), (((3,), (4,)), None)]
            self.assertEqual(capture(), (None, 'browser_changed'))
            grab.reset_mock()

            read_page.side_effect = [(safe, None), (safe, None)]
            frame, reason = capture()
            self.assertIsNone(reason)
            self.assertTrue(frame.data.startswith(b'\xff\xd8'))
            self.assertEqual(frame.source_key, safe)
            grab.assert_called_once_with(chrome.handle, 80, 80)

            grab.reset_mock()
            sensitive.return_value = True
            read_page.side_effect = None
            read_page.return_value = safe, None
            self.assertEqual(capture(), (None, 'sensitive_controls'))
            grab.assert_not_called()

            sensitive.return_value = False
            grab.side_effect = OSError
            self.assertEqual(capture(), (None, 'capture_failed'))

    @unittest.skipUnless(sys.platform == 'win32', 'uses the Windows capture adapter')
    def test_unavailable_uia_skips_image_before_capture(self):
        from activity_context import Privacy
        from windows_acquisition import capture_window

        chrome = Foreground(5, 6, 'Work fixture - Google Chrome', r'C:\Chrome\chrome.exe')
        with (patch('windows_acquisition.foreground', return_value=chrome),
              patch('windows_acquisition.win32gui.IsIconic', return_value=False),
              patch('windows_acquisition._affinity', return_value=False),
              patch('windows_acquisition.window_automation', side_effect=OSError),
              patch('windows_acquisition.window_image') as grab):
            self.assertEqual(capture_window(chrome, (80, 80), 72, Privacy({}).browser_capture_reason),
                             (None, 'uia_unavailable'))
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

    def test_same_title_browser_tab_replaces_pending_image_and_invalidates_the_previous_result(self):
        stream = ContextStream()
        stream.start('sprint', {})
        chrome = {**facts(''), 'application': 'chrome.exe'}
        first = Frame(b'old', b'pixels', 2, 2, 20, ('document-1',))
        second = Frame(b'new', b'pixels', 2, 2, 21, ('document-2',))
        self.assertTrue(second.differs(first))
        stream.observe(chrome, first, ('chrome-window', first.source_key), 20, 8)
        old_stamp = stream.stamp
        stream.observe(chrome, second, ('chrome-window', second.source_key), 21, 8)
        self.assertNotEqual(stream.stamp, old_stamp)
        self.assertIs(stream.pending['SCREEN_FRAME'].data, second)
        self.assertEqual(stream.pending['ACTIVITY_DURATION'].stamp, stream.stamp)

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

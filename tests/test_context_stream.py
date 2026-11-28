import asyncio
import io
import sys
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from aiohttp.test_utils import TestClient, TestServer

from acquisition import Acquisition
from config_store import load_config, save_config
from panel import Panel
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

        def capture(window, dimensions, quality):
            if window == ide:
                release.wait(2)
            return Frame(window.executable.encode(), bytes(100), 80, 80, time.monotonic()), None

        windows.capture_window.side_effect = capture
        windows.capture_browser_window.side_effect = lambda window, dimensions, quality, check: capture(window, dimensions, quality)
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
        self.assertIsNone(source.capture_job)
        source.sample(now + source.interval, chrome)
        await source.capture_job
        source.sample(now + source.interval + .1, chrome)
        self.assertEqual(source.context.frame.data, chrome.executable.encode())
        self.assertEqual(source.frames_captured, 1)
        self.assertEqual(source.context.user_apps, ['editor.exe'])
        windows.capture_window.assert_called_once_with(ide, (1280, 720), 80)
        self.assertEqual(windows.capture_browser_window.call_args.args[:3], (chrome, (1280, 720), 80))
        source.sample(now + source.interval + .2, excluded)
        self.assertIsNone(source.context.frame)
        self.assertIsNone(source.capture_job)
        self.assertEqual(source.capture_skip, 'window_excluded')
        source.stop_capture()
        source.sample(now + 4, ide)
        self.assertIsNone(source.capture_job)

    async def test_refresh_uses_new_duplicate_and_keeps_minimum_capture_interval(self):
        window = Foreground(4, 5, 'Synthetic exercise', r'C:\Editor\editor.exe')
        now = time.monotonic()
        previous = Frame(b'old bytes', b'unchanged', 80, 80, now - 3)
        new = Frame(b'fresh bytes', b'unchanged', 80, 80, now)
        windows = Mock()
        windows.foreground.return_value = window
        windows.user_apps.return_value = []
        windows.capture_window.return_value = new, None
        source = Acquisition({'capture_interval': 1}, windows)
        source.has_capture = True
        source.start_capture('sprint')
        source.context.sample(now, window)
        source.active = True
        source.context.frame = previous
        source.capture_backoff = 4
        source.next_capture = now + 4
        source.capture_not_before = now + .4
        refresh = asyncio.create_task(source.capture_for_refresh())
        await asyncio.sleep(0)
        self.assertEqual(source.capture_backoff, 1)
        source.sample(now + .3, window)
        self.assertIsNone(source.capture_job)
        source.sample(now + .4, window)
        await source.capture_job
        source.sample(now + .5, window)
        self.assertIs(await refresh, new)
        self.assertIs(source.context.frame, previous)
        windows.capture_window.assert_called_once()
        source.suspend()

    async def test_unchanged_frame_stays_valid_and_slows_captures(self):
        chrome = Foreground(4, 5, 'Synthetic exercise - Google Chrome', r'C:\Chrome\chrome.exe')
        windows = Mock()
        windows.user_apps.return_value = []
        now = time.monotonic()
        first = Frame(b'jpeg', bytes(100), 80, 80, now - 60)
        refreshed = Frame(b'jpeg', bytes(100), 80, 80, now)
        windows.capture_browser_window.return_value = refreshed, None
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
        self.assertEqual(source.status()['adaptive_interval'], 4)
        source.suspend()


class ContextSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_rejects_invalid_context_settings_and_new_runtime_reads_valid_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            with (patch('config_store.read_secret', return_value=''), patch('config_store.store_secret'),
                  patch('panel.CONFIG_FILE', str(path)), patch('panel.BACKUP_DIR', str(path.parent / 'backups')),
                  patch('panel.load_config', side_effect=lambda: load_config(path)),
                  patch('panel.save_config', side_effect=lambda values: save_config(values, path))):
                defaults = Acquisition(load_config(path))
                headers = {'X-Panel': '1'}
                async with TestClient(TestServer(Panel(None).app)) as client:
                    response = await client.get('/api/config')
                    schema = (await response.json())['capture_limits']
                    self.assertEqual(schema['capture_width']['max'], 7680)
                    self.assertEqual(schema['capture_height']['max'], 7680)
                    settings = {'capture_width': 1920, 'capture_height': 1080, 'capture_interval': 1.25,
                                'capture_quality': 90, 'media_context_enabled': True,
                                'activity_send_remote_images': True, 'activity_excluded_apps': ['notepad.exe'],
                                'activity_excluded_windows': ['Example title'], 'browser_context_domains': ['example.org']}
                    response = await client.put('/api/config', json={'set': settings}, headers=headers)
                    self.assertEqual(response.status, 200, await response.text())
                    saved = path.read_bytes()
                    self.assertEqual(defaults.status()['settings']['capture_width'], 1280)
                    restarted = Acquisition(load_config(path))
                    self.assertEqual(restarted.status()['settings'], settings)
                    self.assertEqual((restarted.dimensions, restarted.interval, restarted.quality), ((1920, 1080), 1.25, 90))
                    invalid = {'capture_width': 7681, 'capture_height': 719.5, 'capture_interval': 0,
                               'capture_quality': True, 'media_context_enabled': 'false',
                               'activity_send_remote_images': 1, 'activity_excluded_apps': 'notepad.exe',
                               'activity_excluded_windows': [None], 'browser_context_domains': ['https://example.org/path']}
                    for key, value in invalid.items():
                        with self.subTest(key=key):
                            response = await client.put('/api/config', json={'set': {key: value}}, headers=headers)
                            self.assertEqual(response.status, 400, await response.text())
                            self.assertIn(key, (await response.json())['error'])
                            self.assertEqual(path.read_bytes(), saved)
                    response = await client.put('/api/config',
                                                json={'set': {'capture_width': 7680, 'capture_height': 7680}},
                                                headers=headers)
                    self.assertEqual(response.status, 200, await response.text())
                    self.assertEqual(Acquisition(load_config(path)).dimensions, (7680, 7680))

    def test_invalid_on_disk_configuration_disables_capture_with_a_reason(self):
        source = Acquisition({'capture_width': 9000})
        self.assertIsNone(source.status()['settings'])
        self.assertIn('capture_width', source.status()['settings_error'])
        with self.assertRaisesRegex(ValueError, 'capture_width'):
            source.start_capture('sprint')


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
        from windows_acquisition import capture_browser_window

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
                return capture_browser_window(chrome, (80, 80), 72, privacy.browser_capture_reason)

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
        from windows_acquisition import capture_browser_window

        chrome = Foreground(5, 6, 'Work fixture - Google Chrome', r'C:\Chrome\chrome.exe')
        with (patch('windows_acquisition.foreground', return_value=chrome),
              patch('windows_acquisition.win32gui.IsIconic', return_value=False),
              patch('windows_acquisition._affinity', return_value=False),
              patch('windows_acquisition.window_automation', side_effect=OSError),
              patch('windows_acquisition.window_image') as grab):
            self.assertEqual(capture_browser_window(chrome, (80, 80), 72, Privacy({}).browser_capture_reason),
                             (None, 'uia_unavailable'))
            grab.assert_not_called()


    @unittest.skipUnless(sys.platform == 'win32', 'uses the Windows capture adapter')
    def test_ordinary_window_capture_works_without_uia_and_skips_protected_windows(self):
        import ctypes
        from PIL import Image
        from windows_acquisition import capture_window

        notepad = Foreground(5, 6, 'Notes - Notepad', r'C:\Windows\notepad.exe')
        image = Image.new('RGB', (80, 80), 'white')
        image.paste((0, 0, 0), (0, 0, 40, 40))

        def set_affinity(_, output):
            ctypes.cast(output, ctypes.POINTER(ctypes.c_uint32))[0] = 0x11
            return True

        with (patch('windows_acquisition.foreground', return_value=notepad),
              patch('windows_acquisition.win32gui.IsIconic', return_value=False),
              patch('windows_acquisition.win32gui.GetWindowRect', return_value=(0, 0, 80, 80)),
              patch('windows_acquisition._affinity', return_value=False) as affinity,
              patch('windows_acquisition.window_automation', side_effect=OSError) as automation,
              patch('windows_acquisition.window_image', return_value=image) as grab):
            frame, reason = capture_window(notepad, (80, 80), 72)
            self.assertTrue(frame.data.startswith(b'\xff\xd8'))
            self.assertIsNone(reason)
            automation.assert_not_called()

            grab.reset_mock()
            affinity.side_effect = set_affinity
            self.assertEqual(capture_window(notepad, (80, 80), 72), (None, 'protected_window'))
            grab.assert_not_called()


    @unittest.skipUnless(sys.platform == 'win32', 'uses the Windows capture adapter')
    def test_capture_respects_maximum_size_without_upscaling_the_rendered_window(self):
        from PIL import Image
        from windows_acquisition import capture_window

        notepad = Foreground(5, 6, 'Notes - Notepad', r'C:\Windows\notepad.exe')
        image = Image.new('RGB', (1920, 1080), 'white')
        image.paste((0, 0, 0), (0, 0, 640, 400))
        cases = [((1280, 720), (1280, 720)), ((1920, 1080), (1920, 1080)),
                 ((2560, 1440), (1920, 1080)), ((1920, 1200), (1920, 1080))]
        with (patch('windows_acquisition.foreground', return_value=notepad),
              patch('windows_acquisition.win32gui.IsIconic', return_value=False),
              patch('windows_acquisition.win32gui.GetWindowRect', return_value=(0, 0, 1920, 1080)),
              patch('windows_acquisition._affinity', return_value=False),
              patch('windows_acquisition.window_image', side_effect=lambda *_: image.copy()) as grab):
            for maximum, expected in cases:
                with self.subTest(maximum=maximum):
                    config = {'capture_width': maximum[0], 'capture_height': maximum[1]}
                    frame, reason = capture_window(notepad, Acquisition(config).dimensions, 80)
                    self.assertIsNone(reason)
                    self.assertEqual(frame.source_size, (1920, 1080))
                    with Image.open(io.BytesIO(frame.data)) as jpeg:
                        self.assertEqual(jpeg.size, expected)
            grab.reset_mock()
            with patch('windows_acquisition.win32gui.GetWindowRect', return_value=(0, 0, 7680, 4320)):
                self.assertEqual(capture_window(notepad, (7680, 7680), 80), (None, 'invalid_dimensions'))
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

    def test_available_frame_replaces_pending_text_without_waiting_for_text_cadence(self):
        stream = ContextStream()
        stream.start('sprint', {})
        stream.observe(facts(), None, 'window', 20, 2, waiting_for_frame=True)
        self.assertNotIn('ACTIVITY_DURATION', stream.pending)
        first = Frame(b'first', b'a', 1, 1, 20.1)
        stream.observe(facts(), first, 'window', 20.1, 2)
        self.assertIn('ACTIVITY_DURATION', stream.pending)
        stream.pending.clear()
        stream.observe(facts(), first, 'window', 21, 2, waiting_for_frame=True)
        self.assertNotIn('ACTIVITY_DURATION', stream.pending)
        stream.observe(facts(), Frame(b'replaced', b'b', 1, 1, 21.2), 'window', 21.2, 2)
        self.assertIn('ACTIVITY_DURATION', stream.pending)
        newest = Frame(b'latest', b'c', 1, 1, 21.5)
        stream.observe(facts(), newest, 'window', 21.5, 2)
        self.assertIs(stream.pending['SCREEN_FRAME'].data, newest)
        self.assertEqual(next(reversed(stream.pending)), 'ACTIVITY_DURATION')

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

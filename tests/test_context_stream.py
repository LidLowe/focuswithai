import time
import unittest
from unittest.mock import Mock

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

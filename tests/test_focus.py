import asyncio
import os
import time
import unittest
from threading import Event as ThreadEvent
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

os.environ.setdefault('PYGAME_HIDE_SUPPORT_PROMPT', '1')

from focus_meter import ActivityMonitor, ActivityState as State, DistractionMeter, Foreground, LLMClassifier, Severity
from intervention import ForegroundGuard, InterventionEngine, site_host
from voicepack import Event


class MeterTests(unittest.TestCase):
    def test_hysteresis_progression_freeze_and_recovery(self):
        meter = DistractionMeter({'focus_gain_per_second': 10, 'focus_recovery_per_second': 3})
        meter.advance(0, State.OFF_TASK)
        self.assertIsNone(meter.advance(1, State.OFF_TASK))
        self.assertIsNone(meter.advance(2, State.OFF_TASK))
        self.assertEqual(meter.advance(3, State.OFF_TASK), (Severity.NORMAL, Severity.ATTENTION))
        for now, state in enumerate((State.UNCERTAIN, State.IDLE, State.BREAK), 4):
            self.assertIsNone(meter.advance(now, state))
            self.assertEqual(meter.score, 30)
        meter.advance(7, State.ON_TASK)
        self.assertIsNone(meter.advance(8, State.ON_TASK))
        self.assertEqual(meter.score, 27)
        self.assertEqual(meter.advance(9, State.ON_TASK), (Severity.ATTENTION, Severity.NORMAL))
        meter.advance(10, State.OFF_TASK)
        for now in range(11, 30):
            meter.advance(now, State.OFF_TASK)
        self.assertEqual((meter.score, meter.level), (100, Severity.CRITICAL))
        meter.reset(30)
        self.assertEqual((meter.score, meter.level), (0, Severity.NORMAL))

    def test_activity_window_and_idle_are_independent(self):
        expected = Foreground(7, 1, 'Worksheet - Visual Studio Code', 'Code.exe')
        monitor = ActivityMonitor(lambda: expected, ['visual studio code'], 20, lambda: 22)
        self.assertEqual(monitor.sample(), (expected, True, True))


class ClassifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_browser_titles_remain_uncertain_without_inference(self):
        client = AsyncMock()
        classifier = LLMClassifier(client, {})
        for title in ('Google Chrome', 'Новая вкладка — Microsoft Edge', 'New Tab - Mozilla Firefox'):
            self.assertEqual(classifier.observe(50, 'Report', Foreground(title=title), False, True)[0], State.UNCERTAIN)
        client.chat.assert_not_awaited()

    async def test_uncertain_recheck_and_no_stale_window_application(self):
        client = AsyncMock()
        client.chat.side_effect = ['{"state":"UNCERTAIN","reason":"Мало сведений"}',
                                   '{"state":"OFF_TASK","reason":"Окно игры"}']
        classifier = LLMClassifier(client, {})
        now = time.monotonic()
        game = Foreground(7, 1, 'Game', 'Game.exe')
        work = Foreground(9, 2, 'Project', 'Work.exe')
        self.assertEqual(classifier.observe(now, 'Report', game, False, True)[0], State.UNCERTAIN)
        classifier.observe(now + 2, 'Report', game, False, True)
        await classifier.job
        self.assertEqual(classifier.observe(now + 3, 'Report', game, False, True)[0], State.UNCERTAIN)
        self.assertEqual(client.chat.await_count, 1)
        classifier.cache.clear()  # Expire the uncertain result without waiting in real time.
        classifier.observe(now + 30, 'Report', game, False, True)
        self.assertEqual(classifier.observe(now + 30.1, 'Report', work, True, True)[0], State.ON_TASK)
        await classifier.job
        self.assertEqual(classifier.observe(now + 30.2, 'Report', work, True, True)[0], State.ON_TASK)
        self.assertEqual(classifier.observe(now + 30.3, 'New task', game, False, True)[0], State.UNCERTAIN)
        await classifier.close()

    async def test_failed_model_holds_unknown_activity_and_rule_mode_is_explicit(self):
        client = AsyncMock()
        client.chat.side_effect = RuntimeError('offline')
        classifier = LLMClassifier(client, {})
        now, window = time.monotonic(), Foreground(1, 2, 'Browser', 'chrome.exe')
        classifier.observe(now, 'Paper', window, False, True)
        classifier.observe(now + 3, 'Paper', window, False, True)
        await classifier.job
        self.assertEqual(classifier.observe(now + 4, 'Paper', window, False, True)[0], State.UNCERTAIN)
        self.assertEqual(classifier.observe(now + 4, 'Paper', window, False, False)[0], State.OFF_TASK)
        await classifier.close()


class InterventionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.platform = SimpleNamespace(app_path=lambda path: path if path != 'system.exe' else None,
                                        BROWSERS={'chrome.exe'}, browser_address=Mock(),
                                        minimize_if_still_foreground=Mock(return_value=True))

    def test_only_opted_in_app_is_restricted_and_timer_releases(self):
        guard = ForegroundGuard({'intervention_apps': ['system.exe', 'game.exe'], 'intervention_block_seconds': 5}, self.platform)
        game = Foreground(7, 1, 'Game', 'game.exe')
        guard.start(10)
        guard.tick(11, game)
        self.platform.minimize_if_still_foreground.assert_not_called()
        guard.arm()
        guard.start(12)
        guard.tick(13, Foreground(8, 2, 'System', 'system.exe'))
        self.platform.minimize_if_still_foreground.assert_not_called()
        guard.tick(14, game)
        self.platform.minimize_if_still_foreground.assert_called_once_with(game)
        guard.tick(17, game)
        self.assertFalse(guard.armed)
        self.assertEqual(self.platform.minimize_if_still_foreground.call_count, 1)

    async def test_releasing_site_guard_discards_late_uia_result(self):
        entered, finish = ThreadEvent(), ThreadEvent()

        def address(_window):
            entered.set()
            finish.wait(2)
            return 'https://example.com/feed'

        self.platform.browser_address = address
        guard = ForegroundGuard({'intervention_sites': ['example.com']}, self.platform)
        window = Foreground(7, 1, 'Browser', 'chrome.exe')
        guard.arm()
        guard.start(10)
        guard.tick(11, window)
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        guard.release()
        finish.set()
        await guard.probe
        guard.tick(12, window)
        self.platform.minimize_if_still_foreground.assert_not_called()
        self.assertFalse(guard._matches_site('https://notexample.com'))
        self.assertTrue(guard._matches_site('https://sub.example.com/a'))
        for site in ('localhost:8765', '127.0.0.1', 'file:///C:/x', 'chrome://settings', 'secret.local'):
            self.assertEqual(site_host(site), '')

    def test_critical_action_routes_sound_and_debounces(self):
        play = Mock()
        engine = InterventionEngine({'intervention_enabled': True, 'intervention_alarm':True,
            'intervention_announcer':True, 'intervention_dialog':True}, self.platform, play)
        engine.trigger(0, Severity.WARNING, Severity.CRITICAL, 'Report', 'Window')
        self.assertEqual(play.call_args.args, (Event.FOCUS_CRITICAL, 'Report', 'Window', 'alarm_voicepack'))
        self.assertIn(('Пауза', 'break'), engine.current_warning(1)[2])
        engine.trigger(4, Severity.WARNING, Severity.CRITICAL, 'Report', 'Window')
        play.assert_called_once()
        engine.disable()
        engine.trigger(100, Severity.WARNING, Severity.CRITICAL, 'Report', 'Window')
        self.assertIsNone(engine.current_warning(100))
        play.assert_called_once()

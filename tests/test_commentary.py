import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from activity_protocol import Classification, Recommendation, Result
from commentary import CommentaryGate
from decision import DecisionEngine
from focus_meter import ActivityState, Foreground
from live_focus import LiveFocus
from test_context_stream import facts
from test_live_session import until
from mock_live import MockLiveTransport
from voicepack import Event, VoicePackNotifier


class GateTests(unittest.TestCase):
    def test_policy(self):
        gate = CommentaryGate({'live_proactive': True}, 15)
        result = Result('game', Classification.OFF_TASK, .1, .9, 'fixture', Recommendation.AUDIO_WARNING, 'step')
        args = dict(active=True, current=True, enabled=True, busy=False, confirmed=True, min_confidence=.8)
        for key in ('active', 'current', 'enabled', 'confirmed', 'busy'):
            self.assertFalse(gate.admit('DEVIATION', result, 50, **{**args, key: key == 'busy'}))
        self.assertTrue(gate.admit('DEVIATION', result, 50, **args))
        self.assertFalse(gate.admit('DEVIATION', result, 51, **args))
        gate.proactive = False
        self.assertFalse(gate.admit('NEXT_STEP', result, 70, **args))
        self.assertTrue(gate.admit('NEXT_STEP', result, 70, **args, requested=True))


    def test_proactive_warnings_stop_after_configured_repeats(self):
        gate = CommentaryGate({'live_proactive': True, 'voicepack_max_repeats': 1,
                               'voicepack_repeat_interval': 60}, 15)
        r = Result('game', Classification.OFF_TASK, .1, .9, 'fixture', Recommendation.AUDIO_WARNING, 'step')
        args = dict(active=True, current=True, enabled=True, busy=False, confirmed=True, min_confidence=.8)
        gate.observe(('abc', 0))
        self.assertTrue(gate.admit('DEVIATION', r, 100, **args))
        self.assertFalse(gate.admit('DEVIATION', r, 140, **args))
        self.assertTrue(gate.admit('DEVIATION', r, 170, **args))
        self.assertFalse(gate.admit('DEVIATION', r, 300, **args))
        gate.observe(('abc', 0), Classification.ON_TASK)
        self.assertTrue(gate.admit('DEVIATION', r, 400, **args))


class ChainTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_decision_alarm_then_pcm(self):
        cfg = {'live_proactive': True,
               'notification_mode': 'alarm_live', 'alarm_voice_delay_ms': 100,
               'live_analysis_seconds': 2, 'activity_confirmation_seconds': 2}
        notifier = VoicePackNotifier(cfg)
        calls = []
        async def alarm(*args, **kwargs):
            calls.append(('alarm', time.monotonic(), args[1]))
            return True
        notifier.alarms.play_event = alarm
        phase = SimpleNamespace(key=('abc', 0), active=True, sprint={'id': 'abc', 'state': 'ACTIVE'}, context=json.dumps({'goal': 'Synthetic exercise'}))
        win = Foreground(1, 2, '[demo] unrelated game', 'demo.exe')
        signature = (phase.context, win, None)
        owner = SimpleNamespace(phase=lambda: phase, phase_key=phase.key, signature=signature,
            use_ai=lambda: True, acquisition=SimpleNamespace(context=SimpleNamespace(window=win, browser=None),
            foreground_is_current=lambda: True), decision=DecisionEngine(cfg), intervention=None, on_intervention=Mock())
        live = LiveFocus(cfg, owner, notifier, lambda: True, transport_factory=MockLiveTransport)
        self.addAsyncCleanup(live.audio.close)
        self.addAsyncCleanup(live.session.close)
        sink = Mock(write=AsyncMock(side_effect=lambda _: calls.append(('pcm', time.monotonic(), None))),
                    drain=AsyncMock())
        live.audio.sink_factory = lambda rate, level: sink
        live.sync_phase(phase)
        data = facts(win.title)
        now = time.monotonic()
        live.sample(now, phase, signature, data, None)
        await until(lambda: live.session.result)
        result = live.session.result
        state, _, confirmed = owner.decision.evaluate(now - 3, result, live.session.finished_at, signature)
        self.assertEqual(state, ActivityState.OFF_TASK)
        self.assertFalse(confirmed)
        self.assertFalse(calls)
        live.sample(now + 3, phase, signature, data, None)
        await until(lambda: sink.drain.await_count)
        self.assertEqual(calls[0][2], Event.DISTRACTION)
        self.assertEqual(calls[1][0], 'pcm')
        self.assertGreaterEqual(calls[1][1] - calls[0][1], .075)
        self.assertIn(data['sprint']['goal'], live.session.last_comment)
        owner.on_intervention.assert_called_once()
        phase.active = False
        phase.sprint['state'] = 'PAUSED'
        live.sync_phase(phase)
        self.assertEqual(live.session.connection_state, 'CONNECTED')
        self.assertEqual(live.session.analysis_state, 'PAUSED')

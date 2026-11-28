import asyncio
import unittest
from unittest.mock import Mock

from activity_context import Frame
from live_session import LiveSession, Output
from mock_live import MockLiveTransport
from test_context_stream import facts


async def until(condition, timeout=2):
    async def poll():
        while not condition():
            await asyncio.sleep(.005)
    await asyncio.wait_for(poll(), timeout)


class Trace(MockLiveTransport):
    def __init__(self):
        self.kinds = []
        self.closed = False

    async def send(self, event):
        self.kinds.append(event.kind)
        await super().send(event)

    async def close(self):
        self.closed = True
        await super().close()


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_pause_and_discard_old_turn(self):
        transports = []
        def make():
            transports.append(Trace())
            return transports[-1]
        receive = Mock()
        session = LiveSession({}, receive, Mock(), make)
        self.addAsyncCleanup(session.close)
        session.start('one', facts()['sprint'])
        session.observe(facts(), None, 1, 10)
        await until(lambda: session.result)
        old = session.stream.stamp
        old_turn = session.turn
        session.observe(facts('other'), None, 2, 12)
        await transports[0].output.put(Output('ASSESSMENT', old, old_turn, 'invalid'))
        await asyncio.sleep(.02)
        self.assertEqual(session.result.observed_activity, 'other')
        self.assertNotEqual(session.connection_state, 'ERROR')
        session.hold('break', paused=True)
        self.assertEqual(session.connection_state, 'CONNECTED')
        self.assertEqual(session.analysis_state, 'PAUSED')
        session.start('two', {'goal': 'different'}, 'BREAK_ENDED')
        session.observe(facts(), None, 1, 20)
        try:
            await until(lambda: session.result)
        except TimeoutError:
            self.fail(f'{session.diagnostics()}, pending={list(session.stream.pending)}, '
                      f'transports={[(t.closed, t.kinds) for t in transports]}')
        self.assertTrue(transports[0].closed)
        self.assertNotIn('BREAK_ENDED', transports[0].kinds)
        self.assertIn('BREAK_ENDED', transports[1].kinds)
        self.assertNotEqual(session.stream.stamp.generation, old.generation)
        self.assertEqual(session.result.classification, 'UNCERTAIN')
        session.stop('SPRINT_COMPLETED')
        await session.close()
        self.assertTrue(transports[-1].closed)
        self.assertFalse(session.stream.latest)
        self.assertIsNone(session.result)

    async def test_disconnect_replays_compacted_context(self):
        class Broken(Trace):
            async def receive(self):
                raise ConnectionError()
        choices = [Broken(), Trace()]
        it = iter(choices)
        session = LiveSession({}, Mock(), Mock(), lambda: next(it))
        self.addAsyncCleanup(session.close)
        session.start('test', facts()['sprint'])
        session.observe(facts(), None, 5, 10)
        await until(lambda: session.reconnects == 1)
        self.assertEqual(session.connection_state, 'RECONNECTING')
        self.assertIsNone(session.result)
        session.observe(facts(), None, 5, 30)
        await until(lambda: session.result)
        self.assertEqual(session.result.classification, 'ON_TASK')
        self.assertIn('SPRINT_STARTED', choices[1].kinds)
        self.assertTrue(choices[0].closed)

    async def test_pending_observations_coalesce_while_previous_turn_is_busy(self):
        class SlowTurn(Trace):
            def __init__(self):
                super().__init__()
                self.ready = asyncio.Event()
                self.ready.set()
                self.windows = []

            async def wait_ready(self):
                await self.ready.wait()

            async def send(self, event):
                if event.kind == 'FOREGROUND_CHANGED':
                    self.windows.append(event.data[1])
                if event.kind == 'ACTIVITY_DURATION':
                    self.ready.clear()
                await super().send(event)

        transport = SlowTurn()
        session = LiveSession({}, Mock(), Mock(), lambda: transport)
        self.addAsyncCleanup(session.close)
        session.start('one', facts()['sprint'])
        session.observe(facts('Synthetic editor'), None, 1, 10)
        await until(lambda: session.result)
        session.observe(facts('intermediate'), None, 2, 11)
        await asyncio.sleep(.02)
        self.assertEqual(transport.windows, ['Synthetic editor'])
        session.observe(facts('latest'), None, 3, 12)
        transport.ready.set()
        await until(lambda: session.result and session.result.observed_activity == 'latest')
        self.assertEqual(transport.windows, ['Synthetic editor', 'latest'])
        self.assertEqual(session.reconnects, 0)

    async def test_latest_frame_waits_for_audio_turn_and_is_sent_once(self):
        class BusyTurn(Trace):
            def __init__(self):
                super().__init__()
                self.ready = asyncio.Event()
                self.ready.set()
                self.images = []
                self.turns = []

            async def wait_ready(self):
                await self.ready.wait()

            async def send(self, event):
                if event.kind == 'SCREEN_FRAME':
                    self.images.append(event.data)
                if event.kind == 'ACTIVITY_DURATION':
                    self.turns.append(event)
                    self.ready.clear()
                await super().send(event)

        transport = BusyTurn()
        cancel_audio = Mock()
        session = LiveSession({'live_analysis_seconds': 2}, Mock(), cancel_audio, lambda: transport)
        self.addAsyncCleanup(session.close)
        session.start('test', facts()['sprint'])
        first = Frame(b'first', b'a', 1, 1, 20)
        session.observe(facts(), first, 1, 20)
        await until(lambda: session.result)
        cancellations = cancel_audio.call_count
        session.observe(facts(), Frame(b'obsolete', b'b', 1, 1, 22), 1, 22)
        await asyncio.sleep(.02)
        newest = Frame(b'newest', b'c', 1, 1, 23)
        session.observe(facts(), newest, 1, 23)
        self.assertEqual(transport.images, [first])
        self.assertEqual(cancel_audio.call_count, cancellations)
        transport.ready.set()
        await until(lambda: len(transport.turns) == 2)
        self.assertEqual(transport.images, [first, newest])
        self.assertEqual(transport.turns[-1].stamp, session.stream.stamp)
        self.assertEqual(cancel_audio.call_count, cancellations)

    async def test_refresh_wait_discards_image_for_previous_window(self):
        requested, release = asyncio.Event(), asyncio.Event()
        old = Frame(b'old refresh JPEG', b'a', 1, 1, 20)
        new = Frame(b'new window JPEG', b'b', 1, 1, 21)

        class Refresh(Trace):
            def __init__(self):
                super().__init__()
                self.frames = []

            def needs_visual_refresh(self, frame):
                return frame is None

            async def send(self, event):
                if event.kind == 'SCREEN_FRAME':
                    self.frames.append(event.data)
                await super().send(event)

        async def fresh(_stamp):
            requested.set()
            await release.wait()
            return old

        transport = Refresh()
        session = LiveSession({}, Mock(), Mock(), lambda: transport, refresh_visual=fresh)
        self.addAsyncCleanup(session.close)
        session.start('test', facts()['sprint'])
        session.observe(facts(), None, 'old', 20)
        await asyncio.wait_for(requested.wait(), 1)
        session.observe(facts('new window'), new, 'new', 21)
        release.set()
        await until(lambda: session.result is not None)
        self.assertEqual(transport.frames, [new])

    async def test_correction_interrupts_and_clears_accepted_response(self):
        session = LiveSession({}, Mock(), Mock(), MockLiveTransport)
        self.addAsyncCleanup(session.close)
        session.start('one', facts()['sprint'])
        session.observe(facts(), None, 1, 1)
        await until(lambda: session.result)
        stamp = session.stream.stamp
        self.assertTrue(session.command('USER_CORRECTION', 'incorrect'))
        self.assertNotEqual(stamp, session.stream.stamp)
        self.assertIsNone(session.result)

import asyncio
import unittest
from unittest.mock import Mock

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

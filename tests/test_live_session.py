import asyncio
import unittest
from unittest.mock import Mock

from live_session import LiveSession, MockLiveTransport, Output
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
        self.assertIsNone(session.result)
        self.assertNotEqual(session.state, 'ERROR')
        session.stop('BREAK_STARTED', True)
        self.assertEqual(session.state, 'PAUSED')
        session.start('two', {'goal': 'different'}, 'BREAK_ENDED')
        session.observe(facts(), None, 1, 20)
        await until(lambda: session.result)
        self.assertIn('BREAK_STARTED', transports[0].kinds)
        self.assertTrue(transports[0].closed)
        self.assertNotEqual(session.stream.stamp.generation, old.generation)
        self.assertEqual(session.result.classification, 'UNCERTAIN')
        session.stop('SPRINT_COMPLETED')
        await session.close()
        self.assertTrue(transports[-1].closed)
        self.assertIn('SPRINT_COMPLETED', transports[-1].kinds)
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
        self.assertEqual(session.state, 'ERROR')
        self.assertIsNone(session.result)
        session.observe(facts(), None, 5, 30)
        await until(lambda: session.result)
        self.assertEqual(session.result.classification, 'ON_TASK')
        self.assertIn('SPRINT_STARTED', choices[1].kinds)
        self.assertTrue(choices[0].closed)

    async def test_correction_interrupts_and_clears_accepted_response(self):
        session = LiveSession({}, Mock(), Mock())
        self.addAsyncCleanup(session.close)
        session.start('one', facts()['sprint'])
        session.observe(facts(), None, 1, 1)
        await until(lambda: session.result)
        stamp = session.stream.stamp
        self.assertTrue(session.command('USER_CORRECTION', 'incorrect'))
        self.assertNotEqual(stamp, session.stream.stamp)
        self.assertIsNone(session.result)


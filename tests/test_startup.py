import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from launcher import StartupOrchestrator
from startup_manager import STARTUP_DEFAULTS, startup_delay


def configuration(**settings):
    return {**STARTUP_DEFAULTS, **settings}


class StartupConfigTests(unittest.TestCase):
    def test_user_delay_bounded(self):
        self.assertEqual(startup_delay({}), 8)
        self.assertEqual(startup_delay({'startup_delay_seconds': 'bad'}), 8)
        self.assertEqual(startup_delay({'startup_delay_seconds': -2}), 0)
        self.assertEqual(startup_delay({'startup_delay_seconds': 1000}), 180)


class StartupSupervisorTests(unittest.IsolatedAsyncioTestCase):
    def new(self, **settings):
        return StartupOrchestrator(Path('.'), configuration(**settings), None,
                                   tray_factory=Mock(), job=Mock(), open_dashboard=False, delay=0)

    def test_tray_uses_backend_reported_gemini_state(self):
        manager = self.new()
        manager.backend_status = {'sprint': {'state': 'ACTIVE'}, 'focus': {'live_engine': {'state': 'ERROR'}}}
        manager._update_tray()
        manager.tray.update.assert_called_once_with('limited', 'спринт идёт', 'ERROR')

    async def test_backend_recovery_limit_includes_failed_spawn_attempts(self):
        for automatic, count in ((True, 3), (False, 1)):
            manager = self.new(startup_recover=automatic)
            manager._pump = AsyncMock()
            manager.start_backend = AsyncMock(return_value=False)
            manager.wait = AsyncMock(return_value=True)
            manager.status = AsyncMock(return_value=None)

            def stop_when_exhausted(*args):
                if manager.start_backend.await_count >= count:
                    manager.exit.set()

            manager.tray.update.side_effect = stop_when_exhausted
            self.assertEqual(await manager.run(), 1)
            self.assertEqual(manager.start_backend.await_count, count)
            manager.tray.close.assert_called_once()

    async def test_backend_gate_requires_health_before_releasing_monitoring(self):
        manager = self.new()
        spawned = SimpleNamespace(poll=lambda: None)
        manager._launch_backend = Mock(return_value=spawned)
        manager.status = AsyncMock(side_effect=[None, {'startup_ready': True},
                                                 {'startup_ready': True, 'startup_running': True}])
        manager.send = AsyncMock(return_value=True)
        manager.wait = AsyncMock(return_value=True)
        self.assertTrue(await manager.start_backend())
        manager.send.assert_awaited_once_with('control', {'action':'startup_release'})
        self.assertTrue(manager.backend_status['startup_running'])

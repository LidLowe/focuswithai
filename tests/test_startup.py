import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from launcher import MODEL_ID, StartupOrchestrator, local_endpoint, model_is_available
from startup_manager import STARTUP_DEFAULTS, startup_delay


def configuration(**settings):
    return {**STARTUP_DEFAULTS, 'ai_provider': 'openai', 'openai_model': MODEL_ID,
            'openai_base_url': 'http://127.0.0.1:18080/v1', **settings}


class StartupConfigTests(unittest.TestCase):
    def test_local_host_ownership_and_exact_selected_model(self):
        self.assertTrue(local_endpoint(configuration())[3])
        self.assertFalse(local_endpoint(configuration(openai_base_url='http://localhost:11434/v1'))[3])
        self.assertIsNone(local_endpoint(configuration(openai_base_url='https://example.com/v1')))
        self.assertIsNone(local_endpoint(configuration(openai_base_url='http://user@localhost:18080/v1')))
        listing = {'data': [{'id': MODEL_ID}, None, {'id': 5}]}
        self.assertTrue(model_is_available(listing, MODEL_ID))
        self.assertFalse(model_is_available(listing, 'another-model'))

    def test_user_delay_bounded(self):
        self.assertEqual(startup_delay({}), 8)
        self.assertEqual(startup_delay({'startup_delay_seconds': 'bad'}), 8)
        self.assertEqual(startup_delay({'startup_delay_seconds': -2}), 0)
        self.assertEqual(startup_delay({'startup_delay_seconds': 1000}), 180)


class StartupSupervisorTests(unittest.IsolatedAsyncioTestCase):
    def new(self, **settings):
        return StartupOrchestrator(Path('.'), configuration(**settings), None,
                                   tray_factory=Mock(), job=Mock(), open_dashboard=False, delay=0)

    async def test_reuse_existing_llm_and_avoid_duplicate_on_model_mismatch(self):
        manager = self.new()
        manager.probe_llm = AsyncMock(return_value='готов')
        manager._launch_llm = Mock()
        await manager.ensure_llm()
        self.assertIsNone(manager.llm)
        manager.probe_llm.return_value = 'модель не найдена'
        manager.port_occupied = AsyncMock(return_value=True)
        await manager.ensure_llm()
        manager._launch_llm.assert_not_called()

    async def test_llm_recovery_limit_includes_failed_spawn_attempts(self):
        for automatic, count in ((True, 3), (False, 1)):
            manager = self.new(startup_recover=automatic)
            manager.probe_llm = AsyncMock(return_value='недоступен')
            manager.port_occupied = AsyncMock(return_value=False)
            manager.wait = AsyncMock(return_value=True)
            manager._launch_llm = Mock(side_effect=OSError('fixture'))
            with self.assertLogs(level='ERROR'):
                await manager.ensure_llm()
            self.assertEqual(manager._launch_llm.call_count, count)
            self.assertIsNone(manager.llm)

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

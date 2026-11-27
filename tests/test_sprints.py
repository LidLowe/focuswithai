import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from activity_context import ContextAggregator, Privacy, sprint_context
from context_stream import ContextStream
from focus_meter import ActivityState, Foreground
from sprints import SPRINT_TEXT_LIMITS, SprintManager


class SprintTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'sprints.json'
        self.now = 100.0
        self.open_store()

    def open_store(self):
        self.store = SprintManager(self.path, monotonic=lambda: self.now, wall=lambda: 1000 + self.now)

    def start(self, **data):
        return self.store.command('quick_start', {'goal': 'Learn VLANs', **data})['id']

    def command(self, id, action, **data):
        return self.store.command(action, {'id': id, **data})

    def test_pause_break_restart_and_timed_completion(self):
        sprint = self.start(duration_minutes=1)
        self.now += 20
        self.command(sprint, 'pause')
        self.now += 200
        self.assertEqual(self.store.summary()['remaining'], 40)
        self.command(sprint, 'resume')
        self.now += 10
        self.command(sprint, 'break', minutes=1)
        self.now += 59
        self.assertIsNone(self.store.tick())
        self.assertEqual(self.store.summary()['seconds'], 30)
        self.now += 1
        self.assertEqual(self.store.tick(), 'resume')
        self.command(sprint, 'resume')
        self.now += 7
        self.store.tick()
        self.now += 600
        self.open_store()
        self.assertEqual(self.store.summary()['state'], 'PAUSED')
        self.assertTrue(self.store.summary()['recovered'])
        self.assertEqual(self.store.summary()['remaining'], 23)
        self.command(sprint, 'resume')
        self.now += 23
        self.assertEqual(self.store.tick(), 'complete')
        report = self.command(sprint, 'complete')
        self.assertEqual((report['seconds'], report['actual_outcome']), (60, ''))
        self.assertIsNone(self.store.summary())
        self.assertEqual(self.command(sprint, 'outcome', actual_outcome='Configured VLAN 10')['actual_outcome'],
                         'Configured VLAN 10')

    def test_reports_and_goal_revision(self):
        sprint = self.start(subtasks=['Read docs', 'Test switch'])
        self.store.observe(self.now, 'ON_TASK', r'C:\Apps\terminal.exe')
        self.now += 1
        self.store.observe(self.now, 'OFF_TASK', r'C:\Apps\browser.exe')
        self.now += 1
        self.store.observe(self.now, 'UNCERTAIN', r'C:\Apps\browser.exe')
        self.now += 1
        self.store.intervention()
        self.command(sprint, 'subtask', index=0, done=True)
        self.command(sprint, 'edit', description='Network lab')
        self.assertEqual(self.store.current['revision'], 0)
        self.command(sprint, 'edit', goal='Configure routing')
        self.assertEqual(self.store.current['revision'], 1)
        self.assertEqual(json.loads(self.store.llm_context)['goal'], 'Configure routing')
        self.command(sprint, 'pause')
        self.store.observe(self.now, 'OFF_TASK', 'untracked.exe')
        self.store.intervention()
        self.now += 120
        result = self.command(sprint, 'complete', actual_outcome='Routing works')
        self.assertEqual(result['seconds'], 3)
        self.assertEqual(result['report'], {'ON_TASK': 1, 'OFF_TASK': 1, 'interventions': 1,
                                           'apps': {'browser.exe': 2, 'terminal.exe': 1}})
        self.assertTrue(result['subtasks'][0]['done'])

    def test_templates_and_reuse_reset_progress(self):
        sprint = self.start(title='Networking', subtasks=['Test switch'])
        self.command(sprint, 'subtask', index=0, done=True)
        self.command(sprint, 'save_template', name='Lab')
        template = self.store.snapshot()['templates'][0]
        draft = self.command(template['id'], 'use_template')
        self.assertEqual(draft['state'], 'NOT_STARTED')
        self.assertFalse(draft['subtasks'][0]['done'])
        with self.assertRaises(ValueError):
            self.command(draft['id'], 'start')
        self.command(sprint, 'cancel')
        reused = self.command(sprint, 'reuse')
        self.assertEqual(reused['goal'], 'Learn VLANs')
        self.assertFalse(reused['subtasks'][0]['done'])
        started = self.command(draft['id'], 'edit_start', goal='Lab 2')
        self.assertEqual(started['state'], 'ACTIVE')
        self.assertEqual(started['goal'], 'Lab 2')

    def test_invalid_input_and_failed_write_leave_current_unchanged(self):
        with self.assertRaises(ValueError):
            self.store.command('quick_start', {'goal': ' '})
        sprint = self.start()
        with patch.object(self.store, 'save', side_effect=OSError):
            with self.assertRaises(OSError):
                self.command(sprint, 'cancel')
        self.assertEqual(self.store.summary()['state'], 'ACTIVE')
        self.assertEqual(len(self.store.snapshot()['sprints']), 1)


    def test_descriptive_fields_and_all_subtasks_reach_context_without_truncation(self):
        fields = {key: ('  Study circuit models.\n' + 'Ж' * limit)[:limit - len(key)] + key
                  for key, limit in SPRINT_TEXT_LIMITS.items() if key != 'title'}
        tasks = [f'{index:02d} ' + 'x' * 197 for index in range(30)]
        sprint = self.start(**fields, subtasks=tasks)
        self.open_store()
        self.command(sprint, 'resume')
        context = sprint_context(self.store.llm_context)
        for key, value in fields.items():
            self.assertEqual(context[key], value)
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                self.command(sprint, 'edit', **{key: value + 'x'})
        self.assertEqual([task['text'] for task in context['subtasks']], tasks)
        self.assertEqual(sprint_context(json.dumps({'goal': 'Read docs\npassword=fixture'}))['goal'],
                         'Read docs\n[redacted]')

    def test_live_sprint_goal_replaces_old_goal(self):
        item = self.start(goal='Learn VLANs', description='Network lab')
        context = ContextAggregator(Privacy({}))
        context.sample(5, Foreground(title='STM32 manual', executable='editor.exe'))
        facts, _ = context.snapshot(9, self.store.llm_context, 0)
        stream = ContextStream()
        stream.start(item, facts['sprint'])
        self.assertEqual(stream.preview()['SPRINT_STARTED']['goal'], 'Learn VLANs')
        old_stamp = stream.stamp
        self.command(item, 'edit', goal='Read the STM32 manual')
        facts, _ = context.snapshot(10, self.store.llm_context, 0)
        stream.start(item, facts['sprint'], 'SPRINT_UPDATED')
        self.assertNotEqual(old_stamp, stream.stamp)
        self.assertEqual(stream.preview()['SPRINT_UPDATED']['goal'], 'Read the STM32 manual')

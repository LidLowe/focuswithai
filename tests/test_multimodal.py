import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


from activity_protocol import Classification, Result
from activity_context import ContextAggregator, Frame, Privacy
from config_store import SECRET_KEYS, load_config, save_config
from decision import DecisionEngine
from focus_meter import ActivityState, DistractionMeter, Foreground


def response(label='OFF_TASK', relevance=.1, confidence=.93):
    return json.dumps(dict(observed_activity='Mock fixture', classification=label, relevance=relevance,
                           confidence=confidence, reason='Synthetic classification',
                           recommended_action='INTERVENTION', suggested_next_step='Open the lab notes'))


class ObservationTests(unittest.TestCase):
    def test_exclusions_and_approved_window(self):
        approved = r'C:\Work\Editor.exe'
        privacy = Privacy({'capture_apps': [approved, r'C:\Chrome\chrome.exe'],
                           'activity_excluded_windows': ['private document'],
                           'browser_context_domains': ['docs.example.org']})
        self.assertTrue(privacy.capture_allowed(Foreground(1, 2, 'Lab notes', approved)))
        self.assertFalse(privacy.capture_allowed(Foreground(1, 2, 'Sign in', approved)))
        self.assertFalse(privacy.capture_allowed(Foreground(1, 2, 'Private document', approved)))
        self.assertTrue(privacy.capture_allowed(Foreground(1, 2, 'Lab notes', r'C:\Chrome\chrome.exe')))
        self.assertFalse(privacy.capture_allowed(Foreground(1, 2, 'Lab notes - Incognito', r'C:\Chrome\chrome.exe')))
        self.assertTrue(privacy.excluded(Foreground(1, 2, 'Chat', 'signal.exe')))
        self.assertEqual(privacy.browser('api.docs.example.org', 'VLAN docs')['domain'], 'api.docs.example.org')
        self.assertIsNone(privacy.browser('notdocs.example.org', 'VLAN docs'))
        self.assertIsNone(privacy.browser('docs.example.org', 'Banking password'))

    def test_chrome_address_privacy_works_without_extension(self):
        privacy = Privacy({})
        self.assertTrue(privacy.browser_capture_allowed('docs.example.org/reference'))
        self.assertTrue(privacy.browser_capture_allowed('http://localhost:3000/work'))
        for address in ('', 'chrome://settings', 'file:///C:/example.html', 'https://user@example.org/',
                        'https://example.org/oauth/start', 'https://example.org/sign%2Din',
                        'https://our-bank.example.org/', 'https://example.org/?access_token=fixture',
                        'https://example.org/checkout'):
            with self.subTest(address=address):
                self.assertFalse(privacy.browser_capture_allowed(address))
        scoped = Privacy({'browser_context_domains': ['docs.example.org']})
        self.assertTrue(scoped.browser_capture_allowed('https://api.docs.example.org/reference'))
        self.assertFalse(scoped.browser_capture_allowed('https://notdocs.example.org/reference'))

    def test_rolling_history_sanitization_and_image_deduplication(self):
        context = ContextAggregator(Privacy({}))
        window = Foreground(9, 3, 'token=secretvalue VLAN notes', r'C:\Work\editor.exe')
        for index in range(12):
            context.sample(index, Foreground(index + 1, 3, str(index), 'editor.exe'))
        context.sample(12, window)
        frame = Frame(b'jpeg', b'\x05' * 100, 640, 400, 12)
        context.frame = frame
        facts, sent = context.snapshot(15, '{"goal":"VLANs"}', 12)
        self.assertEqual(facts['window_title'], '[redacted] VLAN notes')
        self.assertEqual(facts['application'], 'editor.exe')
        self.assertEqual(facts['activity_seconds'], 3)
        self.assertEqual(len(facts['recent_switches']), 5)
        self.assertIs(sent, frame)
        context.sent_frame = frame
        context.frame = Frame(b'nearly same', b'\x07' * 100, 640, 400, 13)
        self.assertIsNone(context.snapshot(16, '{"goal":"VLANs"}', 12)[1])
        context.frame = Frame(b'different', b'\x30' * 100, 640, 400, 13)
        self.assertIsNotNone(context.snapshot(16, '{"goal":"VLANs"}', 12)[1])
        context.sample(17, Foreground(2, 1, 'Incognito banking', 'chrome.exe'))
        self.assertIsNone(context.frame)

    def test_validation_rejects_tools_confidence_and_injection_fields(self):
        for invalid in ({'confidence': True}, {'confidence': float('nan')}, {'classification': 'BAN_APP'},
                        {'command': 'close IDE'}, {'suggested_next_step': ['command']}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                Result.parse(json.dumps({**json.loads(response()), **invalid}))
        self.assertEqual(Result.parse(response()).classification, Classification.OFF_TASK)

    def test_workflow_decisions_and_delayed_warnings(self):
        statuses = {
            'IDE for sprint goal': ('ON_TASK', 1, .93, ActivityState.ON_TASK),
            'relevant technical docs': ('INDIRECTLY_RELATED', .8, .93, ActivityState.ON_TASK),
            'unrelated technical docs': ('OFF_TASK', .1, .93, ActivityState.OFF_TASK),
            'tutorial video': ('INDIRECTLY_RELATED', .8, .93, ActivityState.ON_TASK),
            'background music': ('ON_TASK', 1, .93, ActivityState.ON_TASK),
            'game unrelated to sprint': ('OFF_TASK', 0, .98, ActivityState.OFF_TASK),
            'brief switch': ('OFF_TASK', .1, .59, ActivityState.UNCERTAIN),
            'untrusted instructions': ('UNCERTAIN', .1, .97, ActivityState.UNCERTAIN),
            'scheduled break': ('BREAK', .1, .95, ActivityState.UNCERTAIN),
        }
        decision = DecisionEngine({})
        for scenario, (label, relevance, confidence, wanted) in statuses.items():
            with self.subTest(scenario=scenario):
                decision.reset()
                result = Result.parse(response(label, relevance, confidence))
                state, weight, confirmed = decision.evaluate(0, result, 1, scenario)
                self.assertIs(state, wanted)
                self.assertFalse(confirmed)
                if wanted is ActivityState.OFF_TASK:
                    self.assertFalse(decision.evaluate(10, result, 1, scenario)[2])
                    self.assertTrue(decision.evaluate(15, result, 2, scenario)[2])
                    self.assertGreater(decision.evaluate(15, result, 2, scenario)[1], weight)
        decision.correct(21, 'game')
        result = Result.parse(response())
        self.assertEqual(decision.evaluate(25, result, 3, 'game'), (ActivityState.UNCERTAIN, 0, False))
        meter = DistractionMeter({'focus_gain_per_second': 2})
        decision.reset()
        meter.advance(25, ActivityState.OFF_TASK)
        meter.advance(26, ActivityState.OFF_TASK)
        state, weight, confirmed = decision.evaluate(27, None, 3, 'offline')
        meter.advance(27, state, weight)
        meter.advance(28, state, weight)
        self.assertEqual((meter.score, confirmed), (2, False))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows credential migration')
    def test_config_migration_backs_up_preferences_and_retains_gemini_credential(self):
        from config_store import GEMINI_KEY
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'config.json'
            original = {'schema_version': 1, GEMINI_KEY: 'fixture-key', 'openai_api_key': 'retired-key',
                        'activity_provider_mode': 'gemini_live', 'activity_allow_remote': True,
                        'focus_gain_per_second': 2, 'alarm_pack_id': 'sc', 'notification_mode': 'alarm_tts'}
            file.write_text(json.dumps(original), encoding='utf-8')
            secrets = {}
            with (patch('config_store.store_secret', side_effect=lambda path, key, value: secrets.update({key: value})),
                  patch('config_store.read_secret', side_effect=lambda path, key: secrets.get(key, ''))):
                config = load_config(file)
                self.assertEqual(config[GEMINI_KEY], 'fixture-key')
                self.assertEqual(config['notification_mode'], 'alarm_live')
                self.assertEqual(config['focus_gain_per_second'], 2)
                self.assertTrue(config['activity_allow_remote'])
                backup = file.with_name('config.pre-gemini-live.json')
                backed_up = json.loads(backup.read_text())
                self.assertEqual(backed_up['notification_mode'], 'alarm_tts')
                self.assertNotIn('fixture-key', backup.read_text())
                self.assertNotIn('retired-key', backup.read_text())
                self.assertFalse(any(key in json.loads(file.read_text()) for key in SECRET_KEYS))
                save_config({**config, GEMINI_KEY: 'replacement'}, file)
                self.assertEqual(load_config(file)[GEMINI_KEY], 'replacement')
                self.assertEqual(json.loads(backup.read_text()), backed_up)

    def test_migration_requires_new_consent_from_other_backends(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps({'schema_version': 1, 'activity_allow_remote': True,
                                        'activity_send_remote_images': True}), encoding='utf-8')
            with patch('config_store.read_secret', return_value=''):
                config = load_config(path)
            self.assertFalse(config['activity_allow_remote'])
            self.assertFalse(config['activity_send_remote_images'])

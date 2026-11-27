"""Bounded local observation history and privacy filters for classification."""

import json
import ntpath
import re
from collections import deque
from dataclasses import dataclass

from focus_meter import Foreground


CONTEXT_DEFAULTS = {
    'activity_excluded_apps': [],
    'activity_excluded_windows': [],
    'browser_context_domains': [],
    'media_context_enabled': False,
    'capture_enabled': False,
    'capture_apps': [],
    'capture_mode': 'event',
    'capture_interval': 8,
    'capture_width': 768,
    'capture_height': 480,
    'capture_quality': 72,
    'activity_provider_mode': 'request',
    'activity_provider_vision': False,
    'activity_allow_remote': False,
    'activity_send_remote_images': False,
    'activity_system_prompt': '',
    'activity_min_confidence': .75,
    'activity_confirmation_seconds': 5,
    'activity_session_token_budget': 6000,
}

BROWSERS = frozenset(('chrome.exe', 'msedge.exe', 'firefox.exe', 'brave.exe', 'opera.exe',
                      'vivaldi.exe', 'chromium.exe', 'arc.exe', 'waterfox.exe', 'librewolf.exe'))
PRIVATE_APPS = frozenset(('credentialuibroker.exe', 'logonui.exe', 'lockapp.exe', 'keepass.exe',
                          'keepassxc.exe', '1password.exe', 'bitwarden.exe', 'telegram.exe',
                          'discord.exe', 'signal.exe', 'whatsapp.exe', 'slack.exe', 'teams.exe',
                          'ms-teams.exe', 'outlook.exe', 'olk.exe', 'thunderbird.exe', 'skype.exe'))
PRIVATE_TEXT = re.compile(
    r'\b(passwords?|passcodes?|sign[ -]?in|log[ -]?in|bank(?:ing)?|payment|wallet|'
    r'private browsing|incognito|inprivate|credentials?|inbox|webmail|direct messages?|'
    r'парол\w*|банк\w*|плат[её]ж\w*|авторизаци\w*|вход|инкогнито|переписк\w*)\b', re.I,
)
SECRET = re.compile(r'\b(?:sk-[a-zA-Z0-9_-]{12,}|AIza[\w-]{30,}|gh[pousr]_[\w]{20,}|'
                    r'eyJ[\w-]+\.eyJ[\w-]+\.[\w-]+|(?:api[_ -]?key|token|secret|password)'
                    r'\s*[:=]\s*[^\s,;]+)', re.I)
DOMAIN = re.compile(r'(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}')


def clean(value, limit=160):
    return SECRET.sub('[redacted]', ' '.join(value.split()))[:limit] if isinstance(value, str) else ''


def items(config, key):
    values = config.get(key, [])
    return tuple(x.strip().casefold() for x in values if isinstance(x, str) and x.strip()) if isinstance(values, list) else ()


def setting(config, key, low, high):
    try:
        value = float(config.get(key, CONTEXT_DEFAULTS[key]))
        if low <= value <= high:
            return value
    except (TypeError, ValueError, OverflowError):
        pass
    return CONTEXT_DEFAULTS[key]


@dataclass(frozen=True)
class Frame:
    data: bytes
    fingerprint: bytes
    width: int
    height: int
    captured_at: float

    def differs(self, other):
        if other is None:
            return True
        pairs = zip(self.fingerprint, other.fingerprint)
        return sum(abs(a - b) >= 12 for a, b in pairs) >= max(1, len(self.fingerprint) // 125)


class Privacy:
    def __init__(self, config):
        self.apps = set(items(config, 'activity_excluded_apps')) | PRIVATE_APPS
        self.windows = items(config, 'activity_excluded_windows')
        self.capture_apps = set(items(config, 'capture_apps'))
        self.domains = set(items(config, 'browser_context_domains'))

    def excluded(self, window):
        exe = ntpath.basename(window.executable).casefold()
        title = window.title.casefold()
        return (exe in self.apps or window.executable.casefold() in self.apps
                or any(term in title for term in self.windows) or bool(PRIVATE_TEXT.search(title)))

    def capture_allowed(self, window):
        executable = window.executable.casefold()
        return (window.handle and bool(ntpath.splitdrive(executable)[0]) and ntpath.isabs(executable)
                and ntpath.basename(executable).endswith('.exe') and executable in self.capture_apps
                and ntpath.basename(executable) not in BROWSERS
                and not self.excluded(window))

    def browser(self, domain, title):
        if not isinstance(domain, str) or not isinstance(title, str):
            return None
        domain = domain.casefold().strip('.')
        if (not DOMAIN.fullmatch(domain) or PRIVATE_TEXT.search(domain.replace('.', ' '))
                or PRIVATE_TEXT.search(title) or any(term in title.casefold() for term in self.windows)
                or not any(domain == entry or domain.endswith('.' + entry) for entry in self.domains)):
            return None
        return {'domain': domain, 'title': clean(title)}


class ContextAggregator:
    def __init__(self, privacy):
        self.privacy = privacy
        self.reset()

    def reset(self):
        self.switches = deque(maxlen=8)
        self.decisions = deque(maxlen=5)
        self.window = Foreground()
        self.entered = 0.0
        self.frame = None
        self.sent_frame = None
        self.browser = None
        self.media = None

    def sample(self, now, window):
        if window != self.window:
            old_app = ntpath.basename(self.window.executable)
            if old_app and not self.privacy.excluded(self.window):
                self.switches.append((now, clean(old_app, 70), round(now - self.entered)))
            self.entered, self.window = now, window
            self.frame = self.sent_frame = None
            self.browser = None

    def decision(self, now, result):
        self.decisions.append((now, result.classification.value, round(result.confidence, 2)))

    def snapshot(self, now, sprint, score):
        window = self.window
        app = ntpath.basename(window.executable)
        browser = None
        if self.browser and now - self.browser['seen'] < 45:
            browser = {key: self.browser[key] for key in ('domain', 'title', 'tab_title')}
            browser['seconds'] = round(now - self.browser['started'])
        media = None
        if self.media and now - self.media[0] < 35:
            media = self.media[1]
        sprint_data = json.loads(sprint)
        facts = {
            'sprint': {key: ([{'text': clean(t['text'], 120), 'done': t['done']} for t in value[:15]]
                             if key == 'subtasks' else clean(value, 650) if isinstance(value, str) else value)
                       for key, value in sprint_data.items()},
            'application': clean(app), 'window_title': clean(window.title) if app.casefold() not in BROWSERS else '',
            'activity_seconds': round(now - self.entered),
            'browser': browser, 'media': media,
            'recent_switches': [{'app': name, 'seconds': duration, 'ago': round(now - at)}
                                for at, name, duration in list(self.switches)[-5:] if now - at < 120],
            'recent_classifications': [{'class': label, 'confidence': confidence, 'ago': round(now - at)}
                                       for at, label, confidence in self.decisions if now - at < 120],
            'distraction_score': int(score),
        }
        frame = self.frame if (self.frame and now - self.frame.captured_at < 60
                               and self.frame.differs(self.sent_frame)) else None
        return facts, frame

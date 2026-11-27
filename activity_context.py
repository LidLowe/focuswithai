"""Bounded local observation history and privacy filters for classification."""

import ipaddress
import json
import ntpath
import re
from collections import deque
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from focus_meter import Foreground


CONTEXT_DEFAULTS = {
    'activity_excluded_apps': [],
    'activity_excluded_windows': [],
    'browser_context_domains': [],
    'media_context_enabled': False,
    'capture_interval': 2,
    'capture_width': 1280,
    'capture_height': 720,
    'capture_quality': 80,
    'live_analysis_seconds': 8,
    'live_volume': .55,
    'live_proactive': False,
    'context_allowed_apps': [],
    'activity_allow_remote': False,
    'activity_classification_prompt': '',
    'activity_send_remote_images': False,
    'activity_min_confidence': .75,
    'activity_confirmation_seconds': 5,
}

CAPTURE_LIMITS = {
    'capture_interval': {'min': 1, 'max': 120, 'integer': False},
    'capture_width': {'min': 160, 'max': 7680, 'integer': True},
    'capture_height': {'min': 160, 'max': 7680, 'integer': True},
    'capture_quality': {'min': 35, 'max': 95, 'integer': True},
}
CONTEXT_FLAGS = ('activity_send_remote_images', 'media_context_enabled')
CONTEXT_LISTS = ('activity_excluded_apps', 'activity_excluded_windows', 'browser_context_domains')

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
PRIVATE_BROWSER_TEXT = re.compile(
    r'\b(?:guest(?: mode)?|auth|oauth2?|sso|accounts?|checkout|billing|mail|token|otp|'
    r'one time code|verification code|credit card|guest session|гостев\w*|секрет\w*|код подтверждения)\b', re.I,
)
SECRET = re.compile(r'\b(?:sk-[a-zA-Z0-9_-]{12,}|AIza[\w-]{30,}|AQ\.[\w-]{20,}|gh[pousr]_[\w]{20,}|'
                    r'eyJ[\w-]+\.eyJ[\w-]+\.[\w-]+|(?:api[_ -]?key|token|secret|password)'
                    r'\s*[:=]\s*[^\s,;]+)', re.I)
DOMAIN = re.compile(r'(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}')


def clean(value, limit=160):
    return SECRET.sub('[redacted]', ' '.join(value.split()))[:limit] if isinstance(value, str) else ''


def items(config, key):
    values = config.get(key, [])
    return tuple(x.strip().casefold() for x in values if isinstance(x, str) and x.strip()) if isinstance(values, list) else ()


def context_source_settings(config):
    return {key: config.get(key, CONTEXT_DEFAULTS[key])
            for key in (*CAPTURE_LIMITS, *CONTEXT_FLAGS, *CONTEXT_LISTS)}


def validate_context_settings(config):
    values = context_source_settings(config)
    for key, limits in CAPTURE_LIMITS.items():
        value = values[key]
        low, high = limits['min'], limits['max']
        if (type(value) not in (int, float) or not low <= value <= high
                or (limits['integer'] and value != int(value))):
            number = 'целое число' if limits['integer'] else 'число'
            raise ValueError(f'{key} — требуется {number} от {low} до {high}.')
    for key in CONTEXT_FLAGS:
        if type(values[key]) is not bool:
            raise ValueError(f'{key} — требуется true или false.')
    for key in CONTEXT_LISTS:
        if not isinstance(values[key], list) or any(not isinstance(item, str) or not item.strip() for item in values[key]):
            raise ValueError(f'{key} — требуется список непустых строк.')
    for item in values['browser_context_domains']:
        host = item.strip().casefold()
        if host != 'localhost' and not DOMAIN.fullmatch(host):
            try:
                ipaddress.ip_address(host)
            except ValueError:
                raise ValueError('browser_context_domains — укажи домен или IP без схемы, порта и пути.') from None
    return values


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
    source_key: tuple | None = None
    source_size: tuple[int, int] | None = None

    def differs(self, other):
        if other is None or self.source_key != other.source_key:
            return True
        pairs = zip(self.fingerprint, other.fingerprint)
        return sum(abs(a - b) >= 12 for a, b in pairs) >= max(1, len(self.fingerprint) // 125)


class Privacy:
    def __init__(self, config):
        self.apps = set(items(config, 'activity_excluded_apps')) | PRIVATE_APPS
        self.windows = items(config, 'activity_excluded_windows')
        self.domains = set(items(config, 'browser_context_domains'))

    def excluded(self, window):
        exe = ntpath.basename(window.executable).casefold()
        title = window.title.casefold()
        return (exe in self.apps or window.executable.casefold() in self.apps
                or any(term in title for term in self.windows) or bool(PRIVATE_TEXT.search(title)))

    def capture_allowed(self, window):
        executable = window.executable.casefold()
        name = ntpath.basename(executable)
        return (window.handle and bool(ntpath.splitdrive(executable)[0]) and ntpath.isabs(executable)
                and name.endswith('.exe') and not self.excluded(window))

    @staticmethod
    def sensitive_page(text):
        text = re.sub(r'[._/=?&%#-]+', ' ', unquote(text))
        return bool(PRIVATE_TEXT.search(text) or PRIVATE_BROWSER_TEXT.search(text))

    def browser_capture_reason(self, address):
        if not address:
            return 'url_unavailable' if self.domains else None
        if any(char.isspace() for char in address):
            return 'url_unavailable'
        # Chrome hides the scheme in the unfocused address bar.
        try:
            page = urlsplit(address if '://' in address else 'https://' + address)
            domain = (page.hostname or '').rstrip('.')
        except ValueError:
            return 'url_unavailable'
        if page.scheme not in ('http', 'https') or page.username is not None or page.password is not None:
            return 'sensitive_page'
        if domain != 'localhost' and not DOMAIN.fullmatch(domain):
            try:
                ipaddress.ip_address(domain)
            except ValueError:
                return 'url_unavailable'
        if self.sensitive_page(address):
            return 'sensitive_page'
        if self.domains and not self._matches_domain(domain):
            return 'domain_excluded'
        return None

    def _matches_domain(self, domain):
        return any(domain == entry or domain.endswith('.' + entry) for entry in self.domains)

    def browser(self, domain, title):
        if not isinstance(domain, str) or not isinstance(title, str):
            return None
        domain = domain.casefold().strip('.')
        if (not DOMAIN.fullmatch(domain) or self.sensitive_page(domain)
                or self.sensitive_page(title) or any(term in title.casefold() for term in self.windows)
                or not self._matches_domain(domain)):
            return None
        return {'domain': domain, 'title': clean(title)}


def sprint_context(sprint):
    return {key: ([{'text': SECRET.sub('[redacted]', task['text']), 'done': task['done']} for task in value]
                  if key == 'subtasks' else SECRET.sub('[redacted]', value) if isinstance(value, str) else value)
            for key, value in json.loads(sprint).items()}


class ContextAggregator:
    def __init__(self, privacy):
        self.privacy = privacy
        self.reset()

    def reset(self):
        self.switches = deque(maxlen=8)
        self.decisions = deque(maxlen=5)
        self.window = Foreground()
        self.entered = 0.0
        self.frame = self.frame_checked_at = None
        self.browser = None
        self.media = None
        self.user_apps = []

    def sample(self, now, window):
        if window != self.window:
            old_app = ntpath.basename(self.window.executable)
            if (old_app and not self.privacy.excluded(self.window)
                    and (old_app.casefold() not in BROWSERS or self.browser)):
                self.switches.append((now, clean(old_app, 70), round(now - self.entered)))
            self.entered, self.window = now, window
            self.frame = self.frame_checked_at = None
            self.browser = None

    def observation_key(self):
        browser = (self.browser['domain'], self.browser['title']) if self.browser else None
        frame_source = self.frame.source_key if self.frame else None
        return self.window, browser, frame_source

    def decision(self, now, result):
        self.decisions.append((now, result.classification.value, round(result.confidence, 2)))

    def current_frame(self, now):
        if self.frame and now - (self.frame_checked_at or self.frame.captured_at) < 60:
            return self.frame
        return None

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
        facts = {
            'sprint': sprint_context(sprint),
            'application': clean(app), 'window_title': clean(window.title) if app.casefold() not in BROWSERS else '',
            'activity_seconds': round(now - self.entered),
            'browser': browser, 'media': media, 'user_apps': self.user_apps,
            'recent_switches': [{'app': name, 'seconds': duration, 'ago': round(now - at)}
                                for at, name, duration in list(self.switches)[-5:] if now - at < 120],
            'recent_classifications': [{'class': label, 'confidence': confidence, 'ago': round(now - at)}
                                       for at, label, confidence in self.decisions if now - at < 120],
            'distraction_score': int(score),
        }
        return facts, self.current_frame(now)

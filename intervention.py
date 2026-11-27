"""Opt-in responses to focus thresholds, with a fail-open foreground guard."""

import asyncio
import ipaddress
import re
from threading import Thread
from pathlib import Path
from urllib.parse import urlsplit

from focus_meter import FOCUS_DEFAULTS, Severity, THRESHOLD_EVENTS, numeric


def site_host(value):
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        return ''
    try:
        parsed = urlsplit(value if '://' in value else 'https://' + value)
        host = (parsed.hostname or '').rstrip('.').encode('idna').decode('ascii').lower()
        if parsed.scheme not in ('http', 'https') or parsed.username is not None:
            return ''
        try:
            ipaddress.ip_address(host)
            return ''
        except ValueError:
            pass
        if host.endswith(('.local', '.localhost')) or not re.fullmatch(r'(?:[a-z0-9-]+\.)+[a-z][a-z0-9-]{1,62}', host):
            return ''
        return host
    except (ValueError, UnicodeError):
        return ''


class ForegroundGuard:
    """Minimizes matching foreground windows only during a time-bounded session."""

    def __init__(self, config, platform):
        self.platform = platform
        apps = config.get('intervention_apps', [])
        sites = config.get('intervention_sites', [])
        self.apps = {path for path in map(platform.app_path, apps if isinstance(apps, list) else []) if path}
        self.sites = {host for host in map(site_host, sites if isinstance(sites, list) else []) if host}
        self.duration = numeric(config, 'intervention_block_seconds', 5, 120)
        self.armed = False
        self.until = 0.0
        self.epoch = 0
        self.last_action = float('-inf')
        self.next_probe = 0.0
        self.probe = None

    def arm(self):
        if not (self.apps or self.sites):
            raise ValueError('Укажи хотя бы один допустимый путь к EXE или домен.')
        self.armed = True

    def start(self, now):
        if self.armed:
            self.until = now + self.duration
            self.epoch += 1

    def release(self):
        self.armed = False
        self.until = 0
        self.epoch += 1

    def _matches_site(self, value):
        host = site_host(value)
        return bool(host and any(host == domain or host.endswith('.' + domain) for domain in self.sites))

    def tick(self, now, window):
        if self.probe and self.probe.done():
            expected, epoch, address = self.probe.result()
            self.probe = None
            if now < self.until and epoch == self.epoch and expected == window and self._matches_site(address):
                self._apply(now, window)
        if self.until and now >= self.until:
            self.release()
        if not self.until or now - self.last_action < .7 or not window.executable:
            return
        if self.apps and self.platform.app_path(window.executable) in self.apps:
            self._apply(now, window)
        elif (self.sites and not self.probe and now >= self.next_probe
              and Path(window.executable).name.lower() in self.platform.BROWSERS):
            self.next_probe = now + .8
            self.probe = self._probe(window, self.epoch)

    def _probe(self, window, epoch):
        loop = asyncio.get_running_loop()
        result = loop.create_future()

        def deliver(address):
            if not result.done():
                result.set_result((window, epoch, address))

        def read_toolbar():
            try:
                address = self.platform.browser_address(window)
            except Exception:
                address = ''  # Fail open if the browser toolbar cannot be read.
            try:
                loop.call_soon_threadsafe(deliver, address)
            except RuntimeError:
                pass  # The browser provider can outlive FocusWithAI shutdown.

        Thread(target=read_toolbar, name='FocusWithAI browser toolbar', daemon=True).start()
        return result

    def _apply(self, now, window):
        if self.platform.minimize_if_still_foreground(window):
            self.last_action = now

    def close(self):
        self.release()
        if self.probe:
            self.probe.cancel()
            self.probe = None


class InterventionEngine:
    def __init__(self, config, platform, play):
        self.config = {**FOCUS_DEFAULTS, **config}
        self.play = play
        self.enabled = bool(self.config['intervention_enabled'])
        self.silenced = False
        self.warning_until = 0
        self.warning = None
        self.last = {}
        self.cooldown = numeric(config, 'focus_cooldown_seconds', 15, 300)
        self.guard = ForegroundGuard(config, platform)

    def disable(self):
        self.silenced = True
        self.clear()

    def clear(self):
        self.warning = None
        self.warning_until = 0
        self.guard.release()

    def trigger(self, now, previous, level, task, reason, *, on_audio=None, next_step=None):
        if self.silenced or (level < previous and level is not Severity.NORMAL):
            return
        if now - self.last.get(level, float('-inf')) < self.cooldown:
            return
        self.last[level] = now
        mode = None
        acted = False
        if level is Severity.CRITICAL and self.enabled:
            if self.config['intervention_alarm'] and self.config['intervention_announcer']:
                mode = 'alarm_voicepack'
            elif self.config['intervention_alarm']:
                mode = 'alarm'
            elif self.config['intervention_announcer']:
                mode = 'voicepack'
            if self.config['intervention_allow_blocking']:
                self.guard.start(now)
                acted = self.guard.until > now
            dialog = self.config['intervention_dialog']
            if self.config['intervention_visual'] or dialog:
                actions = [('К задаче', 'dismiss')] if dialog else []
                if dialog and self.config['intervention_suggest_break']:
                    actions += [('Пауза', 'break'), ('Сменить задачу', 'change_task')]
                if dialog:
                    actions.append(('Отключить', 'disable'))
                self.warning = ('Вернуться к фокусу', next_step or task, tuple(actions))
                self.warning_until = now + (20 if dialog else 8)
                acted = True
        if self.config['focus_audio'] or mode:
            if on_audio and not acted:
                self.play(THRESHOLD_EVENTS[level], task, reason, mode, on_play=on_audio)
            else:
                self.play(THRESHOLD_EVENTS[level], task, reason, mode)
        return acted

    def current_warning(self, now):
        return self.warning if now < self.warning_until else None

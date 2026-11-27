"""Lifecycle of optional, low-frequency context sources."""

import asyncio
import importlib.util
import ntpath
import threading
import time

from activity_context import BROWSERS, CHROME, ContextAggregator, Privacy, clean, items, setting


def background(function, *args):
    """A potentially stalled window provider cannot hold up the event loop or exit."""
    loop = asyncio.get_running_loop()
    result = loop.create_future()

    def deliver(value, error):
        if not result.done():
            result.set_result((value, error))

    def work():
        try:
            pair = function(*args), None
        except Exception as err:
            pair = None, type(err).__name__
        try:
            loop.call_soon_threadsafe(deliver, *pair)
        except RuntimeError:
            pass

    threading.Thread(target=work, daemon=True).start()
    return result


class Acquisition:
    def __init__(self, config, windows=None):
        self.windows = windows
        self.privacy = Privacy(config)
        self.context = ContextAggregator(self.privacy)
        self.enabled = bool(windows and config.get('capture_enabled'))
        self.has_capture = bool(windows and importlib.util.find_spec('PIL'))
        self.mode = config.get('capture_mode', 'event')
        self.interval = setting(config, 'capture_interval', 3, 120)
        self.dimensions = tuple(int(setting(config, key, 160, 1600)) for key in ('capture_width', 'capture_height'))
        self.quality = int(setting(config, 'capture_quality', 35, 95))
        self.media = windows.MediaMonitor() if windows and config.get('media_context_enabled') else None
        self.capture_job = self.media_job = self.apps_job = None
        self.next_capture = self.next_media = self.next_apps = 0
        self.capture_epoch = 0
        self.armed = False
        self.selected = None
        self.capture_error = None
        self.media_error = None
        self.apps = []
        self.allowed_apps = set(items(config, 'context_allowed_apps'))
        self.capture_backoff = 1
        self.active = False

    def arm(self, selection):
        if not self.active or not self.enabled or not self.has_capture or not self.privacy.capture_apps:
            raise ValueError('Нужны активный спринт, Pillow и разрешённый полный путь к рабочему приложению.')
        candidates = self.windows.capture_choices(self.privacy)
        choice = next((w for w in candidates if w['id'] == selection), None)
        if not choice:
            raise ValueError('Выбери доступное разрешённое рабочее окно.')
        self.stop_capture()
        self.selected = (choice['handle'], choice['pid'])
        self.armed = True
        self.next_capture = time.monotonic() + 2
        self.capture_error = None

    def stop_capture(self):
        self.armed = False
        self.selected = None
        self.capture_epoch += 1
        self.context.frame = self.context.sent_frame = self.context.frame_checked_at = None

    def suspend(self, *, keep_selection=False):
        self.active = False
        if self.apps_job:
            self.apps_job.cancel()
            self.apps_job = None
        if keep_selection:
            self.capture_epoch += 1
            self.next_capture = 0
        else:
            self.stop_capture()
        self.context.reset()
        if self.media_job:
            self.media_job.cancel()
            self.media_job = None
        self.apps = []

    def browser_event(self, data, now):
        if not self.active:
            return
        target = self.context.window
        if data.get('clear'):
            self.context.browser = None
            return
        if (ntpath.basename(target.executable).casefold() not in BROWSERS or self.privacy.excluded(target)
                or not isinstance(data.get('title'), str) or not data['title']
                or clean(data['title']) not in clean(target.title)
                or self.windows is None or self.windows.foreground() != target):
            return
        item = self.privacy.browser(data.get('domain'), data['title'])
        if item:
            previous = self.context.browser
            same_page = previous and all(previous[key] == item[key] for key in item)
            self.context.browser = {**item, 'tab_title': item['title'], 'seen': now,
                                    'started': previous['started'] if same_page else now}
        else:
            self.context.browser = None

    def sample(self, now, window, *, idle=False):
        self.active = True
        if self.context.browser and now - self.context.browser['seen'] >= 45:
            self.context.browser = None
        switched = window != self.context.window
        if switched and self.armed and self.selected and (window.handle, window.pid) != self.selected:
            self.capture_epoch += 1
        self.context.sample(now, window)
        if switched:
            self.capture_backoff = 1
        if self.apps_job and self.apps_job.done():
            value, _ = self.apps_job.result()
            self.apps_job = None
            if value is not None:
                self.apps = [name for name in value if name.casefold() not in self.privacy.apps]
                self.context.user_apps = [clean(n, 80) for n in self.apps if n.casefold() in self.allowed_apps][:32]
        if self.windows and not self.apps_job and now >= self.next_apps:
            self.next_apps = now + 20
            self.apps_job = background(self.windows.user_apps)
        if self.media_job and self.media_job.done():
            try:
                self.context.media = (now, self.media_job.result())
                self.media_error = None
            except Exception as err:
                self.media_error = type(err).__name__
                self.context.media = None
            self.media_job = None
        if self.media and not self.media_job and now >= self.next_media:
            self.next_media = now + 15
            self.media_job = asyncio.create_task(asyncio.wait_for(self.media.sample(), 4))

        if self.capture_job and self.capture_job.done():
            value, error = self.capture_job.result()
            self.capture_job = None
            if self.armed and self.job_epoch == self.capture_epoch and self.job_window == window:
                self.capture_error = error or (None if value else 'window_unavailable')
                # A successful identical capture keeps the existing frame fresh without re-sending it.
                self.context.frame_checked_at = value.captured_at if value else None
                if value and value.differs(self.context.frame):
                    self.context.frame = value
                    self.capture_backoff = 1
                else:
                    self.capture_backoff = min(4, self.capture_backoff * 2)
                if not value:
                    self.context.frame = None
                    if ntpath.basename(window.executable).casefold() == CHROME:
                        self.capture_backoff = 1
        if idle or not self.armed or not self.privacy.capture_allowed(window):
            return
        if self.selected is None:
            return
        if self.selected != (window.handle, window.pid) or self.capture_job or now < self.next_capture:
            return
        self.job_window, self.job_epoch = window, self.capture_epoch
        self.next_capture = now + (self.interval if self.mode == 'periodic'
                                   else min(120, self.interval * self.capture_backoff))
        args = (window, self.dimensions, self.quality)
        if ntpath.basename(window.executable).casefold() == CHROME:
            args += (self.privacy.browser_capture_allowed,)
        self.capture_job = background(self.windows.capture_window, *args)

    def browser_context_available(self, now, *, send_images):
        return bool(self.context.browser or (send_images and self.armed and self.context.current_frame(now)))

    def foreground_is_current(self):
        return not self.windows or self.windows.foreground() == self.context.window

    def status(self):
        frame = self.context.frame
        browser = self.context.browser
        tab = None
        if self.active and browser:
            tab = {key: browser[key] for key in ('domain', 'title', 'tab_title')}
            tab['seconds'] = max(0, int(time.monotonic() - browser['started']))
        return {'enabled': self.enabled, 'available': self.has_capture, 'armed': self.armed,
                'capturing': bool(self.armed and self.selected and self.privacy.capture_allowed(self.context.window)
                                  and self.selected == (self.context.window.handle, self.context.window.pid)),
                'last_frame': {'width': frame.width, 'height': frame.height} if frame else None,
                'capture_error': self.capture_error, 'media_error': self.media_error,
                'apps': self.apps, 'browser': tab,
                'media': self.context.media[1] if self.active and self.context.media else None}

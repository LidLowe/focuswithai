"""Lifecycle of optional, low-frequency context sources."""

import asyncio
import importlib.util
import ntpath
import threading
import time

from activity_context import BROWSERS, ContextAggregator, Privacy, clean, items, setting


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
        self.has_capture = bool(windows and importlib.util.find_spec('PIL'))
        self.interval = setting(config, 'capture_interval', 2, 120)
        self.dimensions = tuple(int(setting(config, key, 160, 1600)) for key in ('capture_width', 'capture_height'))
        self.quality = int(setting(config, 'capture_quality', 35, 95))
        self.media = windows.MediaMonitor() if windows and config.get('media_context_enabled') else None
        self.capture_job = self.media_job = self.apps_job = None
        self.next_capture = self.next_media = self.next_apps = 0
        self.capture_epoch = 0
        self.capture_sprint = None
        self.capture_skip = 'disabled'
        self.frames_captured = 0
        self.last_capture = None
        self.last_size = None
        self.media_error = None
        self.apps = []
        self.allowed_apps = set(items(config, 'context_allowed_apps'))
        self.capture_backoff = 1
        self.active = False

    def start_capture(self, sprint_id):
        if not self.has_capture:
            raise ValueError('Автозахват требует Windows и Pillow.')
        if self.capture_sprint == sprint_id:
            return
        self.stop_capture()
        self.capture_sprint = sprint_id
        self.capture_skip = None

    def stop_capture(self):
        self.capture_sprint = None
        self.capture_skip = 'disabled'
        self.reset_frame()

    def reset_frame(self):
        self.capture_epoch += 1
        self.next_capture = 0
        self.capture_backoff = 1
        self.context.frame = self.context.frame_checked_at = None

    def suspend(self):
        self.active = False
        self.reset_frame()
        if self.capture_sprint:
            self.capture_skip = 'sprint_inactive'
        if self.apps_job:
            self.apps_job.cancel()
            self.apps_job = None
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
        if window != self.context.window:
            self.reset_frame()
            if self.capture_sprint:
                self.capture_skip = 'foreground_changed'
        self.context.sample(now, window)
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

        self.sample_frame(now, window, idle)

    def sample_frame(self, now, window, idle):
        if self.capture_job and self.capture_job.done():
            result, error = self.capture_job.result()
            frame, reason = result if result is not None else (None, 'capture_failed')
            self.capture_job = None
            if self.job_epoch == self.capture_epoch:
                self.capture_skip = 'capture_failed' if error else reason
                self.context.frame_checked_at = frame.captured_at if frame else None
                if frame:
                    self.frames_captured += 1
                    self.last_capture = time.time()
                    self.last_size = {'width': frame.width, 'height': frame.height, 'bytes': len(frame.data)}
                    if frame.differs(self.context.frame):
                        self.context.frame = frame
                        self.capture_backoff = 1
                        self.next_capture = now + self.interval
                    else:
                        self.capture_skip = 'unchanged'
                        self.capture_backoff = min(4, self.capture_backoff * 2)
                        self.next_capture = now + min(120, self.interval * self.capture_backoff)
                else:
                    self.context.frame = None
                    self.capture_backoff = 1

        if not self.capture_sprint:
            return
        if idle or not self.privacy.capture_allowed(window):
            if self.context.frame or self.capture_job and self.job_epoch == self.capture_epoch:
                self.reset_frame()
            self.capture_skip = 'idle' if idle else 'window_excluded'
            return
        if self.capture_job or now < self.next_capture:
            return
        self.job_epoch = self.capture_epoch
        self.next_capture = now + min(120, self.interval * self.capture_backoff)
        self.capture_job = background(self.windows.capture_window, window, self.dimensions, self.quality,
                                      self.privacy.browser_capture_reason)

    def foreground_is_current(self):
        return not self.windows or self.windows.foreground() == self.context.window

    def status(self):
        browser = self.context.browser
        frame = self.context.current_frame(time.monotonic())
        tab = None
        if self.active and browser:
            tab = {key: browser[key] for key in ('domain', 'title', 'tab_title')}
            tab['seconds'] = max(0, int(time.monotonic() - browser['started']))
        return {'available': self.has_capture, 'capture_active': bool(self.active and self.capture_sprint),
                'capture_enabled': bool(self.capture_sprint), 'frames_captured': self.frames_captured,
                'last_capture': self.last_capture, 'last_size': self.last_size,
                'preview_frame_id': frame.captured_at if frame else None,
                'capture_skip': self.capture_skip, 'media_error': self.media_error,
                'apps': self.apps, 'browser': tab,
                'media': self.context.media[1] if self.active and self.context.media else None}

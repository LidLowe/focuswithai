"""Bounded, in-memory delta stream. A stamp identifies one usable observation."""
from collections import OrderedDict
from dataclasses import dataclass
import time


@dataclass(frozen=True)
class Stamp:
    sprint_id: str
    generation: int
    observation: int


@dataclass(frozen=True)
class ContextEvent:
    kind: str
    timestamp: float
    stamp: Stamp
    data: object
    sequence: int


class ContextStream:
    """At most one pending value per source, including a single latest JPEG."""
    def __init__(self):
        self.generation = 0
        self.sequence = 0
        self.clear()

    def clear(self):
        self.generation += 1
        self.stamp = None
        self.window_key = None
        self.latest = {}
        self.pending = OrderedDict()
        self.last_duration = None

    def start(self, sprint_id, sprint, kind='SPRINT_STARTED'):
        self.clear()
        self.stamp = Stamp(sprint_id, self.generation, 0)
        self.sprint_kind = kind
        self.put(kind, sprint)

    def advance(self):
        stamp = self.stamp
        self.stamp = Stamp(stamp.sprint_id, stamp.generation, stamp.observation + 1)

    def block(self):
        if self.stamp is None:
            return
        self.advance()
        self.window_key = self.last_duration = None
        self.latest = {self.sprint_kind: self.latest[self.sprint_kind]}
        self.pending.clear()

    def event(self, kind, data=None):
        self.sequence += 1
        return ContextEvent(kind, time.time(), self.stamp, data, self.sequence)

    def put(self, kind, data, remember=True):
        if remember:
            self.latest[kind] = data
        self.pending[kind] = self.event(kind, data)
        self.pending.move_to_end(kind)

    def observe(self, facts, frame, window_key, now, interval):
        browser = facts['browser']
        values = {
            'FOREGROUND_CHANGED': (facts['application'], facts['window_title']),
            'BROWSER_CONTEXT_CHANGED': {k: browser[k] for k in ('title', 'domain')} if browser else None,
            'MEDIA_CHANGED': facts['media'],
            'USER_APPS_CHANGED': facts['user_apps'],
            'SCREEN_FRAME': frame,
        }
        if self.window_key != window_key or self.latest.get('BROWSER_CONTEXT_CHANGED') != values['BROWSER_CONTEXT_CHANGED']:
            self.window_key = window_key
            self.advance()
            self.pending.clear()
            self.last_duration = None
            self.put(self.sprint_kind, self.latest[self.sprint_kind])
            # Explicitly clear old optional sources in the server's conversational context.
            changed = values
        else:
            changed = {k: v for k, v in values.items() if self.latest.get(k) != v}
        for kind, data in changed.items():
            self.put(kind, data)
        if 'ACTIVITY_DURATION' in self.pending:
            self.pending.move_to_end('ACTIVITY_DURATION')
        if self.last_duration is None or now - self.last_duration >= interval:
            self.last_duration = now
            self.put('ACTIVITY_DURATION', facts['activity_seconds'])
            self.put('RECENT_ACTIVITY', {
                key: facts[key] for key in ('recent_switches', 'recent_classifications', 'distraction_score', 'recent_warnings')
                if key in facts
            }, remember=False)
            self.pending.move_to_end('ACTIVITY_DURATION')
        return bool(self.pending)

    def replay(self):
        # Reconnect with current values only. Never replay old audio or corrections as commands.
        self.pending.clear()
        for kind, data in self.latest.copy().items():
            if kind != 'ACTIVITY_DURATION':
                self.put(kind, data)
        self.last_duration = None

    def preview(self):
        data = self.latest.copy()
        frame = data.get('SCREEN_FRAME')
        if frame:
            data['SCREEN_FRAME'] = {'width': frame.width, 'height': frame.height, 'bytes': len(frame.data)}
        return data

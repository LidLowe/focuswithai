"""Bounded, in-memory delta stream. A stamp identifies one usable observation."""
from collections import OrderedDict
from dataclasses import dataclass
import time

from performance import clock


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
    queued_ns: int | None = None
    recording_id: str | None = None
    current_frame: object = None


class ContextStream:
    """At most one pending value per source, including a single latest JPEG."""
    def __init__(self, profiler=None):
        self.profiler = profiler
        self.pending = OrderedDict()
        self.generation = 0
        self.sequence = 0
        self.clear()

    def clear(self):
        self._discard_frame()
        self.generation += 1
        self.stamp = None
        self.window_key = None
        self.latest = {}
        self.pending = OrderedDict()
        self.last_duration = self.last_seen = None
        self.visual_seen = False

    def start(self, sprint_id, sprint, kind='SPRINT_STARTED'):
        self.clear()
        self.stamp = Stamp(sprint_id, self.generation, 0)
        self.sprint_kind = kind
        self.put(kind, sprint)

    def advance(self):
        stamp = self.stamp
        self.visual_seen = False
        self.stamp = Stamp(stamp.sprint_id, stamp.generation, stamp.observation + 1)

    def block(self):
        if self.stamp is None:
            return
        self.advance()
        self.window_key = self.last_duration = None
        self.latest = {self.sprint_kind: self.latest[self.sprint_kind]}
        self._discard_frame()
        self.pending.clear()

    def event(self, kind, data=None):
        self.sequence += 1
        recording = self.profiler.active if self.profiler and kind in ('SCREEN_FRAME', 'ACTIVITY_DURATION') else None
        return ContextEvent(kind, time.time(), self.stamp, data, self.sequence,
                            clock() if recording else None, recording.id if recording else None)

    def _discard_frame(self, keep=None):
        recording = self.profiler.active if self.profiler else None
        event = self.pending.get('SCREEN_FRAME') if recording else None
        if event and event.data is not None and event.data is not keep:
            frame = event.data.profile
            scope = recording.scope(event.stamp, frame=frame.frame_id if frame and frame.recording_id == recording.id else None)
            recording.tally('frames_dropped', scope=scope)
            recording.tally('frame_queue_replaced', scope=scope)

    def put(self, kind, data, remember=True):
        if self.profiler and kind == 'SCREEN_FRAME':
            self._discard_frame(keep=data)
        if remember:
            self.latest[kind] = data
        self.pending[kind] = self.event(kind, data)
        self.pending.move_to_end(kind)

    def observe(self, facts, frame, window_key, now, interval, *, waiting_for_frame=False):
        self.last_seen = now
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
            self._discard_frame(keep=frame)
            self.pending.clear()
            self.last_duration = None
            self.put(self.sprint_kind, self.latest[self.sprint_kind])
            # Explicitly clear old optional sources in the server's conversational context.
            changed = values
        else:
            changed = {k: v for k, v in values.items() if self.latest.get(k) != v}
        for kind, data in changed.items():
            self.put(kind, data)
        turn_pending = 'ACTIVITY_DURATION' in self.pending
        if turn_pending:
            self.pending.move_to_end('ACTIVITY_DURATION')
        # Give the first capture a short head start. Metadata alone is still
        # enough to start a turn when there is no image or capture times out.
        first_image = frame is not None and not self.visual_seen
        self.visual_seen |= frame is not None
        due = self.last_duration is None or first_image or now - self.last_duration >= interval
        if (turn_pending and changed) or (not turn_pending and due and not waiting_for_frame):
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

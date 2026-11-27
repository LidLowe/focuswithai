"""Foreground sampling and distraction scoring."""

import ctypes
import math
import sys
from dataclasses import dataclass
from enum import Enum, IntEnum

from performance import clock
from voicepack import Event


class ActivityState(str, Enum):
    ON_TASK = 'ON_TASK'
    INDIRECTLY_RELATED = 'INDIRECTLY_RELATED'
    UNCERTAIN = 'UNCERTAIN'
    OFF_TASK = 'OFF_TASK'
    IDLE = 'IDLE'
    BREAK = 'BREAK'


class Severity(IntEnum):
    NORMAL = 0
    ATTENTION = 1
    WARNING = 2
    CRITICAL = 3


FOCUS_DEFAULTS = {
    'focus_meter_enabled': False,
    'focus_gain_per_second': 0.8,
    'focus_recovery_per_second': 1.4,
    'focus_thresholds': [30, 60, 85],
    'focus_hysteresis': 5,
    'focus_cooldown_seconds': 45,
    'focus_audio': True,
    'focus_hud_enabled': False,
    'focus_hud_corner': 'top_right',
    'focus_hud_width': 285,
    'focus_hud_height': 108,
    'focus_hud_opacity': 0.88,
    'focus_hud_click_through': True,
    'focus_hud_show_task': True,
    'focus_hud_show_sprint_time': True,
    'focus_hud_show_sprint_state': True,
    'focus_hud_show_distraction': True,
    'intervention_enabled': False,
    'intervention_alarm': False,
    'intervention_announcer': False,
    'intervention_visual': True,
    'intervention_dialog': False,
    'intervention_suggest_break': True,
    'intervention_allow_blocking': False,
    'intervention_block_seconds': 30,
    'intervention_apps': [],
    'intervention_sites': [],
}
THRESHOLD_EVENTS = {
    Severity.NORMAL: Event.RETURN_TO_WORK,
    Severity.ATTENTION: Event.ATTENTION,
    Severity.WARNING: Event.FOCUS_WARNING,
    Severity.CRITICAL: Event.FOCUS_CRITICAL,
}
def numeric(config, key, low, high):
    try:
        value = float(config.get(key, FOCUS_DEFAULTS[key]))
    except (ValueError, TypeError, OverflowError):
        value = FOCUS_DEFAULTS[key]
    if not math.isfinite(value):
        value = FOCUS_DEFAULTS[key]
    return min(high, max(low, value))


class DistractionMeter:
    def __init__(self, config):
        self.gain = numeric(config, 'focus_gain_per_second', 0, 20)
        self.recovery = numeric(config, 'focus_recovery_per_second', 0, 20)
        thresholds = config.get('focus_thresholds')
        if (not isinstance(thresholds, list) or len(thresholds) != 3
                or any(type(value) is not int for value in thresholds)
                or not 1 <= thresholds[0] < thresholds[1] < thresholds[2] <= 100):
            thresholds = FOCUS_DEFAULTS['focus_thresholds']
        self.thresholds = thresholds.copy()
        self.hysteresis = numeric(config, 'focus_hysteresis', 0, min(20, thresholds[0]))
        self.reset(None)

    def reset(self, now):
        self.score = 0.0
        self.level = Severity.NORMAL
        self.updated = now
        self.previous_state = ActivityState.BREAK

    def advance(self, now, state, weight=1.0):
        elapsed = 0 if self.updated is None else min(2.0, max(0, now - self.updated))
        if state != self.previous_state:
            elapsed = 0
        self.updated, self.previous_state = now, state
        if state is ActivityState.OFF_TASK:
            self.score = min(100.0, self.score + self.gain * elapsed * weight)
        elif state in (ActivityState.ON_TASK, ActivityState.INDIRECTLY_RELATED):
            self.score = max(0.0, self.score - self.recovery * elapsed * weight)
        old = self.level
        level = int(old)
        while level < 3 and self.score >= self.thresholds[level]:
            level += 1
        while level and self.score < self.thresholds[level - 1] - self.hysteresis:
            level -= 1
        if level and self.score == 0:
            level = 0
        self.level = Severity(level)
        return (old, self.level) if old != self.level else None


@dataclass(frozen=True)
class Foreground:
    handle: int = 0
    pid: int = 0
    title: str = ''
    executable: str = ''


def input_idle_seconds():
    """Time since mouse or keyboard input, without collecting input itself."""
    if sys.platform == 'win32':
        class LastInput(ctypes.Structure):
            _fields_ = [('size', ctypes.c_uint32), ('tick', ctypes.c_uint32)]

        info = LastInput(ctypes.sizeof(LastInput))
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            tick = ctypes.windll.kernel32.GetTickCount()
            return ((tick - info.tick) & 0xFFFFFFFF) / 1000
    return None


class ActivityMonitor:
    def __init__(self, get_window, idle_threshold, get_idle=input_idle_seconds):
        self.get_window = get_window
        self.get_idle = get_idle
        self.idle_threshold = idle_threshold

    def sample(self, recording=None):
        started = clock() if recording else None
        window = self.get_window()
        if recording:
            recording.latency('foreground', started, clock(), recording.scope())
        seconds = self.get_idle()
        idle = seconds is not None and seconds >= self.idle_threshold
        return window, idle

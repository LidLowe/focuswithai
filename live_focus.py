"""Bridge the duplex transport to the existing decision policy and audio coordinator."""
import time

from activity_context import clean
from commentary import CommentaryGate
from live_audio import LiveAudio
from gemini_live import DEFAULTS, GeminiLiveTransport
from live_session import LiveSession, number
from voicepack import Event

ALARMS = dict(START=Event.SESSION_START, DEVIATION=Event.DISTRACTION, NEXT_STEP=Event.ATTENTION,
              CLARIFY=Event.ATTENTION, RETURN=Event.RETURN_TO_WORK, DIFFICULTY=Event.FOCUS_WARNING,
              BREAK=Event.PERSISTENT_DISTRACTION, SUMMARY=Event.TASK_COMPLETED)


class LiveFocus:
    def __init__(self, config, owner, notifier, sound_enabled, *, transport_factory=None):
        self.owner, self.notifier, self.sound_enabled = owner, notifier, sound_enabled
        volume = number(config, 'live_volume', .55, 0, 1)
        self.audio = LiveAudio(notifier.player, volume)
        self.session = LiveSession({**DEFAULTS, **config}, self.output, self.audio.cancel,
                                   transport_factory or (lambda: GeminiLiveTransport(config)))
        self.gate = CommentaryGate(config, number(config, 'focus_cooldown_seconds', 45, 15, 300))
        self.allow_images = config.get('activity_send_remote_images') is True
        self.focus_audio = config.get('focus_audio', True)

        self.next_start = 'SPRINT_STARTED'
        self.pending_request = None

    def stop(self, kind=None, paused=False, *, keep_request=False):
        self.session.stop(kind, paused)
        if not keep_request:
            self.pending_request = None

    def request(self, kind):
        if not self.sound_allowed():
            raise ValueError('Включи звук фокуса и выбери Live Audio в разделе Live Engine.')
        self.pending_request = kind, time.monotonic() + 60

    def current(self, stamp):
        phase = self.owner.phase()
        acq = self.owner.acquisition
        browser = acq.context.browser
        signature = (phase.context, acq.context.window, (browser['domain'], browser['title']) if browser else None)
        return (phase.active and self.owner.use_ai() and self.owner.phase_key == phase.key
                and stamp == self.session.stream.stamp and signature == self.owner.signature
                and signature == self.session.stream.window_key
                and acq.foreground_is_current())

    def sound_allowed(self):
        return (self.sound_enabled() and self.focus_audio and self.audio.volume > 0
                and self.notifier.mode in ('alarm_live', 'live')
                and not (self.owner.intervention and self.owner.intervention.silenced))

    def sample(self, now, phase, key, facts, frame):
        session = self.session
        self.gate.observe(phase.key, session.result.classification if session.result else None)
        if not session.task:
            session.start(phase.sprint['id'], facts['sprint'], self.next_start)
            self.next_start = 'SPRINT_UPDATED'
        facts['recent_warnings'] = [
            {'kind': kind, 'seconds_ago': round(now - at)}
            for at, kind in self.gate.warnings if now - at < 180
        ]
        session.observe(facts, frame if self.allow_images else None, key, now)
        if self.pending_request:
            kind, expires = self.pending_request
            if now >= expires or session.command('COMMENTARY_REQUEST', kind):
                self.pending_request = None
        if self.audio.busy and session.state == 'READY':
            session.state = 'SPEAKING'
        if session.state == 'SPEAKING' and not self.audio.busy:
            session.state = 'READY'
        if now - session.finished_at <= 2 * session.interval + 2:
            return session.result
        return None

    def output(self, msg):
        session = self.session
        response = (msg.stamp, msg.turn, msg.response_id)
        if not self.current(msg.stamp) or not self.sound_allowed():
            self.audio.cancel()
            return
        if msg.kind == 'INTERRUPTED':
            if self.audio.response == response:
                self.audio.cancel()
        elif msg.kind == 'AUDIO':
            return self.audio.feed_stream(response, msg.data)
        elif msg.kind == 'END':
            self.audio.end(response)
        elif msg.kind == 'COMMENTARY':
            data = msg.data
            if not isinstance(data, dict) or not isinstance(msg.response_id, str) or not 0 < len(msg.response_id) <= 128:
                return
            kind, line = data.get('kind'), clean(data.get('text'), 300)
            if not isinstance(kind, str) or kind not in ALARMS or not line:
                return
            event = ALARMS[kind]
            now = time.monotonic()
            _, _, confirmed = self.owner.decision.evaluate(now, session.result, session.finished_at, self.owner.signature)
            correction = self.owner.decision.corrected
            if now - session.finished_at > 2 * session.interval + 2:
                return
            if correction and correction[0] == self.owner.signature and now < correction[1]:
                return
            accepted = self.gate.admit(kind, session.result, now, active=True, current=True,
                enabled=event not in self.notifier.disabled, busy=self.audio.busy or self.notifier.player.busy,
                confirmed=confirmed, min_confidence=self.owner.decision.min_confidence,
                requested=session.requested == kind, returned=session.previous_label == 'OFF_TASK')
            session.requested = None
            if accepted:
                def started():
                    session.state = 'SPEAKING'
                    session.last_comment = line
                    self.gate.record(time.monotonic(), kind)
                    if self.owner.on_intervention:
                        self.owner.on_intervention()

                def current():
                    return (self.current(msg.stamp) and self.sound_allowed()
                            and time.monotonic() - session.finished_at <= 2 * session.interval + 2)

                async def prelude():
                    await self.notifier.live_alarm(event)

                self.audio.start(response, data.get('rate'), current, prelude, started)

    def diagnostics(self):
        return {**self.session.diagnostics(), 'buffer_bytes': self.audio.buffered,
                'audio_dropped': self.audio.dropped, 'audio_error': self.audio.error}

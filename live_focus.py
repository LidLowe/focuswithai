"""Bridge the duplex transport to the existing decision policy and audio coordinator."""
import logging
import time

from activity_context import clean, sprint_context
from commentary import CommentaryGate
from live_audio import LiveAudio
from gemini_live import DEFAULTS, GeminiLiveTransport
from live_session import LiveSession, number
from voicepack import Event

LOG = logging.getLogger(__name__)

ALARMS = dict(START=Event.SESSION_START, DEVIATION=Event.DISTRACTION, NEXT_STEP=Event.ATTENTION,
              CLARIFY=Event.ATTENTION, RETURN=Event.RETURN_TO_WORK, DIFFICULTY=Event.FOCUS_WARNING,
              BREAK=Event.PERSISTENT_DISTRACTION, SUMMARY=Event.TASK_COMPLETED)


class LiveFocus:
    def __init__(self, config, owner, notifier, sound_enabled, *, transport_factory=None):
        self.owner, self.notifier, self.sound_enabled = owner, notifier, sound_enabled
        volume = number(config, 'live_volume', .55, 0, 1)
        self.audio = LiveAudio(notifier.player, volume)
        profiler = getattr(owner, 'profiler', None)
        self.session = LiveSession({**DEFAULTS, **config}, self.output, self.audio.cancel,
                                   transport_factory or (lambda: GeminiLiveTransport(config, profiler=profiler)),
                                   observation_current=self.current, profiler=profiler,
                                   refresh_visual=self.refresh_visual)
        self.gate = CommentaryGate(config, number(config, 'focus_cooldown_seconds', 45, 15, 300))
        self.allow_images = config.get('activity_send_remote_images') is True
        self.focus_audio = config.get('focus_audio', True)

        self.sprint_context = None
        self.pending_request = None

    def sync_phase(self, phase, reason='sprint_ended'):
        stamp = self.session.stream.stamp
        if not phase.sprint:
            if self.session.task:
                self.session.stop(reason)
                self.pending_request = self.sprint_context = None
        elif phase.active:
            if stamp is None or stamp.sprint_id != phase.sprint['id']:
                self.session.start(phase.sprint['id'], sprint_context(phase.context))
            elif phase.context != self.sprint_context:
                self.session.update_sprint(sprint_context(phase.context))
            self.sprint_context = phase.context
        elif phase.sprint['state'] in ('PAUSED', 'BREAK'):
            self.session.hold(phase.sprint['state'].lower(), paused=True)
            self.pending_request = None
        else:
            self.session.hold('sprint_inactive', paused=True)

    def request(self, kind):
        if not self.sound_allowed():
            raise ValueError('Включи звук фокуса и выбери Live Audio в разделе Live Engine.')
        self.pending_request = kind, time.monotonic() + 60

    def current(self, stamp):
        phase = self.owner.phase()
        acq = self.owner.acquisition
        signature = (phase.context, *acq.context.observation_key())
        return (phase.active and not self.session.block_reason and self.owner.use_ai() and self.owner.phase_key == phase.key
                and stamp == self.session.stream.stamp and signature == self.owner.signature
                and signature == self.session.stream.window_key
                and acq.foreground_is_current())

    async def refresh_visual(self, stamp):
        if not self.allow_images or not self.current(stamp):
            return None
        frame = await self.owner.acquisition.capture_for_refresh()
        return frame if self.current(stamp) else None

    def sound_allowed(self):
        return (self.sound_enabled() and self.focus_audio and self.audio.volume > 0
                and self.notifier.mode in ('alarm_live', 'live')
                and not (self.owner.intervention and self.owner.intervention.silenced))

    def sample(self, now, phase, key, facts, frame):
        session = self.session
        self.gate.observe(phase.key, session.result.classification if session.result else None)
        facts['recent_warnings'] = [
            {'kind': kind, 'seconds_ago': round(now - at)}
            for at, kind in self.gate.warnings if now - at < 180
        ]
        acquisition = self.owner.acquisition
        waiting_for_frame = False
        if (self.allow_images and acquisition.capture_job is not None
                and acquisition.job_epoch == acquisition.capture_epoch):
            capture_started = acquisition.capture_not_before - acquisition.interval
            waiting_for_frame = (now - capture_started < .65
                                 or frame is None and now - acquisition.context.entered < 1.25)
        session.observe(facts, frame if self.allow_images else None, key, now,
                        waiting_for_frame=waiting_for_frame)
        if self.pending_request:
            kind, expires = self.pending_request
            if now >= expires or session.command('COMMENTARY_REQUEST', kind):
                self.pending_request = None
        if now - session.finished_at <= 2 * session.interval + 2:
            return session.result
        return None

    def output(self, msg):
        session = self.session
        response = (msg.stamp, msg.turn, msg.response_id)
        if not self.current(msg.stamp) or not self.sound_allowed():
            self.audio.cancel()
            if msg.kind == 'COMMENTARY':
                LOG.debug('Gemini Live commentary rejected: stale context or audio disabled')
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
                LOG.debug("Gemini Live commentary rejected: invalid envelope")
                return
            kind, line = data.get('kind'), clean(data.get('text'), 300)
            if not isinstance(kind, str) or kind not in ALARMS or not line:
                LOG.debug("Gemini Live commentary rejected: invalid kind or empty text")
                return
            event = ALARMS[kind]
            now = time.monotonic()
            _, _, confirmed = self.owner.decision.evaluate(now, session.result, session.finished_at, self.owner.signature)
            correction = self.owner.decision.corrected
            if now - session.finished_at > 2 * session.interval + 2:
                LOG.debug("Gemini Live commentary rejected: classification expired")
                return
            if correction and correction[0] == self.owner.signature and now < correction[1]:
                LOG.debug("Gemini Live commentary rejected: user corrected classification")
                return
            accepted = self.gate.admit(kind, session.result, now, active=True, current=True,
                enabled=event not in self.notifier.disabled, busy=self.audio.busy or self.notifier.player.busy,
                confirmed=confirmed, min_confidence=self.owner.decision.min_confidence,
                requested=session.requested == kind, returned=session.previous_label == 'OFF_TASK')
            session.requested = None
            if accepted:
                def started():
                    session.last_comment = line
                    self.gate.record(time.monotonic(), kind)
                    if self.owner.on_intervention:
                        self.owner.on_intervention()

                def current():
                    return (self.current(msg.stamp) and self.sound_allowed()
                            and time.monotonic() - session.finished_at <= 2 * session.interval + 2)

                async def prelude():
                    await self.notifier.live_alarm(event)

                if not self.audio.start(response, data.get('rate'), current, prelude, started, profile=msg.profile):
                    LOG.debug('Gemini Live commentary rejected: audio device busy or invalid format')

    def diagnostics(self):
        return {**self.session.diagnostics(), 'audio_state': self.audio.state, 'buffer_bytes': self.audio.buffered,
                'audio_dropped': self.audio.dropped, 'audio_error': self.audio.error}

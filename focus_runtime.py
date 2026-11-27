"""Independent Windows focus loop, coordinated with the existing session timer."""

import asyncio
import base64
import logging
import ntpath
import sys
import time
import webbrowser
from dataclasses import dataclass

from acquisition import Acquisition
from activity_context import BROWSERS, clean
from decision import DecisionEngine
from focus_meter import ActivityMonitor, ActivityState, DistractionMeter, Foreground, Severity
from intervention import InterventionEngine
from live_focus import LiveFocus
from commentary import KINDS


@dataclass(frozen=True)
class Phase:
    key: tuple
    task: str
    focus: bool
    active: bool
    paused: bool
    sprint: dict | None = None
    context: str | None = None


class FocusController:
    def __init__(self, config, phase, use_ai, play, stop_audio, pause, *, notifier, sound_enabled,
                 observe=None, on_intervention=None, get_window=None, transport_factory=None):
        if sys.platform == 'win32':
            from focus_hud import FocusHUD
            import focus_windows
            import windows_acquisition
            get_window = get_window or focus_windows.foreground
        else:
            windows_acquisition = None

        self.phase = phase
        self.use_ai = use_ai
        self.stop_audio = stop_audio
        self.pause = pause
        self.port = int(config.get('panel_port', 8765))
        idle = config.get('voicepack_idle_seconds', 180)
        try:
            idle = max(5, float(idle))
        except (ValueError, TypeError):
            idle = 180
        self.monitor = ActivityMonitor(get_window, idle)
        self.observe = observe
        self.on_intervention = on_intervention
        self.meter_enabled = bool(config.get('focus_meter_enabled'))
        self.live = LiveFocus(config, self, notifier, sound_enabled, transport_factory=transport_factory)
        self.acquisition = Acquisition(config, windows_acquisition)
        self.decision = DecisionEngine(config)
        self.meter = DistractionMeter(config)
        self.intervention = InterventionEngine(config, focus_windows, play) if sys.platform == 'win32' else None
        self.hud = None
        if self.intervention:
            try:
                self.hud = FocusHUD(config, self.action)
            except Exception:
                logging.exception('[HUD] Could not create the overlay.')
        self.phase_key = None
        self.next_sample = 0.0
        self.sample = Foreground(), False
        self.state = ActivityState.BREAK
        self.reason = ''
        self.signature = None
        self.consumed = 0
        self.alerted = Severity.NORMAL
        self.last_tick = None
        self.next_action = None
        self.was_private = False
        self.confirmed = False

    def status(self):
        now = time.monotonic()
        phase = self.phase()
        engine = self.intervention
        guard = engine.guard if engine else None
        hotkey = bool(self.hud and self.hud.hotkey)
        private = self.acquisition.privacy.excluded(self.sample[0])
        browser = ntpath.basename(self.sample[0].executable).casefold() in BROWSERS
        neutral = not phase.active or self.state not in (ActivityState.OFF_TASK, ActivityState.ON_TASK)
        return {
            'score': int(self.meter.score), 'level': self.meter.level.name if not neutral else 'NEUTRAL',
            'activity': self.state.value if phase.active else 'PAUSED' if phase.paused else 'BREAK' if phase.sprint else 'NO_SPRINT',
            'task': phase.task, 'reason': self.reason, 'hud': bool(self.hud and self.hud.visible),
            'hud_available': bool(self.hud),
            'sprint': phase.sprint, 'meter_enabled': self.meter_enabled,
            'window': clean(self.sample[0].title) if phase.active and not (private or browser) else '',
            'application': ntpath.basename(self.sample[0].executable) if phase.active and not private else '',
            'activity_seconds': max(0, int(now - self.acquisition.context.entered)) if phase.active else 0,
            'sources': self.acquisition.status(),
            'live_engine': self.live.diagnostics(),
            'suggested_next_step': self.next_action,
            'hotkey': hotkey, 'silenced': bool(engine and engine.silenced),
            'armed': bool(guard and guard.armed), 'block_remaining': max(0, round(guard.until - now, 1)) if guard else 0,
            'block_available': bool(self.meter_enabled and guard and (guard.apps or guard.sites) and hotkey
                                    and engine.enabled and engine.config['intervention_allow_blocking']),
            'warning': bool(engine and engine.current_warning(now)),
        }

    def suspend(self, reason='observation_reset'):
        self.live.session.hold(reason)
        if self.confirmed:
            self.stop_audio()
        self.confirmed = False
        if self.intervention:
            self.intervention.clear()
        self.acquisition.suspend()
        self.decision.reset()
        self.signature = self.next_action = None
        self.was_private = False
        self.alerted = Severity.NORMAL
        self.next_sample = 0
        self.sample = Foreground(), False
        self.state = ActivityState.BREAK
        self.reason = ''

    def context_preview(self, include_image=False):
        phase = self.phase()
        stream = self.live.session.stream
        if not phase.active or not stream.stamp or not self.live.current(stream.stamp):
            return None
        data = {'context': stream.preview(), 'model': self.live.session.model}
        frame = stream.latest.get('SCREEN_FRAME')
        if include_image and frame and self.acquisition.armed:
            data['image_preview'] = 'data:image/jpeg;base64,' + base64.b64encode(frame.data).decode('ascii')
        return data

    def action(self, action, value=None):
        if action == 'commentary':
            if not self.phase().active or value not in KINDS:
                raise ValueError('Для комментария нужен активный спринт.')
            self.live.request(value)
            return
        if action == 'capture_start':
            if not self.hud or not self.hud.hotkey:
                raise ValueError('Для захвата нужен индикатор HUD и доступная клавиша Ctrl+Shift+F9.')
            self.acquisition.arm(value)
            return
        if action == 'capture_stop':
            self.acquisition.stop_capture()
            self.live.session.hold('capture_stopped')
            return
        if action == 'correct':
            result = self.live.session.result
            if not self.phase().active or not result:
                raise ValueError('Нет текущей классификации для исправления.')
            self.decision.correct(time.monotonic(), self.signature)
            self.live.session.command('USER_CORRECTION', 'Пользователь отклонил последнюю оценку.')
            self.stop_audio()
            self.meter.reset(time.monotonic())
            self.alerted = Severity.NORMAL
            if self.intervention:
                self.intervention.clear()
            return
        if not self.intervention or (action == 'hud' and not self.hud):
            raise ValueError('Это действие доступно через Windows HUD.')
        if action == 'disable':
            self.intervention.disable()
            self.acquisition.stop_capture()
            self.live.session.hold('capture_stopped')
            self.stop_audio()
        elif action == 'enable':
            self.intervention.silenced = False
        elif action == 'hud':
            self.hud.visible = not self.hud.visible
        elif action == 'release':
            self.intervention.guard.release()
        elif action == 'arm':
            phase = self.phase()
            if not phase.active or not self.status()['block_available'] or self.intervention.silenced:
                raise ValueError('Ограничения можно включить для активного рабочего блока при доступной горячей клавише.')
            if self.phase_key != phase.key:
                raise ValueError('Дождись начала рабочего блока и попробуй ещё раз.')
            if self.meter.level.name == 'CRITICAL':
                raise ValueError('Ограничения включаются заранее, до следующего критического уровня.')
            self.intervention.guard.arm()
        elif action == 'dismiss':
            self.intervention.warning_until = 0
        elif action == 'break':
            self.pause()
        elif action == 'change_task':
            self.intervention.clear()
            asyncio.create_task(asyncio.to_thread(webbrowser.open, f'http://127.0.0.1:{self.port}/#/current'))
        else:
            raise ValueError('Неизвестная команда фокуса')

    def _tick(self, now):
        phase = self.phase()
        if self.last_tick is not None and now - self.last_tick > 8:
            self.suspend()
            self.meter.updated = now
        self.last_tick = now
        if phase.key != self.phase_key:
            self.phase_key = phase.key
            self.meter.reset(now)
            self.suspend('sprint_phase_changed')
        self.live.sync_phase(phase)
        weight, confirmed = 0, False
        if not phase.active:
            if self.acquisition.active:
                self.suspend('sprint_inactive')
                self.live.sync_phase(phase)
            self.state = ActivityState.BREAK
            self.reason = ('Возобнови спринт в панели.' if phase.paused else
                           'Идёт перерыв.' if phase.sprint else 'Начни спринт в панели.')
        else:
            if now >= self.next_sample:
                self.sample = self.monitor.sample()
                self.next_sample = now + 1.0
            window, idle = self.sample
            private = self.acquisition.privacy.excluded(window)
            if private and not self.was_private:
                self.suspend('excluded_window')
                self.sample = window, idle
                self.next_sample = now + 1
            self.was_private = private
            self.acquisition.sample(now, window, idle=idle)
            context = self.acquisition.context
            signature = (phase.context, window, (context.browser['domain'], context.browser['title']) if context.browser else None)
            if signature != self.signature:
                new_context = self.signature is None or self.signature[0] != phase.context
                self.decision.reset(keep_correction=not new_context)
                self.signature = signature
            self.next_action = None
            browser = ntpath.basename(window.executable).casefold() in BROWSERS
            blocked = None
            if not self.use_ai():
                blocked = ('analysis_disabled', 'Анализ приостановлен.')
            elif private:
                blocked = ('excluded_window', 'Окно исключено из анализа.')
            elif browser and not context.browser:
                blocked = ('browser_tab_not_allowed', 'Вкладка браузера не разрешена.')
            elif idle:
                blocked = ('idle', 'Давно не было мыши или клавиатуры.')
            elif not window.title:
                blocked = ('missing_window_title', 'У активного окна нет заголовка.')
            elif now - context.entered < 2 or not self.acquisition.foreground_is_current():
                blocked = ('window_settling', 'Ожидаю актуальный контекст окна.')
            if blocked:
                self.live.session.hold(blocked[0], paused=not self.use_ai())
                self.state, self.reason = ActivityState.UNCERTAIN, blocked[1]
            else:
                facts, frame = context.snapshot(now, phase.context, self.meter.score)
                result = self.live.sample(now, phase, signature, facts, frame)
                finished = self.live.session.finished_at
                if result and self.consumed != finished:
                    self.consumed = finished
                    context.decision(now, result)
                self.state, weight, confirmed = self.decision.evaluate(
                    now, result, finished, signature,
                )
                self.reason = result.reason if result else self.live.session.error or 'Ожидаю подтверждения от Gemini Live.'
                self.next_action = result.suggested_next_step if confirmed else None
            if self.observe:
                self.observe(now, ActivityState.IDLE.value if idle and not private else self.state.value,
                             '' if private else window.executable)
            if self.intervention:
                if confirmed:
                    self.intervention.guard.tick(now, window)
                elif self.confirmed or self.intervention.guard.until or self.intervention.current_warning(now):
                    self.intervention.clear()
                    self.stop_audio()
        self.confirmed = confirmed
        if self.meter_enabled:
            self.meter.advance(now, self.state, weight)
        if phase.active and self.intervention:
            level = self.meter.level
            if level < self.alerted:
                self.intervention.warning_until = 0
                previous, self.alerted = self.alerted, level
                if level is Severity.NORMAL and self.state is ActivityState.ON_TASK:
                    self._notify(now, previous, level, phase)
            if confirmed and level > self.alerted:
                previous, self.alerted = self.alerted, level
                self._notify(now, previous, level, phase)
        if self.hud:
            self.hud.update(self.status(), self.intervention.current_warning(now))

    def _notify(self, now, previous, level, phase):
        if (self.intervention.trigger(now, previous, level, phase.task, self.reason,
                                      on_audio=self.on_intervention, next_step=self.next_action)
                and self.on_intervention):
            self.on_intervention()

    async def run(self):
        try:
            while True:
                try:
                    self._tick(time.monotonic())
                except Exception:
                    self.suspend()
                    logging.exception('[Focus] Could not update focus status')
                    await asyncio.sleep(2)
                await asyncio.sleep(.25)
        finally:
            if self.intervention:
                self.intervention.clear()
                self.intervention.guard.close()
            self.acquisition.suspend()
            await self.live.session.close()
            await self.live.audio.close()
            if self.hud:
                self.hud.close()

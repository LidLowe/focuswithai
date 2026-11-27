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
from activity_ai import ActivityAI
from activity_context import BROWSERS, clean
from decision import DecisionEngine
from focus_meter import ActivityMonitor, ActivityState, DistractionMeter, Foreground, Severity
from intervention import InterventionEngine


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
    def __init__(self, config, phase, use_ai, play, stop_audio, pause, *,
                 observe=None, on_intervention=None, get_window=None):
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
        self.monitor = ActivityMonitor(get_window, config.get('target_apps', ['Visual Studio Code']), idle)
        self.observe = observe
        self.on_intervention = on_intervention
        self.meter_enabled = bool(config.get('focus_meter_enabled'))
        self.classifier = ActivityAI(config)
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
        self.sample = Foreground(), False, None
        self.state = ActivityState.BREAK
        self.reason = ''
        self.signature = None
        self.consumed = 0
        self.alerted = Severity.NORMAL
        self.last_tick = None
        self.next_action = None
        self.was_private = False
        self.next_analysis = 0
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
            'ai': self.classifier.diagnostics(),
            'suggested_next_step': self.next_action,
            'hotkey': hotkey, 'silenced': bool(engine and engine.silenced),
            'armed': bool(guard and guard.armed), 'block_remaining': max(0, round(guard.until - now, 1)) if guard else 0,
            'block_available': bool(self.meter_enabled and guard and (guard.apps or guard.sites) and hotkey
                                    and engine.enabled and engine.config['intervention_allow_blocking']),
            'warning': bool(engine and engine.current_warning(now)),
        }

    def suspend(self):
        if self.confirmed:
            self.stop_audio()
        self.confirmed = False
        if self.intervention:
            self.intervention.clear()
        self.classifier.reset()
        self.acquisition.suspend()
        self.decision.reset()
        self.signature = self.next_action = None
        self.was_private = False
        self.alerted = Severity.NORMAL
        self.next_sample = 0
        self.sample = Foreground(), False, None
        self.state = ActivityState.BREAK
        self.reason = ''

    def context_preview(self, include_image=False):
        phase = self.phase()
        ctx = self.acquisition.context
        if not phase.active or not phase.context or not self.acquisition.active:
            return None
        classifier = self.classifier
        next_request, frame = None, None
        if (not self.acquisition.privacy.excluded(ctx.window)
                and (ntpath.basename(ctx.window.executable).casefold() not in BROWSERS or ctx.browser)):
            facts, frame = ctx.snapshot(time.monotonic(), phase.context, self.meter.score)
            if not (classifier.provider and classifier.provider.vision and classifier.send_frames):
                frame = None
            next_request = classifier.preview_for(facts, frame)
        if next_request is None and classifier.preview is None:
            return None
        data = {'next': next_request, 'last_sent': classifier.preview}
        if include_image:
            image = frame or classifier.preview_frame
            if image:
                data['image_preview'] = 'data:image/jpeg;base64,' + base64.b64encode(image.data).decode('ascii')
        return data

    def action(self, action):
        if action == 'capture_start':
            if not self.hud or not self.hud.hotkey:
                raise ValueError('Для захвата нужен индикатор HUD и доступная клавиша Ctrl+Shift+F9.')
            self.acquisition.arm()
            return
        if action == 'capture_stop':
            self.acquisition.stop_capture()
            self.classifier.reset()
            return
        if action == 'correct':
            if not self.phase().active or not self.classifier.last_result:
                raise ValueError('Нет текущей классификации для исправления.')
            self.decision.correct(time.monotonic(), self.classifier.last_observation)
            self.classifier.reset()
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
            self.classifier.reset()
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
            asyncio.create_task(asyncio.to_thread(webbrowser.open, f'http://127.0.0.1:{self.port}/#/sprints'))
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
            self.suspend()
        weight, confirmed = 0, False
        if not phase.active:
            if self.acquisition.active:
                self.suspend()
            self.state = ActivityState.BREAK
            self.reason = ('Возобнови спринт в панели.' if phase.paused else
                           'Идёт перерыв.' if phase.sprint else 'Начни спринт в панели.')
        else:
            if now >= self.next_sample:
                self.sample = self.monitor.sample()
                self.next_sample = now + 1.0
            window, idle, matches = self.sample
            private = self.acquisition.privacy.excluded(window)
            if private and not self.was_private:
                self.suspend()
                self.sample = window, idle, matches
                self.next_sample = now + 1
            self.was_private = private
            self.acquisition.sample(now, window, idle=idle)
            context = self.acquisition.context
            signature = (phase.context, window, (context.browser['domain'], context.browser['title']) if context.browser else None)
            if signature != self.signature:
                new_context = self.signature is None or self.signature[0] != phase.context
                self.classifier.reset(session=new_context, clear_history=new_context)
                self.decision.reset(keep_correction=not new_context)
                self.signature = signature
            self.next_action = None
            browser = ntpath.basename(window.executable).casefold() in BROWSERS
            if private or (browser and not context.browser):
                self.state, self.reason = ActivityState.UNCERTAIN, 'Для окна нет разрешённого контекста.'
            elif idle:
                if self.classifier.result or self.classifier.preview:
                    self.classifier.reset()
                self.state, self.reason = ActivityState.IDLE, 'Давно не было мыши или клавиатуры.'
            elif phase.context and self.use_ai() and window.title and now - context.entered >= 2:
                if now >= max(self.next_analysis, self.classifier.next_request):
                    self.next_analysis = now + 1
                    facts, frame = context.snapshot(now, phase.context, self.meter.score)
                    if frame and not self.acquisition.foreground_is_current():
                        frame = None
                    if self.classifier.request(now, signature, facts, frame):
                        context.sent_frame = frame
                result = self.classifier.result if now - self.classifier.finished_at <= 2 * self.classifier.interval + 25 else None
                if result and self.consumed != self.classifier.finished_at:
                    self.consumed = self.classifier.finished_at
                    context.decision(now, result)
                self.state, weight, confirmed = self.decision.evaluate(
                    now, result, self.classifier.finished_at, signature,
                )
                self.reason = result.reason if result else 'Ожидаю подтверждения от модели.'
                self.next_action = result.suggested_next_step if confirmed else None
            else:
                self.state, self.reason = ActivityState.UNCERTAIN, 'Ожидаю достаточно контекста для анализа.'
            if self.observe:
                self.observe(now, self.state.value, '' if private else window.executable)
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
            await self.classifier.close()
            if self.hud:
                self.hud.close()

"""Duplex activity sessions. Request and receive activity assessments independently."""
import asyncio
import json
import inspect
import math
import re
import struct
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass

from activity_ai import PROTOCOL_PROMPT, SYSTEM_PROMPT, Result
from activity_context import clean
from live_backends import LiveSetupError
from commentary import INSTRUCTION, KINDS
from context_stream import ContextStream, Stamp


@dataclass(frozen=True)
class Output:
    kind: str
    stamp: Stamp
    turn: int
    data: object
    response_id: str = ''


class LiveTransport(ABC):
    @abstractmethod
    async def connect(self, instructions, sample_rate): ...

    @abstractmethod
    async def send(self, event): ...

    @abstractmethod
    async def receive(self) -> Output: ...

    @abstractmethod
    async def interrupt(self): ...

    @abstractmethod
    async def close(self): ...


class MockLiveTransport(LiveTransport):
    """Deterministic local protocol simulator. Never reads the desktop or opens a socket."""
    async def connect(self, instructions, sample_rate):
        self.rate = sample_rate
        self.direct = 'Тон: бодрый и прямой' in instructions
        self.events = {}
        self.output = asyncio.Queue(16)
        self.history = deque(maxlen=4)
        self.first = False
        self.requested = None

    async def interrupt(self):
        while not self.output.empty():
            self.output.get_nowait()
        self.requested = None

    async def close(self):
        self.events.clear()
        self.history.clear()
        await self.interrupt()

    async def receive(self):
        return await self.output.get()

    async def send(self, event):
        kind = event.kind
        if kind in ('SPRINT_STARTED', 'SPRINT_UPDATED', 'BREAK_ENDED'):
            self.events['sprint'] = event.data
            self.first = kind == 'SPRINT_STARTED'
        elif kind == 'COMMENTARY_REQUEST':
            self.requested = event.data
        elif kind == 'USER_CORRECTION':
            self.history.clear()
        elif kind == 'ACTIVITY_DURATION':
            await self.assess(event)
        elif kind != 'SCREEN_FRAME':
            self.events[kind] = event.data  # The simulator does not interpret pixels.

    async def assess(self, event):
        goal = self.events.get('sprint', {}).get('goal', '')
        app, title = self.events.get('FOREGROUND_CHANGED', ('', ''))
        browser = self.events.get('BROWSER_CONTEXT_CHANGED')
        subject = clean((browser or {}).get('title') or title or app, 80)
        words = set(re.findall(r'\w{3,}', goal.casefold()))
        matches = bool(words.intersection(re.findall(r'\w{3,}', subject.casefold())))
        # Only an explicit synthetic fixture produces a confident negative in the simulator.
        label = 'ON_TASK' if matches else 'OFF_TASK' if subject == '[demo] unrelated game' else 'UNCERTAIN'
        sprint = self.events.get('sprint', {})
        goal_excerpt = clean(goal, 85)
        step = (f'Начни с одного проверяемого шага для «{goal_excerpt}».' if self.direct
                else f'Какой один шаг приблизит тебя к цели «{goal_excerpt}»?')
        next_task = next((clean(t.get('text'), 100) for t in sprint.get('subtasks', ())
                          if isinstance(t, dict) and not t.get('done') and t.get('text')), None)
        if next_task:
            step = f'Следующий пункт в «{goal_excerpt}» — «{next_task}». Начнём с него?'
        reason = {'ON_TASK': 'Тема окна совпадает с целью спринта.',
                  'OFF_TASK': 'Тестовое окно обозначено как отвлечение.',
                  'UNCERTAIN': 'По названию окна связь с целью неясна.'}[label]
        assessment = dict(observed_activity=subject or 'Недостаточно данных', classification=label,
                          relevance={'ON_TASK': .9, 'OFF_TASK': .05, 'UNCERTAIN': .5}[label],
                          confidence=.45 if label == 'UNCERTAIN' else .9, reason=reason,
                          recommended_action={'ON_TASK': 'NONE', 'OFF_TASK': 'AUDIO_WARNING',
                                              'UNCERTAIN': 'REQUEST_CLARIFICATION'}[label],
                          suggested_next_step=step if label == 'OFF_TASK' else None)
        async def emit(kind, data, response=''):
            await self.output.put(Output(kind, event.stamp, event.sequence, data, response))
        await emit('ASSESSMENT', json.dumps(assessment, ensure_ascii=False))
        previous = self.history[-1] if self.history else None
        self.history.append(label)
        kind = self.requested
        self.requested = None
        if not kind:
            if label == 'OFF_TASK' and previous == label:
                kind = 'DEVIATION'
            elif label == 'ON_TASK' and previous == 'OFF_TASK':
                kind = 'RETURN'
            elif label == 'ON_TASK' and self.first:
                kind = 'START'
        self.first = False
        if not kind:
            return
        # Example transcripts are derived from this goal/observation. Production text and
        # audio belong to the transport, never to the local playback or policy layer.
        goal = clean(goal, 85)
        lines = {'START': f'Начинаем «{goal}». Открыто «{subject}».',
                 'DEVIATION': f'«{subject}» всё ещё на экране. {step}',
                 'RETURN': f'Снова открыто «{subject}». Продолжим «{goal}»?',
                 'NEXT_STEP': step, 'CLARIFY': f'Как «{subject}» связано с целью «{goal}»?',
                 'BREAK': f'Поставим «{goal}» на паузу?',
                 'DIFFICULTY': f'Какой шаг в «{goal}» сейчас требует помощи?',
                 'SUMMARY': f'Цель спринта — «{goal}». Что удалось закончить?'}
        response = str(event.sequence)
        await emit('COMMENTARY', {'kind': kind, 'text': lines[kind], 'rate': self.rate}, response)
        for start in range(0, self.rate // 4, self.rate // 20):
            pcm = b''.join(struct.pack('<h', round(1800 * math.sin(2 * math.pi * 420 * i / self.rate)
                    * math.sin(math.pi * i / (self.rate // 4)) ** 2))
                    for i in range(start, min(start + self.rate // 20, self.rate // 4)))
            await emit('AUDIO', pcm, response)
        await emit('END', None, response)


class LiveSession:
    def __init__(self, config, on_output, cancel_audio, factory=MockLiveTransport):
        self.stream = ContextStream()
        self.factory = factory
        self.backend = config.get('activity_provider_mode') or getattr(factory, '__name__', 'LiveTransport')
        self.on_output, self.cancel_audio = on_output, cancel_audio
        self.interval = number(config, 'live_analysis_seconds', 8, 2, 120)
        self.rate = int(number(config, 'live_sample_rate', 24000, 8000, 48000))
        style = config.get('live_style', 'concise')
        protocol = PROTOCOL_PROMPT
        if self.backend == 'gemini_live':
            protocol = protocol.replace('You have no tools. Return a single JSON object',
                                        'Submit the assessment through report_focus_assessment as an object')
        self.instructions = '\n'.join((SYSTEM_PROMPT, protocol, INSTRUCTION,
                                      'Тон: ' + ('бодрый и прямой' if style == 'direct' else 'спокойный и краткий')))
        self.state = 'DISCONNECTED'
        self.label = self.previous_label = None
        self.task = None
        self.closing = set()
        self.sent = self.frames = self.reconnects = 0
        self.error = self.connected = self.tokens = self.latency = None
        self.last_success = None
        self.reset_output()

    def reset_output(self):
        self.result = self.last_comment = self.turn = self.requested = self.queued_request = None
        self.assessment_due = None
        self.finished_at = 0.
        self.cancel_audio()

    def start(self, sprint_id, sprint, kind='SPRINT_STARTED'):
        self.stop()
        self.stream.start(sprint_id, sprint, kind)
        self.wake = asyncio.Event()
        self.wake.set()
        self.interrupted = False
        self.terminal = {}
        self.state = 'CONNECTING'
        self.task = asyncio.create_task(self._run(self.stream.generation, self.terminal, tuple(self.closing)))

    def stop(self, kind=None, paused=False):
        if self.task:
            if kind:
                self.terminal['event'] = self.stream.event(kind)
            self.task.cancel()
            self.closing.add(self.task)
            self.task.add_done_callback(self.closing.discard)
            self.task = None
        self.stream.clear()
        self.label = self.previous_label = None
        self.reset_output()
        self.connected = None
        self.state = 'PAUSED' if paused else 'DISCONNECTED'

    def observe(self, facts, frame, key, now):
        before = self.stream.stamp
        self.stream.observe(facts, frame, key, now, self.interval)
        if before != self.stream.stamp:
            self.invalidate()
        if self.stream.pending:
            self.wake.set()

    def invalidate(self):
        self.reset_output()
        self.interrupted = True
        if self.connected:
            self.state = 'READY'
        self.stream.pending.pop('COMMENTARY_REQUEST', None)

    def command(self, kind, value=None):
        if not self.task or self.state not in ('READY', 'ANALYZING', 'SPEAKING'):
            return False
        if kind not in ('COMMENTARY_REQUEST', 'USER_CORRECTION'):
            return False
        if kind == 'COMMENTARY_REQUEST' and value not in KINDS:
            return False
        if kind == 'USER_CORRECTION':
            stamp = self.stream.stamp
            self.stream.stamp = Stamp(stamp.sprint_id, stamp.generation, stamp.observation + 1)
            self.invalidate()
        self.stream.put(kind, value, remember=False)
        self.stream.last_duration = None
        self.wake.set()
        return True

    async def _send(self, transport):
        while True:
            await self.wake.wait()
            self.wake.clear()
            if self.interrupted:
                self.interrupted = False
                await asyncio.wait_for(transport.interrupt(), 2)
            while self.stream.pending and not self.interrupted:
                kind, event = self.stream.pending.popitem(last=False)
                if kind == 'COMMENTARY_REQUEST':
                    self.queued_request = event.data
                if kind == 'ACTIVITY_DURATION':
                    if self.assessment_due is not None:
                        if time.monotonic() >= self.assessment_due:
                            raise ConnectionError('assessment_timeout')
                        continue
                    self.requested, self.queued_request = self.queued_request, None
                    self.turn = event.sequence
                    self.sent_at = time.monotonic()
                    self.assessment_due = self.sent_at + max(12, self.interval * 2)
                    self.state = 'ANALYZING'
                await asyncio.wait_for(transport.send(event), 5)
                self.sent += 1
                self.frames += int(kind == 'SCREEN_FRAME' and event.data is not None)

    async def _receive(self, transport):
        while True:
            try:
                msg = await asyncio.wait_for(transport.receive(), self.interval + 1)
            except asyncio.TimeoutError:
                if self.assessment_due and time.monotonic() > self.assessment_due:
                    raise ConnectionError('assessment_timeout')
                continue
            if msg.stamp != self.stream.stamp:
                continue
            if msg.kind in ('AUDIO', 'END', 'INTERRUPTED'):
                pending = self.on_output(msg)
                if inspect.isawaitable(pending):
                    await pending
                continue
            if self.turn is None or msg.turn != self.turn:
                continue
            if msg.kind == 'ASSESSMENT':
                if getattr(self, 'assessed_turn', None) == msg.turn:
                    continue
                self.result = Result.parse(msg.data)
                self.previous_label, self.label = self.label, self.result.classification
                self.assessed_turn = msg.turn
                self.assessment_due = None
                self.finished_at = max(time.monotonic(), math.nextafter(self.finished_at, math.inf))
                self.latency = round((self.finished_at - self.sent_at) * 1000)
                self.last_success = time.time()
                self.state = 'READY'
            elif msg.kind == 'USAGE' and type(msg.data) is int and msg.data >= 0:
                self.tokens = msg.data
            elif msg.kind == 'COMMENTARY' and self.assessed_turn == msg.turn:
                self.on_output(msg)

    async def _run(self, generation, terminal, previous):
        if previous:
            await asyncio.shield(asyncio.gather(*previous, return_exceptions=True))
        failures = 0
        while self.stream.generation == generation:
            transport = None
            workers = []
            try:
                self.state = 'CONNECTING'
                transport = self.factory()
                self.backend = type(transport).__name__
                await asyncio.wait_for(transport.connect(self.instructions, self.rate), 12)
                self.connected = time.monotonic()
                self.state, self.error = 'READY', None
                self.assessed_turn = None
                workers = [asyncio.create_task(f(transport)) for f in (self._send, self._receive)]
                done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
                for worker in done:
                    worker.result()
                raise ConnectionError()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.state = 'ERROR'
                self.error = str(err) if isinstance(err, LiveSetupError) else type(err).__name__
                self.label = self.previous_label = None
                self.connected = None
                self.reset_output()
                if isinstance(err, LiveSetupError):
                    return
                self.stream.replay()
                self.wake.set()
            finally:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                try:
                    if transport is not None and 'event' in terminal:
                        await asyncio.wait_for(transport.send(terminal['event']), .5)
                except Exception:
                    pass
                finally:
                    try:
                        if transport is not None:
                            await asyncio.wait_for(transport.close(), 2)
                    except Exception:
                        pass
            failures += 1
            self.reconnects += 1
            await asyncio.sleep(min(30, 2 ** min(5, failures - 1)))

    async def close(self):
        self.stop('SPRINT_PAUSED')
        await asyncio.gather(*self.closing, return_exceptions=True)

    def diagnostics(self):
        return dict(backend=self.backend, state=self.state, events=self.sent, frames=self.frames,
                    connected_seconds=int(time.monotonic() - self.connected) if self.connected else 0,
                    latency_ms=self.latency, reconnects=self.reconnects, tokens=self.tokens, error=self.error,
                    result=vars(self.result) if self.result else None, comment=self.last_comment)


def number(config, key, default, low, high):
    try:
        value = float(config.get(key, default))
        return value if low <= value <= high else default
    except (TypeError, ValueError):
        return default


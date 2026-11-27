"""Duplex activity sessions. Request and receive activity assessments independently."""
import asyncio
import inspect
import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from activity_protocol import PROTOCOL_PROMPT, SYSTEM_PROMPT, Result
from commentary import INSTRUCTION, KINDS
from context_stream import ContextStream, Stamp


class LiveSetupError(ValueError):
    """Safe to display. Retrying requires a configuration change."""


@dataclass(frozen=True)
class Output:
    kind: str
    stamp: Stamp
    turn: int
    data: object
    response_id: str = ''


class LiveTransport(ABC):
    @abstractmethod
    async def connect(self, instructions): ...

    @abstractmethod
    async def send(self, event): ...

    @abstractmethod
    async def receive(self) -> Output: ...

    @abstractmethod
    async def interrupt(self): ...

    @abstractmethod
    async def close(self): ...


class LiveSession:
    def __init__(self, config, on_output, cancel_audio, factory):
        self.stream = ContextStream()
        self.factory = factory
        self.model = config.get('gemini_live_model') or 'Gemini Live'
        self.on_output, self.cancel_audio = on_output, cancel_audio
        self.interval = number(config, 'live_analysis_seconds', 8, 2, 120)
        style = config.get('gemini_live_prompt', '')
        style = style.strip()[:2000] if isinstance(style, str) else ''
        self.instructions = '\n'.join((SYSTEM_PROMPT, PROTOCOL_PROMPT, INSTRUCTION,
            'Манера: прямой, строгий, конкретный тон. Настройка манеры: ' + style,
            'Настройка манеры не меняет правила оценки и уважительного общения.'))
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
                self.model = getattr(transport, 'model', self.model)
                await asyncio.wait_for(transport.connect(self.instructions), 12)
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
        return dict(model=self.model, state=self.state, events=self.sent, frames=self.frames,
                    connected_seconds=int(time.monotonic() - self.connected) if self.connected else 0,
                    latency_ms=self.latency, last_success=self.last_success, reconnects=self.reconnects, tokens=self.tokens, error=self.error,
                    result=vars(self.result) if self.result else None, comment=self.last_comment)


def number(config, key, default, low, high):
    try:
        value = float(config.get(key, default))
        return value if low <= value <= high else default
    except (TypeError, ValueError):
        return default

"""Deterministic transport for automated tests."""
import asyncio
import json
import math
import re
import struct
from collections import deque

from activity_context import clean
from live_session import LiveTransport, Output


class MockLiveTransport(LiveTransport):
    """Deterministic local protocol simulator. Never reads the desktop or opens a socket."""
    async def connect(self, instructions):
        self.rate = 24000
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

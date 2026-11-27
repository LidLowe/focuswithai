"""Local sprint lifecycle, clock, and reports."""

import copy
import json
import ntpath
import os
import time
import uuid
from pathlib import Path


OPEN = {'ACTIVE', 'PAUSED', 'BREAK'}
FINISHED = {'COMPLETED', 'CANCELLED'}
SPRINT_TEXT_LIMITS = {'title': 160, 'goal': 8192, 'description': 8192, 'expected_outcome': 8192, 'context': 8192}
FIELDS = (*SPRINT_TEXT_LIMITS, 'subtasks', 'duration_minutes')


def clean(value, limit):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f'Ожидается текст до {limit} символов.')
    return value.strip()


def definition(data):
    result = {}
    for key, limit in SPRINT_TEXT_LIMITS.items():
        value = data.get(key, '')
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f'{key} — ожидается текст до {limit} символов.')
        result[key] = value
    goal = result['goal'].strip()
    if not goal:
        raise ValueError('Укажи цель спринта.')
    result['title'] = result['title'].strip() or goal[:SPRINT_TEXT_LIMITS['title']]
    duration = data.get('duration_minutes')
    if duration is not None and (type(duration) is not int or not 1 <= duration <= 1440):
        raise ValueError('Длительность должна быть от 1 до 1440 минут или без ограничения.')
    tasks = data.get('subtasks', [])
    if not isinstance(tasks, list) or len(tasks) > 30:
        raise ValueError('Доступно до 30 подзадач.')
    result['subtasks'] = [{'text': label, 'done': False}
                          for task in tasks if (label := clean(task, 200))]
    result['duration_minutes'] = duration
    return result


class SprintManager:
    def __init__(self, path='sprints.json', *, monotonic=time.monotonic, wall=time.time):
        self.path = Path(path)
        self.monotonic, self.wall = monotonic, wall
        self.anchor = self.last_save = monotonic()
        self.sample = None
        self.revision = 0
        self.data = {'version': 1, 'current': None, 'sprints': {}, 'templates': {}}
        if self.path.exists():
            with self.path.open(encoding='utf-8') as source:
                self.data = json.load(source)
            if self.data.get('version') != 1:
                raise ValueError('Неизвестная версия sprints.json.')
        if self.current and not self.current.get('recovered'):
            self.current.update(state='PAUSED', break_until=None, recovered=True)
            self.save()
        self._refresh_context()

    @property
    def current(self):
        return self.data['sprints'].get(self.data['current'])

    def save(self):
        temporary = self.path.with_suffix('.json.tmp')
        with temporary.open('w', encoding='utf-8') as output:
            json.dump(self.data, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(self.path)
        self.last_save = self.monotonic()

    def elapsed(self, sprint, now):
        seconds = sprint['seconds']
        if sprint is self.current and sprint['state'] == 'ACTIVE':
            seconds += max(0, now - self.anchor)
        limit = sprint['duration_minutes']
        return min(seconds, limit * 60) if limit is not None else seconds

    def _view(self, sprint, now):
        elapsed = self.elapsed(sprint, now)
        limit = sprint['duration_minutes']
        return {
            **sprint, 'seconds': int(elapsed),
            'remaining': None if limit is None else max(0, int(limit * 60 - elapsed + .999)),
            'break_remaining': max(0, int((sprint['break_until'] or 0) - now + .999)),
        }

    def summary(self):
        item = self.current
        if not item:
            return None
        view = self._view(item, self.monotonic())
        return {key: view[key] for key in ('id', 'title', 'state', 'seconds', 'remaining',
                                           'break_remaining', 'recovered', 'goal')}

    def snapshot(self):
        now = self.monotonic()
        return {
            'revision': self.revision,
            'current': self.summary(),
            'sprints': [self._view(s, now) for s in reversed(list(self.data['sprints'].values()))],
            'templates': list(self.data['templates'].values()),
        }

    def _refresh_context(self):
        item = self.current
        self.llm_context = None if item is None else json.dumps(
            {key: item[key] for key in (*FIELDS, 'state')}, ensure_ascii=False,
        )

    def _record_sample_until(self, now):
        if self.sample is None:
            return
        item = self.current
        at, state, app = self.sample
        seconds = min(1.5, max(0, now - at))
        if item['duration_minutes'] is not None:
            deadline = self.anchor + max(0, item['duration_minutes'] * 60 - item['seconds'])
            seconds = min(seconds, max(0, deadline - at))
        if state in ('ON_TASK', 'OFF_TASK'):
            item['report'][state] += seconds
        if app and state != 'IDLE' and seconds:
            apps = item['report']['apps']
            apps[app] = apps.get(app, 0) + seconds
        self.sample = None

    def observe(self, now, state, executable):
        if self.current is None or self.current['state'] != 'ACTIVE':
            return
        self._record_sample_until(now)
        self.sample = now, state, ntpath.basename(executable or '').lower()[:160]

    def _checkpoint_clock(self, now):
        item = self.current
        self._record_sample_until(now)
        item['seconds'] = self.elapsed(item, now)
        self.anchor = now

    def _transition(self, item, state, now):
        if item['state'] == 'ACTIVE':
            self._checkpoint_clock(now)
        item.update(state=state, recovered=False, break_until=None)
        if state == 'ACTIVE':
            self.anchor = now
            self.data['current'] = item['id']
            if item['started_at'] is None:
                item['started_at'] = self.wall()
        elif state in FINISHED:
            item['ended_at'] = self.wall()
            if item is self.current:
                self.data['current'] = None

    def _new(self, fields):
        ident = uuid.uuid4().hex
        item = {
            **fields, 'id': ident, 'state': 'NOT_STARTED', 'created_at': self.wall(),
            'started_at': None, 'ended_at': None, 'seconds': 0, 'break_until': None,
            'recovered': False, 'revision': 0, 'actual_outcome': '',
            'report': {'ON_TASK': 0, 'OFF_TASK': 0, 'interventions': 0, 'apps': {}},
        }
        self.data['sprints'][ident] = item
        return item

    def _get(self, body, collection):
        ident = body.get('id')
        item = self.data[collection].get(ident) if isinstance(ident, str) else None
        if item is None:
            raise ValueError('Запись не найдена.')
        return item

    def _apply(self, action, body, now):
        if action in ('create', 'quick_start'):
            fields = definition(body)
            if action == 'quick_start' and self.current:
                raise ValueError('Сначала заверши или отмени текущий спринт.')
            item = self._new(fields)
            if action == 'quick_start':
                self._transition(item, 'ACTIVE', now)
            return item
        if action in ('reuse', 'use_template'):
            source = self._get(body, 'sprints' if action == 'reuse' else 'templates')
            fields = {key: copy.deepcopy(source[key]) for key in FIELDS}
            for task in fields['subtasks']:
                task['done'] = False
            return self._new(fields)
        if action == 'delete_template':
            item = self._get(body, 'templates')
            del self.data['templates'][item['id']]
            return None
        item = self._get(body, 'sprints')
        state = item['state']
        if action == 'save_template':
            ident = uuid.uuid4().hex
            label = clean(body.get('name', ''), 160) or item['title']
            self.data['templates'][ident] = {key: copy.deepcopy(item[key]) for key in FIELDS}
            self.data['templates'][ident].update(id=ident, name=label)
        elif action == 'start' and state == 'NOT_STARTED' and self.current is None:
            self._transition(item, 'ACTIVE', now)
        elif action == 'pause' and state in ('ACTIVE', 'BREAK'):
            self._transition(item, 'PAUSED', now)
        elif action == 'resume' and state in ('PAUSED', 'BREAK'):
            self._transition(item, 'ACTIVE', now)
        elif action == 'break' and state == 'ACTIVE':
            minutes = body.get('minutes', 5)
            if type(minutes) is not int or not 1 <= minutes <= 120:
                raise ValueError('Перерыв может длиться от 1 до 120 минут.')
            self._transition(item, 'BREAK', now)
            item['break_until'] = now + minutes * 60
        elif action == 'complete' and state in OPEN:
            item['actual_outcome'] = clean(body.get('actual_outcome', ''), 2000)
            self._transition(item, 'COMPLETED', now)
        elif action == 'cancel' and state not in FINISHED:
            self._transition(item, 'CANCELLED', now)
        elif action == 'outcome' and state == 'COMPLETED':
            outcome = clean(body.get('actual_outcome', ''), 2000)
            if not outcome:
                raise ValueError('Укажи фактический результат.')
            item['actual_outcome'] = outcome
        elif action in ('edit', 'edit_start') and state not in FINISHED:
            if action == 'edit_start' and (state != 'NOT_STARTED' or self.current):
                raise ValueError('Сначала заверши или отмени текущий спринт.')
            original = {key: item[key] for key in FIELDS}
            changes = {key: body[key] for key in FIELDS if key in body}
            if ('goal' in changes and changes.get('title', item['title']) == item['title']
                    and item['title'] == item['goal'].strip()[:SPRINT_TEXT_LIMITS['title']]):
                changes['title'] = ''
            values = {**original, 'subtasks': [task['text'] for task in item['subtasks']], **changes}
            fields = definition(values)
            if state != 'NOT_STARTED' and fields['duration_minutes'] != item['duration_minutes']:
                raise ValueError('Длительность можно изменить до запуска.')
            checked = {}
            for task in item['subtasks']:
                checked.setdefault(task['text'], []).append(task['done'])
            for task in fields['subtasks']:
                if checked.get(task['text']):
                    task['done'] = checked[task['text']].pop(0)
            if state == 'ACTIVE':
                self._record_sample_until(now)
            if item['goal'] != fields['goal']:
                item['revision'] += 1
            item.update(fields)
            if action == 'edit_start':
                self._transition(item, 'ACTIVE', now)
        elif action == 'subtask' and state in OPEN:
            index, done = body.get('index'), body.get('done')
            if type(index) is not int or not 0 <= index < len(item['subtasks']) or type(done) is not bool:
                raise ValueError('Подзадача не найдена.')
            item['subtasks'][index]['done'] = done
        else:
            raise ValueError('Действие недоступно в текущем состоянии спринта.')
        return item

    def command(self, action, body, *, at=None):
        now = self.monotonic() if at is None else at
        before, anchor, sample = copy.deepcopy(self.data), self.anchor, self.sample
        try:
            item = self._apply(action, body, now)
            self.save()
        except Exception:
            self.data, self.anchor, self.sample = before, anchor, sample
            raise
        self._refresh_context()
        self.revision += 1
        return self._view(item, now) if item else None

    def intervention(self):
        if self.current and self.current['state'] == 'ACTIVE':
            self.current['report']['interventions'] += 1

    def tick(self):
        item = self.current
        if item is None:
            return None
        now = self.monotonic()
        if item['state'] == 'BREAK' and now >= item['break_until']:
            return 'resume'
        if item['state'] == 'ACTIVE':
            limit = item['duration_minutes']
            if limit is not None and self.elapsed(item, now) >= limit * 60:
                return 'complete'
            if now - self.last_save >= 5:
                self.checkpoint()
        return None

    def checkpoint(self):
        if self.current and self.current['state'] == 'ACTIVE':
            previous = self.sample
            now = self.monotonic()
            self._checkpoint_clock(now)
            if previous and now - previous[0] <= 1.5:
                self.sample = (now, *previous[1:])
            self.save()

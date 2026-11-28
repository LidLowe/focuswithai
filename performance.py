
"""Bounded, opt-in pipeline telemetry. Only numeric measurements and opaque IDs leave producers."""
import csv
import hashlib
import io
import json
import math
import queue
import sys
import threading
import time
import uuid
from bisect import bisect_left
from collections import Counter, deque
from dataclasses import dataclass, field
from itertools import count


clock = time.perf_counter_ns
SECOND = 1_000_000_000
MILLISECOND = 1_000_000
BUCKET_SECONDS = 5
WINDOWS = {'1m': 60, '5m': 300, '15m': 900}
BUFFER_SIZE = 2048
EXPORT_LIMIT = 12000
# Logarithmic histogram. Percentiles are approximate; scalar moments are exact.
EDGES = (0, *sorted({round(1.05 ** i) for i in range(610)}))
METRICS = {
    'foreground': 'Получение активного окна',
    'window_stabilization': 'От обнаружения окна до допуска к анализу',
    'turn_ready': 'От отправки до готовности к следующему ходу',
    'frame_age_at_send': 'Возраст кадра при подготовке отправки',
    'classification_interval': 'Интервал принятых классификаций',
    'screenshot': 'Захват окна Windows',
    'browser_verification': 'Проверка браузера',
    'resize': 'Масштабирование',
    'jpeg': 'Кодирование JPEG',
    'deduplication': 'Дедупликация кадра',
    'jpeg_to_queue': 'От готового JPEG до постановки в очередь',
    'transmission_queue': 'Очередь хода без ожидания Gemini',
    'frame_queue': 'Очередь JPEG с ожиданием Gemini',
    'previous_turn': 'Ожидание завершения предыдущего хода',
    'base64': 'Кодирование Base64',
    'serialization': 'Сериализация JSON хода',
    'send': 'Локальная отправка WebSocket',
    'first_response': 'От отправки до первого ответа Gemini',
    'structured_response': 'От отправки до валидного toolCall',
    'classification': 'От отправки до принятой классификации',
    'first_audio': 'От отправки до первого PCM',
    'tool_to_audio': 'От ответа на toolCall до первого PCM',
    'playback': 'От отправки до передачи PCM микшеру',
    'audio_local': 'От первого PCM до микшера',
    'capture_to_classification': 'От захвата окна до принятой классификации',
}
TOKEN_FIELDS = ('promptTokenCount', 'responseTokenCount', 'totalTokenCount', 'cachedContentTokenCount',
                'thoughtsTokenCount', 'toolUsePromptTokenCount')
CONFIG_DEFAULTS = {
    'capture_width': 1280, 'capture_height': 720, 'capture_quality': 80, 'capture_interval': 2,
    'live_analysis_seconds': 8, 'activity_send_remote_images': False, 'live_proactive': False,
    'audio_frequency': 48000, 'audio_buffer': 2048, 'alarm_voice_delay_ms': 300,
    'focus_audio': True, 'live_volume': .55,
    'gemini_live_trigger_tokens': 16384, 'gemini_live_target_tokens': 8192,
}
CAPTURE_REASONS = frozenset({
    'blank_frame', 'foreground_changed', 'protected_window', 'invalid_dimensions', 'capture_failed',
    'uia_unavailable', 'browser_mode_unverified', 'private_or_sensitive_window', 'browser_document_unavailable',
    'sensitive_page', 'address_bar_focused', 'url_unavailable', 'domain_excluded', 'browser_changed',
    'sensitive_controls',
})


class Latencies:
    def __init__(self):
        self.n = self.total = self.squares = self.maximum = 0
        self.minimum = None
        self.bins = Counter()

    def add(self, ns):
        self.n += 1
        self.total += ns
        self.squares += ns * ns
        self.maximum = max(self.maximum, ns)
        self.minimum = ns if self.minimum is None else min(self.minimum, ns)
        self.bins[bisect_left(EDGES, ns)] += 1

    def merge(self, other):
        self.n += other.n
        self.total += other.total
        self.squares += other.squares
        self.maximum = max(self.maximum, other.maximum)
        self.minimum = other.minimum if self.minimum is None else min(self.minimum, other.minimum)
        self.bins.update(other.bins)

    def values(self):
        low_mid, high_mid = (self.n + 1) // 2, self.n // 2 + 1
        targets = [low_mid, high_mid, *(math.ceil(self.n * p) for p in (.9, .95, .99))]
        estimates = []
        seen = 0
        for index, count_ in sorted(self.bins.items()):
            seen += count_
            while targets and seen >= targets[0]:
                upper = EDGES[index] if index < len(EDGES) else self.maximum
                estimates.append(min(self.maximum, max(self.minimum, upper)) / MILLISECOND)
                targets.pop(0)
        low, high, p90, p95, p99 = estimates
        return dict(count=self.n, min=self.minimum / MILLISECOND, max=self.maximum / MILLISECOND,
                    mean=self.total / self.n / MILLISECOND, p50=(low + high) / 2, p90=p90, p95=p95, p99=p99,
                    stddev=math.sqrt(self.n * self.squares - self.total ** 2) / self.n / MILLISECOND)


class Totals:
    def __init__(self):
        self.metrics = {}
        self.counters = Counter()

    def add(self, kind, name, value):
        if kind == 'latency':
            if name not in self.metrics:
                self.metrics[name] = Latencies()
            self.metrics[name].add(value)
        elif kind == 'count':
            self.counters[name] += value

    def merge(self, other):
        self.counters.update(other.counters)
        for name, samples in other.metrics.items():
            if name not in self.metrics:
                self.metrics[name] = Latencies()
            self.metrics[name].merge(samples)

    def snapshot(self, seconds):
        return {'seconds': seconds, 'metrics': {k: stats.values() for k, stats in self.metrics.items()},
                'counters': dict(self.counters)}


def working_set():
    if sys.platform != 'win32':
        return None
    try:
        import win32api
        import win32process
        return win32process.GetProcessMemoryInfo(win32api.GetCurrentProcess())['WorkingSetSize']
    except (ImportError, OSError):
        return None


class Recording:
    def __init__(self, config, model, *, capacity=BUFFER_SIZE, export_limit=EXPORT_LIMIT):
        self.id = uuid.uuid4().hex
        self.config, self.model = config, model
        self.started = clock()
        self.wall = time.time_ns()
        self.stopped = None
        self.running = True
        self.queue = queue.Queue(capacity)
        self.rows = deque(maxlen=export_limit)
        self.total_records = 0
        self.total = Totals()
        self.sprint = Totals()
        self.active_sprint = None
        self.sprint_started = self.started
        self.buckets = {}
        self.prompt_history = {}
        self.last_frame = self.resources = self.last_usage = None
        self.frame_ids = count(1)
        self.dropped = 0
        self._original_sprint = self.sprint_key = None
        self._lock = threading.Lock()
        self._overhead_lock = threading.Lock()
        self._enqueue_calls = self._enqueue_ns = self._enqueue_max_ns = 0
        self._worker_cpu_ns = 0
        self._published = self._snapshot(self.started)
        self._thread = threading.Thread(target=self._work, daemon=True, name='focus-performance')
        self._thread.start()

    def set_sprint(self, sprint_id):
        if sprint_id == self._original_sprint:
            return
        self._original_sprint = sprint_id
        self.sprint_key = self.sprint_id(sprint_id)
        self.submit('sprint', 'sprint', None, self.scope())

    @staticmethod
    def sprint_id(sprint_id):
        if sprint_id:
            return hashlib.blake2s(sprint_id.encode(), digest_size=8).hexdigest()
        return None

    def scope(self, stamp=None, turn=None, frame=None):
        return {'sprint_id': self.sprint_id(stamp.sprint_id) if stamp else self.sprint_key,
                'observation_id': f'{stamp.generation}:{stamp.observation}' if stamp else None,
                'turn_id': turn, 'frame_id': frame}

    def submit(self, kind, name, value, scope=None, *, at=None):
        if not self.running:
            return
        begin = clock()
        row = (begin if at is None else at, kind, name, value, scope)
        try:
            self.queue.put_nowait(row)
        except queue.Full:
            self.dropped += 1
        elapsed = clock() - begin
        if self._overhead_lock.acquire(blocking=False):
            try:
                self._enqueue_calls += 1
                self._enqueue_ns += elapsed
                self._enqueue_max_ns = max(self._enqueue_max_ns, elapsed)
            finally:
                self._overhead_lock.release()

    def latency(self, name, begin, end, scope=None):
        if begin is not None and end is not None and end >= begin:
            self.submit('latency', name, end - begin, scope, at=end)

    def tally(self, name, value=1, scope=None):
        self.submit('count', name, value, scope)

    def stop(self):
        self.stopped = clock()
        self.running = False
        self._thread.join()

    def _add(self, row):
        at, kind, name, value, scope = row
        if kind == 'sprint':
            self.active_sprint = scope['sprint_id']
            self.sprint_started = at
            self.sprint = Totals()
        elif kind == 'frame':
            self.last_frame = value
        elif kind == 'resource':
            self.resources = value
        elif kind == 'usage':
            self.last_usage = {'fields': value, 'final': name == 'final', 'turn_id': scope['turn_id']}
        self.total.add(kind, name, value)
        if self.active_sprint and scope and self.active_sprint == scope['sprint_id']:
            self.sprint.add(kind, name, value)
        index = at // (BUCKET_SECONDS * SECOND)
        if kind == 'usage' and 'promptTokenCount' in value:
            self.prompt_history[index] = value['promptTokenCount']
        if index not in self.buckets:
            self.buckets[index] = (Totals(), None)
        self.buckets[index][0].add(kind, name, value)
        if kind == 'resource':
            self.buckets[index] = (self.buckets[index][0], value)
        self.rows.append(row)
        self.total_records += 1

    def _sample_resources(self, now, previous):
        cpu = time.process_time_ns()
        elapsed = now - previous[0]
        values = {'cpu_percent_one_core': 100 * (cpu - previous[1]) / elapsed if elapsed else None,
                  'working_set_bytes': working_set()}
        self._add((now, 'resource', 'process', values, self.scope()))
        return now, cpu

    def _work(self):
        cpu_start = time.thread_time_ns()
        previous = self.started, time.process_time_ns()
        next_sample = self.started + 2 * SECOND
        while self.running or not self.queue.empty():
            batch = []
            try:
                batch.append(self.queue.get(timeout=.25))
                for _ in range(127):
                    batch.append(self.queue.get_nowait())
            except queue.Empty:
                pass
            now = clock()
            with self._lock:
                for row in batch:
                    self._add(row)
                if self.sprint_key != self.active_sprint and self.queue.empty():
                    self.active_sprint = self.sprint_key
                    self.sprint_started = now
                    self.sprint = Totals()
                if now >= next_sample and self.running:
                    previous = self._sample_resources(now, previous)
                    next_sample = now + 2 * SECOND
                    self._prune(now)
                    self._worker_cpu_ns = time.thread_time_ns() - cpu_start
                    self._published = self._snapshot(now)
        with self._lock:
            self._worker_cpu_ns = time.thread_time_ns() - cpu_start
            self._published = self._snapshot(self.stopped or clock())

    def _prune(self, now):
        floor = (now - 900 * SECOND) // (BUCKET_SECONDS * SECOND)
        for key in tuple(self.buckets):
            if key < floor:
                del self.buckets[key]
                self.prompt_history.pop(key, None)

    def _snapshot(self, now):
        duration = max(0, now - self.started) / SECOND
        window_stats = {'recording': self.total.snapshot(duration),
                        'sprint': self.sprint.snapshot((now - self.sprint_started) / SECOND
                                                       if self.active_sprint else 0)}
        for key, seconds in WINDOWS.items():
            period = Totals()
            cutoff = (now - seconds * SECOND) // (BUCKET_SECONDS * SECOND)
            for slot, (totals, _) in self.buckets.items():
                if slot >= cutoff:
                    period.merge(totals)
            window_stats[key] = period.snapshot(min(duration, seconds))
        span = BUCKET_SECONDS * SECOND
        history = []
        for key, (totals, resource) in sorted(self.buckets.items()):
            if key < (now - 900 * SECOND) // span:
                continue
            history.append({'second': max(0, key * span - self.started) / SECOND,
                            'latency_ms': {name: s.values()['p50'] for name, s in totals.metrics.items()},
                            'frames': totals.counters['frames_sent'],
                            'captured': totals.counters['frames_captured'],
                            'out_bytes': totals.counters['ws_out_bytes'], 'in_bytes': totals.counters['ws_in_bytes'],
                            'resources': resource, 'prompt_tokens': self.prompt_history.get(key)})
        return {'recording_id': self.id, 'started_at': self.wall / SECOND, 'running': self.running,
                'duration_seconds': duration, 'model': self.model, 'config': self.config, 'sprint_id': self.active_sprint,
                'windows': window_stats, 'history': history, 'history_bucket_seconds': BUCKET_SECONDS,
                'frame': self.last_frame, 'resources': self.resources, 'usage': self.last_usage,
                'records_retained': len(self.rows), 'records_evicted': self.total_records - len(self.rows),
                'records_dropped': self.dropped,
                'overhead': {'enqueue_samples': self._enqueue_calls,
                             'enqueue_mean_us': self._enqueue_ns / self._enqueue_calls / 1000 if self._enqueue_calls else None,
                             'enqueue_max_us': self._enqueue_max_ns / 1000 if self._enqueue_calls else None,
                             'worker_cpu_ms': self._worker_cpu_ns / MILLISECOND}}

    def snapshot(self):
        return {**self._published, 'running': self.running}

    def export(self, format_):
        with self._lock:
            summary = self._snapshot(self.stopped or clock())
            rows = tuple(self.rows)
        rows = [dict(timestamp=(self.wall + at - self.started) / SECOND, monotonic_ns=at,
                     kind=kind, metric=name, value=value / MILLISECOND if kind == 'latency' else value,
                     unit='ms' if kind == 'latency' else None, recording_id=self.id, model=self.model,
                     **(scope or {}))
                for at, kind, name, value, scope in rows]
        if format_ == 'json':
            return json.dumps({'summary': summary, 'measurements': rows}, ensure_ascii=False)
        output = io.StringIO(newline='')
        writer = csv.DictWriter(output, fieldnames=('timestamp', 'monotonic_ns', 'kind', 'metric', 'value',
            'unit', 'recording_id', 'sprint_id', 'observation_id', 'turn_id', 'frame_id', 'model'))
        writer.writeheader()
        writer.writerow({'kind': 'metadata', 'metric': 'config_and_summary', 'recording_id': self.id,
                         'model': self.model, 'value': json.dumps(summary, ensure_ascii=False)})
        for row in rows:
            if isinstance(row['value'], dict):
                row['value'] = json.dumps(row['value'], ensure_ascii=False)
            writer.writerow(row)
        return output.getvalue()


class PerformanceProfiler:
    def __init__(self, config, model):
        self.config = self._safe_config(config)
        self.model = model
        self.recording = None
        self.lock = threading.Lock()

    @property
    def active(self):
        record = self.recording
        return record if record and record.running else None

    @staticmethod
    def _safe_config(config):
        values = {}
        for key, default in CONFIG_DEFAULTS.items():
            value = config.get(key, default)
            valid = type(value) is bool if type(default) is bool else (
                type(value) is int or type(value) is float and math.isfinite(value))
            values[key] = value if valid else default
        return values

    def control(self, action, active_config=None):
        # Called only by Dashboard worker threads or at backend shutdown.
        with self.lock:
            config = self._safe_config({**self.config, **(active_config or {})})
            if action == 'start':
                if not self.active:
                    self.recording = Recording(config, self.model)
            elif action == 'stop':
                if self.active:
                    self.recording.stop()
            elif action == 'reset':
                active = self.active
                if active:
                    active.stop()
                self.recording = Recording(config, self.model) if active else None
            else:
                raise ValueError('Неизвестная команда профайлера.')

    def snapshot(self):
        record = self.recording
        return {'metrics': METRICS, **(record.snapshot() if record else {'running': False})}

    def export(self, format_):
        if format_ not in ('json', 'csv'):
            raise ValueError('Выбери JSON или CSV.')
        record = self.recording
        if not record:
            raise ValueError('Сначала запусти запись производительности.')
        return record.export(format_)


@dataclass
class FrameTrace:
    recording_id: str
    frame_id: int
    sprint_id: str | None
    started: int | None = None
    timings: dict = field(default_factory=dict)

    def finish(self, name, started, ended=None):
        self.timings[name] = (started, clock() if ended is None else ended)


class TurnTrace:
    """Bound to the exact ACTIVITY_DURATION event and the JPEG attached to it, if any."""
    def __init__(self, recording, event, frame):
        self.recording = recording
        if not frame or frame.recording_id != recording.id:
            frame = None
        self.scope = recording.scope(event.stamp, event.sequence, frame.frame_id if frame else None)
        self.captured = frame.started if frame else None
        self.sent = self.tool_reply = None
        self.points = {}
        self.usage = {}
        self.outcome = None

    def send_complete(self, at):
        self.sent = at
        for name, end in self.points.items():
            self.recording.latency(name, at, end, self.scope)

    def mark(self, name, at):
        if name not in self.points:
            self.points[name] = at
            self.recording.latency(name, self.sent, at, self.scope)
            if name == 'first_audio':
                self.recording.latency('tool_to_audio', self.tool_reply, at, self.scope)
            elif name == 'playback':
                self.recording.latency('audio_local', self.points.get('first_audio'), at, self.scope)

    def assessment(self, outcome, *, category=None, at=None):
        if self.outcome is not None:
            return
        self.outcome = outcome
        self.recording.tally(outcome, scope=self.scope)
        if category:
            self.recording.tally(category, scope=self.scope)
        if outcome == 'classifications_ok':
            self.mark('classification', at)
            self.recording.latency('capture_to_classification', self.captured, at, self.scope)

    def tokens(self, usage):
        if not isinstance(usage, dict):
            return
        changed = {key: usage[key] for key in TOKEN_FIELDS
                   if type(usage.get(key)) is int and usage[key] >= 0 and usage[key] != self.usage.get(key)}
        if changed:
            self.usage.update(changed)
            self.recording.submit('usage', 'partial', dict(self.usage), self.scope)

    def finish(self, *, final):
        if final and self.usage:
            self.recording.submit('usage', 'final', dict(self.usage), self.scope)
            self.recording.tally('token_turns', scope=self.scope)
            for key, value in self.usage.items():
                self.recording.tally(key, value, self.scope)
        self.usage.clear()

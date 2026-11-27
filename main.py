# ---------------------------------------------------------------
# Скрипт: Window Focus Reminder
# Автор: JErfurt
# Дата: 16 октября 2024 года
# Описание: Скрипт отслеживает активные окна и напоминает пользователю
#           о необходимости вернуться к рабочему окну, если он отвлекся.
#
# Лицензия: MIT License
#
# Copyright (c) 2024 JErfurt
#
# Данная работа предоставляется "как есть", без каких-либо гарантий.
# Вы можете использовать, копировать, изменять и распространять её 
# для любых целей при указании оригинального автора.
#
# Этот проект использует библиотеки, лицензированные под MIT, BSD и LGPL лицензиями.
# ---------------------------------------------------------------

import os
import sys
import signal
import subprocess

# Код выхода «перезапусти меня» (кнопка в веб-панели), совпадает с panel.RESTART_CODE
RESTART_CODE = 75

# Лёгкий процесс-надзиратель: запускает трекер дочерним процессом и поднимает
# его заново после «Перезапустить» в панели. Так перезапуск не завершает
# start.bat (и не гасит llama-server), а память старого процесса освобождается.
if __name__ == "__main__" and os.environ.get("WORKREMINDER_CHILD") != "1":
    env = {**os.environ, "WORKREMINDER_CHILD": "1"}
    while True:
        child = subprocess.Popen([sys.executable, *sys.argv], env=env)
        while True:
            try:
                code = child.wait()
                break
            except KeyboardInterrupt:
                continue  # Ctrl+C получает и дочерний процесс, ждём его штатного выхода
        if code != RESTART_CODE:
            sys.exit(code)
        print("[INFO] Перезапуск по запросу из веб-панели...")

if sys.platform == "darwin":
    from AppKit import NSWorkspace

import time
import pygame
from tts import Speaker
import asyncio
import json
import logging
import discord
import panel
from ai_client import AIClientFactory
from assistant import FocusAssistant
from config_store import load_config
from focus_meter import Foreground
from focus_runtime import FocusController, Phase
from sprints import SprintManager
from voicepack import Event, OBSERVATIONS, VoicePackNotifier, notification_mode

# Вывод копируется в буфер, чтобы панель могла показать логи
panel.capture_output()


# Открываем и читаем файл config.json
config = load_config()

# Инициализация звука. Дефолтный буфер pygame (512 сэмплов, ~11 мс) на Windows
# даёт потрескивание, а 48 кГц совпадает с частотой большинства устройств,
# поэтому системе не приходится пересэмплировать звук ещё раз.
pygame.mixer.pre_init(
    frequency=config.get('audio_frequency', 48000),
    size=-16,
    channels=2,
    buffer=config.get('audio_buffer', 2048),
)
pygame.mixer.init()

# Инициализация AI клиента
ai_client = AIClientFactory.create_client(config)

discord_presence_state = config.get('discord_presence', False)
sound_on = True
ai_on = True
restart_requested = False
wake_event = None

# Чтение уровня дебага
debug_level = config.get('debug_level', 0)

# Функция для вывода сообщений в зависимости от уровня дебага
def log_message(level, message):
    if level <= debug_level:
        print(message)

# Логи модулей ai_client/assistant выводятся по тому же уровню дебага.
# force=True: discordrpc при импорте уже настраивает logging, и без него
# этот вызов ничего не делал (уровень дебага и формат не применялись)
logging.basicConfig(
    level={0: logging.ERROR, 1: logging.WARNING, 2: logging.INFO}.get(debug_level, logging.DEBUG),
    format="[%(levelname)s] %(message)s",
    force=True,
)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("comtypes").setLevel(logging.WARNING)

def portable_foreground():
    if sys.platform == 'darwin':
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app:
            name = app.localizedName() or ''
            return Foreground(title=name, executable=name)
    return Foreground()


# Озвучивание: цепочка движков edge -> piper -> sapi (см. tts.py)
speaker = Speaker(config)

async def speak_text(text):
    await speaker.speak(text)

# AI ассистент: промпты, контекст, память диалога и озвучивание ответов
assistant = FocusAssistant(ai_client, config, speak_text)

def save_session_stats(quiet=False):
    """Сохраняет статистику сессий в файл"""
    try:
        with open('session_stats.json', 'w', encoding='utf-8') as f:
            json.dump(session_stats, f, ensure_ascii=False, indent=2)
        if not quiet:
            log_message(2, "[INFO] Статистика сессий сохранена")
    except Exception as e:
        log_message(1, f"[ERROR] Ошибка при сохранении статистики: {e}")

def load_session_stats():
    """Загружает статистику сессий из файла"""
    try:
        with open('session_stats.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {
            "total_focus_time": 0,
            "total_break_time": 0,
            "completed_sessions": 0,
            "current_session_start": time.time()
        }
    except Exception as e:
        log_message(1, f"[ERROR] Ошибка при загрузке статистики: {e}")
        return {
            "total_focus_time": 0,
            "total_break_time": 0,
            "completed_sessions": 0,
            "current_session_start": time.time()
        }

# Загружаем статистику при запуске
session_stats = load_session_stats()

def today_stats():
    """Итоги дня для панели: время в фокусе, в отвлечениях и число отвлечений"""
    today = time.strftime('%Y-%m-%d')
    stats = session_stats.get("today")
    if not isinstance(stats, dict) or stats.get("date") != today:
        stats = {"date": today, "focus_seconds": 0, "lost_seconds": 0, "distractions": 0}
        session_stats["today"] = stats
    return stats

# Флаг для корректного завершения
shutdown_flag = False

def signal_handler(signum, frame):
    """Обработчик сигналов для корректного завершения"""
    global shutdown_flag
    log_message(1, "[INFO] Получен сигнал остановки. Завершаем работу...")
    shutdown_flag = True

# Регистрируем обработчики сигналов
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

async def idle(seconds):
    """Пауза, которую прерывает команда из панели или остановка"""
    end = time.time() + seconds
    while not shutdown_flag and time.time() < end:
        try:
            await asyncio.wait_for(wake_event.wait(), min(1, end - time.time()))
            break
        except asyncio.TimeoutError:
            pass
    wake_event.clear()


class Runtime:
    """Coordinates the sprint lifecycle, monitor, and the local control panel."""

    def __init__(self):
        self.started_at = time.time()
        self.sprints = SprintManager()
        event_config = config
        if not notification_mode(config):
            event_config = {**config, 'notification_mode': 'tts'}
        self.voicepack = VoicePackNotifier(event_config, ai_client, speaker)
        self.focus = None
        self.startup_managed = os.environ.get('FOCUSWITHAI_MANAGED') == '1'
        self.startup_ready = False
        self.startup_running = False
        self.startup_gate = asyncio.Event()
        if not self.startup_managed:
            self.startup_gate.set()

    def start_focus(self):
        self.focus = FocusController(
            config, self.focus_phase, lambda: ai_on,
            self.play_focus_event, self.stop_audio, lambda: self.control('start_break'),
            observe=self.sprints.observe, on_intervention=self.sprints.intervention,
            get_window=None if sys.platform == 'win32' else portable_foreground,
        )

    @property
    def assistant(self):
        return assistant

    @property
    def speaker(self):
        return speaker

    @property
    def target_window(self):
        return self.focus.status()['window'] if self.focus else None

    def state(self):
        sprint = self.sprints.summary()
        focus = self.focus.status() if self.focus else None
        return {
            'started_at': self.started_at,
            'paused': bool(sprint and sprint['state'] == 'PAUSED'),
            'manual_break_seconds': sprint['break_remaining'] if sprint else 0,
            'startup_managed': self.startup_managed,
            'startup_ready': self.startup_ready,
            'startup_running': self.startup_running,
            'sprint': sprint,
            'sprint_revision': self.sprints.revision,
            'active_window': focus['window'] if focus else '',
            'toggles': {'sound': sound_on, 'ai': ai_on, 'discord': discord_presence_state},
            'today': dict(today_stats()),
            'events': [list(event) for event in assistant.events],
            'stats': {key: value for key, value in session_stats.items() if key != 'today'},
            'voicepack': {'decision': self.voicepack.decision, 'mode': self.voicepack.mode,
                          'disabled': sorted(event.value for event in self.voicepack.disabled)}
            if self.voicepack else None,
            'focus': focus,
        }

    def focus_phase(self):
        current = self.sprints.current
        if current is None:
            return Phase((), '', False, False, False)
        sprint = self.sprints.summary()
        active = sprint['state'] == 'ACTIVE' and sprint['remaining'] != 0 and not shutdown_flag
        return Phase((current['id'], current['revision']), current['title'], True, active,
                     sprint['state'] == 'PAUSED', sprint, self.sprints.llm_context)

    def play_focus_event(self, event, task, reason, mode, on_play=None):
        if self.voicepack and sound_on and self.focus_phase().active:
            return self.voicepack.notify(event, {'observation': reason or OBSERVATIONS[event], 'task': task},
                                         use_ai=False, mode_override=mode, on_play=on_play)
        return False

    def stop_audio(self):
        if self.voicepack:
            self.voicepack.stop_audio()

    def sprint_command(self, action, body, *, at=None):
        before = self.focus_phase()
        item = self.sprints.command(action, body, at=at)
        after = self.focus_phase()
        if before.key != after.key or (before.sprint and before.sprint['state'] == 'ACTIVE' and not after.active):
            self.stop_audio()
            if self.focus:
                self.focus.suspend()
                if before.key != after.key:
                    self.focus.meter.reset(time.monotonic())
        if before.active != after.active or (before.sprint and not after.sprint):
            self.update_presence()
        if action in ('quick_start', 'start', 'edit_start') and after.active:
            self.play_focus_event(Event.SESSION_START, after.task, '', None)
        wake_event.set()
        return item

    def update_presence(self):
        if not discord_presence_state or not self.focus_phase().active:
            discord.stop_rpc()
        else:
            started_at = self.sprints.current['started_at']
            discord.restart_rpc(1133456581988732970, 'FocusWithAI', 'Рабочий спринт', int(started_at), 'cat')

    def control(self, action, value=None):
        global sound_on, ai_on, discord_presence_state
        if action == 'startup_release':
            if not self.startup_managed or not self.startup_ready:
                raise ValueError('Трекер ещё не готов к запуску мониторинга.')
            self.startup_gate.set()
        elif action in ('pause', 'resume', 'start_break', 'focus_task'):
            current = self.sprints.current
            if not current:
                return
            if action == 'focus_task':
                self.sprint_command('edit', {'id': current['id'], 'goal': value})
            elif (action, current['state']) in {
                ('pause', 'ACTIVE'), ('pause', 'BREAK'), ('resume', 'PAUSED'),
                ('resume', 'BREAK'), ('start_break', 'ACTIVE'),
            }:
                self.sprint_command('break' if action == 'start_break' else action, {'id': current['id']})
        elif action == 'sound':
            sound_on = bool(value)
            if not sound_on:
                self.stop_audio()
        elif action == 'ai':
            ai_on = bool(value)
            if not ai_on and self.focus:
                self.focus.suspend()
                self.stop_audio()
        elif isinstance(action, str) and action.startswith('focus_'):
            if not self.focus or action[6:] not in ('disable', 'enable', 'hud', 'arm', 'release',
                                                    'capture_start', 'capture_stop', 'correct'):
                raise ValueError('HUD и вмешательства доступны в Windows.')
            self.focus.action(action[6:])
        elif action in ('task_completed', 'attention'):
            phase = self.focus_phase()
            if not phase.active or not self.voicepack:
                raise ValueError('Звуковые команды доступны в активном спринте.')
            self.play_focus_event(Event.TASK_COMPLETED if action == 'task_completed' else Event.ATTENTION,
                                  phase.task, '', None)
        elif action == 'discord':
            discord_presence_state = bool(value)
            self.update_presence()
        else:
            raise ValueError(f'Неизвестная команда: {action}')
        wake_event.set()

    def reset_stats(self):
        session_stats.update({'total_focus_time': 0, 'total_break_time': 0, 'completed_sessions': 0})
        save_session_stats()

    def request_restart(self):
        global restart_requested
        restart_requested = True
        self.request_stop()

    def request_stop(self):
        global shutdown_flag
        shutdown_flag = True
        self.stop_audio()
        if self.focus:
            self.focus.suspend()
        wake_event.set()


async def main():
    global wake_event
    wake_event = asyncio.Event()
    runtime = Runtime()
    panel_config = {**config, 'panel_enabled': True, 'panel_open_browser': False} if runtime.startup_managed else config
    panel_runner = await panel.start_panel(runtime, panel_config)
    if runtime.startup_managed and panel_runner is None:
        return 1
    runtime.startup_ready = panel_runner is not None
    while not runtime.startup_gate.is_set() and not shutdown_flag:
        await idle(1)
    job = None
    try:
        if not shutdown_flag:
            runtime.start_focus()
            runtime.startup_running = True
            job = asyncio.create_task(runtime.focus.run())
        last = time.monotonic()
        while not shutdown_flag:
            try:
                now = time.monotonic()
                current = runtime.sprints.current
                if now - last > 12 and current and current['state'] in ('ACTIVE', 'BREAK'):
                    runtime.sprint_command('pause', {'id': current['id']}, at=last)
                last = now
                action = runtime.sprints.tick()
                if action:
                    runtime.sprint_command(action, {'id': runtime.sprints.current['id']})
            except OSError:
                logging.exception('[Sprint] Could not save the sprint.')
                await idle(2)
            await idle(.25 if runtime.sprints.current else 2)
    finally:
        if job:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        try:
            runtime.sprints.checkpoint()
        except OSError:
            logging.exception('[Sprint] Could not save the sprint at shutdown.')
        if runtime.voicepack:
            await runtime.voicepack.close()
        if panel_runner:
            await panel_runner.cleanup()
        await ai_client.close()
        discord.stop_rpc()


# Запуск программы
if __name__ == "__main__":
    result = asyncio.run(main())
    sys.exit(RESTART_CODE if restart_requested else result or 0)

# STOP PROCRASTINATION  
### BE PRODUCTIVE!  

# Window Focus Reminder  

A script to track active windows and remind users to return to their work applications.  

## Features  

- ✅ Active window tracking  
- ✅ Sound reminders for distractions  
- ✅ Voice notifications via Edge TTS  
- ✅ Alarm and VoicePack notifications from local WAV, MP3 and OGG files
- ✅ AI integration for personalized messages  
- ✅ Goal-based sprints with a timer, breaks, templates and local reports
- ✅ Discord Rich Presence  
- ✅ Support for multiple AI providers (llama.cpp, any OpenAI-compatible API, Gemini) with fallback chain
- ✅ Context-aware AI: time of day, current session, distraction statistics
- ✅ Dialogue memory: the assistant remembers recent replies and does not repeat facts
- ✅ Async requests with timeouts and retries, TTS-safe output cleanup, offline fallback phrases
- ✅ Local web panel: all settings, schedule and assistant memory in the browser instead of editing files

## Supported AI Providers

### 1. llama.cpp (`/completion`, raw prompt)
```json
{
  "ai_provider": "llama",
  "llama_url": "http://127.0.0.1:8080/completion"
}
```

### 2. OpenAI-compatible Chat API (recommended for local models)
Works with llama.cpp (`/v1`), Ollama, LM Studio, vLLM, OpenAI, OpenRouter.
The server applies the model's own chat template, which usually gives better answers than `/completion`.
```json
{
  "ai_provider": "openai",
  "openai_base_url": "http://127.0.0.1:8080/v1",
  "openai_model": "local-model",
  "openai_api_key": ""
}
```
Ollama: `"openai_base_url": "http://127.0.0.1:11434/v1"`, `"openai_model": "qwen2.5:7b"`.

### 3. Google Gemini
```json
{
  "ai_provider": "google_assistant",
  "google_assistant_api_key": "YOUR_API_KEY",
  "google_model": "gemini-2.5-flash"
}
```

### Fallback chain
If the main provider fails, the next one is tried. If all fail, a built-in phrase is spoken.
```json
{
  "ai_provider": "llama",
  "ai_fallback_providers": ["google_assistant"]
}
```

## Installation  

1. Create the virtual environment and install dependencies on Windows:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

2. Configure `config.json`:  
   - Choose an AI provider (`ai_provider`)  
   - Set parameters for the selected provider  
   - Specify target applications in `target_apps`  

3. Optionally create a planning file `sessions.jsonl`:
```json  
{"name": "Focus", "focus": true, "min": 25}  
{"name": "Break", "focus": false, "min": 5}  
{"name": "Focus", "focus": true, "min": 25}  
```  

## Usage  

Run the script:  
```bash  
python main.py  
```  

## Startup Manager on Windows

For an installation with the local `.venv`, launch from the project directory:

```powershell
.\.venv\Scripts\python.exe launcher.py
```

The supervisor owns the tray, checks for an existing tracker, and waits for
the configured local LLM API and model. It starts the bundled llama.cpp CPU
build only when that configured endpoint is free. It then starts the backend,
waits for its status endpoint, releases monitoring and HUD, and opens the
dashboard if requested. Both bundled services listen on `127.0.0.1`. An LLM
failure leaves the panel and monitoring controls available with a warning icon.

In **Автозапуск**, **Включить** registers an interactive logon task for the
current Windows user at normal privilege. **Выключить** disables the task.
An existing FocusWithAI shortcut in the Startup folder is moved to
`.runtime/legacy-startup.lnk` when migrated or disabled. The previous shortcut
is recognized by its launcher path and arguments. Startup registration takes
effect on the next Windows sign-in. [Windows logon triggers](https://learn.microsoft.com/windows/win32/taskschd/logon-trigger-example--scripting-)

The supervisor reads these preferences on launch. Changes to its launch
preferences take effect after **Exit** and a new launch or at the next sign-in.

| Setting | Default | Effect |
| --- | --- | --- |
| `startup_llm_auto` | `true` | Start the bundled local model when needed |
| `focus_hud_enabled` | `false` | Show the configured HUD at startup |
| `startup_open_dashboard` | `false` | Open the dashboard when a scheduled launch is ready |
| `startup_start_minimized` | `true` | Run the backend in the tray with an on-disk log. Disable it to show a backend console |
| `startup_delay_seconds` | `8` | Delay infrastructure startup after sign-in, from 0 to 180 seconds |
| `startup_recover` | `true` | Permit up to two recovery attempts per service per supervisor run |

A manual launch opens the dashboard when ready. Use `--background` or
`--no-browser` to start in the tray directly. The tray menu can open the panel,
show or hide the HUD, resume or pause monitoring, begin a five-minute break,
restart only the backend, open settings, or exit. The dashboard opens on
**Спринты**. In background mode, the tray tooltip and visible HUD prompt you to
start or resume a sprint. An unfinished sprint comes back paused after a
backend restart or Windows sign-in. **Start Monitoring** in the tray resumes
the current sprint or opens **Спринты** to create one.

**Exit** requests an orderly backend shutdown and releases the process groups
created by this supervisor. Reused servers stay running. The single-instance
lock and Task Scheduler's duplicate-start policy prevent a second supervisor.
Check `.runtime/startup.log`, `.runtime/backend.log`, and
`.runtime/llama-server.log` for launch errors. To keep the dashboard available,
the managed backend always starts its localhost panel.

## Web panel

While `main.py` runs, the panel is at **http://127.0.0.1:8765/** (the address is printed at startup).
It listens on localhost only.

- **Сегодня** shows the current sprint, timer and distraction meter. Sound,
  classifier and Discord toggles apply until restart.
- **Спринты** starts, edits and finishes sprints, and opens templates and reports.
- **Расписание** edits `sessions.jsonl` with drag-to-reorder and planning presets.
  Use **В спринт** to copy a work block's name and duration into the sprint form.
- **Focus rules, Character and replies, Model, Voice**: every `config.json` setting with defaults filled in.
  Edits from all sections collect into one draft; changed fields are highlighted until you save (Ctrl+S).
  You can test a reply, the AI connection, a voice and the reminder sound with unsaved settings.
- **Assistant memory**: view and delete events, recent replies and told facts, reset statistics.
- **Discord and debug**: restart or stop the tracker, log level and live logs.
- **config.json manually**: raw editor for `config.json` and `sessions.jsonl`, keys the program
  does not read, backups, export/import.

Most settings are read at startup, so after saving press **Restart** to reload
the backend and resume the restored sprint from **Спринты**. The startup supervisor and local
llama-server keep running during a backend restart.
Every save of `config.json` first copies the old file to `backups/` (last 10 are kept).
API keys are never sent to the browser: the panel shows a mask and keeps the stored key unless you type a new one.

To edit settings without starting the tracker:
```bash
python panel.py
```

Panel settings in `config.json`: `panel_enabled` (default `true`), `panel_port` (default `8765`),
`panel_open_browser` (open the panel on startup, default `false`).

## Sprint Manager

1. Open **Спринты**, enter a goal and select **Начать спринт**.
2. Expand **Параметры спринта** to set a title, description, expected outcome,
   subtasks, time limit and additional model context. An empty time limit counts up.
   **Сохранить черновик** stores the sprint for later.
3. Use **Пауза**, **Возобновить** or **Перерыв на 5 минут**. The five-minute break
   resumes automatically. The sprint timer counts active work time only. Editing
   the goal clears the distraction meter and releases active interventions.
4. Select **Завершить**, or let a timed sprint reach zero. Open its entry under
   **История и отчёты** and confirm what you actually achieved. The report shows
   active duration, confirmed on-task and off-task time, interventions, frequently
   used applications and checked subtasks. Uncertain and idle time remain part of
   active duration. The outcome is supplied by the user.
5. Use **Повторить** or save a sprint to **Шаблоны**. Reuse creates a draft with
   the same definition and fresh progress.

Sprint data lives in the private, git-ignored `sprints.json` in the project
directory. Lifecycle changes save immediately and the active clock checkpoints
every five seconds. A hard shutdown can lose up to the last checkpoint interval.
The next launch asks you to resume, and time spent away stays out of the report.
Reports store app names and durations. The configured AI provider receives the
active sprint fields and the permitted activity context during classification.
With a local model, requests stay on localhost.

## Live Engine на Windows

В Dashboard откройте **Ассистент → Live Engine**, выберите **Live Mock · локальный тест**
и **Alarm + Live Audio** или **Live Audio**. Для эффекта выберите установленный Alarm Pack
в разделе **Эффекты и голос**. Сохраните настройки, перезапустите трекер и начните спринт.
Ключ API и локальный сервер модели этому режиму не нужны. Исходный режим анализа остаётся
значением по умолчанию.

**Как услышать тестовый сигнал.** Создайте спринт с целью «Сетевые технологии»
и откройте тестовый документ с этими словами в заголовке. В панели нажмите
**Предложить следующий шаг** и вернитесь в окно документа. После оценки `ON_TASK` и допуска локальной проверкой
прозвучит короткий тон, а в панели появится текст комментария. При выбранном Alarm Pack
сначала проигрывается доступный эффект, затем следует пауза (по умолчанию 300 мс).
Громкость эффекта, голоса и интервал между вмешательствами задаются отдельно.
Для озвучивания самого текста можно выбрать TTS и настроить установленный SAPI,
Piper или Silero. В тестовой Live-сессии используется только локальный синтез.
Edge TTS и прежние режимы пакетов доступны в обычном режиме.

**Что имитирует Mock.** Детерминированный транспорт сравнивает слова из цели и
заголовка. Для проверки устойчивого отклонения он распознаёт специальный заголовок
`[demo] unrelated game`. Для остальных неоднозначных наблюдений он возвращает
`UNCERTAIN`. Транскрипты строятся из тестовых данных. Тон демонстрирует поток PCM16,
а изображение проверяет передачу кадра. Семантический анализ изображений и естественный
голос доступны в режиме **Gemini 3.8 Live**.

**Подключение Gemini.** В разделе **Live Engine** выберите **Gemini 3.8 Live**,
введите ключ Gemini API и разрешите отправку текстового контекста. Идентификатор
модели `gemini-3.8-live` подставляется автоматически. Кнопка **Проверить подключение**
открывает и закрывает сессию без данных спринта. Сохраните настройки, перезапустите
трекер и начните спринт с включённым анализом AI. Для голоса выберите **Live Audio**
или **Alarm + Live Audio**. Кадры требуют отдельного разрешения и выбора окна.
На Windows приложение хранит ключ в диспетчере учётных данных.

**Контекст и управление.** Инициативные реплики выключены по умолчанию. Частота
анализа по умолчанию составляет один раз в 8 секунд. Источники можно проверить
в **Диагностика AI → Посмотреть контекст**. В список приложений для модели входят
только явно разрешённые имена EXE. Плеер, композиция и состояние воспроизведения
дополняют контекст. Захват окна требует отдельного выбора конкретного окна. Кадры и
PCM хранятся в памяти. Микрофон и системный звук не захватываются. Кнопка
**Остановить звук** прерывает текущую последовательность. Пауза и завершение
спринта очищают контекст. Итоговый комментарий можно запросить **до** завершения.

| Компонент | Ответственность |
| --- | --- |
| `context_stream.py` | Изменения спринта, окна, вкладки, кадра, медиасеанса и разрешённых приложений. Каждое событие содержит время, ID спринта, поколение, номер наблюдения и порядковый номер. |
| `live_session.py` | Независимые задачи send/receive, подтверждённые оценки по прежней JSON-схеме, отмена и переподключение с задержкой до 30 секунд. Для восстановления сохраняется последнее значение каждого источника. |
| `live_focus.py`, `commentary.py` | Проверка соответствия текущему окну и спринту, решения по уверенности и устойчивости, выбор допустимых поводов и интервал тишины. Модель может предложить реплику. Действия выполняет локальный код. |
| `live_audio.py`, `voicepack.py` | Один координатор для пакетов, TTS, предпрослушивания и PCM. Очередь PCM16 mono ограничена двумя секундами и 96 фрагментами. При переполнении отменяется реплика целиком. |
| `focus_runtime.py`, `main.py`, `launcher.py`, `focus_hud.py`, `panel_web/` | Жизненный цикл спринта, автозапуск, отображение состояния и локальное управление. |

Для подключения будущего провайдера реализуйте интерфейс `LiveTransport`
(`connect`, `send`, `receive`, `interrupt`, `close`). В тестах фабрика передаётся
в `LiveSession`, в приложении доступные фабрики перечислены в `live_backends.py`.
Исходящие события содержат компактные изменения. Для ответа
провайдер возвращает `Output` с меткой наблюдения и номером события
`ACTIVITY_DURATION` в поле `turn`. `ASSESSMENT` несёт JSON Result Protocol.
`COMMENTARY` передаёт тип, транскрипт и частоту. Последующие `AUDIO`, `END`
и `INTERRUPTED` используют тот же идентификатор ответа. Для `USAGE`
можно передать счётчик токенов. Политика допуска внешнего транспорта и согласие
на передачу данных должны быть реализованы до его включения.

## Multimodal activity analysis on Windows

The activity monitor samples the foreground window during active sprints and
optionally reads media metadata. The context aggregator keeps a small rolling
history of app switches and classifications and attaches at most one changed
frame. The selected AI provider returns a validated advisory result. The
decision engine checks confidence, relevance, time and two separate
confirmations before escalating. The existing meter, HUD and audio/intervention
engines handle effects locally. Missing or ambiguous evidence holds the meter
and shows a neutral HUD. **Отметить ошибку классификации** resets the meter and
interventions and suppresses that observation for two minutes.

### Launch and model configuration

With the installed Windows environment, start **FocusWithAI.cmd** or run
`.\.venv\Scripts\python.exe launcher.py` from the project directory. Use **Спринты**
to start with a goal. The existing Windows logon task can still be managed from
**Автозапуск**. A sleep or an extended interruption of the monitor pauses the
sprint on return.

1. In **AI Provider**, keep the installed local text model for text analysis,
   or select **OpenAI-совместимый API**, enter the local API address and select
   a loaded model. Use **Загрузить список моделей** to see its identifier.
2. Use `http://127.0.0.1:1234/v1` for the default LM Studio server or
   `http://127.0.0.1:11434/v1` for the default Ollama server. Enable
   **Выбранная модель понимает изображения** only with a vision model.
   [LM Studio API](https://lmstudio.ai/docs/developer/openai-compat)
   [Ollama API](https://docs.ollama.com/api/openai-compatibility)
3. Use **Запрос и ответ** with an OpenAI-compatible endpoint. For local
   LM Studio's native SSE and chained response IDs, select **LM Studio · поток
   и сессия** with the same server address. The client periodically starts a
   fresh session with the compact context. Image requests use the streaming
   endpoint without storing the chat. [Stateful chats](https://lmstudio.ai/docs/developer/rest/stateful-chats)
   [Streaming events](https://lmstudio.ai/docs/developer/rest/streaming-events)
4. Save and restart. The launcher starts bundled llama.cpp only for its own
   configured address and model. Other local model servers are started by
   their respective applications. The existing raw llama.cpp and Gemini
   adapters remain available for text. The bundled Qwen is text-only.

External activity requests require the separate text opt-in in **AI Provider**,
and images require an additional opt-in. Use **Диагностика AI → Посмотреть
контекст** to see the prepared text, last sent context and a local preview of
the available frame. The diagnostics panel shows model, connection
state, latency, requests, frames, token counts where reported and a rough
input-token estimate. Model billing and account quota depend on the provider.

### Screen, browser and media

- In **Монитор активности** enable manual capture and add full paths to trusted
  work executables. Save and restart. During a sprint press **Выбрать рабочее
  окно**, select an individual window from the list, then bring it to the foreground. It is the only eligible window for
  that capture session. **Остановить захват** and **Ctrl+Shift+F9** stop it.
  Capture is off at every startup. Window frames stay in memory. The HUD has
  native capture exclusion. Browser windows, known private apps, sensitive
  titles, and accessible password controls are filtered. Custom-drawn controls
  may lack accessibility metadata. Use the allowlist for apps with work-only
  windows and stop capture before opening sensitive content.
- To provide browser context, load `browser_extension/` as an unpacked
  extension in Chrome or Edge, allow work domains in **Монитор активности**,
  then click the extension button on the chosen work tab. Click again to stop.
  The included extension uses the default dashboard port 8765 and cannot run
  in incognito. It sends only the permitted title and domain to localhost,
  excludes common sensitive pages and password inputs, and never reads
  credential values, cookies, or page text.
  [Chrome activeTab](https://developer.chrome.com/docs/extensions/develop/concepts/activeTab)
- Enable **Метаданные медиасеанса Windows** to include the current published
  playback session. Browser sessions contribute player and playback state.
  Native players can also expose track or video title and artist.
  [Windows GSMTC](https://learn.microsoft.com/en-us/uwp/api/windows.media.control.globalsystemmediatransportcontrolssessionmanager)
- Windows application inventory uses visible, unowned top-level windows.
  Tray-only application names are not inferred from Explorer internals. The
  FocusWithAI tray continues to show the app's own status.

Windows dependencies are listed in `requirements.txt`. The existing local
installation can update its environment with
`.\.runtime\uv.exe pip install --python .venv\Scripts\python.exe -r requirements.txt`.
The first launch migrates the top-level provider keys to Windows Credential
Manager and stamps `schema_version`. Config exports and new backups omit keys.
Older backups created before migration may still contain their original keys.

Run `.\.venv\Scripts\python.exe -m unittest discover -s tests` for protocol,
privacy filtering, decision, playback, startup and sprint regression tests.
Provider tests use synthetic metadata and images and a local mock HTTP server.

## Alarm and VoicePack

In **Голос → Уведомления**, choose TTS, VoicePack, Alarm, Alarm + VoicePack,
or Alarm + TTS. Combined modes play the effect first and wait 300 ms by default
before playing the voice. Alarm + TTS prepares speech before starting the effect.
**Эффекты** and **Голосовые реплики** have independent
pack selectors and volume controls. Save and restart to apply the settings.
The **Исходный режим** option uses the TTS notification path. Older
configurations with `voicepack_enabled: true` open in VoicePack mode.

Use an event selector to choose the import destination. **Импорт файлов**
copies selected WAV, MP3 or OGG files into that event folder after decoding them.
Clips can be up to 16 MiB and 30 seconds. **Прослушать** plays any selected file
from the pack. **Назначить выбранный** binds that file to the selected event even
if its file is in a different event folder. **Сбросить назначение** restores
random selection within the event's own folder. **Активные события** controls
which events can emit an automatic cue.

In **Сегодня**, use **Пауза** to suspend monitoring or **Остановить звук** to
interrupt the current clip and cancel the rest of its sequence. **Подать сигнал**
and **Задача завершена** signal those events during a focus block. Quiet rest mode
allows the `SESSION_END` cue at the focus-to-rest boundary.

### Import StarCraft or Brood War audio

1. Use audio from a local classic installation, an extracted local directory, or
   a ZIP containing audio you are authorized to use. From the project directory,
   run one of the following. Pass several sources in order to let later sources
   provide alternate versions of the same clip. Within an installation folder,
   the importer reads the base archives, then expansion archives, then `patch_rt.mpq`.

   ```powershell
   .\.venv\Scripts\python.exe audio_import.py 'D:\Games\StarCraft'
   .\.venv\Scripts\python.exe audio_import.py 'D:\Audio\Interface.zip' 'D:\Audio\Terran.zip'
   ```

   On Windows, direct classic MPQ import uses the optional Unicode
   `StormLib.dll` in `.runtime/stormlib/`. An alternate local DLL can be supplied
   with `--stormlib`. For other game archive formats, use a local WAV export or
   ZIP as input. The report lists imported files and their duration, missing
   filenames, undecodable clips, and archive errors. [StormLib releases](https://github.com/ladislav-zezula/StormLib/releases)

2. In **Голос**, refresh the pack list and select **StarCraft / Brood War · effects**
   and **Terran Advisor** or **Zerg Advisor**. Preview the clips from your edition
   and adjust their event assignments as needed.
3. Use **Эффекты + VoicePack** for the imported combination. For clips-only
   playback, set **Если голосового файла нет** to **Пропустить**. `python
   audio_import.py --demo` generates short original tones in the local `demo`
   Alarm Pack for testing. **Эффекты + TTS** can use these tones with the existing
   TTS chain.

The release contains the `starcraft`, `terran`, and `zerg` manifests. Imported
and generated audio stays local and is ignored by Git. The Sounds Resource
provides reference listings for [Terran Advisor](https://sounds.spriters-resource.com/pc_computer/starcraft/asset/419574/),
[Zerg Advisor](https://sounds.spriters-resource.com/pc_computer/starcraft/asset/419596/),
[Interface](https://sounds.spriters-resource.com/pc_computer/starcraft/asset/419499/), and
[Miscellaneous](https://sounds.spriters-resource.com/pc_computer/starcraft/asset/419500/).
The site describes the copyright status of its archives in its help and terms.
The importer reads only the local sources you supply. [Help](https://sounds.spriters-resource.com/page/help/) ·
[Terms of Use](https://sounds.spriters-resource.com/page/tou/) ·
[Official StarCraft](https://starcraft.blizzard.com/en-us) ·
[Blizzard EULA](https://www.blizzard.com/en-us/legal/bfbbb648-bcc6-4b78-a5c7-e2fe20c135df/blizzard-end-user-license-agreement)

### Events and candidate files

Event folders use lowercase IDs. The import recognizes both `misc` and `sounds`,
and both `glue` and `interface`, depending on the source. The table specifies
candidate assignments. Preview the actual imported recordings before relying on
their meaning or volume. [Classic sound index](https://www.darkenedfantasies.com/resources/sc_enum_sfxd.htm)

| Event | Trigger | StarCraft effect | Terran / Zerg Advisor candidates |
| --- | --- | --- | --- |
| `SESSION_START` | A focus block begins | `glue/countdown.wav` | — |
| `DISTRACTION` | First sustained off-list window episode | `misc/buzz.wav` | `tadupd00.wav` / `zadupd00.wav` |
| `PERSISTENT_DISTRACTION` | An episode reaches `punishment_interval` | `misc/perror.wav` | `tadupd01.wav` / `zadupd01.wav` |
| `REPEATED_DISTRACTION` | Another episode in the same block | `misc/perror.wav` | `tadupd01.wav` / `zadupd01.wav` |
| `RETURN_TO_WORK` | A work window becomes active after an episode or idle period | — | — |
| `TASK_COMPLETED` | **Задача завершена** | `glue/scorefillend.wav` | `tadupd02.wav`, `tadupd06.wav` / `zadupd02.wav` |
| `SESSION_END` | The focus timer expires | `misc/youwin.wav` | `tadupd06.wav` / `zadupd02.wav` |
| `IDLE` | Windows reports no mouse or keyboard input for the configured period | — | — |
| `ATTENTION` | **Подать сигнал** or the LLM selects a softer initial warning | `misc/transmission.wav` | `tadupd00.wav` / `zadupd00.wav` |
| `FOCUS_MODE_ENABLED` | Monitoring resumes during a focus block | `glue/swishlock.wav` | — |

To create a custom pack, copy `voicepacks/template` into `assets/voicepacks/`
or `assets/alarms/`, rename the folder using ASCII letters, digits, hyphens or
underscores, and edit `name` in `manifest.json`. The manifest version is `1`.
The existing `voicepacks/` location is also scanned.

### Configuration

| `config.json` key | Default | Purpose |
| --- | --- | --- |
| `notification_mode` | `""` | Select `tts`, `voicepack`, `alarm`, `alarm_voicepack`, or `alarm_tts` |
| `alarm_pack_id`, `voicepack_id` | `""` | Selected alarm and voice package folders |
| `alarm_volume`, `voicepack_volume` | `0.40`, `0.65` | Independent volumes from `0` to `1` |
| `alarm_voice_delay_ms` | `300` | Minimum delay after the effect in a combined notification |
| `audio_event_mappings` | `{}` | Overrides by kind (`alarm` or `voice`), pack ID, and event ID |
| `notification_disabled_events` | `[]` | Event IDs whose automatic cues are disabled |
| `voicepack_min_interval` | `15` | Minimum seconds between notifications, with a floor of 5 |
| `voicepack_repeat_interval` | `120` | Interval between persistent warning repeats |
| `voicepack_max_repeats` | `2` | Additional repeats after the first persistent warning in one episode |
| `voicepack_idle_seconds` | `180` | Idle threshold in seconds |
| `voicepack_quiet_breaks` | `true` | Pause observations and extra cues during scheduled rest |
| `voicepack_tts_fallback` | `true` | Use the existing TTS chain when a voice clip is missing or unreadable |

For a candidate event the configured LLM receives activity context and returns
`event` and `reason` as JSON, such as
`{"event":"DISTRACTION","reason":"Активное окно не связано с текущей задачей."}`.
The tracker constrains valid event IDs and falls back to its rules on timeout or
invalid output. **Сегодня** shows the latest decision and its source. Turn off
**Классификация LLM** to use tracker rules directly. Filenames, mappings and audio
are handled locally. Combined playback and preview share one audio sequence at
a time. The player decodes on demand, limits peaks and uses a short fade-in.

Run the audio tests with `python -m unittest discover -s tests -v`.

## Focus HUD and interventions on Windows

Open **HUD и фокус** in the local panel. Enable the meter and HUD as desired,
save, and restart the tracker. The HUD follows the active monitor and uses its
work area. The corner, dimensions, opacity, click-through, sprint name,
timer, status and meter display are configurable. The floating window uses a non-activating Win32 style. **Скрыть HUD**
and **Показать HUD** control visibility immediately. Full-screen exclusive apps
can control their own display surface.

The foreground window is sampled once a second during an active sprint. The
meter updates four times a second and redraws the HUD when its visible content
changes. Classification runs asynchronously, waits for the window to stabilize,
backs off after failures or unchanged observations, and starts at most one request
every 25 seconds by default. Supporting work such as research and reading documentation
is evaluated against the goal. If classification is disabled or evidence is
ambiguous, the state is `UNCERTAIN`. Without an active sprint, there are no
classification requests or interventions.

| Activity | Meter behavior |
| --- | --- |
| `ON_TASK` | Recovers at `focus_recovery_per_second` points per second |
| `OFF_TASK` | Rises at `focus_gain_per_second` points per second |
| `UNCERTAIN` | Holds and rechecks when another classifier request is due |
| `IDLE` | Holds after the Windows idle threshold |
| `BREAK` | Holds outside work and on pause. The HUD labels a manual pause separately |

| Level | Entry range | Audio event |
| --- | --- | --- |
| `NORMAL` | 0–29 | `RETURN_TO_WORK` on recovery |
| `ATTENTION` | 30–59 | `ATTENTION` |
| `WARNING` | 60–84 | `FOCUS_WARNING` |
| `CRITICAL` | 85–100 | `FOCUS_CRITICAL` |

The falling meter must drop below the entry threshold minus the configured
hysteresis, which defaults to 5 points. Downward transitions are quiet until
recovery to `NORMAL`. Each level has its own audible cooldown, 45 seconds by
default. The existing playback cooldown also applies. While the meter is enabled,
it supplies distraction and recovery cues to Alarm, VoicePack, or TTS. Packs
with empty `focus_warning/` or `focus_critical/` folders can use their existing
long or repeated distraction clips. In the `demo` Alarm Pack these levels have
their own generated cues.

### Voluntary interventions

At `CRITICAL`, enable the effect, announcer, visual warning, and dialogue
individually. The dialogue offers a return to work and can also offer a pause
or open the task editor. If neither critical audio option is selected, the
regular threshold chime follows the selected notification mode. Use the sound
switch or take the event out of **Активные события** to silence it.

An optional time-limited foreground restriction can minimize a window whose
full EXE path matches a selected application, or whose active tab matches a
selected domain or its subdomain in Chrome or Edge. For browser restrictions,
the active tab's domain is read locally from the browser toolbar. A missing or
unsupported address is ignored. Network traffic and system functions continue
normally. Apps and browser tabs stay open, and you can restore their windows
after the restriction expires or is released.

To use restrictions, enable **Разрешить временные ограничения**, enter the
targets and apply the settings. During an active sprint, click
**Разрешить ограничения на этот спринт** before the critical level. The configured
timer runs for 5–120 seconds after `CRITICAL`. Pause, break, completion, a change
of goal and restart clear consent. **Снять ограничения** releases the current timer at
once. **Отключить вмешательства** and **Ctrl+Shift+F9** also stop the active
intervention sounds and dismiss the warning. If Windows cannot register the
hotkey, the panel still controls interventions and the restriction stays off.

Run the meter and intervention tests with `python -m unittest discover -s tests -v`.

## Configuration  

### Main Parameters:
- `ai_provider`: `llama`, `openai` or `google_assistant` (alias `gemini`)
- `ai_fallback_providers`: list of backup providers
- `target_apps`: List of target applications
- `reminder_interval`: Time until reminder (seconds)
- `punishment_interval`: Time until AI "punishment" (seconds)
- `punishment_cooldown`: Minimum pause between AI punishments (seconds, default = `punishment_interval`)
- `praise_interval`: Time for AI "praise" (seconds)

### AI behaviour:
- `prompt`: persona / system prompt
- `assistant_name`: persona name (by default taken from the old `punishment_subprompt`, else `Ева`)
- `punishment_instruction`, `praise_instruction`, `fact_instruction`: event prompts with placeholders
  `{target}`, `{distraction}`, `{distracted_seconds}`, `{count}`, `{minutes}`, `{seconds}`.
  Old `*_subprompt` keys still work.
- `ai_response_rules`: extra rules added to the system prompt (short replies, no emoji, etc.)
- `ai_history_size`: how many recent messages to send as dialogue history (default 0 = off).
  Small local models copy their own previous replies from history, so keep it 0 for them;
  for large models (Gemini, GPT) 6–10 can make replies more coherent
- `ai_event_log_size`: how many recent events (distractions, praise, facts, finished sessions)
  the model sees as a compact timestamped log (default 8). Gives continuity without copying
- `ai_style_hints`: `{"punishment": [...], "praise": [...]}` random style hint added to each request
  (e.g. "joke about it", "be strict"), never the same twice in a row
- `ai_fact_topics`: list of fact topics; a random one is picked, excluding recent ones
- `ai_repeat_threshold`: similarity (0..1, default 0.6) above which a reply counts as a repeat.
  A repeat is re-asked once with a hint, then replaced with a fallback phrase
- `ai_context_budget`: max prompt size in tokens (default 1500). Oldest history and events are
  dropped to fit. If the server still reports a context overflow, the budget is halved automatically
- `ai_memory_file`: where history, event log, recent replies and facts are saved (default
  `assistant_memory.json`, `null` = off). Facts are kept across days, the rest only for the current day
- `ai_max_chars`: max reply length passed to TTS (default 400)
- `ai_max_sentences`: hard limit of sentences in a reply (default 2, 0 = off). Small models ignore "be brief"
- `ai_drop_questions`: remove trailing questions from replies (default true), the user can't answer by voice
- `fallback_phrases`: `{"punishment": [...], "praise": [...], "fact": [...]}` spoken when AI is unavailable
- `ai_timeout`, `ai_retries`: request timeout (s) and retry count (default 30 and 2)

### llama.cpp Parameters:
- `llama_url`: `/completion` endpoint URL
- `llama_user_label`: speaker label for events in the transcript (default `System`)
- `api_key`: sent as `Authorization: Bearer`
- `temperature`, `n_predict`, `top_k`, `top_p`, `min_p`, `stop`, ...: sampling parameters

### OpenAI-compatible Parameters:
- `openai_base_url`, `openai_model`, `openai_api_key`
- `openai_max_tokens`: reply token limit (default 300)
- `openai_temperature`, `openai_top_k`, `openai_top_p`, `openai_min_p`, `openai_presence_penalty`,
  `openai_frequency_penalty`, `openai_repeat_penalty`: sent **only if set**
- `openai_thinking`: allow reasoning (default `false`)
- `openai_extra_params`: any extra request fields (applied last, override everything)

Like the llama.cpp web UI, the client does not send sampling parameters by default.
The server then uses its launch flags and the recommended values the model authors put
into the GGUF metadata (e.g. LFM2.5 ships temperature 0.1 and top_k 50). Overriding them with
generic values usually makes small models worse.

Reasoning models (LFM2.5, Qwen3, ...) can spend the whole token limit on thinking and return an
empty reply. With `openai_thinking: false` the client sends `chat_template_kwargs.enable_thinking=false`
(Qwen-style templates, same as the web UI) and `thinking_budget_tokens=0` (templates that always
open `<think>`, e.g. LFM2.5).

Tip: do not put example phrases into prompts and rules. Small models copy them into every reply.

### Text-to-speech:
Replies are spoken by the first working engine in the chain. If an engine fails, the next one
speaks, and the failed one is skipped for `tts_failure_cooldown` seconds (default 600), then retried.
- `tts_engines`: chain order (default `["silero", "edge", "piper", "sapi"]`)
- `silero`: offline [Silero](https://github.com/snakers4/silero-models) voices on CPU, natural Russian
  with auto-stress, ~0.1 s per phrase, ~600 MB RAM. Needs PyTorch (CPU build, installed by `start.bat`:
  `pip install torch --index-url https://download.pytorch.org/whl/cpu`) and the model
  `voices/silero_v5_6_ru.pt` (downloaded by `start.bat`). Model license: CC BY-NC 4.0 (non-commercial).
  `silero_speaker`: `xenia` (default), `baya`, `kseniya`, `aidar`, `eugene`;
  `silero_rate`: `x-slow`..`x-fast`, `silero_pitch`: `x-low`..`x-high` (SSML prosody);
  `silero_sample_rate` (default 48000), `silero_threads` (default 8)
- `edge`: Microsoft Edge online voices, best quality. Unofficial API: if it starts failing with
  `403`, update the package: `pip install -U edge-tts`.
  `edge_tts_voice` (default `ru-RU-SvetlanaNeural`, also `ru-RU-DmitryNeural`), `edge_tts_rate` (e.g. `"+10%"`)
- `piper`: offline [Piper](https://github.com/rhasspy/piper) voices, good quality, ~0.7 s per phrase on CPU.
  `piper_model` (default `voices/ru_RU-irina-medium.onnx`, downloaded by `start.bat`),
  `piper_length_scale` (>1 slower, <1 faster). Other Russian voices: `denis`, `dmitri`, `ruslan` from
  [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices/tree/main/ru/ru_RU)
- `sapi`: built-in Windows voices, offline, nothing to install. `sapi_voice` (default `Irina`), `sapi_rate` (-10..10)

### Audio output:
- `audio_frequency`: mixer sample rate (default 48000, matches most Windows devices)
- `audio_buffer`: mixer buffer in samples (default 2048). If you still hear crackling, try 4096
- `reminder_volume`: warning sound volume 0..1 (default 1.0)

The warning sound and speech are decoded once into memory and play on separate channels,
so the warning does not cut off speech.

### Gemini Parameters:
- `google_assistant_api_key`: API key (sent in a header, not in the URL)
- `google_model`: model name (default `gemini-2.5-flash`)
- `google_max_tokens`, `google_temperature`, `google_top_p`, `google_top_k`
- `google_thinking_budget`: thinking token budget (0 by default for 2.5-flash so short replies are not cut off)

## File Structure  

```  
workReminder/  
├── main.py              # Main script  
├── ai_client.py         # AI providers (llama.cpp, OpenAI-compatible, Gemini)  
├── assistant.py         # Prompts, context, dialogue memory, TTS cleanup  
├── voicepack.py         # Event classification, pack catalog and clip playback
├── audio_import.py     # Local ZIP, directory, MPQ and demo audio imports
├── focus_meter.py     # Activity sampling, LLM classifier and meter
├── focus_runtime.py   # Independent Windows focus loop
├── sprints.py         # Sprint state, clock, templates and report persistence
├── acquisition.py     # Context source lifecycle and single-slot window capture
├── windows_acquisition.py # Windows frame, app inventory and media metadata
├── activity_context.py # Privacy filters and rolling inference context
├── activity_ai.py     # Inference providers, result protocol and diagnostics
├── decision.py        # Confidence and persistence gate
├── config_store.py    # Versioned preferences and Windows credentials
├── browser_extension/ # Optional Chromium foreground tab context
├── focus_hud.py       # Native non-activating HUD and dialogue
├── focus_windows.py   # Foreground and browser toolbar integration
├── intervention.py   # Threshold actions and temporary foreground guard
├── launcher.py       # Startup order, readiness checks and process ownership
├── startup_manager.py # Windows logon task preferences and registration
├── startup_tray.py   # Native Windows notification area controls
├── assets/            # Pack manifests and git-ignored local recordings
├── voicepacks/template/ # Empty user pack template
├── panel.py             # Local web panel server  
├── panel_web/           # Web panel page, styles and script  
├── config.json          # Configuration  
├── sessions.jsonl       # Optional planning blocks
├── sprints.json         # Local sprint data, created on first save
├── session_stats.json   # Session statistics  
├── requirements.txt     # Dependencies  
└── README.MD           # Documentation  
```  

## License  

MIT License  

Python script to help maintain focus on work applications.  
One timer alerts every 5 seconds to return to work,  
another uses AI to generate "punishment" messages,  
and a third prompts AI to generate "praise" messages.  

***After being abandoned by a friend with no moral support for work, I enhanced my AI interface. This solution was eventually condensed into a single Python script.***  

*Hope it helps you stay focused!*  

*JErfurt*  

[YT video (old)](https://www.youtube.com/watch?v=AqjlrRfCPb8)  

## Installation  
Use Conda or Venv to avoid system conflicts.  
For the AI backend, llama.cpp server is recommended. Check out [llama.cpp](https://github.com/ggerganov/llama.cpp) – it's very user-friendly!  

### Simplified Windows Guide  

1. Clone/unpack the repository and open the folder in CMD.  
2. Create a virtual environment:  
```  
python -m venv venv  
```  
3. Activate the environment:  
```  
venv\Scripts\activate  
```  
4. Install dependencies:  
```  
pip install -r requirements.txt  
```  
5. Download a compatible release from [llama.cpp/releases](https://github.com/ggerganov/llama.cpp/releases) (e.g., CUDA for GPU or AVX2 for CPU).  
6. Unpack llama.cpp and place it in your preferred location.  
7. Download a GGUF model from [HuggingFace](https://huggingface.co/) (e.g., llama-3B).  

### Launching the Server  

For CUDA (adjust GPU layers as needed):  
```  
llama-server.exe -m Models/YOUR_MODEL.gguf --n-gpu-layers 29 --split-mode layer --main-gpu 0 --ctx-size 4096  
```  

For CPU-only:  
```  
llama-server.exe -m Models/YOUR_MODEL.gguf --ctx-size 4096  
```  

### Running the Script  

Reactivate the virtual environment if needed:  
```  
venv\Scripts\activate  
```  

Start the main script:  
```  
python main.py  
```  

### Quick Setup Summary  
The process is straightforward. For additional details, refer to the documentation.  

--------

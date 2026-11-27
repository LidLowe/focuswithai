'use strict';

// ---------- утилиты ----------

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const clone = (o) => (o === undefined ? undefined : JSON.parse(JSON.stringify(o)));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const MASK = '••••••••';

function plural(n, one, few, many) {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  if (b === 1) return one;
  return many;
}
const pad = (n) => String(n).padStart(2, '0');
function hhmm(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function fmtMS(sec) {
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}
const isToday = (ts) => ts && new Date(ts * 1000).toDateString() === new Date().toDateString();

const I = {
  x: '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M3 3l6 6M9 3l-6 6"/></svg>',
  x16: '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8"/></svg>',
  chev: '<svg class="chev" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M4 6l4 4 4-4"/></svg>',
  play: '<svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor" aria-hidden="true"><path d="M3 2l9 5-9 5z"/></svg>',
  up: '<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M3 9l4-4 4 4"/></svg>',
  down: '<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M3 5l4 4 4-4"/></svg>',
  grip: '<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M2 4h10M2 7h10M2 10h10"/></svg>',
  speak: '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M3 6h2.5L9 3v10L5.5 10H3z"/><path d="M11.5 5.5a3.5 3.5 0 010 5"/></svg>',
};

// ---------- состояние ----------

const S = {
  cfg: null, defaults: {}, models: {}, voices: {}, mtime: null,
  draft: null, touched: new Set(),
  status: null, statusAt: 0, conn: true,
  route: 'current', loaded: {}, open: {},
  audioPacks: { alarm: [], voice: [], events: [] }, audioEvents: {}, audioFiles: {},
  startup: null,
  sprints: null, sprintEdit: null, sprintForm: null,
  logs: [], logSeq: 0, logPaused: false,
  busy: {},
};

async function api(path, opts = {}) {
  const o = { method: opts.method || 'GET', headers: {} };
  if (opts.body !== undefined) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(opts.body); }
  if (opts.raw !== undefined) o.body = opts.raw;
  if (o.method !== 'GET') o.headers['X-Panel'] = '1';
  const r = await fetch(path, o);
  let data = null;
  try { data = await r.json(); } catch (e) { /* не JSON */ }
  if (!r.ok) throw new Error((data && data.error) || `HTTP ${r.status}`);
  return data;
}

// ---------- черновик настроек ----------

function getP(o, p) {
  for (const k of p.split('.')) {
    if (o == null || typeof o !== 'object') return undefined;
    o = o[k];
  }
  return o;
}
function setP(o, p, v) {
  const ks = p.split('.');
  let c = o;
  for (const k of ks.slice(0, -1)) {
    if (typeof c[k] !== 'object' || c[k] === null || Array.isArray(c[k])) c[k] = {};
    c = c[k];
  }
  if (v === undefined) delete c[ks[ks.length - 1]];
  else c[ks[ks.length - 1]] = v;
}
function eff(p, src = S.draft) {
  const v = getP(src, p);
  return v !== undefined ? v : clone(getP(S.defaults, p));
}
function setv(p, v) {
  setP(S.draft, p, v);
  S.touched.add(p);
  updateDirty();
}
const dirtyPaths = () => [...S.touched].filter((p) => !same(eff(p), eff(p, S.cfg)));
const dirtyCount = () => dirtyPaths().length;
const live = () => !!(S.status && S.status.live);

function loadCfg(r) {
  S.cfg = r.config; S.defaults = r.defaults; S.models = r.models; S.voices = r.voices; S.mtime = r.mtime;
  S.draft = clone(r.config); S.touched.clear();
}

async function saveAll() {
  const paths = dirtyPaths();
  const set = {}, unset = [];
  for (const p of paths) {
    let v = getP(S.draft, p);
    if (Array.isArray(v) && v.every((x) => typeof x === 'string')) v = v.map((x) => x.trim()).filter(Boolean);
    if (v === undefined) unset.push(p); else set[p] = v;
  }
  let cfgSaved = false;
  try {
    if (paths.length) { loadCfg(await api('/api/config', { method: 'PUT', body: { set, unset } })); cfgSaved = true; }
  } catch (e) {
    toast(e.message, { bad: true });
    return;
  }
  render();
  if (cfgSaved) toast('Сохранено. Новые настройки заработают после перезапуска.', live() ? { action: ['Перезапустить', restart] } : {});
}

function revertAll() {
  S.draft = clone(S.cfg); S.touched.clear();
  render();
}

function saveBar() {
  const n = dirtyCount();
  if (!n) return '<div class="savebar" id="savebar">Всё сохранено</div>';
  return `<div class="savebar" id="savebar"><span class="cnt"><span class="dot warn"></span>${n} ${plural(n, 'изменение', 'изменения', 'изменений')}</span>
    <button class="btn" type="button" data-act="revert">Отменить</button>
    <button class="btn dark" type="button" data-act="save">Сохранить</button></div>`;
}
function updateDirty() {
  const el = $('#savebar');
  if (el) el.outerHTML = saveBar();
  const d = new Set(dirtyPaths());
  $$('[data-k]').forEach((f) => {
    if (f.type !== 'radio' && f.type !== 'checkbox' && f.type !== 'range') f.classList.toggle('changed', d.has(f.dataset.k));
  });
}

// ---------- построители полей ----------

let uid = 0;
const nid = () => `f${++uid}`;

function row(label, help, key, ctl, id) {
  return `<div class="row"><div class="lbl">${id ? `<label for="${id}">${label}</label>` : `<div class="t">${label}</div>`}${help ? `<div class="help">${help}</div>` : ''}${key ? `<div class="key">${key}</div>` : ''}</div><div class="ctl">${ctl}</div></div>`;
}
function num(k, o = {}) {
  const int = o.int !== false;
  return `<input${o.id ? ` id="${o.id}"` : ''} class="field num${o.sm ? ' sm' : ''}" type="number" data-k="${k}" data-t="num"${o.scale ? ` data-scale="${o.scale}"` : ''}${o.opt ? ' data-opt="1"' : ''}${int ? ' data-int="1"' : ''} step="${o.step || (int && !o.scale ? 1 : 'any')}"${o.min !== undefined ? ` min="${o.min}"` : ''}${o.max !== undefined ? ` max="${o.max}"` : ''}${o.label ? ` aria-label="${esc(o.label)}"` : ''}${o.ph ? ` placeholder="${esc(o.ph)}"` : ''}${o.style ? ` style="${o.style}"` : ''}>`;
}
function txt(k, o = {}) {
  return `<input${o.id ? ` id="${o.id}"` : ''} class="field${o.mono ? ' mono' : ''}" type="${o.secret ? 'password' : 'text'}" data-k="${k}" data-t="str" autocomplete="off" spellcheck="false"${o.ph ? ` placeholder="${esc(o.ph)}"` : ''}${o.label ? ` aria-label="${esc(o.label)}"` : ''}${o.list ? ` list="${o.list}"` : ''} style="${o.style || 'max-width: 420px; width: 100%'}">`;
}
function area(k, rows, o = {}) {
  return `<textarea${o.id ? ` id="${o.id}"` : ''} class="area" rows="${rows}" data-k="${k}" data-t="str"${o.label ? ` aria-label="${esc(o.label)}"` : ''}></textarea>`;
}
function tog(k, label, extra = '') {
  return `<label class="togl"><input class="tog" type="checkbox" data-k="${k}" data-t="bool">${label}${extra}</label>`;
}
function sel(k, options, o = {}) {
  return `<select${o.id ? ` id="${o.id}"` : ''} class="field${o.mono ? ' mono' : ''}" data-k="${k}" data-t="${o.t || 'str'}" style="max-width: ${o.w || 220}px">${options.map(([v, l]) => `<option value="${esc(v)}">${esc(l ?? v)}</option>`).join('')}</select>`;
}
function pills(k, options, o = {}) {
  return `<div class="pills" role="radiogroup" aria-label="${esc(o.label || k)}">${options.map(([v, l]) => `<label class="pill"><input type="radio" name="${k}" value="${esc(v)}" data-k="${k}" data-t="${o.t || 'str'}">${esc(l)}</label>`).join('')}</div>`;
}
function jsonF(k, o = {}) {
  return `<input class="field mono" type="text" data-k="${k}" data-t="json" spellcheck="false"${o.label ? ` aria-label="${esc(o.label)}"` : ''} style="width: 100%; max-width: 520px">`;
}
const lw = (k, kind, o = {}) => `<div class="lw" data-lw="${k}" data-kind="${kind}" data-ph="${esc(o.ph || '')}" data-add="${esc(o.add || '')}" data-name="${esc(o.name || 'Элемент')}"></div>`;
function det(id, summary, body, o = {}) {
  const open = S.open[id] ?? !!o.open;
  return `<details class="${o.cls || 'card'}" data-det="${id}"${open ? ' open' : ''}><summary><span class="t"><b>${summary}</b>${o.sub ? `<span>${o.sub}</span>` : ''}</span>${I.chev}</summary>${body}</details>`;
}

function fmtOut(el) {
  const v = Number(el.value) * Number(el.dataset.scale || 1);
  const out = $(`#${el.dataset.out}`);
  if (out) out.textContent = Number(el.dataset.scale) === 0.01 && el.dataset.k === 'reminder_volume' ? `${el.value}%` : v.toFixed(2);
}

function hydrate(root) {
  $$('[data-k]', root).forEach((el) => {
    const k = el.dataset.k, t = el.dataset.t;
    const v = eff(k);
    if (el.type === 'radio') el.checked = String(v ?? '') === el.value;
    else if (el.type === 'checkbox') el.checked = !!v;
    else if (t === 'num') {
      el.value = v === undefined || v === null || v === '' ? '' : String(+(v / Number(el.dataset.scale || 1)).toFixed(4));
      if (el.dataset.out) fmtOut(el);
    } else if (t === 'json') el.value = v === undefined ? '' : JSON.stringify(v);
    else if (el.tagName === 'SELECT') {
      if (![...el.options].some((op) => op.value === String(v))) el.add(new Option(String(v), String(v)));
      el.value = String(v);
    } else el.value = v ?? '';
  });
  $$('.lw', root).forEach(renderLW);
  updateDirty();
}

function onField(el) {
  const k = el.dataset.k, t = el.dataset.t;
  let v;
  if (el.type === 'radio') {
    if (!el.checked) return;
    v = t === 'int' ? parseInt(el.value, 10) : el.value;
  } else if (el.type === 'checkbox') v = el.checked;
  else if (t === 'num') {
    if (el.value === '') {
      if (!el.dataset.opt) { el.classList.add('bad'); return; }
      v = undefined;
    } else {
      let n = Number(el.value);
      if (!Number.isFinite(n)) { el.classList.add('bad'); return; }
      n *= Number(el.dataset.scale || 1);
      v = el.dataset.int ? Math.round(n) : +n.toFixed(6);
    }
    if (el.dataset.out) fmtOut(el);
  } else if (t === 'json') {
    try { v = el.value.trim() === '' ? undefined : JSON.parse(el.value); } catch (e) { el.classList.add('bad'); return; }
  } else if (t === 'int') v = parseInt(el.value, 10);
  else if (t === 'bool') v = el.value === 'true';
  else v = el.value;
  el.classList.remove('bad');
  setv(k, v);
  if (el.dataset.rerender) render();
}

// Списки строк: строки с полями ввода или «чипсы»
function renderLW(el) {
  const k = el.dataset.lw, kind = el.dataset.kind, name = el.dataset.name;
  const items = eff(k) || [];
  if (kind === 'rows') {
    el.innerHTML = `<div class="lrows">${items.map((s, i) => `<div class="li"><input class="field sm" type="text" value="${esc(s)}" data-li="${i}" aria-label="${esc(name)} ${i + 1}"><button class="ico" type="button" data-lwdel="${i}" aria-label="Удалить">${I.x}</button></div>`).join('')}</div>
      <div class="inl" style="margin-top: 6px"><button class="btn" type="button" data-lwadd>+ ${esc(el.dataset.add || 'Добавить')}</button></div>`;
  } else {
    el.innerHTML = `<div class="chips">${items.map((s, i) => `<span class="chip">${esc(s)}<button type="button" data-lwdel="${i}" aria-label="Убрать ${esc(s)}">${I.x}</button></span>`).join('') || '<span class="note">Список пуст</span>'}</div>
      <div class="inl" style="margin-top: 12px"><input class="field" type="text" data-lwnew placeholder="${el.dataset.ph}" aria-label="${esc(el.dataset.ph || 'Добавить')}" style="flex: 1 1 260px"><button class="btn" type="button" data-lwadd>Добавить</button></div>`;
  }
}
function lwAdd(el, value) {
  const k = el.dataset.lw, items = clone(eff(k) || []);
  if (el.dataset.kind === 'rows') {
    items.push('');
    setv(k, items); renderLW(el);
    const inputs = $$('[data-li]', el);
    inputs[inputs.length - 1].focus();
    return;
  }
  const input = $('[data-lwnew]', el);
  const v = (value ?? input.value).trim();
  if (!v) { input.focus(); return; }
  if (!items.includes(v)) items.push(v);
  setv(k, items); renderLW(el);
  $('[data-lwnew]', el).focus();
}

// ---------- навигация ----------

const NAV = [
  ['Работа', [['current', 'Текущий спринт'], ['history', 'История спринтов'], ['activity', 'Контекст активности']]],
  ['Фокус', [['live', 'Gemini Live'], ['intervention', 'Вмешательства'], ['voice', 'Сигналы и голосовые пакеты'], ['focus', 'HUD']]],
  ['Система', [['startup', 'Автозапуск'], ['diagnostics', 'Диагностика']]],
];
const TITLES = Object.fromEntries(NAV.flatMap(([, items]) => items));

function statusLine() {
  const st = S.status;
  if (!S.conn) return '<span class="dot warn"></span>Нет связи с панелью';
  if (!st) return '<span class="dot off"></span>Подключение…';
  if (!st.live) return '<span class="dot off"></span>Трекер не запущен';
  const state = st.sprint?.state;
  return '<span class="dot ' + (state === 'ACTIVE' ? 'on' : 'warn') + '"></span>' +
    ({ ACTIVE: 'Идёт спринт', PAUSED: 'Пауза', BREAK: 'Перерыв' }[state] || 'Ожидает спринт');
}

function renderNav() {
  if (!S.cfg) { $('#nav').innerHTML = '<div class="brand"><b>FocusWithAI</b></div>'; return; }
  const saved = S.mtime ? (isToday(S.mtime) ? `в ${hhmm(S.mtime)}` : new Date(S.mtime * 1000).toLocaleDateString('ru-RU')) : '—';
  setHtml($('#nav'), `
    <div class="brand"><b>FocusWithAI</b><div class="st">${statusLine()}</div></div>
    ${NAV.map(([group, items]) => `<div class="navg"><h2>${group}</h2>${items.map(([id, title]) => `<a href="#/${id}"${S.route === id ? ' aria-current="page"' : ''}>${title}</a>`).join('')}</div>`).join('')}
    <div class="side-foot"><div>Конфиг сохранён ${saved}</div>
      ${live() ? '<button class="btn wide" type="button" data-act="restart">Перезапустить скрипт</button>' : '<div>Трекер запускается через <span class="mono">start.bat</span></div>'}</div>`);
}

// Опрос идёт каждые 2 секунды: DOM меняется только когда разметка правда другая,
// иначе кнопки под курсором пересоздавались бы и клики терялись
function setHtml(el, html) {
  if (el._html === html) return false;
  el._html = html;
  el.innerHTML = html;
  return true;
}

function routeFromHash() {
  const r = location.hash.replace(/^#\/?/, '');
  return TITLES[r] ? r : 'current';
}

function render() {
  renderNav();
  const page = PAGES[S.route];
  const main = $('#main');
  main.className = `main${page.narrow ? ' narrow' : ''}`;
  if (!S.cfg) { main.innerHTML = '<div class="loading">Загрузка…</div>'; return; }
  if (page.load && !S.loaded[S.route]) {
    S.loaded[S.route] = true;
    page.load().then(() => { if (S.route) render(); }).catch((e) => toast(e.message, { bad: true }));
  }
  const html = page.render();
  main._html = html;
  main.innerHTML = html;
  document.title = `${TITLES[S.route]} — FocusWithAI`;
  hydrate(main);
  if (page.after) page.after();
}

const PAGES = {};

const FOCUS_STATES = { ON_TASK: 'В работе', UNCERTAIN: 'Уточняю активность', OFF_TASK: 'Отвлечение',
  IDLE: 'Бездействие', BREAK: 'Перерыв', PAUSED: 'На паузе', NO_SPRINT: 'Нет активного спринта' };
const FOCUS_LEVELS = { NEUTRAL: 'Нет подтверждения', NORMAL: 'Норма', ATTENTION: 'Внимание', WARNING: 'Предупреждение', CRITICAL: 'Критический' };


const SPRINT_STATES = { NOT_STARTED: 'Черновик', ACTIVE: 'В работе', PAUSED: 'На паузе', BREAK: 'Перерыв',
  COMPLETED: 'Завершён', CANCELLED: 'Отменён' };

function sprintButton(action, id, label, primary = false, disabled = false) {
  return `<button class="btn${primary ? ' dark' : ''}" type="button" data-act="sprint" data-cmd="${action}"
    data-id="${esc(id)}"${disabled ? ' disabled' : ''}>${label}</button>`;
}

function sprintLive(st, controls = false) {
  if (!st?.live) return '<p class="note">Запусти FocusWithAI, чтобы управлять спринтами.</p>';
  const sprint = st.sprint;
  if (!sprint) return '<h2>Начни с одной цели</h2><div class="note">Оценка активности начнётся после запуска спринта.</div>';
  const { id, title, goal, state } = sprint;
  const active = state === 'ACTIVE', onBreak = state === 'BREAK';
  const timer = onBreak ? sprint.break_remaining : sprint.remaining ?? sprint.seconds;
  return `<div class="sprint-heading">
    <div><div class="note">${sprint.recovered ? 'Спринт восстановлен · возобнови, когда будешь готов' : SPRINT_STATES[state]}</div>
    <h2>${esc(title)}</h2>${title === goal ? '' : `<div>${esc(goal)}</div>`}</div>
    <div class="sprint-time"><b data-sprint-clock>${fmtMS(timer)}</b>
      <span class="note">${onBreak ? 'до конца перерыва' : sprint.remaining === null ? 'в работе' : 'осталось'}</span></div>
    </div>
    ${controls ? `<div class="inl sprint-actions">
      ${sprintButton(active ? 'pause' : 'resume', id, active ? 'Пауза' : 'Возобновить', !active)}
      ${active ? sprintButton('break', id, 'Перерыв на 5 минут') : ''}
      ${sprintButton('complete', id, 'Завершить')}
      ${sprintButton('cancel', id, 'Отменить')}
      ${sprintButton('edit_form', id, 'Редактировать цель')}
    </div>` : ''}`;
}

function sprintForm() {
  const edit = S.sprintEdit;
  const draft = S.sprintForm || edit || {};
  const subtasks = draft.subtasks?.map((t) => t.text ?? t).join('\n') || '';
  const timed = !edit || edit.state === 'NOT_STARTED';
  const quick = !edit && !S.status?.sprint && S.status?.startup_running;
  const canStartDraft = edit?.state === 'NOT_STARTED';
  return `<form class="card pad sprint-form" data-sprint-form>
    <h2>${edit ? 'Редактирование спринта' : 'Новый спринт'}</h2>
    <label>Цель <input class="field" name="goal" placeholder="Например, разобраться с VLAN и настроить стенд"
      value="${esc(draft.goal)}" required maxlength="500"></label>
    <details${edit || draft.duration_minutes ? ' open' : ''}><summary>Параметры спринта</summary>
      <div class="sprint-fields">
        <label>Название <input class="field" name="title" maxlength="160" value="${esc(draft.title)}"></label>
        <label>Длительность, мин <input class="field" type="number" name="duration_minutes" min="1" max="1440"
          placeholder="Без ограничения" value="${esc(draft.duration_minutes)}"${timed ? '' : ' disabled'}></label>
        <label>Описание задачи <textarea class="field" name="description" rows="2" maxlength="2000">${esc(draft.description)}</textarea></label>
        <label>Ожидаемый результат <textarea class="field" name="expected_outcome" rows="2" maxlength="1000">${esc(draft.expected_outcome)}</textarea></label>
        <label>Подзадачи, по одной на строку <textarea class="field" name="subtasks" rows="3">${esc(subtasks)}</textarea></label>
        <label>Дополнительный контекст для модели <textarea class="field" name="context" rows="3" maxlength="2000">${esc(draft.context)}</textarea></label>
      </div>
    </details>
    <div class="inl">
      ${edit ? `<button class="btn dark" type="submit" value="edit">Сохранить изменения</button>
        ${canStartDraft ? `<button class="btn" type="submit" value="save_start" data-start-draft${S.status?.sprint ? ' disabled' : ''}>Сохранить и начать</button>` : ''}
        ${sprintButton('close_edit', edit.id, 'Закрыть редактор')}`
        : `<button class="btn dark" data-sprint-quick type="submit" value="quick_start"${quick ? '' : ' disabled'}>Начать спринт</button>
           <button class="btn" type="submit" value="create"${S.status?.startup_running ? '' : ' disabled'}>Сохранить черновик</button>`}
    </div>
  </form>`;
}

function sprintReport(sprint) {
  const report = sprint.report;
  const tasks = sprint.subtasks.filter((task) => task.done).map((task) => `<li>${esc(task.text)}</li>`).join('');
  const apps = Object.entries(report.apps).sort((a, b) => b[1] - a[1]).slice(0, 5)
    .map(([name, seconds]) => `<li>${esc(name)} · ${fmtMS(seconds)}</li>`).join('');
  const summary = [['Время работы', sprint.seconds], ['По цели', report.ON_TASK], ['Отвлечения', report.OFF_TASK]];
  const date = sprint.started_at && new Date(sprint.started_at * 1000).toLocaleString('ru-RU');
  return `<div class="sprint-report">
    ${date ? `<p class="note">Начало · ${esc(date)}</p>` : ''}
    <dl>${summary.map(([label, seconds]) => `<div><dt>${label}</dt><dd>${fmtMS(seconds)}</dd></div>`).join('')}
      <div><dt>Вмешательства</dt><dd>${report.interventions}</dd></div></dl>
    <p class="note">Неоднозначная активность и бездействие входят в общее время работы.</p>
    ${sprint.expected_outcome ? `<p>Ожидалось · ${esc(sprint.expected_outcome)}</p>` : ''}
    <div class="sprint-fields">
      <div><b>Основные приложения</b>${apps ? `<ul>${apps}</ul>` : '<div class="note">Нет данных</div>'}</div>
      <div><b>Выполненные подзадачи</b>${tasks ? `<ul>${tasks}</ul>` : '<div class="note">Нет отметок</div>'}</div>
    </div>
    ${sprint.state === 'COMPLETED' ? `<form data-sprint-outcome="${sprint.id}">
      <label>Фактический результат
        <textarea class="field" name="actual_outcome" rows="2" maxlength="2000" required
          placeholder="Что получилось и что осталось сделать?">${esc(sprint.actual_outcome)}</textarea></label>
      ${!sprint.actual_outcome ? '<div class="note">Ожидает твоего подтверждения</div>' : ''}
      <button class="btn" type="submit">Подтвердить результат</button>
    </form>` : ''}
  </div>`;
}

function sprintLibrary() {
  if (!S.sprints) return '<div class="note">Загрузка истории…</div>';
  const current = S.sprints.current;
  const items = S.sprints.sprints;
  const selected = current && items.find((item) => item.id === current.id);
  const drafts = items.filter((item) => item.state === 'NOT_STARTED');
  if (S.route === 'history') {
    const history = items.filter((item) => ['COMPLETED', 'CANCELLED'].includes(item.state));
    return sprintHistory(history);
  }
  return `${selected?.subtasks.length ? `<section class="card pad">
    <h2>Подзадачи</h2>
    ${selected.subtasks.map((task, index) => `<label class="swrow"><span>${esc(task.text)}</span>
      <input type="checkbox" data-sprint-task="${index}" data-id="${selected.id}"${task.done ? ' checked' : ''}></label>`).join('')}
  </section>` : ''}
  ${drafts.length ? `<section class="card pad">
    <h2>Черновики</h2>
    ${drafts.map((item) => `<div class="sprint-row"><b>${esc(item.title)}</b>
      <div class="inl">${sprintButton('start', item.id, 'Начать', false, !!current)}
      ${sprintButton('edit_form', item.id, 'Редактировать')}${sprintButton('save_template', item.id, 'В шаблоны')}
      ${sprintButton('cancel', item.id, 'Отменить')}</div></div>`).join('')}
  </section>` : ''}
  <section class="card pad">
    <h2>Шаблоны</h2>
    ${selected ? `<div class="inl sprint-actions">${sprintButton('save_template', selected.id, 'Сохранить текущий спринт')}</div>` : ''}
    ${S.sprints.templates.map((item) => `<div class="sprint-row"><b>${esc(item.name)}</b>
      <div class="inl">${sprintButton('use_template', item.id, 'Создать черновик')}${sprintButton('delete_template', item.id, 'Удалить')}</div></div>`).join('')
      || '<p class="note">Шаблон можно сохранить из спринта или черновика.</p>'}
  </section>`;
}

function sprintHistory(history) {
  return `<section class="card pad">
    ${history.map((item) => `<details class="sprint-history" data-sprint-report="${item.id}"${S.sprintReport === item.id ? ' open' : ''}>
      <summary>${esc(item.title)} <span class="note">· ${SPRINT_STATES[item.state]}</span></summary>
      ${sprintReport(item)}
      <div class="inl sprint-actions">${sprintButton('reuse', item.id, 'Повторить')}
        ${sprintButton('save_template', item.id, 'В шаблоны')}</div></details>`).join('')
      || '<p class="note">Пока нет завершённых спринтов.</p>'}
  </section>`;
}

async function loadSprintLibrary() {
  if (!S.status?.live) return;
  try {
    const pid = S.status.pid;
    const data = await api('/api/sprints');
    if (S.sprintPid !== pid || !S.sprints || data.revision >= S.sprints.revision) {
      S.sprints = data;
      S.sprintPid = pid;
    }
    const library = $('[data-sprint-library]');
    if (library) {
      const open = $$('[data-sprint-report][open]', library).map((el) => el.dataset.sprintReport);
      const drafts = $$('[data-sprint-outcome]', library).map((form) => [form.dataset.sprintOutcome, form.elements.actual_outcome]);
      const changed = drafts.filter(([, input]) => input.value !== input.defaultValue);
      if (setHtml(library, sprintLibrary())) {
        open.forEach((id) => { const detail = $(`[data-sprint-report="${id}"]`, library); if (detail) detail.open = true; });
        changed.forEach(([id, input]) => {
          const form = $(`[data-sprint-outcome="${id}"]`, library);
          if (form) form.elements.actual_outcome.value = input.value;
        });
      }
    }
  } catch (error) { toast(error.message, { bad: true }); }
}

PAGES.current = {
  narrow: true,
  async load() { await loadSprintLibrary(); },
  render() {
    return `<header class="ph"><h1>Текущий спринт</h1></header>
      <section class="card pad" data-sprint-live>${sprintLive(S.status, true)}</section>
      <section class="card pad"><a href="#/history">История спринтов</a></section>
      ${S.status?.live ? `<div data-sprint-editor>${sprintForm()}</div><div data-sprint-library>${sprintLibrary()}</div>` : ''}`;
  },
};

PAGES.history = {
  narrow: true,
  async load() { await loadSprintLibrary(); },
  render() {
    return `<header class="ph"><h1>История спринтов</h1></header>
      <div data-sprint-library>${sprintLibrary()}</div>`;
  },
};

function updateSprintEditor(focus = false) {
  const editor = $('[data-sprint-editor]');
  if (!editor) return;
  editor.innerHTML = sprintForm();
  if (focus) editor.querySelector('[name="goal"]').focus();
}

async function changeSprint(body, apply) {
  if (S.busy.sprint) return;
  S.busy.sprint = true;
  try {
    S.sprints = await api('/api/sprints', { method: 'POST', body });
    S.sprintPid = S.status?.pid;
    await apply?.(S.sprints.item);
    setStatus(await api('/api/status'));
    await poll();
    await loadSprintLibrary();
  } catch (error) {
    toast(error.message, { bad: true });
    await loadSprintLibrary();
  } finally {
    S.busy.sprint = false;
  }
}

PAGES.startup = {
  narrow: true,
  load: async () => { S.startup = await api('/api/startup'); },
  render() {
    const task = S.startup;
    const flag = (label, key) => audioField(label, key, (id) => sel(key, [[false, 'Выкл.'], [true, 'Вкл.']], { id, t: 'bool' }));
    return `<header class="ph"><h1>Автозапуск</h1>${saveBar()}</header>
      <section class="card pad" aria-labelledby="startup-task-heading">
        <div class="inl" style="justify-content:space-between"><div><h2 id="startup-task-heading">Запуск при входе в Windows</h2>
        <div class="note">${!task ? 'Проверяю Планировщик заданий…' : !task.supported ? 'Доступно в Windows.' : task.enabled ? 'Включён в Планировщике заданий.' : task.legacy ? 'Обнаружен прежний ярлык автозапуска.' : 'Выключен.'}</div></div>
        ${task?.supported ? `<div class="inl"><button class="btn${task.enabled ? '' : ' dark'}" type="button" data-act="startuptask" data-enabled="${!task.enabled}"${S.busy.startupTask ? ' disabled' : ''}>${task.enabled ? 'Выключить' : task.legacy ? 'Перенести в Планировщик' : 'Включить'}</button>
        ${task.legacy && !task.enabled ? '<button class="btn" type="button" data-act="startuptask" data-enabled="false">Выключить прежний запуск</button>' : ''}</div>` : ''}</div>
      </section>
      <section class="card" aria-labelledby="startup-prefs-heading">
        <div class="card-top"><div><h2 id="startup-prefs-heading">При следующем запуске</h2><div class="note">Настройки считываются при запуске диспетчера.</div></div></div>
        ${flag('Показывать HUD', 'focus_hud_enabled')}
        ${flag('Статус в Discord', 'discord_presence')}
        <div class="card pad inl"><button class="btn" data-act="restart">Перезапустить</button>
        <button class="btn" data-act="stop">Выйти</button></div>
        ${flag('Открывать панель в браузере при входе', 'startup_open_dashboard')}
        ${flag('Запускать свёрнутым в трей', 'startup_start_minimized')}
        ${audioField('Задержка после входа, с', 'startup_delay_seconds', (id) => num('startup_delay_seconds', { id, min: 0, max: 180 }))}
        ${flag('Восстанавливать после сбоя', 'startup_recover')}
      </section>`;
  },
};

function focusStatus(st) {
  const f = st?.focus;
  if (!f) return '<div class="note">Состояние появится после запуска трекера.</div>';
  const level = Object.hasOwn(FOCUS_LEVELS, f.level) ? f.level : 'NEUTRAL';
  return `<div class="focus-status" data-level="${level}">
    <div class="focus-top"><div><h2>${f.meter_enabled ? "Отвлечение" : "Активность"}</h2><div class="note">${esc(FOCUS_STATES[f.activity] || f.activity)}${f.meter_enabled ? " · " + FOCUS_LEVELS[level] : ""}</div></div>${f.meter_enabled ? `<strong>${f.score}%</strong>` : ""}</div>
    ${f.meter_enabled ? `<progress max="100" value="${f.score}" aria-label="Уровень отвлечения"></progress>` : ""}
    ${f.task ? `<div class="focus-task">${esc(f.task)}</div>` : ''}
    ${f.reason ? `<div class="note">${esc(f.reason)}</div>` : ''}
    ${f.block_remaining ? `<div class="note">Ограничения действуют ещё ${Math.ceil(f.block_remaining)} с</div>` : f.armed ? '<div class="note">Ограничения включены для этого спринта.</div>' : ''}
    <div class="inl focus-actions">
      ${f.hud_available ? `<button class="btn" type="button" data-ctl="focus_hud">${f.hud ? 'Скрыть HUD' : 'Показать HUD'}</button>` : ""}
      ${f.hud_available && f.meter_enabled ? `<button class="btn" type="button" data-ctl="${f.silenced ? 'focus_enable' : 'focus_disable'}">${f.silenced ? 'Возобновить вмешательства' : 'Отключить вмешательства'}</button>` : ""}
      ${f.armed ? '<button class="btn" type="button" data-ctl="focus_release">Снять ограничения</button>' : f.block_available && !f.silenced && st.sprint?.state === 'ACTIVE' ? '<button class="btn" type="button" data-ctl="focus_arm">Разрешить ограничения на этот спринт</button>' : ''}
      ${f.hotkey ? '<span class="note">Отключить · <kbd>Ctrl</kbd> + <kbd>Shift</kbd> + <kbd>F9</kbd></span>' : ''}
    </div>
  </div>`;
}


function activityStatus(st) {
  const f = st?.focus;
  if (!f) return '<p class="note">Запусти FocusWithAI для просмотра активности.</p>';
  const src = f.sources;
  if (!src) return '<p class="note">Обнови backend, чтобы получить новый экран мониторинга.</p>';
  const browser = src.browser;
  const media = src.media;
  const live = st.sprint?.state === 'ACTIVE';
  return `<div class="activity-grid">
    <div><span class="note">Спринт</span><b>${live ? esc(st.sprint.title) : 'Ожидание'}</b></div>
    <div><span class="note">Активность</span><b>${esc(f.application || '—')} · ${fmtMS(f.activity_seconds)}</b></div>
    <div><span class="note">Рабочее окно</span><b>${esc(f.window || browser?.title || '—')}</b></div>
    <div><span class="note">Вкладка</span><b>${browser ? esc(browser.domain) + ' · ' + fmtMS(browser.seconds) : '—'}</b></div>
    <div><span class="note">Сеанс воспроизведения</span><b>${media ? esc([media.title, media.artist, media.player, media.state].filter(Boolean).join(' · ')) : '—'}</b></div>
    <div><span class="note">Открытые приложения</span><b>${esc(src.apps.join(', ') || '—')}</b></div>
  </div>
  <div class="inl sprint-actions">
    ${src.armed ? '<button class="btn dark" type="button" data-ctl="focus_capture_stop">Остановить захват</button>'
    : live && src.enabled && src.available ?
      '<button class="btn" type="button" data-act="capturechoices">Выбрать рабочее окно</button>' : ''}
    ${live ? '<button class="btn" type="button" data-ctl="pause">Пауза мониторинга</button>' : ''}
  </div>
  <p class="note">${src.armed ? src.capturing ? 'Захват рабочего окна включён. Ctrl+Shift+F9 останавливает его.' :
    'Захват ожидает выбранное рабочее окно на переднем плане.' :
    'Захват выключен. Кадры остаются в памяти приложения.'}</p>
  ${src.capture_error || src.media_error ? `<p class="note">Источник данных недоступен · ${esc(src.capture_error || src.media_error)}</p>` : ''}`;
}

function contextField(label, key, options) {
  return audioField(label, key, (id) => sel(key, options, { id, t: typeof options[0][0] === 'boolean' ? 'bool' : undefined }));
}

function contextFlag(label, key) {
  return contextField(label, key, [[false, 'Выкл.'], [true, 'Вкл.']]);
}

function contextNumber(label, key, min, max, step = 1) {
  return audioField(label, key, (id) => num(key, { id, min, max, step, int: step === 1 }));
}

PAGES.activity = {
  narrow: true,
  render() {
    return `<header class="ph"><h1>Монитор активности</h1>${saveBar()}</header>
      <section class="card pad" data-activity-live>${activityStatus(S.status)}</section>
      <section class="card pad" data-capture-choices hidden></section>
      <section class="card">
        <div class="card-top"><h2>Источники контекста</h2></div>
        ${contextFlag('Разрешить ручной захват окна', 'capture_enabled')}
        ${contextField('Режим захвата', 'capture_mode', [['event', 'По изменению кадра'], ['periodic', 'Периодический']])}
        ${contextNumber('Интервал захвата, с', 'capture_interval', 3, 120)}
        ${contextNumber('Максимальная ширина, px', 'capture_width', 160, 1600)}
        ${contextNumber('Максимальная высота, px', 'capture_height', 160, 1600)}
        ${contextNumber('Качество JPEG', 'capture_quality', 35, 95)}
        ${row('Рабочие приложения для захвата', 'Укажи полный путь к EXE. Разрешение применяется после перезапуска.', 'capture_apps',
          lw('capture_apps', 'rows', { name: 'Путь к EXE', add: 'Добавить приложение' }))}
        ${row('Исключённые приложения', 'Имя EXE или полный путь', 'activity_excluded_apps',
          lw('activity_excluded_apps', 'rows', { name: 'Приложение', add: 'Исключить приложение' }))}
        ${row('Исключённые окна', 'Любой фрагмент заголовка', 'activity_excluded_windows',
          lw('activity_excluded_windows', 'rows', { name: 'Заголовок', add: 'Исключить окно' }))}
        ${row('Рабочие домены браузера', 'Для каждой вкладки включай передачу кнопкой расширения FocusWithAI.', 'browser_context_domains',
          lw('browser_context_domains', 'rows', { name: 'Домен', add: 'Добавить домен' }))}
        ${contextFlag('Метаданные медиасеанса Windows', 'media_context_enabled')}
      </section>`;
  },
};

PAGES.diagnostics = {
  narrow: true,
  after() { renderLogs(true); },
  render() {
    return `<header class="ph"><h1>Диагностика</h1>${saveBar()}</header>
      <section class="card pad" data-diagnostics-live>${liveStatus(S.status)}</section>
      <section class="card pad">
        <div class="inl"><h2>Данные для модели</h2>
        <button class="btn" type="button" data-act="contextpreview">Посмотреть контекст</button></div>
        <pre class="context-preview" data-context-preview hidden></pre>
        <img class="context-image" data-context-image alt="Кадр анализа рабочего окна" hidden>
      </section>
      <section class="card pad">
        <div class="inl"><h2>Журнал</h2><button class="btn" data-act="logpause">${S.logPaused ? 'Продолжить журнал' : 'Приостановить журнал'}</button>
        <button class="btn" data-act="logcopy">Копировать</button></div>
        ${contextNumber('Уровень логирования', 'debug_level', 0, 3)}
        <div class="logbox"><pre id="logpre" tabindex="0" aria-label="Журнал"></pre></div>
      </section>`;
  },
};

function liveStatus(st) {
  const live = st?.focus?.live_engine;
  if (!live) return '<p>Статус появится после запуска трекера.</p>';
  if (!live.connection_state) return '<p>Перезапусти FocusWithAI после спринта, чтобы применить обновление.</p>';
  const fields = [['Соединение', live.connection_state], ['Анализ', live.analysis_state], ['Аудио', live.audio_state],
    ['Модель', live.model], ['Время соединения', fmtMS(live.connected_seconds)],
    ['События', live.events], ['Кадры', live.frames], ['Задержка, мс', live.latency_ms ?? '—'],
    ['Классификация', live.result?.classification || (live.block_reason && st.sprint?.state === 'ACTIVE' ? 'UNCERTAIN' : '—')], ['Комментарий', live.comment || '—'],
    ['Буфер PCM, байт', live.buffer_bytes], ['Прервано при переполнении', live.audio_dropped], ['Переподключения', live.reconnects],
    ['Токены', live.tokens ?? '—'], ['Ошибка', live.error || live.audio_error || '—']];
  const result = live.result;
  return `<div class="activity-grid">${fields.map(([label, value]) => `<div><span class="note">${label}</span><b>${esc(value)}</b></div>`).join('')}</div>
    ${live.block_reason && st.focus.reason ? `<p class="note">${esc(st.focus.reason)}</p>` : ''}
    ${result ? `<p>${esc(result.observed_activity)} · ${Math.round(result.confidence * 100)}%</p><p>${esc(result.reason)}</p>
    ${st.focus.suggested_next_step ? `<p>Следующий шаг · ${esc(st.focus.suggested_next_step)}</p>` : ''}
    <button class="btn" data-ctl="focus_correct">Отметить ошибку классификации</button>` : ''}`;
}

const LIVE_CHECKS = {connection: 'Подключение', classification: 'Классификация', audio: 'Нативное аудио'};

function liveProbeStatus(probe) {
  if (!probe?.stages) return '';
  const statuses = {ok: 'Успешно', error: 'Ошибка', skipped: 'Пропущено'};
  return `<div class="activity-grid">${Object.entries(LIVE_CHECKS).map(([stage, label]) => {
    const result = probe.stages[stage];
    return `<div><span class="note">${label}</span><b>${statuses[result.status]}</b>
      ${result.detail ? `<span>${esc(result.detail)}</span>` : ''}</div>`;
  }).join('')}</div>`;
}

PAGES.live = {
  narrow: true,
  render() {
    return `<header class="ph"><h1>Gemini Live</h1>${saveBar()}</header>
      <section class="card pad" data-live-session>${liveStatus(S.status)}</section>
      <section class="card">
        ${contextField('Модель', 'gemini_live_model', Object.entries(S.models))}
        ${contextField('Голос', 'gemini_live_voice', Object.entries(S.voices))}
        ${row('API-ключ Gemini', '', 'google_assistant_api_key', txt('google_assistant_api_key', {secret: true, label: 'API-ключ Gemini'}))}
        ${contextFlag('Передавать цель и текстовый контекст активности в Google', 'activity_allow_remote')}
        ${contextFlag('Передавать кадры выбранного окна в Google', 'activity_send_remote_images')}
        ${contextNumber('Интервал анализа, с', 'live_analysis_seconds', 2, 120)}
        ${contextNumber('Громкость Live Audio', 'live_volume', 0, 1, .05)}
        ${contextFlag('Инициативные комментарии', 'live_proactive')}
        ${row('Системный промпт для манеры речи', '', 'gemini_live_prompt', area('gemini_live_prompt', 4))}
        ${row('Приложения в контексте модели', 'Имена разрешённых EXE, например Code.exe.', 'context_allowed_apps',
          lw('context_allowed_apps', 'rows', {name:'Имя EXE', add:'Добавить'}))}
      </section>
      <section class="card pad">
        <h2>Проверка Gemini Live</h2>
        <div class="inl">${Object.entries(LIVE_CHECKS).map(([stage, label]) =>
          `<button class="btn" data-act="testgeminilive" data-through="${stage}" ${S.busy.gemini || !S.status?.focus?.live_engine?.connection_state ? 'disabled' : ''}>${label}</button>`).join('')}</div>
        ${S.busy.gemini ? '<p role="status">Проверяю…</p>' : liveProbeStatus(S.geminiProbe)}
        <p class="note">Классификация и аудио проверяются на синтетическом примере. Аудио проверяется без воспроизведения.</p>
      </section>
      <section class="card pad inl">
        <button class="btn" data-ctl="focus_commentary" data-value="NEXT_STEP">Предложить следующий шаг</button>
        <button class="btn" data-ctl="focus_commentary" data-value="SUMMARY">Подвести итоги</button>
        <button class="btn" data-act="stopaudio">Остановить звук</button>
      </section>`;
  },
};

PAGES.intervention = {
  narrow: true,
  render() {
    const flag = contextFlag;
    const number = contextNumber;
    return `<header class="ph"><h1>Вмешательства</h1>${saveBar()}</header>
      <section class="card pad" data-focus-live>${focusStatus(S.status)}</section>
      <section class="card">
        <div class="card-top"><h2>Подтверждение отвлечения</h2></div>
        ${number('Минимальная уверенность, от 0,50 до 0,99', 'activity_min_confidence', .50, .99, .01)}
        ${number('Минимальное время подтверждения, с', 'activity_confirmation_seconds', 2, 60)}
      </section>
      <section class="card" aria-labelledby="focus-intervention-heading" style="padding-bottom:20px">
        <div class="card-top"><h2 id="focus-intervention-heading">Критический уровень</h2></div>
        ${flag('Активные вмешательства', 'intervention_enabled')}
        ${flag('Воспроизвести эффект', 'intervention_alarm')}
        ${flag('Воспроизвести реплику анноунсера', 'intervention_announcer')}
        ${flag('Показать предупреждение на экране', 'intervention_visual')}
        ${flag('Показать диалог возвращения к задаче', 'intervention_dialog')}
        ${flag('Предложить паузу и смену задачи в диалоге', 'intervention_suggest_break')}
        ${flag('Разрешить временные ограничения', 'intervention_allow_blocking')}
        ${number('Длительность ограничения, с', 'intervention_block_seconds', 5, 120)}
        ${row('Приложения', 'Полные пути к EXE. Ограничение сворачивает совпавшее активное окно.', 'intervention_apps', lw('intervention_apps', 'rows', {name:'Путь к EXE', add:'Добавить приложение'}))}
        ${row('Сайты', 'Домены для Chrome и Edge. Ограничение срабатывает на открытой вкладке.', 'intervention_sites', lw('intervention_sites', 'rows', {name:'Домен', add:'Добавить сайт'}))}
        <div class="note" style="margin-top:12px">Разрешай ограничения отдельно на каждый активный спринт через «Текущий спринт» или в этом разделе.</div>
      </section>`;
  },
};

PAGES.focus = {
  narrow: true,
  render() {
    const flag = (label, key) => audioField(label, key, (id) => sel(key, [[false, 'Выкл.'], [true, 'Вкл.']], { id, t: 'bool' }));
    const number = (label, key, min, max, step = 1) => audioField(label, key, (id) => num(key, { id, min, max, step, int: step === 1 }));
    return `<header class="ph"><h1>HUD и фокус</h1>${saveBar()}</header>
      <section class="card pad" data-focus-live>${focusStatus(S.status)}</section>
      <section class="card" aria-labelledby="focus-meter-heading">
        <div class="card-top"><h2 id="focus-meter-heading">Шкала отвлечения</h2></div>
        ${flag('Шкала и вмешательства', 'focus_meter_enabled')}
        ${number('Рост при отвлечении, п.п./с', 'focus_gain_per_second', 0, 20, .1)}
        ${number('Восстановление в работе, п.п./с', 'focus_recovery_per_second', 0, 20, .1)}
        ${row('Пороги внимания, предупреждения и критического уровня', 'Три числа по возрастанию', 'focus_thresholds', jsonF('focus_thresholds'))}
        ${number('Гистерезис, п.п.', 'focus_hysteresis', 0, 20, .5)}
        ${number('Интервал повторного уведомления уровня, с', 'focus_cooldown_seconds', 15, 300)}
        ${flag('Звук при смене уровня', 'focus_audio')}
      </section>
      <section class="card" aria-labelledby="focus-hud-heading">
        <div class="card-top"><h2 id="focus-hud-heading">Focus HUD</h2></div>
        ${flag('Показывать при запуске', 'focus_hud_enabled')}
        ${audioField('Угол активного экрана', 'focus_hud_corner', (id) => sel('focus_hud_corner', [['top_left','Сверху слева'], ['top_right','Сверху справа'], ['bottom_left','Снизу слева'], ['bottom_right','Снизу справа']], { id }))}
        ${number('Ширина, px', 'focus_hud_width', 210, 620)}
        ${number('Высота, px', 'focus_hud_height', 88, 240)}
        ${number('Непрозрачность от 0,25 до 1', 'focus_hud_opacity', .25, 1, .05)}
        ${flag('Пропускать клики сквозь HUD', 'focus_hud_click_through')}
        ${flag('Показывать название спринта', 'focus_hud_show_task')}
        ${flag('Показывать таймер спринта', 'focus_hud_show_sprint_time')}
        ${flag('Показывать состояние спринта', 'focus_hud_show_sprint_state')}
        ${flag('Показывать шкалу отвлечения', 'focus_hud_show_distraction')}
      </section>
`;
  },
};

const PACK_EVENTS = {
  SESSION_START: 'Начало сессии', DISTRACTION: 'Отвлечение', PERSISTENT_DISTRACTION: 'Долгое отвлечение',
  REPEATED_DISTRACTION: 'Повторное отвлечение', RETURN_TO_WORK: 'Возвращение к работе',
  TASK_COMPLETED: 'Задача завершена', SESSION_END: 'Конец сессии', IDLE: 'Бездействие', NONE: 'Связано с задачей',
  ATTENTION: 'Внимание', FOCUS_MODE_ENABLED: 'Возобновление фокуса',
  FOCUS_WARNING: 'Предупреждение фокуса', FOCUS_CRITICAL: 'Критический уровень фокуса',
};
const AUDIO_MODES = {
  alarm: 'Только сигналы', voicepack: 'Только голосовой пакет', live: 'Только Gemini Live',
  alarm_voicepack: 'Сигнал + голосовой пакет', alarm_live: 'Сигнал + Gemini Live',
};

function audioField(label, key, renderControl) {
  const id = nid();
  return row(label, '', key, renderControl(id), id);
}

function audioPackSettings(kind) {
  const alarm = kind === 'alarm';
  const packKey = alarm ? 'alarm_pack_id' : 'voicepack_id';
  const volumeKey = alarm ? 'alarm_volume' : 'voicepack_volume';
  const selected = eff(packKey);
  const pack = S.audioPacks[kind].find((p) => p.id === selected);
  const choices = [['', 'Выбрать пакет'], ...S.audioPacks[kind].map((p) => [p.id, p.name])];
  if (selected && !pack) choices.push([selected, `${selected} (не найден)`]);
  const event = S.audioEvents[kind] || S.audioPacks.events[0];
  const files = Object.entries(pack?.clips || {}).flatMap(([group, names]) =>
    names.map((name) => `${group.toLowerCase()}/${name}`));
  const key = `audio_event_mappings.${kind}.${selected}.${event}`;
  const mapping = pack ? eff(key) : '';
  const chosen = files.find((path) => path === S.audioFiles[kind]) || files.find((path) => path === mapping)
    || files.find((path) => path.startsWith(`${event.toLowerCase()}/`));
  return `<section class="card" aria-labelledby="audio-${kind}" style="padding-bottom: 20px">
    <div class="card-top"><h2 id="audio-${kind}">${alarm ? 'Звуковые эффекты' : 'Голосовые реплики'}</h2></div>
    ${audioField(alarm ? 'Alarm Pack' : 'Voice Pack', packKey, (id) => sel(packKey, choices, { id, w: 340 }))}
    ${audioField('Громкость от 0 до 1', volumeKey, (id) => num(volumeKey, { id, min: 0, max: 1, int: false, step: .05 }))}
    ${pack && event ? `<div style="display: grid; gap: 10px; margin: 14px 0 0">
      <div class="inl"><select class="field" data-audio-event="${kind}" aria-label="Событие ${alarm ? 'эффекта' : 'реплики'}">${S.audioPacks.events.map((value) => `<option value="${esc(value)}"${value === event ? ' selected' : ''}>${esc(PACK_EVENTS[value] || value)}</option>`).join('')}</select>
      <button class="btn" type="button" data-act="chooseaudio" data-kind="${kind}"${S.busy.importAudio ? ' disabled' : ''}>Импорт файлов</button>
      <input type="file" data-import-audio="${kind}" data-pack="${esc(selected)}" data-event="${esc(event)}" accept=".wav,.mp3,.ogg" multiple hidden></div>
      <div class="note">Назначение · ${esc(mapping || 'случайный файл из папки события')}</div>
      ${files.length ? `<div class="inl">
        <select class="field" data-audio-file="${kind}" aria-label="Файл ${alarm ? 'эффекта' : 'реплики'}" style="max-width: 420px">${files.map((path) => `<option value="${esc(path)}"${path === chosen ? ' selected' : ''}>${esc(path)}</option>`).join('')}</select>
        <button class="btn" type="button" data-act="previewaudio" data-kind="${kind}" data-pack="${esc(selected)}"${S.busy.previewAudio ? ' disabled' : ''}>${I.play}Прослушать</button>
        <button class="btn" type="button" data-act="mapaudio" data-kind="${kind}" data-map="${esc(key)}">Назначить выбранный</button>
      </div>` : '<div class="note">Добавь WAV, MP3 или OGG для этого пакета.</div>'}
      ${mapping ? `<div><button class="btn" type="button" data-act="resetaudio" data-map="${esc(key)}">Сбросить назначение</button></div>` : ''}
    </div>` : ''}
  </section>`;
}

function audioSettings() {
  const disabled = eff('notification_disabled_events') || [];
  return `<section class="card" aria-labelledby="audio-settings" style="padding-bottom: 20px">
    <div class="card-top"><h2 id="audio-settings">Уведомления</h2><div class="inl">
      <button class="btn" type="button" data-act="reloadpacks"${S.busy.packList ? ' disabled' : ''}>Обновить пакеты</button>
      <label class="togl"><input class="tog" type="checkbox" data-ctltog="sound"${S.status?.toggles?.sound ? ' checked' : ''}>Звук</label>
      <button class="btn" type="button" data-act="stopaudio">Остановить звук</button></div></div>
    ${audioField('Режим', 'notification_mode', (id) => sel('notification_mode', Object.entries(AUDIO_MODES), { id, w: 330 }))}
    ${audioField('Пауза между эффектом и голосом, мс', 'alarm_voice_delay_ms', (id) => num('alarm_voice_delay_ms', { id, min: 0, max: 10000 }))}
    ${audioField('Интервал между уведомлениями, с', 'voicepack_min_interval', (id) => num('voicepack_min_interval', { id, min: 5 }))}
    ${audioField('Интервал повторных предупреждений, с', 'voicepack_repeat_interval', (id) => num('voicepack_repeat_interval', { id, min: 5 }))}
    ${audioField('Повторов после долгого предупреждения', 'voicepack_max_repeats', (id) => num('voicepack_max_repeats', { id, min: 0, max: 20 }))}
    ${audioField('Порог бездействия, с', 'voicepack_idle_seconds', (id) => num('voicepack_idle_seconds', { id, min: 5 }))}
    ${det('audio-events', 'Активные события', `<div class="inl" style="gap: 12px; padding-bottom: 14px">${S.audioPacks.events.map((event) => `<label class="togl"><input class="tog" type="checkbox" data-notification-event="${esc(event)}"${disabled.includes(event) ? '' : ' checked'}>${esc(PACK_EVENTS[event] || event)}</label>`).join('')}</div>`, { cls: 'sub' })}
  </section>`;
}

PAGES.voice = {
  narrow: true,
  async load() { S.audioPacks = await api('/api/audio/packs'); },
  render() {
    return `<header class="ph"><h1>Сигналы и голосовые пакеты</h1>${saveBar()}</header>
      ${audioSettings()}${audioPackSettings('alarm')}${audioPackSettings('voice')}`;
  },
};

function renderLogs(scroll) {
  const pre = $('#logpre');
  if (!pre) return;
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
  pre.innerHTML = S.logs.length ? S.logs.map((l) => `<span class="note">${new Date(l.ts * 1000).toLocaleTimeString('ru-RU')}</span>  <span class="lv-${l.level}">${esc(l.text)}</span>`).join('\n') : '<span class="note">Пока пусто</span>';
  if (scroll || atBottom) pre.scrollTop = pre.scrollHeight;
}

async function pollLogs() {
  if (S.route !== 'diagnostics' || S.logPaused) return;
  try {
    const r = await api(`/api/logs?after=${S.logSeq}`);
    if (r.lines.length) {
      S.logs.push(...r.lines);
      S.logs = S.logs.slice(-500);
      S.logSeq = r.lines[r.lines.length - 1].seq;
      renderLogs(false);
    }
  } catch (e) { /* панель недоступна */ }
}

let toastTimer = null;
function toast(msg, o = {}) {
  const el = $('#toast');
  el.className = `toast${o.bad ? ' bad' : ''}`;
  el.innerHTML = `<span>${esc(msg)}</span>${o.action ? '<button class="btn" type="button" id="toastact"></button>' : ''}<button class="x" type="button" id="toastx" aria-label="Закрыть">${I.x}</button>`;
  if (o.action) { const b = $('#toastact'); b.textContent = o.action[0]; b.onclick = () => { el.hidden = true; o.action[1](); }; }
  $('#toastx').onclick = () => { el.hidden = true; };
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, o.action ? 12000 : o.bad ? 8000 : 4000);
}

async function restart() {
  if (dirtyCount() && !confirm('Есть несохранённые изменения. Перезапустить без них?')) return;
  const before = S.status && S.status.started_at;
  try { await api('/api/process', { method: 'POST', body: { action: 'restart' } }); } catch (e) { toast(e.message, { bad: true }); return; }
  toast('Перезапускаю скрипт…');
  const started = Date.now();
  const wait = async () => {
    try {
      const st = await api('/api/status');
      if (st.live && st.started_at !== before) {
        setStatus(st);
        loadCfg(await api('/api/config'));
        render();
        toast('Скрипт перезапущен, настройки применены');
        return;
      }
    } catch (e) { /* ещё поднимается */ }
    if (Date.now() - started < 90000) setTimeout(wait, 1000);
    else toast('Скрипт не поднялся за 90 секунд, посмотри окно консоли', { bad: true });
  };
  setTimeout(wait, 1500);
}

async function control(action, value) {
  try {
    await api('/api/control', { method: 'POST', body: { action, value } });
    if (action === 'task_completed') toast('Задача отмечена завершённой');
    await poll();
  } catch (e) { toast(e.message, { bad: true }); }
}

async function busy(key, fn) {
  S.busy[key] = true; render();
  try { await fn(); } catch (e) { toast(e.message, { bad: true }); }
  S.busy[key] = false; render();
}

const ACTIONS = {
  save: saveAll,
  async capturechoices() {
    try {
      const { windows } = await api('/api/activity/windows');
      const list = $('[data-capture-choices]');
      if (!list) return;
      list.hidden = false;
      list.innerHTML = windows.length ? windows.map(w =>
        `<button class="btn" type="button" data-ctl="focus_capture_start" data-value="${esc(w.id)}">${esc(w.title)}</button>`).join(' ')
        : 'Нет открытых окон из списка разрешённых EXE.';
    } catch (error) { toast(error.message, { bad: true }); }
  },
  async contextpreview() {
    try {
      const { preview } = await api('/api/activity/preview', { method: 'POST', body: {} });
      const pre = $('[data-context-preview]');
      if (!pre) return;
      const image = $('[data-context-image]');
      const { image_preview: frame, ...data } = preview || {};
      image.hidden = !frame;
      if (frame) image.src = frame;
      else image.removeAttribute('src');
      pre.hidden = false;
      pre.textContent = preview ? JSON.stringify(data, null, 2) : 'Доступного контекста пока нет.';
    } catch (error) { toast(error.message, { bad: true }); }
  },
  revert: revertAll,
  restart,
  async sprint(el) {
    const { cmd, id } = el.dataset;
    if (cmd === 'edit_form') {
      S.sprintEdit = S.sprints?.sprints.find((item) => item.id === id);
      S.sprintForm = null;
      updateSprintEditor(true);
      return;
    }
    if (cmd === 'close_edit') {
      S.sprintEdit = S.sprintForm = null;
      updateSprintEditor();
      return;
    }
    const body = { action: cmd, id };
    if (cmd === 'cancel' && !confirm('Отменить этот спринт? Он останется в истории.')) return;
    if (cmd === 'delete_template' && !confirm('Удалить шаблон спринта?')) return;
    if (cmd === 'save_template') {
      const item = S.sprints.sprints.find((sprint) => sprint.id === id);
      const name = prompt('Название шаблона', item?.title || '');
      if (name === null) return;
      body.name = name;
    }
    await changeSprint(body, (item) => {
      if (cmd === 'complete') {
        S.sprintReport = id;
        location.hash = '#/history';
      }
      if (cmd === 'reuse' || cmd === 'use_template') {
        S.sprintEdit = item;
        S.sprintForm = null;
        if (S.route === 'history') location.hash = '#/current';
        updateSprintEditor(true);
      } else if (S.sprintEdit?.id === id && ['complete', 'cancel', 'start'].includes(cmd)) {
        S.sprintEdit = S.sprintForm = null;
        updateSprintEditor();
      }
    });
    if (cmd === 'complete' && S.sprintReport === id) {
      const outcome = $('[data-sprint-outcome="' + id + '"] textarea');
      if (outcome) {
        outcome.closest('details').open = true;
        outcome.focus();
      }
    }
  },
  startuptask: (el) => busy('startupTask', async () => {
    S.startup = await api('/api/startup', { method: 'POST', body: { enabled: el.dataset.enabled === 'true' } });
    toast(S.startup.enabled ? 'Автозапуск включён' : 'Автозапуск выключен');
  }),
  async stop() {
    if (!confirm('Остановить трекер? Панель закроется вместе с ним.')) return;
    try { await api('/api/process', { method: 'POST', body: { action: 'stop' } }); toast('Трекер останавливается'); } catch (e) { toast(e.message, { bad: true }); }
  },
  testgeminilive: (el) => busy('gemini', async () => {
    S.geminiProbe = null;
    const r = await api('/api/test/gemini-live', {method: 'POST', body: {through: el.dataset.through, draft: {
      google_assistant_api_key: eff('google_assistant_api_key'), activity_allow_remote: eff('activity_allow_remote'),
      gemini_live_model: eff('gemini_live_model'), gemini_live_voice: eff('gemini_live_voice')
    }}});
    S.geminiProbe = r;
  }),
  reloadpacks: () => busy('packList', async () => { S.audioPacks = await api('/api/audio/packs'); }),
  chooseaudio: (el) => $(`[data-import-audio="${el.dataset.kind}"]`)?.click(),
  async stopaudio() {
    try { await api('/api/audio/stop', { method: 'POST', body: {} }); } catch (e) { toast(e.message, { bad: true }); }
  },
  previewaudio: (el) => {
    const { kind, pack } = el.dataset;
    const path = $(`[data-audio-file="${kind}"]`)?.value;
    if (!path) return;
    const [category, file] = path.split('/');
    S.audioFiles[kind] = path;
    return busy('previewAudio', async () => {
      await api('/api/audio/preview', { method: 'POST', body: {
        kind, pack, event: category.toUpperCase(), file, volume: eff(kind === 'alarm' ? 'alarm_volume' : 'voicepack_volume'),
      } });
    });
  },
  mapaudio: (el) => {
    const { kind, map } = el.dataset;
    const selected = $(`[data-audio-file="${kind}"]`)?.value;
    if (selected) { S.audioFiles[kind] = selected; setv(map, selected); render(); }
  },
  resetaudio: (el) => { setv(el.dataset.map, undefined); render(); },
  logpause() { S.logPaused = !S.logPaused; render(); },
  async logcopy() {
    try { await navigator.clipboard.writeText(S.logs.map((l) => l.text).join('\n')); toast('Логи скопированы'); } catch (e) { toast('Не удалось скопировать', { bad: true }); }
  },
};

document.addEventListener('submit', async (event) => {
  const form = event.target;
  if (!form.matches('[data-sprint-form], [data-sprint-outcome]')) return;
  event.preventDefault();
  const values = Object.fromEntries(new FormData(form));
  if (form.dataset.sprintOutcome) {
    await changeSprint({ ...values, action: 'outcome', id: form.dataset.sprintOutcome }, () => {
      const input = form.elements.actual_outcome;
      input.defaultValue = input.value.trim();
      toast('Результат подтверждён');
    });
    return;
  }
  const action = event.submitter?.value || (S.sprintEdit ? 'edit' : 'quick_start');
  const id = S.sprintEdit?.id;
  values.subtasks = values.subtasks.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  if ('duration_minutes' in values) values.duration_minutes = values.duration_minutes ? +values.duration_minutes : null;
  await changeSprint({ ...values, id, action: action === 'save_start' ? 'edit_start' : action }, () => {
    S.sprintEdit = S.sprintForm = null;
    updateSprintEditor();
  });
});

document.addEventListener('click', async (e) => {
  const t = e.target.closest('button, a');
  if (!t) return;
  const d = t.dataset;
  if (d.act && ACTIONS[d.act]) { e.preventDefault(); ACTIONS[d.act](t); return; }
  if (d.ctl) {
    if (d.ctl === 'focus_capture_start') t.closest('[data-capture-choices]')?.setAttribute('hidden', '');
    control(d.ctl, d.value);
    return;
  }

  // списки
  const lwEl = t.closest('.lw');
  if (lwEl && d.lwdel !== undefined) {
    const items = clone(eff(lwEl.dataset.lw) || []);
    items.splice(+d.lwdel, 1);
    setv(lwEl.dataset.lw, items); renderLW(lwEl);
    return;
  }
  if (lwEl && d.lwadd !== undefined) lwAdd(lwEl);
});

document.addEventListener('input', (e) => {
  const el = e.target;
  if (el.dataset.k && el.type !== 'radio' && el.type !== 'checkbox' && el.tagName !== 'SELECT') { onField(el); return; }
  if (el.dataset.li !== undefined) {
    const lwEl = el.closest('.lw');
    const items = clone(eff(lwEl.dataset.lw) || []);
    items[+el.dataset.li] = el.value;
    setv(lwEl.dataset.lw, items); return;
  }
});

document.addEventListener('change', async (e) => {
  if (e.target.matches('[data-sprint-task]')) {
    const { sprintTask, id } = e.target.dataset;
    await changeSprint({ action: 'subtask', index: +sprintTask, id, done: e.target.checked });
    const current = S.sprints?.sprints.find((item) => item.id === id);
    if (current?.subtasks[+sprintTask]) e.target.checked = current.subtasks[+sprintTask].done;
    return;
  }
  const el = e.target;
  if (el.dataset.audioEvent) { S.audioEvents[el.dataset.audioEvent] = el.value; delete S.audioFiles[el.dataset.audioEvent]; render(); return; }
  if (el.dataset.audioFile) { S.audioFiles[el.dataset.audioFile] = el.value; return; }
  if (el.dataset.notificationEvent) {
    const disabled = new Set(eff('notification_disabled_events') || []);
    if (el.checked) disabled.delete(el.dataset.notificationEvent); else disabled.add(el.dataset.notificationEvent);
    setv('notification_disabled_events', [...disabled]); return;
  }
  if (el.dataset.k && (el.type === 'radio' || el.type === 'checkbox' || el.tagName === 'SELECT')) {
    onField(el);
    if (['voicepack_id', 'alarm_pack_id'].includes(el.dataset.k)) render();
    return;
  }
  if (el.dataset.ctltog === 'sound') { await control('sound', el.checked); return; }
  if (el.dataset.importAudio && el.files.length) {
    const { importAudio: kind, pack, event } = el.dataset;
    const files = [...el.files];
    await busy('importAudio', async () => {
      let imported = 0;
      const problems = [];
      for (const file of files) {
        const query = new URLSearchParams({ kind, pack, event, filename: file.name });
        try {
          const result = await api(`/api/audio/import?${query}`, { method: 'POST', raw: file });
          S.audioFiles[kind] = `${event.toLowerCase()}/${result.file}`;
          imported++;
        } catch (err) { problems.push(`${file.name} · ${err.message}`); }
      }
      S.audioPacks = await api('/api/audio/packs');
      if (problems.length) toast(`Готово ${imported} из ${files.length}. ${problems[0]}`, { bad: true });
      else toast(`Файлы готовы · ${imported}`);
    });
    return;
  }
});

document.addEventListener('keydown', (e) => {
  const el = e.target;
  if (el.dataset && el.dataset.lwnew !== undefined && e.key === 'Enter') {
    e.preventDefault(); const lwEl = el.closest('.lw'); lwAdd(lwEl);
  }
  if ((e.ctrlKey || e.metaKey) && e.key === 's') {
    e.preventDefault();
    if (dirtyCount()) saveAll();
  }
});

document.addEventListener('toggle', (e) => {
  if (e.target.dataset && e.target.dataset.det) S.open[e.target.dataset.det] = e.target.open;
}, true);

window.addEventListener('beforeunload', (e) => {
  if (dirtyCount()) { e.preventDefault(); e.returnValue = ''; }
});

// ---------- опрос состояния ----------

function setStatus(st) {
  S.status = st; S.statusAt = Date.now(); S.conn = true;
  if (st.config_mtime && (!S.mtime || st.config_mtime > S.mtime + 0.5) && !dirtyCount()) {
    // config.json изменили снаружи (вручную или из другой вкладки)
    api('/api/config').then((r) => { loadCfg(r); render(); }).catch(() => {});
  }
}

async function poll() {
  if (document.hidden) return;
  try {
    setStatus(await api('/api/status'));
  } catch (e) {
    S.conn = false;
  }
  renderNav();
  if (['focus', 'intervention'].includes(S.route) && S.cfg) {
    const section = $('[data-focus-live]');
    if (section) {
      const current = document.activeElement?.dataset.ctl;
      if (setHtml(section, focusStatus(S.status)) && current) $(`[data-ctl="${current}"]`, section)?.focus();
    }
  } else if (S.route === 'live' && S.cfg) {
    const section = $('[data-live-session]');
    if (section) setHtml(section, liveStatus(S.status));
  } else if (S.route === 'activity' && S.cfg) {
    const section = $('[data-activity-live]');
    if (section) setHtml(section, activityStatus(S.status));
  } else if (S.route === 'diagnostics' && S.cfg) {
    const section = $('[data-diagnostics-live]');
    if (section) setHtml(section, liveStatus(S.status));
    const pre = $('[data-context-preview]');
    if ((!S.status?.sprint || S.status.sprint.state !== 'ACTIVE') && pre) {
      pre.hidden = true;
      pre.textContent = '';
    }
    if (!S.status?.focus?.sources?.armed) {
      const image = $('[data-context-image]');
      if (image) {
        image.hidden = true;
        image.removeAttribute('src');
      }
    }
  } else if (['current', 'history'].includes(S.route) && S.cfg) {
    const section = $('[data-sprint-live]');
    if (section) setHtml(section, sprintLive(S.status, true));
    const quick = $('[data-sprint-quick]');
    if (quick) quick.disabled = !!S.status?.sprint || !S.status?.startup_running;
    const start = $('[data-start-draft]');
    if (start) start.disabled = !!S.status?.sprint || !S.status?.startup_running;
    if (S.status?.live && !S.busy.sprint &&
        (S.sprints?.revision !== S.status.sprint_revision || S.sprintPid !== S.status.pid)) {
      await loadSprintLibrary();
    }
    tick();
  }
}

function tick() {
  const timer = $('[data-sprint-clock]');
  const sprint = S.status?.sprint;
  if (!timer || !sprint) return;
  const advanced = (Date.now() - S.statusAt) / 1000;
  const value = sprint.state === 'BREAK' ? sprint.break_remaining - advanced
    : sprint.remaining === null ? sprint.seconds + (sprint.state === 'ACTIVE' ? advanced : 0)
    : sprint.remaining - (sprint.state === 'ACTIVE' ? advanced : 0);
  timer.textContent = fmtMS(value);
}

async function boot() {
  S.route = routeFromHash();
  window.addEventListener('hashchange', () => {
    const next = routeFromHash();
    S.route = next;
    S.loaded[S.route] = false;
    render();
    window.scrollTo(0, 0);
  });
  try {
    const [c, st] = await Promise.all([api('/api/config'), api('/api/status')]);
    loadCfg(c);
    setStatus(st);
  } catch (e) {
    $('#main').innerHTML = `<div class="offline"><div><b>Не удалось загрузить настройки</b><div class="err">${esc(e.message)}</div></div></div>`;
    return;
  }
  render();
  setInterval(poll, 2000);
  setInterval(tick, 1000);
  setInterval(pollLogs, 2000);
}

boot();

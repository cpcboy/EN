/* 每日工作计划 · Daily Mission Plan —— 前端逻辑
   多设备同步策略：本地改动先行（乐观更新）→ 后台推送；
   无改动时每 5 秒拉取服务器数据，保证 iPad 常显页面自动刷新。 */
'use strict';

const $ = (id) => document.getElementById(id);
const els = {
  clockHM: $('clockHM'), clockDate: $('clockDate'),
  progressNum: $('progressNum'), progressFill: $('progressFill'),
  progressRocket: $('progressRocket'), progressNote: $('progressNote'),
  syncStatus: $('syncStatus'),
  prevDay: $('prevDay'), nextDay: $('nextDay'), dateLabel: $('dateLabel'),
  carryover: $('carryover'), carryCount: $('carryCount'), carryBtn: $('carryBtn'),
  listCount: $('listCount'), taskList: $('taskList'), empty: $('empty'),
  addForm: $('addForm'), addInput: $('addInput'), addTime: $('addTime'), addPri: $('addPri'),
  editOverlay: $('editOverlay'), editText: $('editText'), editTime: $('editTime'),
  editPri: $('editPri'), editUp: $('editUp'), editDown: $('editDown'),
  editDelete: $('editDelete'), editCancel: $('editCancel'), editSave: $('editSave'),
  codeOverlay: $('codeOverlay'), codeInput: $('codeInput'), codeSubmit: $('codeSubmit'),
};

const WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
const pad = (n) => String(n).padStart(2, '0');
const dstr = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const todayStr = () => dstr(new Date());
const uid = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 8);

function addDays(dateStr, n) {
  const [y, m, d] = dateStr.split('-').map(Number);
  return dstr(new Date(y, m - 1, d + n));
}
function humanDate(dateStr) {
  const [y, m, d] = dateStr.split('-').map(Number);
  const w = WEEK[new Date(y, m - 1, d).getDay()];
  return `${m}月${d}日 ${w}`;
}

const state = {
  date: todayStr(),
  tasks: [],
  code: localStorage.getItem('dp_code') || '',
  editingId: null,
  bootTime: Date.now(),
  lastToday: todayStr(),
};

/* ---------------- 本地缓存（断网 / 刚开机时先展示） ---------------- */
const cacheKey = (date) => 'dp_tasks_' + date;
function cacheGet(date) {
  try {
    const v = JSON.parse(localStorage.getItem(cacheKey(date)));
    return Array.isArray(v) ? v : null;
  } catch (_) { return null; }
}
function cacheSet(date, tasks) {
  try { localStorage.setItem(cacheKey(date), JSON.stringify(tasks)); } catch (_) {}
}

/* ---------------- 网络 ---------------- */
function api(path, opts = {}) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers);
  if (state.code) headers['X-Access-Code'] = state.code;
  return fetch(path, Object.assign({}, opts, { headers })).then((r) => {
    if (r.status === 401) {
      showCodeGate();
      throw new Error('unauthorized');
    }
    return r;
  });
}

function setSync(kind) {
  els.syncStatus.textContent = {
    ok: 'SYNCED · 已同步',
    saving: 'SAVING · 保存中…',
    offline: 'OFFLINE · 离线，改动将自动重试',
    code: 'ACCESS CODE REQUIRED · 请输入口令',
  }[kind] || '';
}

/* 待推送队列：date -> 请求体。改动先记账，flush 逐条推送；失败保留下次重试 */
const pending = new Map();
let flushTimer = null;
let flushing = false;

function markDirty() {
  pending.set(state.date, JSON.stringify({ tasks: state.tasks }));
  cacheSet(state.date, state.tasks);
  clearTimeout(flushTimer);
  flushTimer = setTimeout(flush, 250);
}

async function flush() {
  if (flushing) return;
  flushing = true;
  try {
    for (const [date, body] of [...pending]) {
      setSync('saving');
      const r = await api(`/api/tasks?date=${date}`, { method: 'PUT', body });
      if (!r.ok) throw new Error('save failed');
      if (pending.get(date) === body) pending.delete(date); // 期间无新改动才出队
      setSync('ok');
    }
  } catch (e) {
    setSync(e.message === 'unauthorized' ? 'code' : 'offline');
  } finally {
    flushing = false;
  }
}

async function pull() {
  const date = state.date;
  if (pending.has(date)) return; // 有未推送的本地改动时不拉取，避免覆盖
  try {
    const r = await api(`/api/tasks?date=${date}`);
    if (!r.ok) throw new Error('pull failed');
    const data = await r.json();
    if (date !== state.date || pending.has(date)) return; // 期间已切换或已编辑
    if (JSON.stringify(data.tasks) !== JSON.stringify(state.tasks)) {
      state.tasks = data.tasks;
      render();
    }
    cacheSet(date, data.tasks);
    setSync('ok');
  } catch (e) {
    if (e.message !== 'unauthorized') setSync('offline');
  }
}

function syncNow() {
  if (pending.size) flush().then(() => pull());
  else pull();
}

/* ---------------- 渲染 ---------------- */
function render() {
  renderDateNav();
  renderList();
  renderProgress();
}

function renderDateNav() {
  const isToday = state.date === todayStr();
  els.dateLabel.classList.toggle('not-today', !isToday);
  els.dateLabel.textContent = '';
  els.dateLabel.append((isToday ? '今天 · ' : '') + humanDate(state.date));
  if (!isToday) {
    const hint = document.createElement('span');
    hint.className = 'back-hint';
    hint.textContent = 'TAP TO RETURN · 点击回到今天';
    els.dateLabel.append(hint);
  }
}

function renderList() {
  els.taskList.textContent = '';
  for (const task of state.tasks) {
    const li = document.createElement('li');
    li.className = 'task' + (task.done ? ' done' : '') + (task.priority ? ' priority' : '');

    const check = document.createElement('button');
    check.className = 't-check';
    check.type = 'button';
    check.setAttribute('aria-label', task.done ? '标记为未完成' : '标记为完成');
    check.addEventListener('click', () => toggleTask(task.id));

    const time = document.createElement('span');
    time.className = 't-time';
    time.textContent = task.time || '';

    const text = document.createElement('button');
    text.className = 't-text';
    text.type = 'button';
    const mark = document.createElement('span');
    mark.className = 't-pri-mark';
    mark.textContent = '▲';
    text.append(mark, document.createTextNode(task.text));
    text.addEventListener('click', () => openEdit(task.id));

    li.append(check, time, text);
    els.taskList.append(li);
  }
  const total = state.tasks.length;
  els.empty.hidden = total !== 0;
  els.listCount.textContent = total
    ? `${state.tasks.filter((t) => t.done).length} / ${total} DONE`
    : '';
}

function renderProgress() {
  const total = state.tasks.length;
  const done = state.tasks.filter((t) => t.done).length;
  const pct = total ? Math.round((done / total) * 100) : 0;
  els.progressNum.textContent = total ? `${done}/${total} · ${pct}%` : '—';
  els.progressFill.style.width = pct + '%';
  els.progressRocket.style.left = pct + '%';
  els.progressNote.textContent = !total
    ? 'STANDING BY · 暂无任务'
    : done === total
      ? 'MISSION COMPLETE · 今日全部完成'
      : 'IN FLIGHT · 进行中';
}

/* ---------------- 任务操作 ---------------- */
function findTask(id) { return state.tasks.find((t) => t.id === id); }

function toggleTask(id) {
  const t = findTask(id);
  if (!t) return;
  t.done = !t.done;
  markDirty();
  render();
}

function addTask(text, time, priority) {
  state.tasks.push({ id: uid(), text, time: time || '', priority: !!priority, done: false });
  markDirty();
  render();
}

/* ---------------- 编辑弹窗 ---------------- */
function openEdit(id) {
  const t = findTask(id);
  if (!t) return;
  state.editingId = id;
  els.editText.value = t.text;
  els.editTime.value = t.time || '';
  els.editPri.setAttribute('aria-pressed', String(!!t.priority));
  els.editOverlay.hidden = false;
  els.editText.focus();
}
function closeEdit() {
  state.editingId = null;
  els.editOverlay.hidden = true;
}
function saveEdit() {
  const t = findTask(state.editingId);
  if (t) {
    const text = els.editText.value.trim();
    if (!text) return deleteEdit();
    t.text = text;
    t.time = els.editTime.value || '';
    t.priority = els.editPri.getAttribute('aria-pressed') === 'true';
    markDirty();
    render();
  }
  closeEdit();
}
function deleteEdit() {
  state.tasks = state.tasks.filter((t) => t.id !== state.editingId);
  markDirty();
  render();
  closeEdit();
}
function moveEdit(delta) {
  const i = state.tasks.findIndex((t) => t.id === state.editingId);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= state.tasks.length) return;
  [state.tasks[i], state.tasks[j]] = [state.tasks[j], state.tasks[i]];
  markDirty();
  render();
}

/* ---------------- 昨日未完成 → 移到今天 ---------------- */
let carryDay = null; // { date, tasks } 昨日完整数据
async function checkCarryover() {
  els.carryover.hidden = true;
  carryDay = null;
  if (state.date !== todayStr()) return;
  const yesterday = addDays(state.date, -1);
  try {
    const r = await api(`/api/tasks?date=${yesterday}`);
    if (!r.ok) return;
    const data = await r.json();
    if (state.date !== todayStr()) return;
    const undone = (data.tasks || []).filter((t) => !t.done);
    if (undone.length) {
      carryDay = { date: yesterday, tasks: data.tasks || [] };
      els.carryCount.textContent = String(undone.length);
      els.carryover.hidden = false;
    }
  } catch (_) { /* 离线时忽略 */ }
}

function doCarryover() {
  if (!carryDay || state.date !== todayStr()) return;
  const existing = new Set(state.tasks.map((t) => t.id));
  const moved = carryDay.tasks.filter((t) => !t.done && !existing.has(t.id));
  const doneYesterday = carryDay.tasks.filter((t) => t.done);
  state.tasks = [...moved, ...state.tasks];
  // 昨天只保留已完成的任务
  pending.set(carryDay.date, JSON.stringify({ tasks: doneYesterday }));
  cacheSet(carryDay.date, doneYesterday);
  carryDay = null;
  els.carryover.hidden = true;
  markDirty();
  render();
}

/* ---------------- 日期切换 ---------------- */
function loadDate(date) {
  state.date = date;
  state.tasks = cacheGet(date) || [];
  render();
  pull();
  checkCarryover();
}

/* ---------------- 时钟（含跨天自动切换） ---------------- */
function tickClock() {
  const now = new Date();
  els.clockHM.textContent = `${pad(now.getHours())}:${pad(now.getMinutes())}`;
  els.clockDate.textContent =
    `${now.getFullYear()}.${pad(now.getMonth() + 1)}.${pad(now.getDate())} · ${WEEK[now.getDay()]}`;

  const t = todayStr();
  if (t !== state.lastToday) {
    // 午夜跨天：常显的 iPad 自动翻到新的一天
    if (state.date === state.lastToday) loadDate(t);
    state.lastToday = t;
  }
  // 每天凌晨 4 点静默刷新页面，保持长期运行的 Safari 稳定
  if (now.getHours() === 4 && now.getMinutes() === 5 &&
      Date.now() - state.bootTime > 2 * 3600 * 1000) {
    location.reload();
  }
}

/* ---------------- 屏幕常亮（需 HTTPS；HTTP 下请在 iPad 设置中关闭自动锁定） ---------------- */
async function requestWakeLock() {
  if (!('wakeLock' in navigator)) return;
  try { await navigator.wakeLock.request('screen'); } catch (_) {}
}

/* ---------------- 访问口令 ---------------- */
function showCodeGate() {
  if (els.codeOverlay.hidden) {
    els.codeOverlay.hidden = false;
    els.codeInput.focus();
  }
}
function submitCode() {
  state.code = els.codeInput.value.trim();
  localStorage.setItem('dp_code', state.code);
  els.codeOverlay.hidden = true;
  els.codeInput.value = '';
  syncNow();
  checkCarryover();
}

/* ---------------- 事件绑定 ---------------- */
els.addForm.addEventListener('submit', (e) => {
  e.preventDefault();
  const text = els.addInput.value.trim();
  if (!text) return;
  addTask(text, els.addTime.value, els.addPri.getAttribute('aria-pressed') === 'true');
  els.addInput.value = '';
  els.addTime.value = '';
  els.addPri.setAttribute('aria-pressed', 'false');
  els.addInput.focus();
});
els.addPri.addEventListener('click', () => {
  const on = els.addPri.getAttribute('aria-pressed') === 'true';
  els.addPri.setAttribute('aria-pressed', String(!on));
});

els.prevDay.addEventListener('click', () => loadDate(addDays(state.date, -1)));
els.nextDay.addEventListener('click', () => loadDate(addDays(state.date, 1)));
els.dateLabel.addEventListener('click', () => loadDate(todayStr()));
els.carryBtn.addEventListener('click', doCarryover);

els.editSave.addEventListener('click', saveEdit);
els.editCancel.addEventListener('click', closeEdit);
els.editDelete.addEventListener('click', deleteEdit);
els.editUp.addEventListener('click', () => moveEdit(-1));
els.editDown.addEventListener('click', () => moveEdit(1));
els.editPri.addEventListener('click', () => {
  const on = els.editPri.getAttribute('aria-pressed') === 'true';
  els.editPri.setAttribute('aria-pressed', String(!on));
});
els.editText.addEventListener('keydown', (e) => { if (e.key === 'Enter') saveEdit(); });
els.editOverlay.addEventListener('click', (e) => { if (e.target === els.editOverlay) closeEdit(); });

els.codeSubmit.addEventListener('click', submitCode);
els.codeInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') submitCode(); });

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    requestWakeLock();
    syncNow();
    checkCarryover();
  }
});
window.addEventListener('pagehide', () => {
  // 关页前尽力把未保存的改动发出去
  for (const [date, body] of pending) {
    const q = state.code ? `&code=${encodeURIComponent(state.code)}` : '';
    navigator.sendBeacon(`/api/tasks?date=${date}${q}`, body);
  }
});

/* ---------------- 启动 ---------------- */
tickClock();
setInterval(tickClock, 1000);
loadDate(todayStr());
requestWakeLock();
setInterval(syncNow, 5000);

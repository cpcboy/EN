#!/usr/bin/env node
/**
 * 每日工作计划 · Daily Mission Plan
 * 零依赖 Node.js 服务：静态页面 + 任务存储 API（数据保存在 data/tasks.json）
 *
 * 启动：node server.js
 * 环境变量（均可选）：
 *   PORT=3000          监听端口
 *   HOST=0.0.0.0       监听地址
 *   ACCESS_CODE=xxxx   访问口令（设置后 API 需要口令，前端会弹出输入框）
 *   DATA_DIR=./data    数据目录
 */
'use strict';

const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const PORT = parseInt(process.env.PORT, 10) || 3000;
const HOST = process.env.HOST || '0.0.0.0';
const ACCESS_CODE = process.env.ACCESS_CODE || '';
const DATA_DIR = process.env.DATA_DIR || path.join(__dirname, 'data');
const DATA_FILE = path.join(DATA_DIR, 'tasks.json');
const PUBLIC_DIR = path.join(__dirname, 'public');

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.webmanifest': 'application/manifest+json',
  '.txt': 'text/plain; charset=utf-8',
};

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

// ---------------- 存储 ----------------
// 结构：{ days: { 'YYYY-MM-DD': { tasks: [...], updatedAt: ISO } } }
let db = { days: {} };
let saveTimer = null;

function loadDb() {
  try {
    const parsed = JSON.parse(fs.readFileSync(DATA_FILE, 'utf8'));
    if (parsed && typeof parsed === 'object' && parsed.days && typeof parsed.days === 'object') {
      db = parsed;
    }
  } catch (_) {
    /* 首次运行或文件损坏时使用空库 */
  }
}

function saveDb() {
  fs.mkdirSync(DATA_DIR, { recursive: true });
  const tmp = DATA_FILE + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(db, null, 2));
  fs.renameSync(tmp, DATA_FILE); // 原子替换，避免写一半断电损坏数据
}

function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    try {
      saveDb();
    } catch (e) {
      console.error('[daily-plan] 数据保存失败:', e.message);
    }
  }, 100);
}

function sanitizeTasks(input) {
  if (!Array.isArray(input)) return null;
  const out = [];
  for (const t of input.slice(0, 200)) {
    if (!t || typeof t !== 'object') continue;
    const text = String(t.text == null ? '' : t.text).slice(0, 500);
    if (!text.trim()) continue;
    out.push({
      id: String(t.id || crypto.randomBytes(6).toString('hex')).slice(0, 40),
      text,
      time: /^\d{2}:\d{2}$/.test(String(t.time)) ? String(t.time) : '',
      priority: !!t.priority,
      done: !!t.done,
    });
  }
  return out;
}

// ---------------- 额度合并（防呆护栏） ----------------
const QUOTA_FRESH_MS = 15 * 60 * 1000;

function sanitizeQuotaTool(t) {
  if (!t || typeof t !== 'object' || typeof t.name !== 'string' || !t.name.trim()) return null;
  const out = { name: t.name.slice(0, 40) };
  if (Array.isArray(t.windows)) {
    const wins = [];
    for (const w of t.windows.slice(0, 4)) {
      if (!w || typeof w !== 'object') continue;
      const used = Number(w.usedPercent);
      if (!Number.isFinite(used)) continue;
      wins.push({
        label: String(w.label || '').slice(0, 4),
        usedPercent: Math.min(100, Math.max(0, used)),
        resetsAt: w.resetsAt && Number.isFinite(Number(w.resetsAt)) ? Number(w.resetsAt) : null,
      });
    }
    if (wins.length) out.windows = wins;
  }
  if (t.asOf && Number.isFinite(Number(t.asOf))) out.asOf = Number(t.asOf);
  if (t.error) out.error = String(t.error).slice(0, 160);
  return out.windows || out.error ? out : null;
}

const quotaAllZero = (wins) => wins.every((w) => !w.usedPercent);

function mergeQuotaTool(prev, next) {
  if (!prev || !prev.windows) return next;
  if (!next.windows) {
    // 本次只有错误：保留已有数值；15 分钟内还有正常数据就先不显示错误
    if (!prev.error && prev.asOf && Date.now() - prev.asOf < QUOTA_FRESH_MS) return prev;
    return { name: prev.name, windows: prev.windows, asOf: prev.asOf, error: next.error };
  }
  if (prev.asOf && next.asOf) {
    if (next.asOf <= prev.asOf) {
      // 旧数据不覆盖新数据；除非已存的是全 0 占位而本次是真实数值
      return quotaAllZero(prev.windows) && !quotaAllZero(next.windows) ? next : prev;
    }
    // 紧跟在真实数值后面的全 0 记录（<6 小时）多半是会话初始化占位，不采纳
    if (quotaAllZero(next.windows) && !quotaAllZero(prev.windows)
        && next.asOf - prev.asOf < 6 * 3600 * 1000) {
      return prev;
    }
  }
  return next;
}

// ---------------- HTTP ----------------
function send(res, status, body, headers) {
  const h = Object.assign({ 'Cache-Control': 'no-store' }, headers);
  res.writeHead(status, h);
  res.end(body);
}

function sendJson(res, status, obj) {
  send(res, status, JSON.stringify(obj), { 'Content-Type': MIME['.json'] });
}

function authorized(req, url) {
  if (!ACCESS_CODE) return true;
  const given = req.headers['x-access-code'] || url.searchParams.get('code') || '';
  const a = Buffer.from(String(given));
  const b = Buffer.from(ACCESS_CODE);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

function readBody(req, limit, cb) {
  let size = 0;
  const chunks = [];
  req.on('data', (c) => {
    size += c.length;
    if (size > limit) {
      cb(new Error('body too large'));
      req.destroy();
      return;
    }
    chunks.push(c);
  });
  req.on('end', () => cb(null, Buffer.concat(chunks).toString('utf8')));
  req.on('error', (e) => cb(e));
}

function handleApi(req, res, url) {
  if (!authorized(req, url)) {
    return sendJson(res, 401, { error: 'unauthorized', needCode: true });
  }

  if (url.pathname === '/api/health') {
    return sendJson(res, 200, { ok: true, time: new Date().toISOString() });
  }

  // AI 工具额度：Mac 上的 quota-reporter.py 定时 POST，页面 GET 展示。
  // 合并在服务器端按工具逐个进行（多台电脑上报互不覆盖，异常不清空数据）
  if (url.pathname === '/api/quota') {
    if (req.method === 'GET') {
      return sendJson(res, 200, { quota: db.quota || null });
    }
    if (req.method === 'PUT' || req.method === 'POST') {
      return readBody(req, 64 * 1024, (err, raw) => {
        if (err) return sendJson(res, 413, { error: 'body too large' });
        let parsed;
        try {
          parsed = JSON.parse(raw);
        } catch (_) {
          return sendJson(res, 400, { error: 'invalid json' });
        }
        const incoming = parsed && Array.isArray(parsed.tools) ? parsed.tools : null;
        if (!incoming) return sendJson(res, 400, { error: 'tools must be an array' });

        const stored = db.quota && db.quota.data && Array.isArray(db.quota.data.tools)
          ? db.quota.data.tools : [];
        const map = new Map();
        const order = [];
        for (const t of stored) {
          if (t && t.name) { map.set(t.name, t); order.push(t.name); }
        }
        for (const rawTool of incoming.slice(0, 8)) {
          const t = sanitizeQuotaTool(rawTool);
          if (!t) continue;
          if (!map.has(t.name)) order.push(t.name);
          map.set(t.name, mergeQuotaTool(map.get(t.name), t));
        }
        db.quota = {
          data: { tools: order.map((n) => map.get(n)) },
          updatedAt: new Date().toISOString(),
        };
        scheduleSave();
        return sendJson(res, 200, { ok: true, quota: db.quota });
      });
    }
    return sendJson(res, 405, { error: 'method not allowed' });
  }

  if (url.pathname === '/api/tasks') {
    const date = url.searchParams.get('date') || '';
    if (!DATE_RE.test(date)) return sendJson(res, 400, { error: 'invalid date' });

    if (req.method === 'GET') {
      const day = db.days[date] || { tasks: [], updatedAt: null };
      return sendJson(res, 200, { date, tasks: day.tasks, updatedAt: day.updatedAt });
    }

    // POST 与 PUT 等效（navigator.sendBeacon 只能发 POST）
    if (req.method === 'PUT' || req.method === 'POST') {
      return readBody(req, 512 * 1024, (err, raw) => {
        if (err) return sendJson(res, 413, { error: 'body too large' });
        let parsed;
        try {
          parsed = JSON.parse(raw);
        } catch (_) {
          return sendJson(res, 400, { error: 'invalid json' });
        }
        const tasks = sanitizeTasks(parsed && parsed.tasks);
        if (!tasks) return sendJson(res, 400, { error: 'tasks must be an array' });
        const updatedAt = new Date().toISOString();
        if (tasks.length === 0) delete db.days[date];
        else db.days[date] = { tasks, updatedAt };
        scheduleSave();
        return sendJson(res, 200, { date, tasks, updatedAt });
      });
    }

    return sendJson(res, 405, { error: 'method not allowed' });
  }

  return sendJson(res, 404, { error: 'not found' });
}

function serveStatic(req, res, url) {
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    return send(res, 405, 'Method Not Allowed', { 'Content-Type': MIME['.txt'] });
  }
  let p;
  try {
    p = decodeURIComponent(url.pathname);
  } catch (_) {
    return send(res, 400, 'Bad Request', { 'Content-Type': MIME['.txt'] });
  }
  if (p === '/') p = '/index.html';
  const file = path.normalize(path.join(PUBLIC_DIR, p));
  if (!file.startsWith(PUBLIC_DIR + path.sep)) {
    return send(res, 403, 'Forbidden', { 'Content-Type': MIME['.txt'] });
  }
  fs.readFile(file, (err, buf) => {
    if (err) return send(res, 404, 'Not Found', { 'Content-Type': MIME['.txt'] });
    const ext = path.extname(file).toLowerCase();
    const cache = ext === '.html' ? 'no-store' : 'no-cache';
    send(res, 200, req.method === 'HEAD' ? undefined : buf, {
      'Content-Type': MIME[ext] || 'application/octet-stream',
      'Cache-Control': cache,
    });
  });
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname.startsWith('/api/')) return handleApi(req, res, url);
  return serveStatic(req, res, url);
});

loadDb();
server.listen(PORT, HOST, () => {
  console.log(`[daily-plan] 每日工作计划已启动: http://${HOST}:${PORT}`);
  console.log(`[daily-plan] 数据文件: ${DATA_FILE}`);
  if (ACCESS_CODE) console.log('[daily-plan] 已启用访问口令');
});

process.on('SIGINT', () => {
  try { saveDb(); } catch (_) {}
  process.exit(0);
});
process.on('SIGTERM', () => {
  try { saveDb(); } catch (_) {}
  process.exit(0);
});

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日工作计划 · AI 额度上报脚本（在 Mac 上运行）

读取 Claude Code 与 Codex 的 5 小时 / 7 天额度使用情况，上报到工作计划服务器，
供网页左下角的「额度监控」面板显示。

用法（通常由 crontab 每分钟调用）：
    python3 quota-reporter.py http://服务器IP:3000 [访问口令]

数据来源：
  - Claude Code：~/.claude/.credentials.json 里的 OAuth 令牌 → 官方 usage 接口
  - Codex：~/.codex/sessions/ 会话日志中最近一条 rate_limits 记录（本地解析，不联网）
仅上报百分比与重置时间，不上传任何令牌或对话内容。
"""
import datetime
import glob
import json
import os
import subprocess
import sys
import urllib.request

SERVER = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get('DP_SERVER', '')).rstrip('/')
ACCESS_CODE = sys.argv[2] if len(sys.argv) > 2 else os.environ.get('DP_ACCESS_CODE', '')
CLAUDE_CRED = os.environ.get('CLAUDE_CRED_FILE', os.path.expanduser('~/.claude/.credentials.json'))
CLAUDE_USAGE_URL = os.environ.get('CLAUDE_USAGE_URL', 'https://api.anthropic.com/api/oauth/usage')
CODEX_SESSIONS = os.environ.get('CODEX_SESSIONS_DIR', os.path.expanduser('~/.codex/sessions'))


def now_ms():
    return int(datetime.datetime.now().timestamp() * 1000)


def pct(v):
    """兼容 0-1 小数与 0-100 百分数两种写法。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return round(v * 100, 1) if 0 <= v <= 1 else round(v, 1)


def iso_to_ms(s):
    try:
        return int(datetime.datetime.fromisoformat(str(s).replace('Z', '+00:00')).timestamp() * 1000)
    except Exception:
        return None


# ---------------- Claude Code ----------------
def read_claude_credentials():
    """macOS 新版 Claude Code 把凭证存在钥匙串；旧版/Linux 存在文件。钥匙串优先。"""
    if sys.platform == 'darwin':
        try:
            out = subprocess.run(
                ['security', 'find-generic-password', '-s', 'Claude Code-credentials', '-w'],
                capture_output=True, text=True, timeout=10)
            if out.returncode == 0 and out.stdout.strip():
                return json.loads(out.stdout.strip())
        except Exception:
            pass
    try:
        with open(CLAUDE_CRED) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def claude_quota():
    tool = {'name': 'CLAUDE CODE'}
    try:
        cred = read_claude_credentials()
        if not cred:
            tool['error'] = '未找到登录凭证，先在 Mac 登录 Claude Code'
            return tool
        token = (cred.get('claudeAiOauth') or {}).get('accessToken')
        if not token:
            tool['error'] = '未找到登录凭证，先在 Mac 登录 Claude Code'
            return tool
        req = urllib.request.Request(CLAUDE_USAGE_URL, headers={
            'Authorization': 'Bearer ' + token,
            'anthropic-beta': 'oauth-2025-04-20',
            'User-Agent': 'daily-plan-quota-reporter/1.0',
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        windows = []
        for key, label in (('five_hour', '5H'), ('seven_day', '7D')):
            w = data.get(key)
            if isinstance(w, dict):
                used = pct(w.get('utilization'))
                if used is not None:
                    windows.append({'label': label, 'usedPercent': used,
                                    'resetsAt': iso_to_ms(w.get('resets_at'))})
        if not windows:
            tool['error'] = 'usage 接口返回了无法识别的格式'
        else:
            tool['windows'] = windows
            tool['asOf'] = now_ms()
    except urllib.error.HTTPError as e:
        tool['error'] = ('登录已过期：在 Claude Code 里运行 /login 后自动恢复'
                         if e.code == 401 else 'usage 接口返回 HTTP %d' % e.code)
    except Exception as e:
        tool['error'] = '读取失败：' + type(e).__name__
    return tool


# ---------------- Codex ----------------
def find_rate_limits(obj):
    """在事件 JSON 里递归寻找 rate_limits 对象。"""
    if isinstance(obj, dict):
        rl = obj.get('rate_limits')
        if isinstance(rl, dict):
            return rl
        for v in obj.values():
            found = find_rate_limits(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_rate_limits(v)
            if found:
                return found
    return None


def codex_quota():
    tool = {'name': 'CODEX'}
    try:
        files = glob.glob(os.path.join(CODEX_SESSIONS, '**', '*.jsonl'), recursive=True)
        files.sort(key=os.path.getmtime, reverse=True)
        for path in files[:10]:
            try:
                with open(path, errors='ignore') as f:
                    lines = f.readlines()
            except OSError:
                continue
            for line in reversed(lines):
                if '"rate_limits"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                rl = find_rate_limits(obj)
                if not rl:
                    continue
                base = iso_to_ms(obj.get('timestamp')) or int(os.path.getmtime(path) * 1000)
                windows = []
                for key, fallback in (('primary', '5H'), ('secondary', '7D')):
                    w = rl.get(key)
                    if not isinstance(w, dict):
                        continue
                    used = pct(w.get('used_percent'))
                    if used is None:
                        continue
                    mins = w.get('window_minutes') or 0
                    label = fallback if not mins else ('5H' if mins <= 1440 else '7D')
                    resets = w.get('resets_in_seconds')
                    windows.append({'label': label, 'usedPercent': used,
                                    'resetsAt': base + int(resets) * 1000 if resets else None})
                if windows:
                    tool['windows'] = windows
                    tool['asOf'] = base
                    return tool
        tool['error'] = '未找到额度记录，先在 Codex 里跑一次任务'
    except Exception as e:
        tool['error'] = '读取失败：' + type(e).__name__
    return tool


def merge_previous(tools, headers):
    """某工具本次读取失败时，沿用服务器上已有的数值，仅附加错误说明，避免面板被清空。"""
    try:
        req = urllib.request.Request(SERVER + '/api/quota', headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            prev = json.load(resp)
        prev_tools = {}
        for t in ((((prev or {}).get('quota') or {}).get('data') or {}).get('tools') or []):
            if isinstance(t, dict) and t.get('name'):
                prev_tools[t['name']] = t
    except Exception:
        return tools
    merged = []
    for t in tools:
        if t.get('error') and not t.get('windows'):
            p = prev_tools.get(t['name'])
            if p and p.get('windows'):
                t = {'name': t['name'], 'windows': p['windows'],
                     'asOf': p.get('asOf'), 'error': t['error']}
        merged.append(t)
    return merged


def main():
    if not SERVER:
        sys.exit('用法：python3 quota-reporter.py http://服务器IP:3000 [访问口令]')
    headers = {'Content-Type': 'application/json'}
    if ACCESS_CODE:
        headers['X-Access-Code'] = ACCESS_CODE
    tools = merge_previous([claude_quota(), codex_quota()], headers)
    payload = {'tools': tools, 'reportedAt': now_ms()}
    req = urllib.request.Request(SERVER + '/api/quota',
                                 data=json.dumps(payload).encode(),
                                 headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()
    print('已上报：', json.dumps(payload, ensure_ascii=False))


if __name__ == '__main__':
    main()

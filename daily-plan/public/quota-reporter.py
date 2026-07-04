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
import re
import subprocess
import sys
import urllib.request

_args = [a for a in sys.argv[1:] if not a.startswith('--')]
DEBUG_MODE = '--debug' in sys.argv
INSTALL_MODE = '--install' in sys.argv
INSTALL_HOOK_MODE = '--install-hook' in sys.argv
REFRESH_CRED_MODE = '--refresh-cred' in sys.argv
SERVER = (_args[0] if _args else os.environ.get('DP_SERVER', '')).rstrip('/')
ACCESS_CODE = _args[1] if len(_args) > 1 else os.environ.get('DP_ACCESS_CODE', '')
AGENT_LABEL = 'com.dailyplan.quota-reporter'
CLAUDE_CRED = os.environ.get('CLAUDE_CRED_FILE', os.path.expanduser('~/.claude/.credentials.json'))
CLAUDE_USAGE_URL = os.environ.get('CLAUDE_USAGE_URL', 'https://api.anthropic.com/api/oauth/usage')
CLAUDE_TOKEN_URL = os.environ.get('CLAUDE_TOKEN_URL', 'https://console.anthropic.com/v1/oauth/token')
CLAUDE_CLIENT_ID = '9d1c250a-e61b-44d9-88ed-5944d1962f5e'  # Claude Code 的公开 OAuth client_id
CODEX_SESSIONS = os.environ.get('CODEX_SESSIONS_DIR', os.path.expanduser('~/.codex/sessions'))


STATE_FILE = os.path.expanduser('~/.quota-reporter.state')


def now_ms():
    return int(datetime.datetime.now().timestamp() * 1000)


# ---------------- 网络通道 ----------------
# macOS 上浏览器走系统代理/PAC，但命令行的 Python 默认直连；
# 直连 api.anthropic.com 在国内不通，所以这里自动探测可用代理。
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except OSError:
        pass


def open_url(req, timeout, proxy):
    handler = urllib.request.ProxyHandler(
        {} if proxy == 'DIRECT' else {'http': proxy, 'https': proxy})
    return urllib.request.build_opener(handler).open(req, timeout=timeout)


def proxy_candidates(state):
    cands = []
    if os.environ.get('DP_PROXY'):        # 手动指定，最优先
        cands.append(os.environ['DP_PROXY'])
    if state.get('proxy'):                 # 上次成功的通道
        cands.append(state['proxy'])
    try:                                   # 环境变量 / 系统静态代理
        sysp = urllib.request.getproxies()
        for k in ('https', 'http'):
            if sysp.get(k):
                cands.append(sysp[k])
    except Exception:
        pass
    if sys.platform == 'darwin':           # PAC 文件里的 PROXY 条目
        try:
            out = subprocess.run(['scutil', '--proxy'], capture_output=True,
                                 text=True, timeout=5).stdout
            m = re.search(r'ProxyAutoConfigURLString\s*:\s*(\S+)', out)
            if m:
                pac = urllib.request.urlopen(m.group(1), timeout=5).read().decode(errors='ignore')
                for hp in re.findall(r'PROXY\s+([\w.\-]+:\d+)', pac):
                    cands.append('http://' + hp)
        except Exception:
            pass
    for port in (7890, 7897, 1087, 6152, 8118, 8888):   # 常见本地代理端口
        cands.append('http://127.0.0.1:%d' % port)
    cands.append('DIRECT')                 # 直连（增强/TUN 模式下可用）放最后
    seen, ordered = set(), []
    for c in cands:
        if '://' not in c and c != 'DIRECT':
            c = 'http://' + c
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered


def _curl_fetch(url, headers, timeout, body=None):
    """用系统 curl 请求（TLS 栈与指纹和 Python 不同，可绕过按客户端指纹放行的 DPI）。
    header 经 stdin、body 经临时文件传入，避免敏感内容出现在进程列表；
    返回解析后的 JSON，非 2xx 抛 HTTPError。"""
    import tempfile
    marker = '__HTTP_STATUS__:'
    cmd = ['curl', '-sS', '--max-time', str(timeout), '-H', '@-',
           '-w', '\n' + marker + '%{http_code}']
    tmp_body = None
    if body is not None:
        fd, tmp_body = tempfile.mkstemp()
        os.write(fd, body)
        os.close(fd)
        os.chmod(tmp_body, 0o600)
        cmd += ['--data-binary', '@' + tmp_body]
    cmd.append(url)
    hdr_text = '\n'.join('%s: %s' % (k, v) for k, v in headers.items())
    try:
        out = subprocess.run(cmd, input=hdr_text, capture_output=True, text=True, timeout=timeout + 8)
    finally:
        if tmp_body:
            try:
                os.unlink(tmp_body)
            except OSError:
                pass
    if out.returncode != 0:
        raise OSError('curl: ' + ((out.stderr or '').strip().splitlines() or ['exit %d' % out.returncode])[-1][:80])
    body, _, status = out.stdout.rpartition('\n' + marker)
    if not status.strip().isdigit():
        raise OSError('curl: no status')
    code = int(status.strip())
    if not 200 <= code < 300:
        raise urllib.error.HTTPError(url, code, 'curl', None, None)
    return json.loads(body)


def fetch_via_any_proxy(req, state):
    """依次尝试各通道。收到 HTTP 响应（含 401 等错误码）说明已连通，按原样抛出/返回。
    专线/直连场景网络会间歇抖动，直连多试几次；urllib 全部失败后用 curl 兜底。"""
    last = None
    for proxy in proxy_candidates(state):
        attempts = 3 if proxy == 'DIRECT' else 1
        for _ in range(attempts):
            try:
                with open_url(req, 10, proxy) as resp:
                    data = json.load(resp)
                state['proxy'] = proxy
                return data
            except urllib.error.HTTPError:
                state['proxy'] = proxy
                raise
            except Exception as e:
                last = e
    try:
        headers = dict(req.header_items())
        return _curl_fetch(req.full_url, headers, 12, body=req.data)
    except urllib.error.HTTPError:
        raise
    except Exception as e:
        last = e
    raise last if last else OSError('no proxy route')


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
CRED_CACHE = os.path.expanduser('~/.quota-reporter.cred')


def _keychain_read():
    """从钥匙串读凭证，返回 (JSON文本或None, 失败原因)。
    launchd 后台上下文默认搜索列表有时不含登录钥匙串，失败后显式指定路径再试。"""
    if sys.platform != 'darwin':
        return None, ''
    login_kc = os.path.expanduser('~/Library/Keychains/login.keychain-db')
    last_err = ''
    for extra in ([], [login_kc]):
        try:
            out = subprocess.run(
                ['security', 'find-generic-password', '-s', 'Claude Code-credentials', '-w'] + extra,
                capture_output=True, text=True, timeout=10)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip(), ''
            lines = (out.stderr or '').strip().splitlines()
            last_err = lines[-1] if lines else ('exit %d' % out.returncode)
        except Exception as e:
            last_err = type(e).__name__
    return None, last_err


def read_claude_credentials():
    """钥匙串 → 官方凭证文件 → 本脚本的缓存。返回 (凭证dict或None, 钥匙串失败原因)。
    钥匙串读取成功时写入缓存：后台(launchd/cron)读不到钥匙串时靠缓存工作，
    在终端手动运行一次本脚本即可刷新缓存。"""
    raw, kc_err = _keychain_read()
    if raw:
        try:
            with open(CRED_CACHE, 'w') as f:
                f.write(raw)
            os.chmod(CRED_CACHE, 0o600)
        except OSError:
            pass
        try:
            return json.loads(raw), ''
        except ValueError:
            pass
    try:
        with open(CLAUDE_CRED) as f:
            return json.load(f), ''
    except (OSError, ValueError):
        pass
    try:
        with open(CRED_CACHE) as f:
            return json.load(f), kc_err
    except (OSError, ValueError):
        pass
    return None, kc_err


def _keychain_write(raw_json):
    """把续期后的凭证写回钥匙串，保持 Claude Code 与脚本的一致。失败不致命。"""
    try:
        out = subprocess.run(
            ['security', 'find-generic-password', '-s', 'Claude Code-credentials'],
            capture_output=True, text=True, timeout=10)
        m = re.search(r'"acct"<blob>="([^"]*)"', out.stdout or '')
        if out.returncode != 0 or not m:
            return False
        r = subprocess.run(
            ['security', 'add-generic-password', '-U', '-s', 'Claude Code-credentials',
             '-a', m.group(1), '-w', raw_json],
            capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def refresh_claude_cred(cred, state):
    """access token 过期时用 refresh token 自动续期（与 Claude Code 自身的续期方式一致），
    成功后写入缓存并尽量写回钥匙串。返回新凭证，失败返回 None。"""
    rt = (cred.get('claudeAiOauth') or {}).get('refreshToken')
    if not rt:
        return None
    if now_ms() < state.get('claude_refresh_next_ms', 0):
        return None
    state['claude_refresh_next_ms'] = now_ms() + 30 * 60 * 1000  # 失败退避 30 分钟
    body = json.dumps({'grant_type': 'refresh_token', 'refresh_token': rt,
                       'client_id': CLAUDE_CLIENT_ID}).encode()
    req = urllib.request.Request(CLAUDE_TOKEN_URL, data=body, method='POST', headers={
        'Content-Type': 'application/json',
        'User-Agent': 'daily-plan-quota-reporter/1.0',
    })
    try:
        data = fetch_via_any_proxy(req, state)
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get('access_token'):
        return None
    oauth = dict(cred.get('claudeAiOauth') or {})
    oauth['accessToken'] = data['access_token']
    if data.get('refresh_token'):
        oauth['refreshToken'] = data['refresh_token']
    if data.get('expires_in'):
        oauth['expiresAt'] = now_ms() + int(data['expires_in']) * 1000
    new_cred = dict(cred)
    new_cred['claudeAiOauth'] = oauth
    raw = json.dumps(new_cred)
    try:
        with open(CRED_CACHE, 'w') as f:
            f.write(raw)
        os.chmod(CRED_CACHE, 0o600)
    except OSError:
        pass
    if sys.platform == 'darwin':
        _keychain_write(raw)
    state['claude_refresh_next_ms'] = now_ms()  # 成功则清除退避
    return new_cred


def _fetch_claude_windows(token, state):
    """带令牌请求 usage 接口，解析出 5H/7D 两个窗口。"""
    req = urllib.request.Request(CLAUDE_USAGE_URL, headers={
        'Authorization': 'Bearer ' + token,
        'anthropic-beta': 'oauth-2025-04-20',
        'User-Agent': 'daily-plan-quota-reporter/1.0',
    })
    data = fetch_via_any_proxy(req, state)
    windows = []
    for key, label in (('five_hour', '5H'), ('seven_day', '7D')):
        w = data.get(key)
        if isinstance(w, dict):
            used = pct(w.get('utilization'))
            if used is not None:
                windows.append({'label': label, 'usedPercent': used,
                                'resetsAt': iso_to_ms(w.get('resets_at'))})
    return windows


def claude_quota(state):
    tool = {'name': 'CLAUDE CODE'}
    # 后台(cron)调度策略：成功后 5 分钟一查（共享出口 IP 查太频繁会 429）；
    # 网络抖动失败则下一分钟就重试；被限流退避 15 分钟。
    # 间隙沿用服务器上已有数据；手动在终端运行时不受此限制。
    interactive = sys.stdout.isatty()
    if not interactive and now_ms() < state.get('claude_next_fetch_ms', 0):
        tool['reuse'] = True
        return tool
    state['claude_next_fetch_ms'] = now_ms() + 5 * 60 * 1000
    try:
        cred, kc_err = read_claude_credentials()
        oauth = (cred or {}).get('claudeAiOauth') or {}
        token = oauth.get('accessToken')
        if not token:
            tool['error'] = ('读不到凭证：在终端手动运行一次本脚本即可修复'
                             + ('（钥匙串：%s）' % kc_err[:60] if kc_err else ''))
            return tool
        exp = oauth.get('expiresAt')
        if exp and exp < now_ms():
            refreshed = refresh_claude_cred(cred, state)
            if refreshed:
                oauth = refreshed['claudeAiOauth']
                token = oauth.get('accessToken')
            else:
                tool['error'] = '凭证已过期，自动续期暂未成功，稍后自动重试'
                return tool
        try:
            windows = _fetch_claude_windows(token, state)
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            # 令牌被提前作废：自动续期后重试一次
            refreshed = refresh_claude_cred(cred, state)
            if not refreshed:
                raise
            windows = _fetch_claude_windows(refreshed['claudeAiOauth'].get('accessToken'), state)
        if not windows:
            tool['error'] = 'usage 接口返回了无法识别的格式'
        else:
            tool['windows'] = windows
            tool['asOf'] = now_ms()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            tool['error'] = '登录失效且自动续期未成功：在终端 Claude Code 里 /login 一次可恢复'
        elif e.code == 429:
            tool['error'] = '接口限流，已自动退避，稍后恢复'
            state['claude_next_fetch_ms'] = now_ms() + 15 * 60 * 1000
        else:
            tool['error'] = 'usage 接口返回 HTTP %d' % e.code
    except urllib.error.URLError:
        tool['error'] = '连不上 Anthropic（网络波动），每分钟自动重试中'
        state['claude_next_fetch_ms'] = now_ms() + 55 * 1000
    except Exception as e:
        tool['error'] = '读取失败：' + type(e).__name__
        state['claude_next_fetch_ms'] = now_ms() + 55 * 1000
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
        zero_fallback = None
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
                if not windows:
                    continue
                if all(w['usedPercent'] == 0 for w in windows):
                    # Codex（尤其桌面版）会在会话初始化时写全 0 的额度记录，
                    # 不可信；继续找最新的非零记录，实在没有才采用
                    if zero_fallback is None:
                        zero_fallback = (windows, base)
                    continue
                tool['windows'] = windows
                tool['asOf'] = base
                return tool
        if zero_fallback:
            tool['windows'], tool['asOf'] = zero_fallback
            return tool
        tool['error'] = '未找到额度记录，先在 Codex 里跑一次任务'
    except Exception as e:
        tool['error'] = '读取失败：' + type(e).__name__
    return tool


def refresh_cred():
    """只刷新凭证缓存（供 Claude Code 启动钩子在用户会话内调用）。"""
    cred, kc_err = read_claude_credentials()
    oauth = (cred or {}).get('claudeAiOauth') or {}
    if oauth.get('accessToken'):
        exp = oauth.get('expiresAt')
        ok = not exp or exp > now_ms()
        print('凭证缓存已刷新：token ' + ('有效' if ok else '已过期（Claude Code 使用中会自动续期）'))
    else:
        print('未读到凭证' + ('（钥匙串：%s）' % kc_err[:60] if kc_err else ''))


def install_hook():
    """在 ~/.claude/settings.json 注册 SessionStart 钩子：
    每次使用 Claude Code（终端或桌面版）时自动刷新凭证缓存，
    后台上报永远拿得到新鲜凭证，不再需要手动跑脚本。"""
    if sys.platform != 'darwin':
        sys.exit('--install-hook 仅支持 macOS')
    import shutil
    settings_path = os.path.expanduser('~/.claude/settings.json')
    cmd = '/usr/bin/python3 %s --refresh-cred >/dev/null 2>&1 &' % os.path.expanduser('~/.quota-reporter.py')
    settings = {}
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except FileNotFoundError:
        pass
    except ValueError:
        sys.exit('~/.claude/settings.json 不是合法 JSON，为安全起见不自动修改，请手动检查')
    if 'quota-reporter' in json.dumps(settings):
        print('启动钩子已存在，无需重复安装')
        return
    if os.path.exists(settings_path):
        shutil.copy2(settings_path, settings_path + '.bak-quota')
    settings.setdefault('hooks', {}).setdefault('SessionStart', []).append(
        {'hooks': [{'type': 'command', 'command': cmd}]})
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    with open(settings_path, 'w') as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
    print('✅ 已安装 Claude Code 启动钩子：每次使用 Claude Code 自动刷新凭证缓存')
    if os.path.exists(settings_path + '.bak-quota'):
        print('   原配置已备份到 ~/.claude/settings.json.bak-quota')


def debug_network():
    """网络诊断：python3 quota-reporter.py --debug，把输出发给维护者定位问题。"""
    import socket
    print('== 系统 DNS 服务器 ==')
    if sys.platform == 'darwin':
        try:
            out = subprocess.run(['scutil', '--dns'], capture_output=True, text=True, timeout=5).stdout
            servers = sorted(set(re.findall(r'nameserver\[\d+\] : (\S+)', out)))
            print('  ' + (', '.join(servers) if servers else '(未识别)'))
        except Exception as e:
            print('  读取失败:', type(e).__name__)
    print('== 系统代理配置 ==')
    print('  getproxies:', urllib.request.getproxies() or '(无)')
    for host in ('api.anthropic.com', 'claude.ai', 'chatgpt.com', 'api.openai.com'):
        print('== %s ==' % host)
        try:
            infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
            ips = sorted(set(ai[4][0] for ai in infos))
        except Exception as e:
            print('  DNS 解析失败:', type(e).__name__, e)
            continue
        for ip in ips[:4]:
            try:
                s = socket.create_connection((ip, 443), timeout=5)
                s.close()
                result = 'TCP 443 可连接'
            except Exception as e:
                result = 'TCP 443 连不上（%s）' % type(e).__name__
            print('  %s  %s' % (ip.ljust(39), result))
    print('== 直连 HTTPS 测试（收到任何 HTTP 状态码都代表网络通） ==')
    for u in ('https://api.anthropic.com/api/oauth/usage', 'https://chatgpt.com/'):
        try:
            req = urllib.request.Request(u, headers={'User-Agent': 'daily-plan-debug/1.0'})
            with open_url(req, 12, 'DIRECT') as resp:
                print('  %s -> HTTP %s（通）' % (u, resp.status))
        except urllib.error.HTTPError as e:
            print('  %s -> HTTP %s（通）' % (u, e.code))
        except Exception as e:
            print('  %s -> 失败：%s %s' % (u, type(e).__name__, getattr(e, 'reason', e)))


def install_launch_agent():
    """安装为 macOS LaunchAgent，每分钟运行一次。
    必须用 LaunchAgent 而不是 cron：cron 运行在用户登录会话之外，
    读不到钥匙串里的 Claude Code 凭证。"""
    if sys.platform != 'darwin':
        sys.exit('--install 仅支持 macOS（Linux 请继续用 crontab）')
    if not SERVER:
        sys.exit('用法：python3 quota-reporter.py --install http://服务器IP:3000 [访问口令]')
    import plistlib
    script = os.path.abspath(__file__)
    plist_path = os.path.expanduser('~/Library/LaunchAgents/%s.plist' % AGENT_LABEL)
    args = ['/usr/bin/python3', script, SERVER] + ([ACCESS_CODE] if ACCESS_CODE else [])
    os.makedirs(os.path.dirname(plist_path), exist_ok=True)
    with open(plist_path, 'wb') as f:
        plistlib.dump({
            'Label': AGENT_LABEL,
            'ProgramArguments': args,
            'StartInterval': 60,
            'RunAtLoad': True,
            'LimitLoadToSessionType': 'Aqua',
            'StandardOutPath': '/tmp/quota-reporter.log',
            'StandardErrorPath': '/tmp/quota-reporter.log',
        }, f)
    subprocess.run(['launchctl', 'unload', plist_path], capture_output=True)
    r = subprocess.run(['launchctl', 'load', plist_path], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit('launchctl load 失败：' + (r.stderr or r.stdout))
    # 移除旧的 crontab 方式，避免重复上报
    cr = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
    if cr.returncode == 0 and 'quota-reporter' in cr.stdout:
        kept = '\n'.join(l for l in cr.stdout.splitlines() if 'quota-reporter' not in l)
        subprocess.run(['crontab', '-'], input=kept + '\n', text=True)
        print('已移除旧的 crontab 定时任务')
    print('✅ 已安装 LaunchAgent（%s），每分钟自动上报一次' % AGENT_LABEL)
    print('   运行日志：/tmp/quota-reporter.log')
    print('   卸载方法：launchctl unload %s && rm %s' % (plist_path, plist_path))


def main():
    if DEBUG_MODE:
        return debug_network()
    if REFRESH_CRED_MODE:
        return refresh_cred()
    if INSTALL_HOOK_MODE:
        return install_hook()
    if INSTALL_MODE:
        return install_launch_agent()
    if not SERVER:
        sys.exit('用法：python3 quota-reporter.py http://服务器IP:3000 [访问口令]')
    headers = {'Content-Type': 'application/json'}
    if ACCESS_CODE:
        headers['X-Access-Code'] = ACCESS_CODE
    state = load_state()
    # 数据合并在服务器端完成（多机互不覆盖、异常不清空）；
    # 退避期跳过的工具（reuse）直接不上报，服务器保留原值
    tools = [t for t in (claude_quota(state), codex_quota()) if not t.get('reuse')]
    save_state(state)
    payload = {'tools': tools, 'reportedAt': now_ms()}
    req = urllib.request.Request(SERVER + '/api/quota',
                                 data=json.dumps(payload).encode(),
                                 headers=headers, method='POST')
    try:
        with open_url(req, 15, 'DIRECT') as resp:   # 服务器在国内，固定直连
            resp.read()
    except Exception as e:
        sys.exit('上报失败（%s）：连不上工作计划服务器，下一分钟自动重试' % type(e).__name__)
    print('已上报：', json.dumps(payload, ensure_ascii=False))


if __name__ == '__main__':
    main()

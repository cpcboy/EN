# 每日工作计划 · Daily Mission Plan

SpaceX 风格（白底黑字、宇宙与火箭线稿插画）的每日任务面板：

- **iPad 横放在桌面常显**：大字时钟 + 今日任务 + 完成进度，页面每 5 秒自动同步，跨天自动翻页
- **iPhone / Mac 随时查看和编辑**：打开同一个网址即可增删改任务，改动几秒内出现在 iPad 上
- **零依赖**：服务端只需要 Node.js，一个文件跑起来，数据存在 `data/tasks.json`
- 支持：任务时间、重点标记（▲）、排序、昨日未完成一键移到今天、按日期翻页提前规划

## 目录结构

```
daily-plan/
├── server.js            # 服务端（Node.js，无任何第三方依赖）
├── daily-plan.service   # systemd 开机自启配置
├── public/              # 前端页面
└── data/tasks.json      # 任务数据（运行后自动生成）
```

## 本地试运行

```bash
cd daily-plan
node server.js          # 打开 http://localhost:3000
```

## 部署到阿里云服务器

### 方式一：一键脚本（推荐）

以 root 登录服务器（SSH 或阿里云控制台的 Workbench 远程连接均可），粘贴执行：

```bash
curl -fsSL --connect-timeout 8 https://ghfast.top/https://raw.githubusercontent.com/cpcboy/EN/claude/practical-cray-oe82xr/daily-plan/install.sh -o /tmp/dp.sh \
 || curl -fsSL --connect-timeout 8 https://raw.githubusercontent.com/cpcboy/EN/claude/practical-cray-oe82xr/daily-plan/install.sh -o /tmp/dp.sh \
 || curl -fsSL --connect-timeout 8 https://mirror.ghproxy.com/https://raw.githubusercontent.com/cpcboy/EN/claude/practical-cray-oe82xr/daily-plan/install.sh -o /tmp/dp.sh
bash /tmp/dp.sh
```

脚本会自动：装 Node.js（走国内镜像）→ 下载代码到 `/opt/daily-plan` → 配置 systemd 开机自启 → 启动并自检。重复执行即为升级，数据不会丢。

可选参数：`DP_PORT=3000 DP_ACCESS_CODE=你的口令 bash /tmp/dp.sh`

### 方式二：手动部署

```bash
# 1. 安装 Node.js（已安装可跳过，node -v 显示 v14 以上即可）
sudo apt update && sudo apt install -y nodejs

# 2. 上传代码到 /opt/daily-plan（本机执行，把 IP 换成你的服务器）
scp -r daily-plan root@你的服务器IP:/opt/

# 3. 设置开机自启并启动（服务器上执行）
cd /opt/daily-plan
sudo cp daily-plan.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now daily-plan
systemctl status daily-plan     # 看到 active (running) 即成功
```

**最后一步：开放端口。** 在阿里云控制台 → ECS → 安全组 → 入方向规则，放行 TCP **3000** 端口（授权对象 `0.0.0.0/0`）。

然后在任何设备的浏览器打开：`http://你的服务器IP:3000`

### 更新版本

```bash
scp -r daily-plan root@你的服务器IP:/opt/    # 重新上传（data/ 不会被覆盖删除）
sudo systemctl restart daily-plan
```

## iPad 桌面常显设置（重要）

1. 用 **Safari** 打开 `http://你的服务器IP:3000`
2. 点分享按钮 → **添加到主屏幕**，从主屏幕图标打开 → 全屏无地址栏，像一个 App
3. 设置 → 显示与亮度 → **自动锁定 → 永不**（iPad 一直插电摆在桌面）
4. 可选：设置 → 辅助功能 → **引导式访问** 打开后三击电源键锁定在本应用，防止误触
5. iPad 横放效果最佳：左边时钟 + 进度 + 火箭，右边任务清单

## iPhone / Mac 使用

- iPhone：同样用 Safari 打开后「添加到主屏幕」，随时点开增删任务
- Mac：浏览器直接打开网址，收藏为书签即可
- 所有设备看到的是同一份数据，任何一端修改，其他设备几秒内自动刷新

## 可选配置

| 配置 | 方法 |
|------|------|
| 换端口 | `daily-plan.service` 里改 `Environment=PORT=3000` 后 `sudo systemctl daemon-reload && sudo systemctl restart daily-plan` |
| 访问口令 | 服务文件里取消注释 `Environment=ACCESS_CODE=你的口令`，重启服务。之后每台设备首次打开会要求输一次口令（服务器在公网上，建议设置） |
| 域名 + HTTPS | 用 nginx 反代 3000 端口并配证书。配了 HTTPS 后网页还能自动申请「屏幕常亮」，无需改自动锁定设置 |

## 可选：显示 Claude Code / Codex 额度

页面左下角的「SYSTEMS · 额度监控」可以灰色显示两个工具的 5 小时 / 7 天剩余额度。
数据由你的 **Mac** 每分钟上报（额度凭证都在 Mac 本地，服务器不保存任何令牌）。

在 Mac 终端执行（把 IP 换成你的服务器）：

```bash
curl -fsSL http://你的服务器IP:3000/quota-reporter.py -o ~/.quota-reporter.py
python3 ~/.quota-reporter.py --install http://你的服务器IP:3000
```

`--install` 会注册一个每分钟运行的 **LaunchAgent**（macOS 必须用它而不是 cron：
cron 在用户登录会话之外运行，读不到钥匙串里的 Claude Code 凭证），
并自动清理旧的 crontab 方式。运行日志在 `/tmp/quota-reporter.log`。

前提：Mac 上登录过 Claude Code；Codex 至少跑过一次任务（额度取自其最近会话记录）。
设置了访问口令的话，命令末尾追加口令参数：`… http://IP:3000 你的口令`。
Mac 关机/睡眠期间不上报，页面会显示「未在更新」，开机后自动恢复。
排查网络问题可运行：`python3 ~/.quota-reporter.py --debug`。

**多台电脑**：在每台 Mac 上重复上面两条命令即可。Claude 额度是账号级的，任一台
上报的都是总量；Codex 额度取自各机器本地记录。合并在**服务器端**按工具逐个进行：
旧数据不覆盖新数据、全 0 占位记录不采纳、错误上报不清空已有数值。

**推荐再装一个自愈钩子**（每台 Mac 一次）：

```bash
python3 ~/.quota-reporter.py --install-hook
```

它在 Claude Code 里注册一个启动钩子：每次使用 Claude Code（终端或桌面版）时自动
刷新凭证缓存。此后凭证每日轮换也无需手动干预，面板即使显示「凭证已过期」，
用一次 Claude Code 就自动恢复。原配置会先备份到 `settings.json.bak-quota`。

## 数据备份

所有任务都在一个文件里：`/opt/daily-plan/data/tasks.json`，定期复制走即可。

## 常见问题

- **打不开网页**：先在服务器上 `curl http://localhost:3000/api/health`，正常说明服务在跑，多半是安全组端口没放行
- **iPad 不刷新**：页面每 5 秒自动拉取一次；如果显示 OFFLINE，检查服务器或 Wi-Fi
- **服务挂了**：systemd 会 3 秒内自动拉起；手动看日志 `journalctl -u daily-plan -f`

#!/usr/bin/env bash
# 每日工作计划 · 一键安装 / 更新脚本（在服务器上以 root 运行）
# 用法：bash install.sh
# 可选环境变量：
#   DP_PORT=3000        服务端口
#   DP_ACCESS_CODE=xxx  访问口令（设置后打开网页需输一次口令）
set -euo pipefail

REPO="cpcboy/EN"
BRANCH="claude/practical-cray-oe82xr"
DEST="${DP_DEST:-/opt/daily-plan}"
PORT="${DP_PORT:-3000}"
ACCESS_CODE="${DP_ACCESS_CODE:-}"
NODE_VER="v20.18.0"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

log()  { printf '\033[1m[安装]\033[0m %s\n' "$*"; }
fail() { printf '\033[1m[失败]\033[0m %s\n' "$*" >&2; exit 1; }

dl() { # dl <输出文件> <URL...>：依次尝试多个地址
  local out="$1"; shift
  local u
  for u in "$@"; do
    log "下载 $u"
    if curl -fL --connect-timeout 10 --retry 2 -o "$out" "$u"; then return 0; fi
    log "该地址不可用，换下一个 …"
  done
  return 1
}

# ---------- 1. 确保 Node.js ----------
ensure_node() {
  if command -v node >/dev/null 2>&1; then
    local major
    major="$(node -p 'process.versions.node.split(".")[0]')"
    if [ "$major" -ge 14 ]; then
      log "Node.js 已安装：$(node -v)"
      return 0
    fi
    log "Node.js 版本过旧（$(node -v)），安装新版 …"
  else
    log "未检测到 Node.js，开始安装 …"
  fi

  local arch
  case "$(uname -m)" in
    x86_64)  arch=x64 ;;
    aarch64) arch=arm64 ;;
    *) fail "不支持的架构：$(uname -m)" ;;
  esac
  local pkg="node-${NODE_VER}-linux-${arch}"
  dl "$TMP/node.tar.gz" \
    "https://npmmirror.com/mirrors/node/${NODE_VER}/${pkg}.tar.gz" \
    "https://mirrors.tuna.tsinghua.edu.cn/nodejs-release/${NODE_VER}/${pkg}.tar.gz" \
    "https://nodejs.org/dist/${NODE_VER}/${pkg}.tar.gz" \
    || fail "Node.js 下载失败，请检查服务器外网"
  tar -xzf "$TMP/node.tar.gz" -C /usr/local/
  ln -sf "/usr/local/${pkg}/bin/node" /usr/local/bin/node
  ln -sf "/usr/local/${pkg}/bin/npm"  /usr/local/bin/npm
  log "Node.js 安装完成：$(/usr/local/bin/node -v)"
}

# ---------- 2. 获取代码 ----------
fetch_code() {
  # 自包含版会把代码内嵌在脚本里；标准版从 GitHub（含国内镜像）下载
  local tarball="https://github.com/${REPO}/archive/refs/heads/${BRANCH}.tar.gz"
  dl "$TMP/src.tar.gz" \
    "$tarball" \
    "https://ghfast.top/${tarball}" \
    "https://mirror.ghproxy.com/${tarball}" \
    "https://gh-proxy.com/${tarball}" \
    || fail "代码下载失败：GitHub 及镜像均不可达。请改用『自包含安装包』方式（见部署说明）"
  tar -xzf "$TMP/src.tar.gz" -C "$TMP"
  SRC_DIR="$(find "$TMP" -maxdepth 3 -type d -name daily-plan | head -1)"
  [ -n "$SRC_DIR" ] || fail "压缩包里没有找到 daily-plan 目录"
}

# ---------- 3. 安装文件（保留 data/ 数据） ----------
install_files() {
  mkdir -p "$DEST"
  cp -a "$SRC_DIR/server.js" "$DEST/"
  rm -rf "$DEST/public"
  cp -a "$SRC_DIR/public" "$DEST/"
  log "代码已安装到 $DEST（数据目录 data/ 不受影响）"
}

# ---------- 4. systemd 服务 ----------
setup_service() {
  if ! command -v systemctl >/dev/null 2>&1 || ! systemctl list-units >/dev/null 2>&1; then
    log "无可用 systemd，改用后台进程方式启动"
    pkill -f "node ${DEST}/server.js" 2>/dev/null || true
    (cd "$DEST" && PORT="$PORT" ACCESS_CODE="$ACCESS_CODE" nohup node "$DEST/server.js" > "$DEST/server.log" 2>&1 &)
    return 0
  fi
  local code_line=""
  [ -n "$ACCESS_CODE" ] && code_line="Environment=ACCESS_CODE=${ACCESS_CODE}"
  cat > /etc/systemd/system/daily-plan.service <<EOF
[Unit]
Description=Daily Mission Plan (每日工作计划)
After=network.target

[Service]
WorkingDirectory=${DEST}
ExecStart=/usr/bin/env node server.js
Restart=always
RestartSec=3
Environment=PORT=${PORT}
${code_line}

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable daily-plan >/dev/null 2>&1 || true
  systemctl restart daily-plan
  log "systemd 服务已启动（开机自启）"
}

# ---------- 5. 尽量放行本机防火墙（阿里云安全组仍需在控制台放行） ----------
open_firewall() {
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "${PORT}/tcp" >/dev/null && log "已放行 ufw 端口 ${PORT}"
  fi
  if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port="${PORT}/tcp" >/dev/null && firewall-cmd --reload >/dev/null \
      && log "已放行 firewalld 端口 ${PORT}"
  fi
}

# ---------- 6. 健康检查 ----------
health_check() {
  sleep 1
  local i
  for i in 1 2 3 4 5; do
    if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
      echo
      log "✅ 部署成功！服务运行正常。"
      log "浏览器打开：http://$(curl -fsS --max-time 5 ifconfig.me 2>/dev/null || echo 服务器IP):${PORT}"
      log "若外网打不开：阿里云控制台 → ECS → 安全组 → 入方向放行 TCP ${PORT} 端口"
      return 0
    fi
    sleep 1
  done
  fail "服务未响应，查看日志：journalctl -u daily-plan -n 50"
}

ensure_node
if [ -z "${DP_EMBEDDED_SRC:-}" ]; then fetch_code; else SRC_DIR="$DP_EMBEDDED_SRC"; fi
install_files
setup_service
open_firewall
health_check

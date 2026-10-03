#!/usr/bin/env bash
# 中国象棋联网对战：一键把本机服务暴露到公网（Linux / macOS / WSL）。
# 只做部署，不改业务代码。用法：
#   bash tools/serve-public.sh              # Cloudflare 隧道（跨国）
#   bash tools/serve-public.sh --lan        # 局域网直连（监听 0.0.0.0）
#   PORT=9000 bash tools/serve-public.sh    # 换端口
#   NO_SERVER=1 bash tools/serve-public.sh  # 只开隧道，不启动/检查本机服务
set -euo pipefail

PORT="${PORT:-8000}"
LAN=0
for arg in "$@"; do
    case "$arg" in
        --lan) LAN=1 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "未知参数：$arg（可用：--lan）" >&2; exit 2 ;;
    esac
done

TOOLS_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$TOOLS_DIR")"
BIN_DIR="$TOOLS_DIR/bin"
URL_FILE="$TOOLS_DIR/public-url.txt"
LOG_FILE="${TMPDIR:-/tmp}/xiangqi-tunnel.log"

if [ "$LAN" -eq 1 ]; then BIND_HOST="0.0.0.0"; else BIND_HOST="127.0.0.1"; fi

info()  { printf '%s\n' "$*"; }
ok()    { printf '\033[32m%s\033[0m\n' "$*"; }
warn()  { printf '\033[33m%s\033[0m\n' "$*"; }
die()   { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

server_alive() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 3 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1
    else
        python - "$PORT" <<'PY' >/dev/null 2>&1
import sys, urllib.request
urllib.request.urlopen("http://127.0.0.1:%s/api/health" % sys.argv[1], timeout=3)
PY
    fi
}

# ---------------------------------------------------------------- cloudflared
cf_asset() {
    local os arch
    os="$(uname -s | tr '[:upper:]' '[:lower:]')"
    arch="$(uname -m)"
    case "$os" in
        linux)  os="linux" ;;
        darwin) os="darwin" ;;
        *) die "不认识的操作系统：$os（请手动安装 cloudflared 后重试）" ;;
    esac
    case "$arch" in
        x86_64|amd64) arch="amd64" ;;
        aarch64|arm64) arch="arm64" ;;
        *) die "不认识的架构：$arch（请手动安装 cloudflared 后重试）" ;;
    esac
    printf 'cloudflared-%s-%s' "$os" "$arch"
}

cf_ensure() {
    if command -v cloudflared >/dev/null 2>&1; then
        CF_BIN="$(command -v cloudflared)"
        ok "使用系统已安装的 cloudflared：$CF_BIN"
        return
    fi
    local asset="$BIN_DIR/cloudflared"
    if [ -x "$asset" ]; then
        CF_BIN="$asset"
        ok "使用本地已下载的 cloudflared：$asset"
        return
    fi
    mkdir -p "$BIN_DIR"
    local name base
    name="$(cf_asset)"
    base="https://github.com/cloudflare/cloudflared/releases/latest/download/$name"
    info "正在下载 cloudflared（约 40-60 MB）…"
    if ! curl -fL --retry 2 --connect-timeout 20 -o "$asset" "$base"; then
        warn "从 github.com 下载失败，改用 GitHub API 取直链（可能被限速）…"
        local api url
        api="https://api.github.com/repos/cloudflare/cloudflared/releases/latest"
        url="$(curl -fsSL -H 'User-Agent: xiangqi-deploy' "$api" \
            | tr ',' '\n' | grep -o "https[^\"]*$name" | head -n 1 || true)"
        [ -n "$url" ] || die "下载失败。请手动安装 cloudflared（Linux: apt/yum 官方源；macOS: brew install cloudflared），或手动下载后放到 $asset"
        curl -fL --retry 2 -H 'User-Agent: xiangqi-deploy' -o "$asset" "$url" \
            || die "下载失败：$url"
    fi
    chmod +x "$asset"
    CF_BIN="$asset"
    ok "cloudflared 已就绪（$(command -v sha256sum >/dev/null 2>&1 && sha256sum "$asset" | cut -d' ' -f1 || shasum -a 256 "$asset" | cut -d' ' -f1)）"
    warn "请与 Cloudflare 官方下载页面公布的校验值比对；本机为 Linux/macOS，无法用 Authenticode 验证。"
}

cleanup() {
    if [ -n "${TUNNEL_PID:-}" ] && kill -0 "$TUNNEL_PID" 2>/dev/null; then
        kill "$TUNNEL_PID" 2>/dev/null || true
        info "隧道已关闭。"
    fi
    if [ -n "${SERVER_PID:-}" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null || true
        info "本脚本启动的游戏服务器已关闭。"
    fi
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------- 本机服务
if [ "${NO_SERVER:-0}" != "1" ]; then
    if server_alive; then
        ok "检测到本机 $PORT 端口已有服务，直接复用。"
    else
        command -v python >/dev/null 2>&1 || die "找不到 python"
        info "正在启动本机服务：python main.py --serve --host $BIND_HOST --port $PORT"
        ( cd "$PROJECT_DIR" && PYTHONUNBUFFERED=1 python main.py --serve --host "$BIND_HOST" --port "$PORT" \
            >"${TMPDIR:-/tmp}/xiangqi-server.log" 2>&1 & echo $! >"${TMPDIR:-/tmp}/xiangqi-server.pid" )
        SERVER_PID="$(cat "${TMPDIR:-/tmp}/xiangqi-server.pid")"
        for _ in $(seq 1 40); do server_alive && break; sleep 0.5; done
        server_alive || die "服务启动超时，请看 ${TMPDIR:-/tmp}/xiangqi-server.log"
        ok "游戏服务器已就绪：http://127.0.0.1:$PORT/"
    fi
fi

if [ "$LAN" -eq 1 ]; then
    LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    [ -n "$LAN_IP" ] || LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || echo '<本机IP>')"
    cat <<EOF

==================== 局域网直连模式 ====================
把下面地址发给同一网络里的朋友： http://$LAN_IP:$PORT/
自己也可以继续用： http://127.0.0.1:$PORT/
若对方连不上，放行防火墙（择一）：
  ufw allow $PORT/tcp            # Debian/Ubuntu
  firewall-cmd --add-port=$PORT/tcp   # RHEL/CentOS
跨国请去掉 --lan（走 Cloudflare 隧道）。
=======================================================
EOF
    if [ -n "${SERVER_PID:-}" ]; then info '按 Ctrl+C 结束（本脚本启动的服务器会一并关闭）。'; fi
    wait "${SERVER_PID:-0}" 2>/dev/null || sleep infinity
    exit 0
fi

cf_ensure

info "正在建立 Cloudflare 隧道 → http://127.0.0.1:$PORT …"
: >"$LOG_FILE"
"$CF_BIN" tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate --loglevel info \
    >"$LOG_FILE" 2>&1 &
TUNNEL_PID=$!

URL=""
for _ in $(seq 1 120); do
    kill -0 "$TUNNEL_PID" 2>/dev/null || break
    URL="$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG_FILE" | head -n 1 || true)"
    [ -n "$URL" ] && break
    sleep 0.5
done
[ -n "$URL" ] || { warn "60 秒内没拿到公网地址，隧道日志末尾："; tail -n 20 "$LOG_FILE" || true; die "隧道建立失败（常见原因：本机 UDP 7844/TCP 443 被封，可给 cloudflared 加 --protocol http2）"; }
printf '%s\n' "$URL" >"$URL_FILE"

cat <<EOF

================ 公网对战已就绪（Cloudflare 隧道） ================
发给国外朋友： $URL/
朋友打开后：点「加入房间」并输入你的 6 位房间号（或直接点你发的 ?room=XXXXXX 链接）。
你自己也打开： $URL/  → 点「创建房间」→ 把房间号/链接发给对方

传输说明（经 Cloudflare 快速隧道的实测结论）：
  · WebSocket 正常 —— 浏览器会自动优先用它，对战体验最好；
  · 长轮询正常 —— 备用通道；
  · SSE 会被快速隧道整包缓冲（不实时），客户端约 60 秒后自动降级到长轮询，不影响能下棋。

公网地址也保存在： $URL_FILE
注意：快速隧道是临时地址（重启隧道就变），无 SLA；对局数据全在你本机内存里。
按 Ctrl+C 结束隧道。
=================================================================
EOF

wait "$TUNNEL_PID"

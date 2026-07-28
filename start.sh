#!/bin/sh
set -eu

DATA_DIR="${DATA_DIR:-/data}"
MCP_TOKEN="${MCP_TOKEN:-}"
VNC_USERNAME="${VNC_USERNAME:-shanshan}"
VNC_PASSWORD="${VNC_PASSWORD:-}"

fail() {
    echo "[startup error] $1" >&2
    exit 1
}

[ -n "$MCP_TOKEN" ] || fail "MCP_TOKEN is required"
[ "${#MCP_TOKEN}" -ge 32 ] || fail "MCP_TOKEN must be at least 32 characters"
case "$MCP_TOKEN" in
    *[!A-Za-z0-9._~-]*)
        fail "MCP_TOKEN may only contain letters, numbers, dot, underscore, tilde and hyphen"
        ;;
esac

[ -n "$VNC_USERNAME" ] || fail "VNC_USERNAME is required"
case "$VNC_USERNAME" in
    *[!A-Za-z0-9._-]*)
        fail "VNC_USERNAME may only contain letters, numbers, dot, underscore and hyphen"
        ;;
esac

[ -n "$VNC_PASSWORD" ] || fail "VNC_PASSWORD is required"
[ "${#VNC_PASSWORD}" -ge 12 ] || fail "VNC_PASSWORD must be at least 12 characters"

mkdir -p "$DATA_DIR/tmp" "$DATA_DIR/browser-profile"
export TMPDIR="$DATA_DIR/tmp"

# 生成 noVNC 网页 Basic Auth 与内部 VNC 密码文件
htpasswd -bc "$DATA_DIR/.htpasswd" "$VNC_USERNAME" "$VNC_PASSWORD" >/dev/null
x11vnc -storepasswd "$VNC_PASSWORD" "$DATA_DIR/vnc.pass" >/dev/null
chmod 600 "$DATA_DIR/.htpasswd" "$DATA_DIR/vnc.pass"

# 只替换 MCP_TOKEN，保留 nginx 自己的 $变量
rm -f /etc/nginx/sites-enabled/default /etc/nginx/conf.d/default.conf
envsubst '${MCP_TOKEN}' \
    < /etc/nginx/templates/browser.conf.template \
    > /etc/nginx/conf.d/browser.conf

nginx -t

# 启动虚拟显示器
Xvfb :99 -screen 0 1280x900x24 &
XVFB_PID=$!
export DISPLAY=:99
sleep 2

# 启动带密码的 VNC 服务；关闭 X DAMAGE，避免云端虚拟显示器黑屏或不刷新
x11vnc \
    -display :99 \
    -rfbauth "$DATA_DIR/vnc.pass" \
    -noxdamage \
    -forever \
    -shared \
    -rfbport 5900 &
VNC_PID=$!
sleep 2

# 启动 noVNC，仅供 nginx 反向代理
websockify --web=/usr/share/novnc 6080 127.0.0.1:5900 &
WEBSOCKIFY_PID=$!
sleep 1

# MCP 服务运行在容器内部 8081，由 nginx 的 /mcp 暴露
PORT=8081 python main.py &
MCP_PID=$!

# nginx 必须持续监听 Zeabur 对外暴露的 8080；使用前台模式并纳入进程监控
nginx -g 'daemon off;' &
NGINX_PID=$!

cleanup() {
    trap - EXIT INT TERM
    kill "$NGINX_PID" "$MCP_PID" "$WEBSOCKIFY_PID" "$VNC_PID" "$XVFB_PID" 2>/dev/null || true
    wait "$NGINX_PID" "$MCP_PID" "$WEBSOCKIFY_PID" "$VNC_PID" "$XVFB_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# 任一关键进程退出，都让容器退出并由 Zeabur 自动重启，避免表面运行却持续 502
while :; do
    kill -0 "$XVFB_PID" 2>/dev/null || fail "Xvfb exited unexpectedly"
    kill -0 "$VNC_PID" 2>/dev/null || fail "x11vnc exited unexpectedly"
    kill -0 "$WEBSOCKIFY_PID" 2>/dev/null || fail "websockify exited unexpectedly"
    kill -0 "$MCP_PID" 2>/dev/null || fail "MCP server exited unexpectedly"
    kill -0 "$NGINX_PID" 2>/dev/null || fail "nginx exited unexpectedly"
    sleep 2
done

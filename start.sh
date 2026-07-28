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
export DISPLAY=:99
sleep 2

# 启动带密码的 VNC 服务
x11vnc \
    -display :99 \
    -rfbauth "$DATA_DIR/vnc.pass" \
    -forever \
    -shared \
    -rfbport 5900 &
sleep 2

# 启动 noVNC，仅供 nginx 反向代理
websockify --web=/usr/share/novnc 6080 127.0.0.1:5900 &
sleep 1

nginx

# MCP 服务运行在容器内部 8081，由 nginx 的 /mcp 暴露
PORT=8081 exec python main.py

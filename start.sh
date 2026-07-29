#!/bin/sh
 
# 把临时目录重定向到可写路径
mkdir -p /data/tmp
export TMPDIR=/data/tmp

# 清理上一个 Zeabur 容器遗留的 Chromium 临时锁
mkdir -p /data/browser-profile
rm -f /data/browser-profile/SingletonLock
rm -f /data/browser-profile/SingletonSocket
rm -f /data/browser-profile/SingletonCookie
 
# 启动虚拟显示器
Xvfb :99 -screen 0 1280x900x24 &
export DISPLAY=:99
sleep 3
 
# 启动 VNC 服务
x11vnc -display :99 -nopw -forever -shared -noxdamage -rfbport 5900 &
sleep 2
 
# 启动 noVNC
websockify --web=/usr/share/novnc 6080 localhost:5900 &
sleep 1
 
# 启动 nginx
nginx &
 
# 启动 MCP 服务
PORT=8081 python main.py

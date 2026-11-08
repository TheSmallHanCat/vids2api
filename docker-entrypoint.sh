#!/usr/bin/env bash
set -e

cd /app
mkdir -p /app/data

SECRET_FILE=/app/data/vnc_secret
if [ ! -s "$SECRET_FILE" ]; then
  python -c 'import secrets; print(secrets.token_hex(4))' > "$SECRET_FILE"
  chmod 600 "$SECRET_FILE"
fi
VNC_PASS="$(cat "$SECRET_FILE")"

Xvfb :99 -screen 0 1280x800x24 -nolisten tcp &
x11vnc -display :99 -rfbport 5901 -localhost -shared -forever -passwd "$VNC_PASS" > /tmp/x11vnc.log 2>&1 &
websockify --web /usr/share/novnc "${VIDS2API_VNC_PORT:-8690}" 127.0.0.1:5901 > /tmp/websockify.log 2>&1 &

exec python main.py

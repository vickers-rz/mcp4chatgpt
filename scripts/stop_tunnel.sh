#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$ROOT/tmp.cloudflared.pid"

if [ ! -f "$PID_FILE" ]; then
  echo "Cloudflare Tunnel is not running: no pid file"
  exit 0
fi

pid="$(cat "$PID_FILE" 2>/dev/null || true)"
if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "Cloudflare Tunnel is not running: stale pid file removed"
  exit 0
fi

kill "$pid" 2>/dev/null || true
for _ in 1 2 3 4 5; do
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "Cloudflare Tunnel stopped"
    exit 0
  fi
  sleep 1
done

kill -9 "$pid" 2>/dev/null || true
rm -f "$PID_FILE"
echo "Cloudflare Tunnel force-stopped"

#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$ROOT/tmp.cloudflared.pid"
OUT_LOG="$ROOT/logs/cloudflared.out.log"
ERR_LOG="$ROOT/logs/cloudflared.err.log"
CONFIG="$ROOT/deploy/cloudflared-mcp4chatgpt.yml"
CLOUDFLARED="/opt/homebrew/bin/cloudflared"

mkdir -p "$ROOT/logs"

if [ ! -x "$CLOUDFLARED" ]; then
  echo "cloudflared not found at $CLOUDFLARED"
  exit 1
fi

if [ -f "$PID_FILE" ]; then
  old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
    echo "Cloudflare Tunnel is already running: pid=$old_pid"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

nohup "$CLOUDFLARED" tunnel --config "$CONFIG" run mcp4chatgpt > "$OUT_LOG" 2> "$ERR_LOG" &
pid="$!"
echo "$pid" > "$PID_FILE"

ok=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  sleep 1
  if kill -0 "$pid" 2>/dev/null && curl -fsS https://mcp.runzhe.uk/health >/dev/null 2>&1; then
    ok=1
    break
  fi
done

if [ "$ok" = "1" ]; then
  echo "Cloudflare Tunnel started: pid=$pid"
  echo "Public health: https://mcp.runzhe.uk/health"
  exit 0
fi

echo "Cloudflare Tunnel started but public health is not ready yet: pid=$pid"
echo "Check logs:"
echo "  $OUT_LOG"
echo "  $ERR_LOG"

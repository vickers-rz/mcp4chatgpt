#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$ROOT/tmp.caddy.pid"
OUT_LOG="$ROOT/logs/caddy.out.log"
ERR_LOG="$ROOT/logs/caddy.err.log"
CADDY_BIN="${CADDY_BIN:-/opt/homebrew/bin/caddy}"
CADDYFILE="$ROOT/deploy/Caddyfile.local"

mkdir -p "$ROOT/logs" "$ROOT/data/caddy"

if [ ! -x "$CADDY_BIN" ]; then
  echo "Caddy not found at $CADDY_BIN"
  exit 1
fi

if [ -f "$PID_FILE" ]; then
  old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
    echo "Caddy proxy is already running: pid=$old_pid"
    exit 0
  fi
fi

cd "$ROOT"
XDG_DATA_HOME="$ROOT/data/caddy" HOME="$ROOT/data/caddy" nohup "$CADDY_BIN" run --config "$CADDYFILE" > "$OUT_LOG" 2> "$ERR_LOG" &
pid="$!"
echo "$pid" > "$PID_FILE"
sleep 2

if kill -0 "$pid" 2>/dev/null; then
  echo "Caddy proxy started: pid=$pid"
  echo "Legacy IPv6/DDNS health target: https://m6.ic2id.fun/health"
  echo "Recommended ChatGPT Web path uses Cloudflare Tunnel: https://mcp.runzhe.uk/mcp"
  exit 0
fi

echo "Caddy proxy failed to start. stderr:"
sed -n '1,160p' "$ERR_LOG" 2>/dev/null || true
exit 1

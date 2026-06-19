#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$ROOT/tmp.cloudflared.pid"
OUT_LOG="$ROOT/logs/cloudflared.out.log"
ERR_LOG="$ROOT/logs/cloudflared.err.log"
CONFIG="$ROOT/deploy/cloudflared-mcp4chatgpt.yml"
CLOUDFLARED="/opt/homebrew/bin/cloudflared"
TMUX_SESSION="mcp4chatgpt-cloudflared"
TUNNEL_PATTERN="[c]loudflared tunnel --config $CONFIG run mcp4chatgpt"

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

if command -v tmux >/dev/null 2>&1; then
  if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    detected_pid="$(pgrep -f "$TUNNEL_PATTERN" | head -n 1 || true)"
    if [ -n "$detected_pid" ] && curl -fsS https://mcp.runzhe.uk/health >/dev/null 2>&1; then
      echo "$detected_pid" > "$PID_FILE"
      echo "Cloudflare Tunnel is already running in tmux: pid=$detected_pid"
      exit 0
    fi
    tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
  fi

  tmux new-session -d -s "$TMUX_SESSION" -c "$ROOT" \
    "$CLOUDFLARED tunnel --config '$CONFIG' run mcp4chatgpt >> '$OUT_LOG' 2>> '$ERR_LOG'"
else
  nohup "$CLOUDFLARED" tunnel --config "$CONFIG" run mcp4chatgpt > "$OUT_LOG" 2> "$ERR_LOG" &
fi

ok=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  sleep 1
  pid="$(pgrep -f "$TUNNEL_PATTERN" | head -n 1 || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && curl -fsS https://mcp.runzhe.uk/health >/dev/null 2>&1; then
    echo "$pid" > "$PID_FILE"
    ok=1
    break
  fi
done

if [ "$ok" = "1" ]; then
  echo "Cloudflare Tunnel started: pid=$pid"
  echo "Public health: https://mcp.runzhe.uk/health"
  exit 0
fi

rm -f "$PID_FILE"
echo "Cloudflare Tunnel started but public health is not ready yet."
echo "Check logs:"
echo "  $OUT_LOG"
echo "  $ERR_LOG"

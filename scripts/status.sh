#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$ROOT/tmp.service.pid"
LAUNCHD_LABEL="com.vickers.mcp4chatgpt"
LAUNCHD_SERVICE="gui/$(id -u)/$LAUNCHD_LABEL"
TMUX_SESSION="mcp4chatgpt"

status="stopped"
pid=""
launchd_status="not loaded"
if [ "$(uname -s)" = "Darwin" ] && command -v launchctl >/dev/null 2>&1; then
  if launchctl print "$LAUNCHD_SERVICE" >/dev/null 2>&1; then
    launchd_status="loaded"
  fi
fi
tmux_status="not running"
if command -v tmux >/dev/null 2>&1 && tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
  tmux_status="running"
fi

if [ -f "$PID_FILE" ]; then
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    status="running"
  else
    rm -f "$PID_FILE"
    pid=""
  fi
fi

# Foreground starts from Codex/Terminal do not create tmp.service.pid. Fall
# back to process detection so status reflects the actual runtime state.
if [ "$status" = "stopped" ]; then
  detected_pid="$(pgrep -f "mcp4chatgpt.server" | head -n 1 || true)"
  if [ -n "$detected_pid" ]; then
    status="running"
    pid="$detected_pid"
  fi
fi

echo "Status: $status"
echo "Launchd: $launchd_status"
echo "Tmux: $tmux_status"
if [ -n "$pid" ]; then
  echo "PID: $pid"
fi

if curl -fsS http://127.0.0.1:8766/health >/dev/null 2>&1; then
  echo "Health: ok"
else
  echo "Health: unavailable"
fi

echo "Public URL target: https://mcp.runzhe.uk/mcp"
if [ -f "$ROOT/tmp.caddy.pid" ]; then
  caddy_pid="$(cat "$ROOT/tmp.caddy.pid" 2>/dev/null || true)"
  if [ -n "$caddy_pid" ] && kill -0 "$caddy_pid" 2>/dev/null; then
    echo "Proxy: running pid=$caddy_pid"
  else
    rm -f "$ROOT/tmp.caddy.pid"
    echo "Proxy: stopped"
  fi
else
  echo "Proxy: stopped"
fi
if [ -f "$ROOT/tmp.cloudflared.pid" ]; then
  tunnel_pid="$(cat "$ROOT/tmp.cloudflared.pid" 2>/dev/null || true)"
  if [ -n "$tunnel_pid" ] && kill -0 "$tunnel_pid" 2>/dev/null; then
    echo "Cloudflare Tunnel: running pid=$tunnel_pid"
  else
    rm -f "$ROOT/tmp.cloudflared.pid"
    echo "Cloudflare Tunnel: stopped"
  fi
else
  tunnel_pid="$(pgrep -f "cloudflared tunnel --config .*cloudflared-mcp4chatgpt.yml run mcp4chatgpt" | head -n 1 || true)"
  if [ -n "$tunnel_pid" ]; then
    echo "Cloudflare Tunnel: running pid=$tunnel_pid"
  else
    echo "Cloudflare Tunnel: stopped"
  fi
fi
echo "Logs:"
echo "  $ROOT/logs/service.out.log"
echo "  $ROOT/logs/service.err.log"
echo "  $ROOT/logs/cloudflared.out.log"
echo "  $ROOT/logs/cloudflared.err.log"
echo "  $ROOT/logs/caddy.out.log"
echo "  $ROOT/logs/caddy.err.log"
echo "  $ROOT/logs/audit.jsonl"

#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$ROOT/tmp.service.pid"
LAUNCHD_LABEL="com.vickers.mcp4chatgpt"
LAUNCHD_SERVICE="gui/$(id -u)/$LAUNCHD_LABEL"
TMUX_SESSION="mcp4chatgpt"
SERVICE_PATTERN="[m]cp4chatgpt.server"
TUNNEL_PATTERN="[c]loudflared tunnel --config .*cloudflared-mcp4chatgpt.yml run mcp4chatgpt"
MCP_BIND_HOST="${MCP_BIND_HOST:-0.0.0.0}"
MCP_BIND_PORT="${MCP_BIND_PORT:-8766}"
MCP_HEALTH_HOST="${MCP_HEALTH_HOST:-127.0.0.1}"
MCP_PUBLIC_BASE_URL="${MCP_PUBLIC_BASE_URL:-https://mcp.runzhe.uk}"
MCP_EXTERNAL_TUNNEL="${MCP_EXTERNAL_TUNNEL:-1}"

health_host() {
  if [ -n "$MCP_HEALTH_HOST" ]; then
    echo "$MCP_HEALTH_HOST"
    return 0
  fi
  case "$MCP_BIND_HOST" in
    0.0.0.0|::) echo "127.0.0.1" ;;
    *) echo "$MCP_BIND_HOST" ;;
  esac
}

external_tunnel_enabled() {
  case "$MCP_EXTERNAL_TUNNEL" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

LOCAL_HEALTH="http://$(health_host):${MCP_BIND_PORT}/health"

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
  detected_pid="$(pgrep -f "$SERVICE_PATTERN" | head -n 1 || true)"
  if [ -n "$detected_pid" ]; then
    status="running"
    pid="$detected_pid"
  fi
fi

echo "Status: $status"
echo "Launchd: $launchd_status"
echo "Tmux: $tmux_status"
echo "Bind host: $MCP_BIND_HOST"
if [ -n "$pid" ]; then
  echo "PID: $pid"
fi

if curl -fsS "$LOCAL_HEALTH" >/dev/null 2>&1; then
  echo "Health: ok"
else
  echo "Health: unavailable"
fi

echo "Local health URL: $LOCAL_HEALTH"
echo "Public URL target: ${MCP_PUBLIC_BASE_URL%/}/mcp"
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
if external_tunnel_enabled; then
  echo "Cloudflare Tunnel: external"
elif [ -f "$ROOT/tmp.cloudflared.pid" ]; then
  tunnel_pid="$(cat "$ROOT/tmp.cloudflared.pid" 2>/dev/null || true)"
  if [ -n "$tunnel_pid" ] && kill -0 "$tunnel_pid" 2>/dev/null; then
    echo "Cloudflare Tunnel: running pid=$tunnel_pid"
  else
    rm -f "$ROOT/tmp.cloudflared.pid"
    echo "Cloudflare Tunnel: stopped"
  fi
else
  tunnel_pid="$(pgrep -f "$TUNNEL_PATTERN" | head -n 1 || true)"
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

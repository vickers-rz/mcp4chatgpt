#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$ROOT/tmp.service.pid"
LAUNCHD_LABEL="com.vickers.mcp4chatgpt"
LAUNCHD_SERVICE="gui/$(id -u)/$LAUNCHD_LABEL"
TMUX_SESSION="mcp4chatgpt"

if command -v tmux >/dev/null 2>&1 && tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
  tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
  rm -f "$PID_FILE"
  echo "MCP4ChatGPT stopped"
  exit 0
fi

if [ "$(uname -s)" = "Darwin" ] && command -v launchctl >/dev/null 2>&1; then
  if launchctl print "$LAUNCHD_SERVICE" >/dev/null 2>&1; then
    launchctl bootout "$LAUNCHD_SERVICE" 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "MCP4ChatGPT stopped"
    exit 0
  fi
fi

if [ ! -f "$PID_FILE" ]; then
  echo "MCP4ChatGPT is not running: no pid file"
  exit 0
fi

pid="$(cat "$PID_FILE" 2>/dev/null || true)"
if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "MCP4ChatGPT is not running: stale pid file removed"
  exit 0
fi

kill "$pid" 2>/dev/null || true
for _ in 1 2 3 4 5; do
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "MCP4ChatGPT stopped"
    exit 0
  fi
  sleep 1
done

kill -9 "$pid" 2>/dev/null || true
rm -f "$PID_FILE"
echo "MCP4ChatGPT force-stopped"

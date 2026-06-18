#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$ROOT/tmp.service.pid"
OUT_LOG="$ROOT/logs/service.out.log"
ERR_LOG="$ROOT/logs/service.err.log"
LAUNCHD_LABEL="com.vickers.mcp4chatgpt"
LAUNCHD_PLIST="$ROOT/deploy/$LAUNCHD_LABEL.plist"
LAUNCHD_SERVICE="gui/$(id -u)/$LAUNCHD_LABEL"
TMUX_SESSION="mcp4chatgpt"

mkdir -p "$ROOT/logs" "$ROOT/data"

if command -v tmux >/dev/null 2>&1 && [ "${MCP_USE_LAUNCHD:-0}" != "1" ]; then
  if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    detected_pid="$(pgrep -f "mcp4chatgpt.server" | head -n 1 || true)"
    if [ -n "$detected_pid" ]; then
      echo "$detected_pid" > "$PID_FILE"
      echo "MCP4ChatGPT is already running in tmux: pid=$detected_pid"
      exit 0
    fi
    tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
  fi

  tmux new-session -d -s "$TMUX_SESSION" -c "$ROOT" "$ROOT/scripts/dev.sh > '$OUT_LOG' 2> '$ERR_LOG'"
  ok=0
  for _ in 1 2 3 4 5; do
    sleep 1
    if curl -fsS http://127.0.0.1:8766/health >/dev/null 2>&1; then
      ok=1
      break
    fi
  done

  if [ "$ok" = "1" ]; then
    detected_pid="$(pgrep -f "mcp4chatgpt.server" | head -n 1 || true)"
    if [ -n "$detected_pid" ]; then
      echo "$detected_pid" > "$PID_FILE"
      echo "MCP4ChatGPT started in tmux: pid=$detected_pid"
    else
      rm -f "$PID_FILE"
      echo "MCP4ChatGPT started in tmux"
    fi
    echo "Local health: http://127.0.0.1:8766/health"
    exit 0
  fi

  echo "MCP4ChatGPT failed to start in tmux. Cleaning up."
  tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
  rm -f "$PID_FILE"
  echo "--- stderr (last 120 lines) ---"
  sed -n '1,120p' "$ERR_LOG" 2>/dev/null || true
  exit 1
fi

if [ "${MCP_USE_LAUNCHD:-0}" = "1" ] && [ "$(uname -s)" = "Darwin" ] && command -v launchctl >/dev/null 2>&1 && [ -f "$LAUNCHD_PLIST" ]; then
  if ! launchctl print "$LAUNCHD_SERVICE" >/dev/null 2>&1; then
    launchctl bootstrap "gui/$(id -u)" "$LAUNCHD_PLIST"
  fi
  launchctl kickstart -k "$LAUNCHD_SERVICE"

  ok=0
  for _ in 1 2 3 4 5; do
    sleep 1
    if curl -fsS http://127.0.0.1:8766/health >/dev/null 2>&1; then
      ok=1
      break
    fi
  done

  if [ "$ok" = "1" ]; then
    detected_pid="$(pgrep -f "mcp4chatgpt.server" | head -n 1 || true)"
    if [ -n "$detected_pid" ]; then
      echo "$detected_pid" > "$PID_FILE"
      echo "MCP4ChatGPT started with launchd: pid=$detected_pid"
    else
      rm -f "$PID_FILE"
      echo "MCP4ChatGPT started with launchd"
    fi
    echo "Local health: http://127.0.0.1:8766/health"
    exit 0
  fi

  echo "MCP4ChatGPT failed to start with launchd."
  echo "--- launchd status ---"
  launchctl print "$LAUNCHD_SERVICE" 2>/dev/null || true
  echo "--- stderr (last 120 lines) ---"
  sed -n '1,120p' "$ERR_LOG" 2>/dev/null || true
  exit 1
fi

if [ -f "$PID_FILE" ]; then
  old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
    echo "MCP4ChatGPT is already running: pid=$old_pid"
    exit 0
  fi
fi

cd "$ROOT"
nohup "$ROOT/scripts/dev.sh" </dev/null > "$OUT_LOG" 2> "$ERR_LOG" &
pid="$!"
echo "$pid" > "$PID_FILE"

# Wait up to 5 seconds (1 s intervals) for the health endpoint to respond.
ok=0
for i in 1 2 3 4 5; do
  sleep 1
  if kill -0 "$pid" 2>/dev/null && curl -fsS http://127.0.0.1:8766/health >/dev/null 2>&1; then
    ok=1
    break
  fi
done

if [ "$ok" = "1" ]; then
  echo "MCP4ChatGPT started: pid=$pid"
  echo "Local health: http://127.0.0.1:8766/health"
  exit 0
fi

# Startup failed — kill the orphaned process and remove the PID file so the
# next invocation of start.sh does not mistakenly report "already running".
echo "MCP4ChatGPT failed to start. Cleaning up."
kill "$pid" 2>/dev/null || true
rm -f "$PID_FILE"
echo "--- stderr (last 120 lines) ---"
sed -n '1,120p' "$ERR_LOG" 2>/dev/null || true
exit 1

#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$ROOT/tmp.service.pid"
OUT_LOG="$ROOT/logs/service.out.log"
ERR_LOG="$ROOT/logs/service.err.log"

mkdir -p "$ROOT/logs" "$ROOT/data"

if [ -f "$PID_FILE" ]; then
  old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
    echo "MCP4ChatGPT is already running: pid=$old_pid"
    exit 0
  fi
fi

cd "$ROOT"
nohup "$ROOT/scripts/dev.sh" > "$OUT_LOG" 2> "$ERR_LOG" &
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

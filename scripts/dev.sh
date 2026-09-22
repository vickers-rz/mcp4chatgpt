#!/bin/sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
MCP_BIND_HOST="${MCP_BIND_HOST:-127.0.0.1}"
MCP_BIND_PORT="${MCP_BIND_PORT:-8766}"
MCP_PUBLIC_BASE_URL="${MCP_PUBLIC_BASE_URL:-http://127.0.0.1:${MCP_BIND_PORT}}"
MCP_EXTERNAL_TUNNEL="${MCP_EXTERNAL_TUNNEL:-0}"
MCP_HEALTH_HOST="${MCP_HEALTH_HOST:-127.0.0.1}"
# Development is intentionally non-interactive by default. Opt in explicitly
# with MCP_COMPUTER_MODE=interact and a concrete allowlist (or "*" for a
# reviewed single-user full-access deployment).
MCP_COMPUTER_MODE="${MCP_COMPUTER_MODE:-off}"
MCP_COMPUTER_ALLOWED_APPS="${MCP_COMPUTER_ALLOWED_APPS:-}"
MCP_COMPUTER_BACKEND="${MCP_COMPUTER_BACKEND:-auto}"
export MCP_BIND_HOST MCP_BIND_PORT MCP_PUBLIC_BASE_URL MCP_EXTERNAL_TUNNEL MCP_HEALTH_HOST
export MCP_COMPUTER_MODE MCP_COMPUTER_ALLOWED_APPS MCP_COMPUTER_BACKEND
PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="/opt/homebrew/bin/python3"
fi
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3)"
fi
PYTHONPATH=src exec "$PYTHON_BIN" -m mcp4chatgpt.runtime

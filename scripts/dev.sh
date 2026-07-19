#!/bin/sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
MCP_BIND_HOST="${MCP_BIND_HOST:-0.0.0.0}"
MCP_BIND_PORT="${MCP_BIND_PORT:-8766}"
MCP_PUBLIC_BASE_URL="${MCP_PUBLIC_BASE_URL:-https://mcp.runzhe.uk}"
MCP_EXTERNAL_TUNNEL="${MCP_EXTERNAL_TUNNEL:-1}"
MCP_HEALTH_HOST="${MCP_HEALTH_HOST:-127.0.0.1}"
export MCP_BIND_HOST MCP_BIND_PORT MCP_PUBLIC_BASE_URL MCP_EXTERNAL_TUNNEL MCP_HEALTH_HOST
PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="/opt/homebrew/bin/python3"
fi
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3)"
fi
PYTHONPATH=src exec "$PYTHON_BIN" -m mcp4chatgpt.server

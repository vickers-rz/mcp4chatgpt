#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Explicit single-user full-access profile. This preserves the historical
# MCP4ChatGPT development behavior without making it the generic default.
export MCP_BIND_HOST="${MCP_FULL_BIND_HOST:-0.0.0.0}"
export MCP_BIND_PORT="${MCP_BIND_PORT:-8766}"
export MCP_PUBLIC_BASE_URL="${MCP_FULL_PUBLIC_BASE_URL:-https://mcp.runzhe.uk}"
export MCP_EXTERNAL_TUNNEL="${MCP_FULL_EXTERNAL_TUNNEL:-1}"
export MCP_HEALTH_HOST="${MCP_HEALTH_HOST:-127.0.0.1}"
export MCP_COMPUTER_MODE="${MCP_FULL_COMPUTER_MODE:-interact}"
export MCP_COMPUTER_ALLOWED_APPS="${MCP_FULL_COMPUTER_ALLOWED_APPS:-*}"
export MCP_COMPUTER_BACKEND="${MCP_FULL_COMPUTER_BACKEND:-auto}"

exec "$ROOT/scripts/dev.sh"

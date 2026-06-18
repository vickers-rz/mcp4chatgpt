#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env"
  set +a
fi

if [ -z "${MCP_AUTH_SECRET:-}" ]; then
  echo "MCP_AUTH_SECRET is not set. Check $ROOT/.env" >&2
  exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="/opt/homebrew/bin/python3"
fi
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3)"
fi

token="$(PYTHONPATH=src "$PYTHON_BIN" - <<'PY'
from mcp4chatgpt.ext_bridge import _derive_token
import os
print(_derive_token(os.environ["MCP_AUTH_SECRET"]))
PY
)"

if command -v pbcopy >/dev/null 2>&1; then
  printf "%s" "$token" | pbcopy
  echo "Chrome extension bridge token copied to clipboard."
else
  printf "%s\n" "$token"
fi

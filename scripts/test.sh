#!/bin/sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
PYTHON_BIN="${MCP_TEST_PYTHON:-$ROOT_DIR/.venv/bin/python}"

if [ ! -x "$PYTHON_BIN" ]; then
  echo "Project test interpreter is not executable: $PYTHON_BIN" >&2
  exit 2
fi

cd "$ROOT_DIR"
exec "$PYTHON_BIN" -m pytest "$@"

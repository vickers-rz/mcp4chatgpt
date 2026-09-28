#!/bin/sh
# Public OAuth service reached by the existing Lucky tunnel on N100.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec "$ROOT/MCP4ChatGPT.command" restart-public

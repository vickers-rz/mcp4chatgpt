#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/logs"
touch "$ROOT/logs/service.out.log" "$ROOT/logs/service.err.log" "$ROOT/logs/audit.jsonl"
open "$ROOT/logs"


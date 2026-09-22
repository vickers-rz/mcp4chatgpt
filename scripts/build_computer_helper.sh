#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
output="${1:-/tmp/mcp4chatgpt-computer-helper}"
if [ "$(uname -s)" != Darwin ]; then
  echo "macOS is required" >&2
  exit 1
fi
xcrun swiftc -O -target "$(uname -m)-apple-macosx14.0" \
  "$repo_root/src/mcp4chatgpt/native/ComputerUseHelper.swift" -o "$output"
echo "$output"

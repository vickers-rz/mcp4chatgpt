#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MIN_AGE_SEC=1800
ACTION="dry-run"
SERVICE_PATTERN="[m]cp4chatgpt.server"

usage() {
  cat <<EOF
Audit or clean Codex/co-te helper processes.

Usage:
  scripts/cleanup_codex_co_te.sh [--dry-run]
  scripts/cleanup_codex_co_te.sh --kill [--min-age-sec SECONDS]

Default behavior is dry-run. The script only considers:
  - /Users/vickers/Documents/MCP_Creator/codex_work_with_apps/co-te.py
  - /Applications/Codex.app/Contents/Resources/cua_node/bin/node_repl

It does not target the Codex app, MCP4ChatGPT, cloudflared, tmux, or unrelated
Python/Node processes. With --kill, it sends TERM only to candidates older than
the minimum age and not descended from the current MCP service.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)
      ACTION="dry-run"
      shift
      ;;
    --kill)
      ACTION="kill"
      shift
      ;;
    --min-age-sec)
      if [ "$#" -lt 2 ]; then
        echo "--min-age-sec requires a value" >&2
        exit 2
      fi
      MIN_AGE_SEC="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$MIN_AGE_SEC" in
  ''|*[!0-9]*)
    echo "--min-age-sec must be a non-negative integer" >&2
    exit 2
    ;;
esac

service_pid() {
  if [ -f "$ROOT/tmp.service.pid" ]; then
    pid="$(cat "$ROOT/tmp.service.pid" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "$pid"
      return 0
    fi
  fi
  pgrep -f "$SERVICE_PATTERN" | head -n 1 || true
}

parent_pid() {
  ps -o ppid= -p "$1" 2>/dev/null | tr -d ' ' || true
}

is_descendant_of() {
  child="$1"
  ancestor="$2"
  [ -n "$child" ] || return 1
  [ -n "$ancestor" ] || return 1

  walk_pid="$child"
  hops=0
  while [ "$walk_pid" != "0" ] && [ "$walk_pid" != "1" ] && [ "$hops" -lt 32 ]; do
    if [ "$walk_pid" = "$ancestor" ]; then
      return 0
    fi
    walk_pid="$(parent_pid "$walk_pid")"
    [ -n "$walk_pid" ] || return 1
    hops=$((hops + 1))
  done
  return 1
}

format_age() {
  total="$1"
  days=$((total / 86400))
  rem=$((total % 86400))
  hours=$((rem / 3600))
  rem=$((rem % 3600))
  mins=$((rem / 60))
  secs=$((rem % 60))
  if [ "$days" -gt 0 ]; then
    printf "%dd%02dh%02dm" "$days" "$hours" "$mins"
  elif [ "$hours" -gt 0 ]; then
    printf "%dh%02dm%02ds" "$hours" "$mins" "$secs"
  else
    printf "%dm%02ds" "$mins" "$secs"
  fi
}

etime_to_seconds() {
  value="$1"
  days=0
  clock="$value"
  case "$value" in
    *-*)
      days="${value%%-*}"
      clock="${value#*-}"
      ;;
  esac

  old_ifs="$IFS"
  IFS=:
  set -- $clock
  IFS="$old_ifs"
  case "$#" in
    3)
      hours="$1"
      mins="$2"
      secs="$3"
      ;;
    2)
      hours=0
      mins="$1"
      secs="$2"
      ;;
    *)
      hours=0
      mins=0
      secs=0
      ;;
  esac
  echo $((days * 86400 + hours * 3600 + mins * 60 + secs))
}

MCP_PID="$(service_pid)"
TMP_CANDIDATES="$(mktemp "${TMPDIR:-/tmp}/mcp4chatgpt-codex-cleanup.XXXXXX")"
trap 'rm -f "$TMP_CANDIDATES"' EXIT

echo "Mode: $ACTION"
echo "Minimum age for --kill: ${MIN_AGE_SEC}s"
if [ -n "$MCP_PID" ]; then
  echo "Protected MCP service PID: $MCP_PID"
else
  echo "Protected MCP service PID: none detected"
fi
echo
printf "%-7s %-7s %-10s %-8s %-12s %-10s %s\n" "PID" "PPID" "AGE" "RSS_KB" "KIND" "ACTION" "COMMAND"

ps -axo pid=,ppid=,etime=,rss=,command= > "$TMP_CANDIDATES"

while read -r pid ppid etime rss command; do
  case "$command" in
    *"/Users/vickers/Documents/MCP_Creator/codex_work_with_apps/co-te.py"*)
      kind="co-te.py"
      ;;
    *"/Applications/Codex.app/Contents/Resources/cua_node/bin/node_repl"*)
      kind="node_repl"
      ;;
    *)
      continue
      ;;
  esac

  etime_sec="$(etime_to_seconds "$etime")"
  age="$(format_age "$etime_sec")"
  decision="skip"

  if [ -n "$MCP_PID" ] && is_descendant_of "$pid" "$MCP_PID"; then
    decision="protect-mcp"
  elif [ "$etime_sec" -lt "$MIN_AGE_SEC" ]; then
    decision="too-young"
  elif [ "$ACTION" = "kill" ]; then
    if kill "$pid" 2>/dev/null; then
      decision="term-sent"
    else
      decision="term-failed"
    fi
  else
    decision="candidate"
  fi

  printf "%-7s %-7s %-10s %-8s %-12s %-10s %s\n" "$pid" "$ppid" "$age" "$rss" "$kind" "$decision" "$command"
done < "$TMP_CANDIDATES"

echo
if ! awk '
  /\/Users\/vickers\/Documents\/MCP_Creator\/codex_work_with_apps\/co-te.py/ { found = 1 }
  /\/Applications\/Codex.app\/Contents\/Resources\/cua_node\/bin\/node_repl/ { found = 1 }
  END { exit found ? 0 : 1 }
' "$TMP_CANDIDATES"; then
  echo "No Codex/co-te helper candidates found."
elif [ "$ACTION" = "dry-run" ]; then
  echo "Dry-run only. Re-run with --kill to terminate candidates older than ${MIN_AGE_SEC}s."
else
  echo "TERM requested for eligible candidates. Re-run in dry-run mode to verify what remains."
fi

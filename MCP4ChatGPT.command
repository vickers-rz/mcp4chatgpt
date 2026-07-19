#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
MCP_BIND_HOST="${MCP_BIND_HOST:-0.0.0.0}"
MCP_BIND_PORT="${MCP_BIND_PORT:-8766}"
MCP_PUBLIC_BASE_URL="${MCP_PUBLIC_BASE_URL:-https://mcp.runzhe.uk}"
MCP_EXTERNAL_TUNNEL="${MCP_EXTERNAL_TUNNEL:-1}"
MCP_HEALTH_HOST="${MCP_HEALTH_HOST:-127.0.0.1}"
export MCP_BIND_HOST MCP_BIND_PORT MCP_PUBLIC_BASE_URL MCP_EXTERNAL_TUNNEL MCP_HEALTH_HOST
LOCAL_HEALTH=""
PUBLIC_HEALTH="${MCP_PUBLIC_BASE_URL%/}/health"
CONNECTOR_URL="${MCP_PUBLIC_BASE_URL%/}/mcp"
SERVICE_PID_FILE="$ROOT/tmp.service.pid"
TUNNEL_PID_FILE="$ROOT/tmp.cloudflared.pid"
SERVICE_PATTERN="[m]cp4chatgpt.server"
TUNNEL_PATTERN="[c]loudflared tunnel --config .*cloudflared-mcp4chatgpt.yml run mcp4chatgpt"

health_host() {
  if [ -n "$MCP_HEALTH_HOST" ]; then
    echo "$MCP_HEALTH_HOST"
    return 0
  fi
  case "$MCP_BIND_HOST" in
    0.0.0.0|::) echo "127.0.0.1" ;;
    *) echo "$MCP_BIND_HOST" ;;
  esac
}

external_tunnel_enabled() {
  case "$MCP_EXTERNAL_TUNNEL" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

LOCAL_HEALTH="http://$(health_host):${MCP_BIND_PORT}/health"

usage() {
  cat <<EOF
MCP4ChatGPT control

Usage:
  ./MCP4ChatGPT.command start       Start MCP service and optional Cloudflare Tunnel
  ./MCP4ChatGPT.command stop        Stop MCP service and optional Cloudflare Tunnel
  ./MCP4ChatGPT.command restart     Stop then start both
  ./MCP4ChatGPT.command clean-restart
                                    Stop, clean Codex/co-te helpers, then start
  ./MCP4ChatGPT.command status      Show process and health status
  ./MCP4ChatGPT.command check       Run public/local health checks
  ./MCP4ChatGPT.command logs        Open log directory in Finder
  ./MCP4ChatGPT.command tail        Tail service/tunnel/audit logs
  ./MCP4ChatGPT.command rotate-logs Rotate/compress old logs
  ./MCP4ChatGPT.command cleanup     Audit Codex/co-te helper process residue
  ./MCP4ChatGPT.command url         Print ChatGPT Connector URL

Double-clicking this .command file opens an interactive menu.
EOF
}

rotate_logs() {
  "$ROOT/scripts/rotate_logs.sh"
}

service_pid() {
  if [ -f "$SERVICE_PID_FILE" ]; then
    pid="$(cat "$SERVICE_PID_FILE" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "$pid"
      return 0
    fi
    rm -f "$SERVICE_PID_FILE"
  fi

  # Foreground starts from Terminal/Codex do not write tmp.service.pid.
  pgrep -f "$SERVICE_PATTERN" | head -n 1 || true
}

tunnel_pid() {
  if [ -f "$TUNNEL_PID_FILE" ]; then
    pid="$(cat "$TUNNEL_PID_FILE" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "$pid"
      return 0
    fi
    rm -f "$TUNNEL_PID_FILE"
  fi

  # The match is restricted to this project's named tunnel config.
  pgrep -f "$TUNNEL_PATTERN" | head -n 1 || true
}

local_ok() {
  curl -fsS --connect-timeout 5 "$LOCAL_HEALTH" >/dev/null 2>&1
}

public_ok() {
  curl -fsS --connect-timeout 10 "$PUBLIC_HEALTH" >/dev/null 2>&1
}

curl_with_retries() {
  url="$1"
  attempts="${2:-3}"
  i=1
  while [ "$i" -le "$attempts" ]; do
    if curl -fsS --connect-timeout 10 "$url"; then
      return 0
    fi
    if [ "$i" -lt "$attempts" ]; then
      echo
      echo "Attempt $i failed; retrying..."
      sleep 2
    fi
    i=$((i + 1))
  done
  return 1
}

status() {
  rotate_logs >/dev/null 2>&1 || true
  spid="$(service_pid)"
  tpid="$(tunnel_pid)"

  if [ -n "$spid" ]; then
    echo "MCP service: running pid=$spid"
  else
    echo "MCP service: stopped"
  fi

  if local_ok; then
    echo "Local health: ok ($LOCAL_HEALTH)"
  else
    echo "Local health: unavailable ($LOCAL_HEALTH)"
  fi

  if external_tunnel_enabled; then
    echo "Cloudflare Tunnel: external"
  elif [ -n "$tpid" ]; then
    echo "Cloudflare Tunnel: running pid=$tpid"
  else
    echo "Cloudflare Tunnel: stopped"
  fi

  if public_ok; then
    echo "Public health: ok ($PUBLIC_HEALTH)"
  else
    echo "Public health: unavailable ($PUBLIC_HEALTH)"
  fi

  echo "Connector URL: $CONNECTOR_URL"
  echo "Bind host: $MCP_BIND_HOST"
}

start_all() {
  mkdir -p "$ROOT/logs" "$ROOT/data"
  rotate_logs

  if local_ok; then
    spid="$(service_pid)"
    echo "MCP service already healthy${spid:+: pid=$spid}"
  else
    echo "Starting MCP service..."
    "$ROOT/scripts/start.sh"
  fi

  if external_tunnel_enabled; then
    if public_ok; then
      echo "External tunnel public health: ready"
    else
      echo "External tunnel mode enabled; skipping local cloudflared startup."
    fi
  elif public_ok; then
    tpid="$(tunnel_pid)"
    echo "Cloudflare Tunnel already healthy${tpid:+: pid=$tpid}"
  else
    if [ -n "$(tunnel_pid)" ]; then
      echo "Cloudflare Tunnel process exists but public health is not ready."
      echo "Check logs with: ./MCP4ChatGPT.command tail"
      return 1
    fi
    echo "Starting Cloudflare Tunnel..."
    "$ROOT/scripts/start_tunnel.sh"
  fi

  echo
  status
}

stop_pid() {
  label="$1"
  pid="$2"
  pid_file="$3"

  if [ -z "$pid" ]; then
    echo "$label: not running"
    [ -n "$pid_file" ] && rm -f "$pid_file"
    return 0
  fi

  echo "Stopping $label: pid=$pid"
  kill "$pid" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    if ! kill -0 "$pid" 2>/dev/null; then
      [ -n "$pid_file" ] && rm -f "$pid_file"
      echo "$label stopped"
      return 0
    fi
    sleep 1
  done

  echo "$label did not exit cleanly; force stopping"
  kill -9 "$pid" 2>/dev/null || true
  [ -n "$pid_file" ] && rm -f "$pid_file"
}

stop_all() {
  if external_tunnel_enabled; then
    echo "Cloudflare Tunnel: managed externally"
  else
    stop_pid "Cloudflare Tunnel" "$(tunnel_pid)" "$TUNNEL_PID_FILE"
  fi
  stop_pid "MCP service" "$(service_pid)" "$SERVICE_PID_FILE"
}

restart_all() {
  stop_all
  start_all
}

check_all() {
  rotate_logs >/dev/null 2>&1 || true
  echo "Checking local health..."
  curl_with_retries "$LOCAL_HEALTH" 3
  echo
  echo "Checking public health..."
  curl_with_retries "$PUBLIC_HEALTH" 3
  echo
  echo "Checking OAuth discovery..."
  curl_with_retries "${MCP_PUBLIC_BASE_URL%/}/.well-known/oauth-authorization-server" 3 | python3 -m json.tool
}

open_logs() {
  mkdir -p "$ROOT/logs"
  open "$ROOT/logs"
}

tail_logs() {
  mkdir -p "$ROOT/logs"
  touch "$ROOT/logs/service.out.log" "$ROOT/logs/service.err.log" \
    "$ROOT/logs/cloudflared.out.log" "$ROOT/logs/cloudflared.err.log" \
    "$ROOT/logs/audit.jsonl"
  tail -n 80 -f \
    "$ROOT/logs/service.out.log" \
    "$ROOT/logs/service.err.log" \
    "$ROOT/logs/cloudflared.out.log" \
    "$ROOT/logs/cloudflared.err.log" \
    "$ROOT/logs/audit.jsonl"
}

cleanup_helpers() {
  "$ROOT/scripts/cleanup_codex_co_te.sh" --dry-run
}

clean_restart_all() {
  echo "Stopping MCP service and Cloudflare Tunnel..."
  stop_all
  echo
  echo "Cleaning known Codex/co-te helper residue..."
  "$ROOT/scripts/cleanup_codex_co_te.sh" --kill --min-age-sec 0
  sleep 2
  echo
  echo "Remaining helper audit:"
  "$ROOT/scripts/cleanup_codex_co_te.sh" --dry-run
  echo
  echo "Starting MCP service and Cloudflare Tunnel..."
  start_all
}

interactive_menu() {
  while true; do
    clear 2>/dev/null || true
    status
    cat <<EOF

Choose an action:
  1) Start
  2) Stop
  3) Restart
  4) Check health
  5) Tail logs
  6) Open logs folder
  7) Rotate/compress old logs
  8) Audit Codex/co-te helpers
  9) Clean-restart MCP and helpers
  10) Print Connector URL
  q) Quit
EOF
    printf "> "
    read choice || exit 0
    set +e
    case "$choice" in
      1) start_all; action_status=$? ;;
      2) stop_all; action_status=$? ;;
      3) restart_all; action_status=$? ;;
      4) check_all; action_status=$? ;;
      5) tail_logs; action_status=$? ;;
      6) open_logs; action_status=$? ;;
      7) rotate_logs; action_status=$? ;;
      8) cleanup_helpers; action_status=$? ;;
      9) clean_restart_all; action_status=$? ;;
      10) echo "$CONNECTOR_URL"; action_status=$? ;;
      q|Q) exit 0 ;;
      *) echo "Unknown choice: $choice"; action_status=2 ;;
    esac
    set -e
    if [ "$action_status" -ne 0 ]; then
      echo
      echo "Action exited with status $action_status."
    fi
    echo
    printf "Press Enter to continue..."
    read _ || exit 0
  done
}

cmd="${1:-menu}"
case "$cmd" in
  start) start_all ;;
  stop) stop_all ;;
  restart) restart_all ;;
  clean-restart|restart-clean) clean_restart_all ;;
  status) status ;;
  check) check_all ;;
  logs) open_logs ;;
  tail) tail_logs ;;
  rotate-logs) rotate_logs ;;
  cleanup) cleanup_helpers ;;
  url) echo "$CONNECTOR_URL" ;;
  menu) interactive_menu ;;
  -h|--help|help) usage ;;
  *)
    echo "Unknown command: $cmd"
    echo
    usage
    exit 2
    ;;
esac

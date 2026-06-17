#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$ROOT/logs"
ARCHIVE_DIR="$LOG_DIR/archive"
RETENTION_DAYS="${MCP_LOG_RETENTION_DAYS:-30}"
TODAY="$(date +%Y-%m-%d)"

mkdir -p "$LOG_DIR" "$ARCHIVE_DIR"

is_active_log() {
  case "$(basename "$1")" in
    audit.jsonl|service.out.log|service.err.log|cloudflared.out.log|cloudflared.err.log|caddy.out.log|caddy.err.log)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

file_day() {
  # macOS/BSD stat.
  stat -f "%Sm" -t "%Y-%m-%d" "$1"
}

file_stamp() {
  stat -f "%Sm" -t "%Y-%m-%d-%H%M%S" "$1"
}

service_running() {
  pgrep -f "mcp4chatgpt.server" >/dev/null 2>&1
}

tunnel_running() {
  pgrep -f "cloudflared tunnel --config .*cloudflared-mcp4chatgpt.yml run mcp4chatgpt" >/dev/null 2>&1
}

caddy_running() {
  pgrep -f "caddy.*Caddyfile" >/dev/null 2>&1
}

rotate_stopped_active_log() {
  file="$1"
  [ -f "$file" ] || return 0
  [ -s "$file" ] || return 0
  [ "$(file_day "$file")" != "$TODAY" ] || return 0

  base="$(basename "$file")"
  case "$base" in
    service.*.log)
      service_running && return 0
      ;;
    cloudflared.*.log)
      tunnel_running && return 0
      ;;
    caddy.*.log)
      caddy_running && return 0
      ;;
    audit.jsonl)
      # The Python AuditLogger owns audit.jsonl rotation while the service is
      # running. If the service is stopped, preserve stale audit logs here.
      service_running && return 0
      ;;
    *)
      return 0
      ;;
  esac

  stamp="$(file_stamp "$file")"
  stem="${base%.*}"
  ext="${base##*.}"
  rotated="$LOG_DIR/$stem.$stamp.$ext"
  counter=1
  while [ -e "$rotated" ]; do
    rotated="$LOG_DIR/$stem.$stamp.$counter.$ext"
    counter=$((counter + 1))
  done
  mv "$file" "$rotated"
  echo "Rotated stale log: $rotated"
}

rotate_stale_active_logs() {
  rotate_stopped_active_log "$LOG_DIR/audit.jsonl"
  rotate_stopped_active_log "$LOG_DIR/service.out.log"
  rotate_stopped_active_log "$LOG_DIR/service.err.log"
  rotate_stopped_active_log "$LOG_DIR/cloudflared.out.log"
  rotate_stopped_active_log "$LOG_DIR/cloudflared.err.log"
  rotate_stopped_active_log "$LOG_DIR/caddy.out.log"
  rotate_stopped_active_log "$LOG_DIR/caddy.err.log"
}

archive_day() {
  day="$1"
  list_file="$ARCHIVE_DIR/.archive-$day.list"
  archive_file="$ARCHIVE_DIR/$day.logs.tar.gz"
  : > "$list_file"

  find "$LOG_DIR" -maxdepth 1 -type f | while IFS= read -r file; do
    [ "$(file_day "$file")" = "$day" ] || continue
    is_active_log "$file" && continue
    printf '%s\n' "$(basename "$file")" >> "$list_file"
  done

  if [ ! -s "$list_file" ]; then
    rm -f "$list_file"
    return 0
  fi

  tmp_archive="$archive_file.tmp"
  (cd "$LOG_DIR" && tar -czf "$tmp_archive" -T "$list_file")
  mv "$tmp_archive" "$archive_file"

  while IFS= read -r rel; do
    rm -f "$LOG_DIR/$rel"
  done < "$list_file"
  rm -f "$list_file"
  echo "Archived logs for $day: $archive_file"
}

days_to_archive() {
  find "$LOG_DIR" -maxdepth 1 -type f | while IFS= read -r file; do
    day="$(file_day "$file")"
    [ "$day" != "$TODAY" ] || continue
    is_active_log "$file" && continue
    echo "$day"
  done | sort -u
}

rotate_stale_active_logs

for day in $(days_to_archive); do
  archive_day "$day"
done

# Delete old daily archives. This keeps disk use bounded without touching active
# logs or the latest compressed archives.
find "$ARCHIVE_DIR" -type f -name "*.logs.tar.gz" -mtime +"$RETENTION_DAYS" -print -delete

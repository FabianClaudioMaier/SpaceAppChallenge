#!/usr/bin/env bash
set -euo pipefail

FRONTEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIDDLEWARE_DIR="$FRONTEND_DIR/../middleware"
PORT="${PORT:-5173}"

echo "Frontend und Middleware laufen unter http://127.0.0.1:${PORT}/"
python3 "$MIDDLEWARE_DIR/clean_cache.py" &
clean_cache_pid=$!
python3 "$MIDDLEWARE_DIR/server.py" "$PORT" &
server_pid=$!

cleanup() {
  trap - EXIT INT TERM
  kill "$server_pid" "$clean_cache_pid" 2>/dev/null || true
  wait "$server_pid" "$clean_cache_pid" 2>/dev/null || true
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
wait "$server_pid"

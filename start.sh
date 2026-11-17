#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIDDLEWARE_DIR="$ROOT_DIR/middleware"
BACKEND_DIR="$ROOT_DIR/python-backend"
"$BACKEND_DIR/setup_venv.sh"
PY="$BACKEND_DIR/.venv/bin/python"
PORT="${PORT:-5173}"
HOST="${HOST:-127.0.0.1}"

echo "Frontend und Middleware laufen unter http://${HOST}:${PORT}/"
"$PY" "$MIDDLEWARE_DIR/clean_cache.py" &
clean_cache_pid=$!
"$PY" "$MIDDLEWARE_DIR/server.py" "$PORT" &
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

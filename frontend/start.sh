#!/usr/bin/env bash
set -euo pipefail

FRONTEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-5173}"

echo "Frontend läuft unter http://127.0.0.1:${PORT}/"
exec python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$FRONTEND_DIR"

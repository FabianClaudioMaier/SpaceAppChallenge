#!/usr/bin/env bash
set -euo pipefail

FRONTEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-5173}"

echo "Frontend und Middleware laufen unter http://127.0.0.1:${PORT}/"
exec python3 "$FRONTEND_DIR/../middleware/server.py" "$PORT"

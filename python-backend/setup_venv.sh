#!/usr/bin/env bash
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$BACKEND_DIR/.venv"
PY="$VENV/bin/python"
REQ="$BACKEND_DIR/requirements.txt"
STAMP="$VENV/.requirements.sha256"

if ! "$PY" -c 'import sys' >/dev/null 2>&1; then
  echo "Creating venv..."
  rm -rf "$VENV"                 # throw away a half-created venv
  python3 -m venv "$VENV"
fi

want="$(sha256sum "$REQ" | cut -d' ' -f1)"
have="$(cat "$STAMP" 2>/dev/null || true)"
if [[ "$want" != "$have" ]]; then
  echo "Installing requirements..."
  "$PY" -m pip install -r "$REQ"
  echo "$want" > "$STAMP"        # written only after pip succeeded
fi

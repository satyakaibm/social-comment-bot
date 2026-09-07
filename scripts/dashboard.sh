#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT"
HOST="${DASHBOARD_HOST:-127.0.0.1}"
PORT="${DASHBOARD_PORT:-8765}"

echo "Admin dashboard: http://${HOST}:${PORT}/"
exec "$ROOT/.venv/bin/python" -m app.cli dashboard

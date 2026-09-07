#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

export PYTHONPATH="$SCRIPT_DIR"
HOST="$("$SCRIPT_DIR/.venv/bin/python" -c 'from app import config; print(config.WEBHOOK_HOST)')"
PORT="$("$SCRIPT_DIR/.venv/bin/python" -c 'from app import config; print(config.WEBHOOK_PORT)')"

exec "$SCRIPT_DIR/.venv/bin/gunicorn" \
  --workers 1 \
  --threads 4 \
  --bind "$HOST:$PORT" \
  --access-logfile - \
  --error-logfile - \
  'app.webhook:create_serving_app()'

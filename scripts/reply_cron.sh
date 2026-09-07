#!/usr/bin/env bash
# One cron tick for YouTube, which does not provide comment webhooks.
# Meta keeps polling until META_WEBHOOK_ENABLED=true after webhook activation.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -f "$ROOT/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"
fi

export PYTHONPATH="$ROOT"
LOG="${CRON_LOG:-$ROOT/data/cron.log}"
mkdir -p "$(dirname "$LOG")"
YOUTUBE_LIMIT="${YOUTUBE_CRON_LIMIT:-50}"

{
  echo "==== $(date -u +"%Y-%m-%dT%H:%M:%SZ") ===="
  python -m app.cli poll
  if [[ "${META_WEBHOOK_ENABLED:-false}" != "true" ]]; then
    python -m app.cli poll-facebook
    python -m app.cli poll-instagram
    python -m app.cli post --platform instagram --pending
    python -m app.cli post --platform facebook --pending --like-comments
  fi
  python -m app.cli post --platform youtube --pending --limit "$YOUTUBE_LIMIT"
} >>"$LOG" 2>&1

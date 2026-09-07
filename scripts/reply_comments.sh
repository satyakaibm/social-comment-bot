#!/usr/bin/env bash
# One cron tick for YouTube, which does not provide comment webhooks.
# Meta keeps polling until META_WEBHOOK_ENABLED=true after webhook activation.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

if [[ -f "$SCRIPT_DIR/.venv/bin/activate" ]]; then
  source "$SCRIPT_DIR/.venv/bin/activate"
fi

export PYTHONPATH="$SCRIPT_DIR"
LOG="${CRON_LOG:-$SCRIPT_DIR/data/polling.log}"
mkdir -p "$(dirname "$LOG")"
YOUTUBE_LIMIT="${YOUTUBE_CRON_LIMIT:-50}"

{
  echo ""
  echo "==== Polling cycle started: $(date +"%Y-%m-%d %H:%M:%S %Z") ===="
  failures=0
  youtube_poll_ok=false

  echo "[$(date +"%H:%M:%S")] YouTube: polling latest comments..."
  if python -m app.cli poll; then
    youtube_poll_ok=true
    echo "[$(date +"%H:%M:%S")] YouTube: polling completed."
  else
    failures=$((failures + 1))
    echo "[$(date +"%H:%M:%S")] YouTube: polling failed; publishing skipped for this cycle."
  fi

  if [[ "${META_WEBHOOK_ENABLED:-false}" != "true" ]]; then
    echo "[$(date +"%H:%M:%S")] Facebook: polling latest comments..."
    if python -m app.cli poll-facebook; then
      echo "[$(date +"%H:%M:%S")] Facebook: polling completed."
    else
      failures=$((failures + 1))
      echo "[$(date +"%H:%M:%S")] Facebook: polling failed."
    fi

    echo "[$(date +"%H:%M:%S")] Instagram: polling latest comments..."
    if python -m app.cli poll-instagram; then
      echo "[$(date +"%H:%M:%S")] Instagram: polling completed."
    else
      failures=$((failures + 1))
      echo "[$(date +"%H:%M:%S")] Instagram: polling failed."
    fi

    echo "[$(date +"%H:%M:%S")] Instagram: publishing pending replies..."
    if python -m app.cli post --platform instagram --pending; then
      echo "[$(date +"%H:%M:%S")] Instagram: publishing completed."
    else
      failures=$((failures + 1))
      echo "[$(date +"%H:%M:%S")] Instagram: publishing failed."
    fi

    echo "[$(date +"%H:%M:%S")] Facebook: publishing pending replies and likes..."
    if python -m app.cli post --platform facebook --pending --like-comments; then
      echo "[$(date +"%H:%M:%S")] Facebook: publishing completed."
    else
      failures=$((failures + 1))
      echo "[$(date +"%H:%M:%S")] Facebook: publishing failed."
    fi
  else
    echo "[$(date +"%H:%M:%S")] Meta: polling skipped because webhooks are enabled."
  fi

  if [[ "$youtube_poll_ok" == "true" ]]; then
    echo "[$(date +"%H:%M:%S")] YouTube: publishing up to $YOUTUBE_LIMIT pending replies..."
    if python -m app.cli post --platform youtube --pending --limit "$YOUTUBE_LIMIT"; then
      echo "[$(date +"%H:%M:%S")] YouTube: publishing completed."
    else
      failures=$((failures + 1))
      echo "[$(date +"%H:%M:%S")] YouTube: publishing failed."
    fi
  fi

  echo "==== Polling cycle finished: $(date +"%Y-%m-%d %H:%M:%S %Z"); failed steps: $failures ===="
  exit "$failures"
} >>"$LOG" 2>&1

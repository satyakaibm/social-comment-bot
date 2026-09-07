#!/usr/bin/env bash
# One cron tick for YouTube, which does not provide comment webhooks.
# Meta keeps polling until META_WEBHOOK_ENABLED=true after webhook activation.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

POLLING_CONFIG_FILE="${POLLING_CONFIG_FILE:-$SCRIPT_DIR/config/polling.env}"
if [[ ! -f "$POLLING_CONFIG_FILE" ]]; then
  echo "Polling config file not found: $POLLING_CONFIG_FILE" >&2
  exit 1
fi

# Export the values so app/config.py receives the same limits as this script.
set -a
# shellcheck disable=SC1090
source "$POLLING_CONFIG_FILE"
set +a

required_limits=(
  YOUTUBE_VIDEO_LIMIT
  YOUTUBE_COMMENT_LIMIT
  YOUTUBE_PUBLISH_LIMIT
  FACEBOOK_POST_LIMIT
  FACEBOOK_COMMENT_LIMIT
  INSTAGRAM_MEDIA_LIMIT
  INSTAGRAM_COMMENT_LIMIT
)
for limit_name in "${required_limits[@]}"; do
  limit_value="${!limit_name:-}"
  if [[ ! "$limit_value" =~ ^[1-9][0-9]*$ ]]; then
    echo "$limit_name must be a positive integer in $POLLING_CONFIG_FILE" >&2
    exit 1
  fi
done

if [[ -f "$SCRIPT_DIR/.venv/bin/activate" ]]; then
  source "$SCRIPT_DIR/.venv/bin/activate"
fi

export PYTHONPATH="$SCRIPT_DIR"
LOG="${CRON_LOG:-$SCRIPT_DIR/data/polling.log}"
mkdir -p "$(dirname "$LOG")"

{
  echo ""
  echo "==== Polling cycle started: $(date +"%Y-%m-%d %H:%M:%S %Z") ===="
  echo "Config: $POLLING_CONFIG_FILE"
  echo "Limits: YouTube ${YOUTUBE_VIDEO_LIMIT} videos/${YOUTUBE_COMMENT_LIMIT} comments/${YOUTUBE_PUBLISH_LIMIT} publishes; Facebook ${FACEBOOK_POST_LIMIT} posts/${FACEBOOK_COMMENT_LIMIT} comments; Instagram ${INSTAGRAM_MEDIA_LIMIT} media/${INSTAGRAM_COMMENT_LIMIT} comments."
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
    echo "[$(date +"%H:%M:%S")] YouTube: publishing up to $YOUTUBE_PUBLISH_LIMIT pending replies..."
    if python -m app.cli post --platform youtube --pending --limit "$YOUTUBE_PUBLISH_LIMIT"; then
      echo "[$(date +"%H:%M:%S")] YouTube: publishing completed."
    else
      failures=$((failures + 1))
      echo "[$(date +"%H:%M:%S")] YouTube: publishing failed."
    fi
  fi

  echo "==== Polling cycle finished: $(date +"%Y-%m-%d %H:%M:%S %Z"); failed steps: $failures ===="
  exit "$failures"
} >>"$LOG" 2>&1

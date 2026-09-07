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

LOG_FILE="${POLLING_LOG_FILE:-${CRON_LOG:-data/polling.log}}"
if [[ "$LOG_FILE" != /* ]]; then
  LOG_FILE="$SCRIPT_DIR/$LOG_FILE"
fi
mkdir -p "$(dirname "$LOG_FILE")"
exec >>"$LOG_FILE" 2>&1

required_limits=(
  YOUTUBE_VIDEO_LIMIT
  YOUTUBE_COMMENT_LIMIT
  YOUTUBE_PUBLISH_LIMIT
  PUBLISH_ERROR_LIMIT
  FACEBOOK_POST_LIMIT
  FACEBOOK_COMMENT_LIMIT
  FACEBOOK_PUBLISH_LIMIT
  INSTAGRAM_MEDIA_LIMIT
  INSTAGRAM_COMMENT_LIMIT
  INSTAGRAM_PUBLISH_LIMIT
)
for limit_name in "${required_limits[@]}"; do
  limit_value="${!limit_name:-}"
  if [[ ! "$limit_value" =~ ^[1-9][0-9]*$ ]]; then
    echo "$limit_name must be a positive integer in $POLLING_CONFIG_FILE" >&2
    exit 1
  fi
done

LOCK_DIR="${COMMENT_BOT_LOCK_DIR:-$SCRIPT_DIR/data/reply_comments.lock}"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  lock_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [[ "$lock_pid" =~ ^[0-9]+$ ]] && kill -0 "$lock_pid" 2>/dev/null; then
    echo "[$(date +"%Y-%m-%d %H:%M:%S %Z")] Another reply_comments.sh cycle is already running (PID $lock_pid); this cycle was skipped."
    exit 0
  fi
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || true
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "Could not acquire run lock: $LOCK_DIR" >&2
    exit 1
  fi
fi
echo "$$" > "$LOCK_DIR/pid"

current_pid=""
cleanup() {
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
stop_cycle() {
  echo "[$(date +"%H:%M:%S")] Stop requested; ending the active operation cleanly."
  if [[ -n "$current_pid" ]]; then
    kill -TERM "$current_pid" 2>/dev/null || true
    wait "$current_pid" 2>/dev/null || true
  fi
  exit 130
}
run_cli() {
  python -m app.cli "$@" &
  current_pid=$!
  wait "$current_pid"
  command_status=$?
  current_pid=""
  return "$command_status"
}
trap cleanup EXIT
trap stop_cycle INT TERM HUP

if [[ -f "$SCRIPT_DIR/.venv/bin/activate" ]]; then
  source "$SCRIPT_DIR/.venv/bin/activate"
fi

export PYTHONPATH="$SCRIPT_DIR"
meta_webhook_enabled="$(python -c 'from app import config; print(str(config.META_WEBHOOK_ENABLED).lower())')"
echo ""
echo "==== Polling cycle started: $(date +"%Y-%m-%d %H:%M:%S %Z") ===="
echo "Config: $POLLING_CONFIG_FILE"
echo "Log: $LOG_FILE"
echo "Limits: YouTube ${YOUTUBE_VIDEO_LIMIT} videos/${YOUTUBE_COMMENT_LIMIT} comments/${YOUTUBE_PUBLISH_LIMIT} publishes; Facebook ${FACEBOOK_POST_LIMIT} posts/${FACEBOOK_COMMENT_LIMIT} comments/${FACEBOOK_PUBLISH_LIMIT} publishes; Instagram ${INSTAGRAM_MEDIA_LIMIT} media/${INSTAGRAM_COMMENT_LIMIT} comments/${INSTAGRAM_PUBLISH_LIMIT} publishes; stop after ${PUBLISH_ERROR_LIMIT} consecutive publish errors."
failures=0
youtube_poll_ok=false

  echo "[$(date +"%H:%M:%S")] YouTube: polling latest comments..."
  if run_cli poll; then
    youtube_poll_ok=true
    echo "[$(date +"%H:%M:%S")] YouTube: polling completed."
  else
    failures=$((failures + 1))
    echo "[$(date +"%H:%M:%S")] YouTube: polling failed; publishing skipped for this cycle."
  fi

  if [[ "$meta_webhook_enabled" != "true" ]]; then
    echo "[$(date +"%H:%M:%S")] Facebook: polling latest comments..."
    if run_cli poll-facebook; then
      echo "[$(date +"%H:%M:%S")] Facebook: polling completed."
    else
      failures=$((failures + 1))
      echo "[$(date +"%H:%M:%S")] Facebook: polling failed."
    fi

    echo "[$(date +"%H:%M:%S")] Instagram: polling latest comments..."
    if run_cli poll-instagram; then
      echo "[$(date +"%H:%M:%S")] Instagram: polling completed."
    else
      failures=$((failures + 1))
      echo "[$(date +"%H:%M:%S")] Instagram: polling failed."
    fi

    echo "[$(date +"%H:%M:%S")] Instagram: publishing pending replies..."
    if run_cli post --platform instagram --pending --retry-failed --limit "$INSTAGRAM_PUBLISH_LIMIT"; then
      echo "[$(date +"%H:%M:%S")] Instagram: publishing completed."
    else
      failures=$((failures + 1))
      echo "[$(date +"%H:%M:%S")] Instagram: publishing failed."
    fi

    echo "[$(date +"%H:%M:%S")] Facebook: publishing pending replies and likes..."
    if run_cli post --platform facebook --pending --retry-failed --like-comments --limit "$FACEBOOK_PUBLISH_LIMIT"; then
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
    if run_cli post --platform youtube --pending --retry-failed --limit "$YOUTUBE_PUBLISH_LIMIT"; then
      echo "[$(date +"%H:%M:%S")] YouTube: publishing completed."
    else
      failures=$((failures + 1))
      echo "[$(date +"%H:%M:%S")] YouTube: publishing failed."
    fi
  fi

echo "==== Polling cycle finished: $(date +"%Y-%m-%d %H:%M:%S %Z"); failed steps: $failures ===="
exit "$failures"

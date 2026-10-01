#!/usr/bin/env bash
# One legacy Meta (Facebook/Instagram) polling tick for a billing tenant, run
# on the VM's host cron. Unlike scripts/reply_comments.sh (which uses the
# host .venv directly against the main instance's data/), this runs the CLI
# *inside* the tenant's own dashboard container via `docker compose exec` --
# deliberately avoiding the host-venv-vs-Docker-container ownership clashes
# that twice broke comments.db permissions on this VM (see project memory:
# tenant_data_dir_permissions_incident, host_venv_writes_corrupt_encrypted_db).
# YouTube is handled separately by the tenant's own dedicated
# <tenant_key>-youtube-comments container; this script only covers Meta.
set -euo pipefail

TENANT_KEY="${1:?Usage: reply_comments_tenant.sh <tenant_key>}"

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
TENANT_COMPOSE_FILE="$SCRIPT_DIR/tenants/${TENANT_KEY}.compose.yml"
if [[ ! -f "$TENANT_COMPOSE_FILE" ]]; then
  echo "Tenant compose file not found: $TENANT_COMPOSE_FILE" >&2
  exit 1
fi
SERVICE="${TENANT_KEY}-dashboard"

# Deliberately *not* polling.log: the tenant's YouTube worker container owns
# that file (uid 1000, created mode 0644), and this script runs as the host
# cron user, which only reaches the data/ dir as a *group* member via its
# setgid bit. `exec >>polling.log` therefore failed with EACCES on every
# hourly tick and killed the run before a single line was written -- a cron
# that looked scheduled in syslog but produced no output anywhere.
LOG_FILE="$SCRIPT_DIR/tenants/${TENANT_KEY}/data/meta_polling.log"
mkdir -p "$(dirname "$LOG_FILE")"
if ! { : >>"$LOG_FILE"; } 2>/dev/null; then
  echo "reply_comments_tenant.sh: cannot append to $LOG_FILE; refusing to run blind." >&2
  exit 1
fi
exec >>"$LOG_FILE" 2>&1

LOCK_DIR="$SCRIPT_DIR/tenants/${TENANT_KEY}/data/reply_comments.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  lock_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [[ "$lock_pid" =~ ^[0-9]+$ ]] && kill -0 "$lock_pid" 2>/dev/null; then
    echo "[$(date +"%Y-%m-%d %H:%M:%S %Z")] Another reply_comments_tenant.sh cycle for ${TENANT_KEY} is already running (PID $lock_pid); this cycle was skipped."
    exit 0
  fi
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || true
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "Could not acquire run lock: $LOCK_DIR" >&2
    exit 1
  fi
fi
echo "$$" >"$LOCK_DIR/pid"
cleanup() {
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT

# The host cron user is deliberately not in the `docker` group -- membership
# there is effectively root -- so /var/run/docker.sock is unreadable and a
# bare `docker compose` fails with "permission denied while trying to connect
# to the docker API". It does have passwordless sudo (the Jenkins deploy
# stage relies on the same thing), so probe the socket once and fall back.
DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
  if sudo -n docker info >/dev/null 2>&1; then
    DOCKER=(sudo -n docker)
  else
    echo "[$(date +"%H:%M:%S")] Cannot reach the Docker daemon as $(id -un), and passwordless 'sudo docker' is unavailable. Add the user to the docker group or grant NOPASSWD sudo for docker." >&2
    exit 1
  fi
fi

run_cli() {
  "${DOCKER[@]}" compose -p social-comment-bot -f "$COMPOSE_FILE" -f "$TENANT_COMPOSE_FILE" \
    exec -T "$SERVICE" python -m app.cli "$@"
}

# `docker compose exec` keeps the container's CRLF line endings, so strip them
# before the value is compared.
run_cli_python() {
  "${DOCKER[@]}" compose -p social-comment-bot -f "$COMPOSE_FILE" -f "$TENANT_COMPOSE_FILE" \
    exec -T "$SERVICE" python -c "$1" | tr -d '\r\n'
}

echo ""
echo "==== Polling cycle started for ${TENANT_KEY}: $(date +"%Y-%m-%d %H:%M:%S %Z") ===="
failures=0

# Same guard as scripts/reply_comments.sh: when Meta webhooks are delivering
# in real time, polling here would draft and post the same comments the
# webhook handler already handled -- two independent writers racing to reply
# to one comment. Read it from inside the container so we get the tenant's
# own config rather than the host's.
meta_webhook_enabled="$(run_cli_python 'from app import config; print(str(config.META_WEBHOOK_ENABLED).lower())')" || {
  echo "[$(date +"%H:%M:%S")] Could not read META_WEBHOOK_ENABLED from ${SERVICE}; is the container running?" >&2
  exit 1
}

if [[ "$meta_webhook_enabled" == "true" ]]; then
  echo "[$(date +"%H:%M:%S")] Meta: polling skipped because webhooks are enabled for ${TENANT_KEY}."
else
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

  echo "[$(date +"%H:%M:%S")] Instagram: publishing pending replies and likes..."
  if run_cli post --platform instagram --pending --retry-failed --like-comments; then
    echo "[$(date +"%H:%M:%S")] Instagram: publishing completed."
  else
    failures=$((failures + 1))
    echo "[$(date +"%H:%M:%S")] Instagram: publishing failed."
  fi

  echo "[$(date +"%H:%M:%S")] Facebook: publishing pending replies and likes..."
  if run_cli post --platform facebook --pending --retry-failed --like-comments; then
    echo "[$(date +"%H:%M:%S")] Facebook: publishing completed."
  else
    failures=$((failures + 1))
    echo "[$(date +"%H:%M:%S")] Facebook: publishing failed."
  fi
fi

echo "==== Polling cycle finished for ${TENANT_KEY}: $(date +"%Y-%m-%d %H:%M:%S %Z"); failed steps: $failures ===="
exit "$failures"

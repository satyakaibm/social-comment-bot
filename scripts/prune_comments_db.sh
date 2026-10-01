#!/usr/bin/env bash
# Stop every container touching comments.db, back it up (including its WAL and
# SHM files), prune handled rows, then start again. Keeps comment IDs and
# activity timestamps so replies are not posted twice.
#
# Works on the main instance (no arguments) or on a billing tenant
# (--tenant <key>). Both paths run the CLI *inside a throwaway container built
# from the app's own image* rather than from a host virtualenv: the host .venv
# is maintained separately from the image and has drifted out of sync with it
# before, missing sqlcipher3 and taking the bot down mid-prune (see project
# memory: prune_script_vm_venv_drift). The image always has what the app needs,
# and a tenant's config only exists inside its own container anyway.
#
# Log rotation is NOT handled here -- deploy/logrotate/social-comment-bot owns
# it, so that one thing truncates these files on one schedule.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

PROJECT_NAME="social-comment-bot"

OLDER_THAN_DAYS=""
IMPORT_STATS=false
BACKUP=true
RESTART=true
REBUILD=false
BACKUP_PATH=""
TENANT_KEY=""

usage() {
  cat <<'EOF'
Usage: scripts/prune_comments_db.sh [options]

  --tenant KEY         Prune that billing tenant instead of the main instance
  --older-than-days N  Only prune handled comments older than N days
  --import-stats       Restore dashboard counts from the backup after prune
  --backup PATH        Backup file (default: <data dir>/comments.db.bak)
  --no-backup          Skip backing up comments.db before prune
  --no-restart         Leave the containers stopped
  --rebuild            Restart with docker compose up -d --build
  -h, --help           Show this help

Examples:
  scripts/prune_comments_db.sh --older-than-days 90
  scripts/prune_comments_db.sh --tenant travel_explorer_satya --older-than-days 90
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tenant)
      TENANT_KEY="${2:-}"
      if [[ -z "$TENANT_KEY" ]]; then
        echo "--tenant needs a tenant key" >&2
        exit 1
      fi
      shift 2
      ;;
    --older-than-days)
      OLDER_THAN_DAYS="${2:-}"
      if [[ ! "$OLDER_THAN_DAYS" =~ ^[0-9]+$ ]]; then
        echo "--older-than-days needs a non-negative integer" >&2
        exit 1
      fi
      shift 2
      ;;
    --import-stats)
      IMPORT_STATS=true
      shift
      ;;
    --backup)
      BACKUP_PATH="${2:-}"
      if [[ -z "$BACKUP_PATH" ]]; then
        echo "--backup needs a path" >&2
        exit 1
      fi
      shift 2
      ;;
    --no-backup)
      BACKUP=false
      shift
      ;;
    --no-restart)
      RESTART=false
      shift
      ;;
    --rebuild)
      REBUILD=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

# All three services mount the same data dir and can write comments.db
# concurrently (video-stats and youtube-comments run independently of the
# dashboard), so all of them must be stopped before the file is backed up or
# pruned -- otherwise a live writer can hold WAL frames a plain copy misses.
COMPOSE_FILES=(-f "$SCRIPT_DIR/docker-compose.yml")
if [[ -n "$TENANT_KEY" ]]; then
  TENANT_COMPOSE_FILE="$SCRIPT_DIR/tenants/${TENANT_KEY}.compose.yml"
  if [[ ! -f "$TENANT_COMPOSE_FILE" ]]; then
    echo "Tenant compose file not found: $TENANT_COMPOSE_FILE" >&2
    exit 1
  fi
  COMPOSE_FILES+=(-f "$TENANT_COMPOSE_FILE")
  COMPOSE_SERVICES=(
    "${TENANT_KEY}-dashboard"
    "${TENANT_KEY}-video-stats"
    "${TENANT_KEY}-youtube-comments"
  )
  DATA_DIR="$SCRIPT_DIR/tenants/${TENANT_KEY}/data"
  LABEL="tenant ${TENANT_KEY}"
else
  COMPOSE_SERVICES=(dashboard video-stats youtube-comments)
  DATA_DIR="$SCRIPT_DIR/data"
  LABEL="main instance"
fi
CLI_SERVICE="${COMPOSE_SERVICES[0]}"

DB_PATH="$DATA_DIR/comments.db"
if [[ -z "$BACKUP_PATH" ]]; then
  BACKUP_PATH="$DATA_DIR/comments.db.bak"
elif [[ "$BACKUP_PATH" != /* ]]; then
  BACKUP_PATH="$SCRIPT_DIR/$BACKUP_PATH"
fi
# Both the backup and the stats import run inside the container, which only
# mounts this one directory as /app/data -- so the backup has to live in it.
if [[ "$(dirname "$BACKUP_PATH")" != "$DATA_DIR" ]]; then
  echo "--backup must name a file inside $DATA_DIR (the container sees only that directory)." >&2
  exit 1
fi
BACKUP_NAME="$(basename "$BACKUP_PATH")"

# The host cron user is deliberately not in the `docker` group -- membership
# there is effectively root -- so /var/run/docker.sock is unreadable and a bare
# `docker compose` fails with "permission denied while trying to connect to the
# docker API". It does have passwordless sudo (the Jenkins deploy stage relies
# on the same thing), so probe the socket once and fall back. Same approach as
# scripts/reply_comments_tenant.sh, where this bit the hourly cron.
DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
  if sudo -n docker info >/dev/null 2>&1; then
    DOCKER=(sudo -n docker)
  else
    echo "Cannot reach the Docker daemon as $(id -un), and passwordless 'sudo docker' is unavailable." >&2
    exit 1
  fi
fi

compose() {
  "${DOCKER[@]}" compose -p "$PROJECT_NAME" "${COMPOSE_FILES[@]}" "$@"
}

# A throwaway container from the service's own image, with the same data mount
# and the same env file. --no-deps so it doesn't resurrect the very containers
# we just stopped; ports are not published by `run` unless --service-ports, so
# this cannot collide with the real instance.
run_cli() {
  compose run --rm --no-deps -T "$CLI_SERVICE" "$@"
}

if [[ ! -f "$DB_PATH" ]]; then
  echo "Database not found: $DB_PATH" >&2
  exit 1
fi

echo "==== Prune comments.db started (${LABEL}): $(date +"%Y-%m-%d %H:%M:%S %Z") ===="
echo "DB: $DB_PATH"

# Fail before anything is stopped, not after. The original reason for this
# check was host-venv drift; it still earns its place by catching a wrong
# DB_ENCRYPTION_KEY or an unreadable file while the bot is still serving.
echo "[$(date +"%H:%M:%S")] Verifying database access..."
run_cli python -c "
from app import db
with db.connect() as conn:
    conn.execute('SELECT 1')
"

CONTAINERS_STOPPED=false
RESTART_DONE=false

start_containers() {
  if [[ "$REBUILD" == "true" ]]; then
    echo "[$(date +"%H:%M:%S")] Starting containers with rebuild..."
    compose up -d --build "${COMPOSE_SERVICES[@]}"
  else
    echo "[$(date +"%H:%M:%S")] Starting containers..."
    compose up -d "${COMPOSE_SERVICES[@]}"
  fi
  compose ps
  RESTART_DONE=true
}

on_exit() {
  local exit_code=$?
  if [[ "$exit_code" != 0 && "$CONTAINERS_STOPPED" == "true" && "$RESTART_DONE" == "false" && "$RESTART" == "true" ]]; then
    echo "[$(date +"%H:%M:%S")] Script failed (exit $exit_code) after stopping containers; restarting them so the bot isn't left down..." >&2
    start_containers || echo "Automatic restart also failed -- run 'docker compose up -d' manually." >&2
  fi
}
trap on_exit EXIT

echo "[$(date +"%H:%M:%S")] Stopping containers: ${COMPOSE_SERVICES[*]}..."
compose stop "${COMPOSE_SERVICES[@]}"
CONTAINERS_STOPPED=true

if [[ "$BACKUP" == "true" ]]; then
  # Fold the WAL into the main file first so the single comments.db copied
  # below is a complete, self-contained snapshot -- import-stats opens the
  # backup path directly and never looks for a companion -wal file next to
  # it. Safe now that every writer above is stopped.
  echo "[$(date +"%H:%M:%S")] Checkpointing WAL into $DB_PATH..."
  run_cli python -c "
from app import db
with db.connect() as conn:
    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
"
  # Copy from *inside* the container. comments.db is mode 0600 owned by the
  # container's appuser (db.py's _restrict_db_file chmods it), so a host-side
  # cp cannot even read it -- and a root-side cp would drop a root-owned
  # backup into a data dir the container has to keep writing to, which is the
  # ownership trap that has broken this VM twice.
  echo "[$(date +"%H:%M:%S")] Backing up to $BACKUP_PATH..."
  run_cli python -c "
import pathlib, shutil
src = pathlib.Path('/app/data/comments.db')
dst = pathlib.Path('/app/data/${BACKUP_NAME}')
shutil.copy2(src, dst)
# Defensive: copy any WAL/SHM remnants too, in case the checkpoint above
# couldn't fully drain them (e.g. a stray reader still had the db open).
for suffix in ('-wal', '-shm'):
    extra = src.with_name(src.name + suffix)
    if extra.exists():
        shutil.copy2(extra, dst.with_name(dst.name + suffix))
print('backed up to', dst)
"
  ls -lh "$DB_PATH" "$BACKUP_PATH"
fi

prune_args=(prune)
if [[ -n "$OLDER_THAN_DAYS" ]]; then
  prune_args+=(--older-than-days "$OLDER_THAN_DAYS")
fi
echo "[$(date +"%H:%M:%S")] Running python -m app.cli ${prune_args[*]}..."
run_cli python -m app.cli "${prune_args[@]}"
ls -lh "$DB_PATH"

if [[ "$IMPORT_STATS" == "true" ]]; then
  if [[ ! -f "$BACKUP_PATH" ]]; then
    echo "Cannot import stats; backup not found: $BACKUP_PATH" >&2
    exit 1
  fi
  echo "[$(date +"%H:%M:%S")] Restoring dashboard counts from $BACKUP_PATH..."
  run_cli python -m app.cli import-stats --backup "/app/data/${BACKUP_NAME}"
fi

if [[ "$RESTART" == "true" ]]; then
  start_containers
else
  echo "[$(date +"%H:%M:%S")] Containers left stopped (--no-restart)."
fi

echo "==== Prune comments.db finished (${LABEL}): $(date +"%Y-%m-%d %H:%M:%S %Z") ===="

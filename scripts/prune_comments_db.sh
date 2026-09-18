#!/usr/bin/env bash
# Stop every container touching comments.db, back it up (including its WAL
# and SHM files), prune handled rows, rotate polling.log, then start again.
# Keeps comment IDs and activity timestamps so replies are not posted twice.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

# All three containers mount ./data and can write comments.db concurrently
# (video-stats and youtube-comments run independently of the dashboard), so
# all of them must be stopped before the file is backed up or pruned --
# otherwise a live writer can hold WAL frames that a plain file copy misses.
COMPOSE_SERVICES=(dashboard video-stats youtube-comments)

OLDER_THAN_DAYS=""
IMPORT_STATS=false
BACKUP=true
RESTART=true
REBUILD=false
BACKUP_PATH=""

usage() {
  cat <<'EOF'
Usage: scripts/prune_comments_db.sh [options]

  --older-than-days N  Only prune handled comments older than N days
  --import-stats       Restore dashboard counts from the backup after prune
  --backup PATH        Backup file (default: data/comments.db.bak)
  --no-backup          Skip backing up comments.db and polling.log before prune
  --no-restart         Leave the containers stopped
  --rebuild            Restart with docker compose up -d --build
  -h, --help           Show this help

Example:
  scripts/prune_comments_db.sh
  scripts/prune_comments_db.sh --older-than-days 90
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
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

if [[ ! -f "$SCRIPT_DIR/.venv/bin/activate" ]]; then
  echo "Virtualenv not found at $SCRIPT_DIR/.venv. Create it first." >&2
  exit 1
fi
# shellcheck disable=SC1091
source "$SCRIPT_DIR/.venv/bin/activate"
export PYTHONPATH="$SCRIPT_DIR"

DB_PATH="$SCRIPT_DIR/data/comments.db"
POLLING_LOG_PATH="$SCRIPT_DIR/data/polling.log"
if [[ -z "$BACKUP_PATH" ]]; then
  BACKUP_PATH="$SCRIPT_DIR/data/comments.db.bak"
elif [[ "$BACKUP_PATH" != /* ]]; then
  BACKUP_PATH="$SCRIPT_DIR/$BACKUP_PATH"
fi
POLLING_LOG_BACKUP_PATH="$(dirname "$BACKUP_PATH")/polling.log.bak"

if [[ ! -f "$DB_PATH" ]]; then
  echo "Database not found: $DB_PATH" >&2
  exit 1
fi

echo "==== Prune comments.db started: $(date +"%Y-%m-%d %H:%M:%S %Z") ===="
echo "DB: $DB_PATH"

# Fail before anything is stopped, not after: the host venv used for this
# script is maintained separately from the app's Docker image (which always
# has sqlcipher3 from requirements.txt) and can drift out of sync with it.
# A DB-open failure here previously surfaced only after the containers were
# already stopped, leaving the bot down.
echo "[$(date +"%H:%M:%S")] Verifying database access..."
python -c "
from app import db
with db.connect() as conn:
    conn.execute('SELECT 1')
"

CONTAINERS_STOPPED=false
RESTART_DONE=false

start_containers() {
  if [[ "$REBUILD" == "true" ]]; then
    echo "[$(date +"%H:%M:%S")] Starting containers with rebuild..."
    docker compose up -d --build
  else
    echo "[$(date +"%H:%M:%S")] Starting containers..."
    docker compose up -d
  fi
  docker compose ps
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
docker compose stop "${COMPOSE_SERVICES[@]}"
CONTAINERS_STOPPED=true

if [[ "$BACKUP" == "true" ]]; then
  mkdir -p "$(dirname "$BACKUP_PATH")"
  # Fold the WAL into the main file first so the single comments.db file
  # copied below is a complete, self-contained snapshot -- import-stats
  # opens the backup path directly and never looks for a companion -wal
  # file next to it. Safe now that every writer above is stopped.
  echo "[$(date +"%H:%M:%S")] Checkpointing WAL into $DB_PATH..."
  python -c "
from app import db
with db.connect() as conn:
    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
"
  echo "[$(date +"%H:%M:%S")] Backing up to $BACKUP_PATH..."
  cp "$DB_PATH" "$BACKUP_PATH"
  # Defensive: copy any WAL/SHM remnants too, in case the checkpoint above
  # couldn't fully drain them (e.g. a stray reader still had the db open).
  for suffix in -wal -shm; do
    if [[ -f "${DB_PATH}${suffix}" ]]; then
      cp "${DB_PATH}${suffix}" "${BACKUP_PATH}${suffix}"
    fi
  done
  ls -lh "$DB_PATH" "$BACKUP_PATH"

  if [[ -f "$POLLING_LOG_PATH" ]]; then
    echo "[$(date +"%H:%M:%S")] Backing up and rotating $POLLING_LOG_PATH..."
    cp "$POLLING_LOG_PATH" "$POLLING_LOG_BACKUP_PATH"
    : > "$POLLING_LOG_PATH"
    ls -lh "$POLLING_LOG_BACKUP_PATH" "$POLLING_LOG_PATH"
  fi
fi

prune_args=(prune)
if [[ -n "$OLDER_THAN_DAYS" ]]; then
  prune_args+=(--older-than-days "$OLDER_THAN_DAYS")
fi
echo "[$(date +"%H:%M:%S")] Running python -m app.cli ${prune_args[*]}..."
python -m app.cli "${prune_args[@]}"
ls -lh "$DB_PATH"

if [[ "$IMPORT_STATS" == "true" ]]; then
  if [[ ! -f "$BACKUP_PATH" ]]; then
    echo "Cannot import stats; backup not found: $BACKUP_PATH" >&2
    exit 1
  fi
  echo "[$(date +"%H:%M:%S")] Restoring dashboard counts from $BACKUP_PATH..."
  python -m app.cli import-stats --backup "$BACKUP_PATH"
fi

if [[ "$RESTART" == "true" ]]; then
  start_containers
else
  echo "[$(date +"%H:%M:%S")] Containers left stopped (--no-restart)."
fi

echo "==== Prune comments.db finished: $(date +"%Y-%m-%d %H:%M:%S %Z") ===="

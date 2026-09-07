#!/usr/bin/env bash
# Stop the dashboard, back up comments.db, prune handled rows, then start again.
# Keeps comment IDs and activity timestamps so replies are not posted twice.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

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
  --no-backup          Skip copying comments.db before prune
  --no-restart         Leave the dashboard stopped
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
if [[ -z "$BACKUP_PATH" ]]; then
  BACKUP_PATH="$SCRIPT_DIR/data/comments.db.bak"
elif [[ "$BACKUP_PATH" != /* ]]; then
  BACKUP_PATH="$SCRIPT_DIR/$BACKUP_PATH"
fi

if [[ ! -f "$DB_PATH" ]]; then
  echo "Database not found: $DB_PATH" >&2
  exit 1
fi

echo "==== Prune comments.db started: $(date +"%Y-%m-%d %H:%M:%S %Z") ===="
echo "DB: $DB_PATH"

echo "[$(date +"%H:%M:%S")] Stopping dashboard container..."
docker compose stop dashboard

if [[ "$BACKUP" == "true" ]]; then
  mkdir -p "$(dirname "$BACKUP_PATH")"
  echo "[$(date +"%H:%M:%S")] Backing up to $BACKUP_PATH..."
  cp "$DB_PATH" "$BACKUP_PATH"
  ls -lh "$DB_PATH" "$BACKUP_PATH"
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
  if [[ "$REBUILD" == "true" ]]; then
    echo "[$(date +"%H:%M:%S")] Starting dashboard with rebuild..."
    docker compose up -d --build
  else
    echo "[$(date +"%H:%M:%S")] Starting dashboard..."
    docker compose up -d
  fi
  docker compose ps
else
  echo "[$(date +"%H:%M:%S")] Dashboard left stopped (--no-restart)."
fi

echo "==== Prune comments.db finished: $(date +"%Y-%m-%d %H:%M:%S %Z") ===="

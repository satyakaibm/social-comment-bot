#!/usr/bin/env bash
# One-way daily refresh: pulls a consistent snapshot of the live VM's
# comments.db down into this machine's local data/ directory, so the local
# dashboard (used for testing UI/code changes) shows real-ish data instead
# of a frozen, ever-more-stale copy. Nothing ever goes back to the VM.
#
# Uses Python's sqlite3 backup API (via `docker exec` into the live
# container) rather than a raw file copy -- that gives a consistent
# snapshot even while the live container keeps reading/writing, with no
# need to stop anything on the VM. sqlite3 isn't installed on the VM host
# itself, hence going through the container instead of a plain `sqlite3
# .backup` command.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

PROJECT="project-e1de8eb7-3b06-4142-9b3"
ZONE="us-central1-a"
INSTANCE="social-comment-bot"
REMOTE_SNAPSHOT="/opt/social-comment-bot/data/comments_sync.db"
LOCAL_TMP="data/comments.db.sync.tmp"
LOCAL_DB="data/comments.db"
LOG_FILE="data/db_sync.log"

mkdir -p data
exec >>"$LOG_FILE" 2>&1
echo "==== DB sync started: $(date -u +"%Y-%m-%d %H:%M:%S UTC") ===="

cleanup_remote() {
  gcloud compute ssh "$INSTANCE" --zone="$ZONE" --project="$PROJECT" --tunnel-through-iap --quiet \
    --command="sudo rm -f ${REMOTE_SNAPSHOT}" || true
}
trap cleanup_remote EXIT

gcloud compute ssh "$INSTANCE" --zone="$ZONE" --project="$PROJECT" --tunnel-through-iap --quiet \
  --command="sudo docker exec social-comment-bot-dashboard-1 python -c \"
import sqlite3
src = sqlite3.connect('/app/data/comments.db')
dst = sqlite3.connect('/app/data/comments_sync.db')
src.backup(dst)
dst.close()
src.close()
\" && sudo chmod 644 ${REMOTE_SNAPSHOT}"

gcloud compute scp --zone="$ZONE" --project="$PROJECT" --tunnel-through-iap --quiet \
  "${INSTANCE}:${REMOTE_SNAPSHOT}" "$LOCAL_TMP"

# Swap in the fresh copy. Stop the local container first if it's running,
# so nothing has the old file open mid-swap; restart it after if it was up.
WAS_RUNNING=""
if docker compose ps --status running --services 2>/dev/null | grep -q '^dashboard$'; then
  WAS_RUNNING=1
  docker compose stop dashboard
fi

mv "$LOCAL_TMP" "$LOCAL_DB"
rm -f data/comments.db-wal data/comments.db-shm

if [[ -n "$WAS_RUNNING" ]]; then
  docker compose start dashboard
fi

echo "==== DB sync finished: $(date -u +"%Y-%m-%d %H:%M:%S UTC") ===="

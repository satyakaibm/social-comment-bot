#!/usr/bin/env bash
# Rebuild the images and recreate EVERY container on the VM -- main instance
# and all tenants -- in one pass.
#
#   sudo ./scripts/rebuild_all_containers.sh            # build + recreate all
#   sudo ./scripts/rebuild_all_containers.sh --pull     # git pull first
#   sudo ./scripts/rebuild_all_containers.sh --dry-run  # print the command only
#   sudo ./scripts/rebuild_all_containers.sh --no-build # recreate on current images
#
# Why this exists. Jenkins deploys with:
#
#     cd /opt/social-comment-bot && sudo git pull && sudo docker compose up -d --build
#
# which names no tenant compose file, so it rebuilds the images and recreates
# only the main instance's three containers. Tenant containers keep running
# whatever image they were created with and silently fall behind: on
# 2026-10-04 the main instance was on the day's build while
# travel_explorer_satya's video-stats and youtube-comments were still on an
# image from 2026-10-02, two days and one merged PR behind. Nothing reports
# that drift -- the containers are "healthy", just old.
#
# Two rules this script encodes, both learned the hard way:
#
#   1. NEVER pass --remove-orphans. Every tenant shares one Compose project
#      ("social-comment-bot") but lives in its own -f file. A compose command
#      that omits a tenant's file considers that tenant's containers orphans,
#      and --remove-orphans would delete the live stack of a paying tenant.
#      Instead we pass every tenant file in a single command, so nothing is
#      an orphan in the first place.
#
#   2. A tenant with no YouTube channel must not get a youtube-comments
#      container. billing/cli.py provision always writes all three services
#      into <tenant>.compose.yml, but a Meta-only tenant (gudiakateni) has an
#      empty YOUTUBE_REFRESH_TOKEN, so config.youtube_page_keys() returns
#      nothing and the worker would just log "No YouTube channels are
#      configured" every poll interval forever. We read each tenant's .env
#      and skip that service when the channel isn't configured, rather than
#      keeping a hand-maintained exclusion list that would rot.
set -euo pipefail

REPO="${BOT_REPO_ROOT:-/opt/social-comment-bot}"
PROJECT="social-comment-bot"
HEALTH_URL="http://localhost:9001/api/health"

DO_PULL=0
DO_BUILD=1
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --pull)     DO_PULL=1 ;;
    --no-build) DO_BUILD=0 ;;
    --dry-run)  DRY_RUN=1 ;;
    -h|--help)  sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

cd "$REPO"

# --- Report what is running now, so drift is visible before and after. ------
show_images() {
  printf '%-60s %-14s %s\n' CONTAINER IMAGE STARTED
  docker ps --format '{{.Names}}' | sort | while read -r c; do
    img=$(docker inspect -f '{{.Image}}' "$c" 2>/dev/null || echo "sha256:unknown")
    started=$(docker inspect -f '{{.Created}}' "$c" 2>/dev/null | cut -c1-19)
    printf '%-60s %-14s %s\n' "$c" "${img:7:12}" "$started"
  done
}

echo "=== BEFORE ==="
show_images
echo

if [[ $DO_PULL -eq 1 ]]; then
  echo "=== git pull ==="
  git pull
  echo
fi

# --- Assemble the -f list and the explicit service list. -------------------
# Services are named explicitly rather than letting compose start everything,
# because the tenant compose files always define youtube-comments even for
# tenants that must not run it (see rule 2 above).
COMPOSE_FILES=(-f docker-compose.yml)
SERVICES=(dashboard video-stats youtube-comments)

shopt -s nullglob
for env_file in "$REPO"/tenants/*.env; do
  tenant="$(basename "$env_file" .env)"
  compose_file="$REPO/tenants/$tenant.compose.yml"
  if [[ ! -f "$compose_file" ]]; then
    echo "WARNING: $tenant has an .env but no .compose.yml -- skipping." >&2
    continue
  fi
  COMPOSE_FILES+=(-f "$compose_file")
  SERVICES+=("$tenant-dashboard" "$tenant-video-stats")

  # Only give a tenant the YouTube worker when it actually has a channel.
  # grep -c tolerates the var being absent entirely, not just empty.
  yt="$(sed -n 's/^YOUTUBE_REFRESH_TOKEN=//p' "$env_file" | tr -d '[:space:]')"
  if [[ -n "$yt" ]]; then
    SERVICES+=("$tenant-youtube-comments")
  else
    echo "note: $tenant has no YOUTUBE_REFRESH_TOKEN -- skipping its youtube-comments service."
  fi
done
shopt -u nullglob

BUILD_FLAG=()
[[ $DO_BUILD -eq 1 ]] && BUILD_FLAG=(--build)

echo
echo "=== command ==="
printf 'docker compose -p %s' "$PROJECT"
printf ' %s' "${COMPOSE_FILES[@]}"
printf ' up -d --force-recreate'
printf ' %s' "${BUILD_FLAG[@]+${BUILD_FLAG[@]}}"
printf ' %s' "${SERVICES[@]}"
printf '\n(NOTE: --remove-orphans is deliberately absent -- it would delete other tenants.)\n\n'

if [[ $DRY_RUN -eq 1 ]]; then
  echo "--dry-run: nothing executed."
  exit 0
fi

docker compose -p "$PROJECT" \
  "${COMPOSE_FILES[@]}" \
  up -d --force-recreate "${BUILD_FLAG[@]+${BUILD_FLAG[@]}}" "${SERVICES[@]}"

# --- Wait for the main dashboard, the same gate the Jenkins deploy uses. ----
echo
echo "=== health ==="
for _ in $(seq 1 12); do
  if curl -sf "$HEALTH_URL" >/dev/null 2>&1; then
    echo "main dashboard healthy."
    break
  fi
  sleep 5
done
curl -sf "$HEALTH_URL" >/dev/null 2>&1 || {
  echo "main dashboard did NOT become healthy; check: docker compose -p $PROJECT logs dashboard" >&2
  exit 1
}

# Each tenant dashboard is published on its own 127.0.0.1 port; read the port
# from its env rather than assuming the allocation order in billing.db.
shopt -s nullglob
for env_file in "$REPO"/tenants/*.env; do
  tenant="$(basename "$env_file" .env)"
  port="$(sed -n 's/^DASHBOARD_PORT=//p' "$env_file" | tr -d '[:space:]')"
  [[ -z "$port" ]] && continue
  if curl -sf "http://localhost:$port/api/health" >/dev/null 2>&1; then
    echo "$tenant healthy on $port."
  else
    echo "WARNING: $tenant NOT healthy on $port." >&2
  fi
done
shopt -u nullglob

echo
echo "=== AFTER ==="
show_images
echo
echo "Done. Every container above should now share the freshly built image ids."

#!/usr/bin/env bash
# Clean up old build records/logs for this project's Jenkins job:
# https://jenkins.hindolroad.download/job/social-comment-bot/
#
# Adapted from content_my_trip/scripts/jenkins-media-cleanup.sh -- that
# script also cleans up raw video source clips, which don't exist in this
# project, so only the Jenkins-build-cleanup half made the trip over. It
# also looped over every job on the shared Jenkins controller; this one
# defaults to just this project's job (JENKINS_JOB) so it never touches
# content_my_trip's build history, even though both run on the same
# controller/Docker Desktop daemon (see this repo's Jenkinsfile header
# comment) and the same JENKINS_CONTAINER.
#
# Safe by design: defaults to report-only. Nothing is deleted unless you
# pass --delete, and even then it asks for confirmation before touching
# anything -- unless stdin isn't an interactive terminal (e.g. run via a
# tool that doesn't attach a real TTY for input), in which case it says so
# explicitly and does nothing, rather than silently no-op'ing as if you'd
# answered "no". Pass --yes to confirm without a prompt either way.
# nextBuildNumber is left untouched so numbering keeps incrementing
# normally.
#
# Usage:
#   ./scripts/jenkins-build-cleanup.sh                    # report only, no changes
#   ./scripts/jenkins-build-cleanup.sh --delete            # delete what's found, but
#                                                           # only offered if disk is tight
#   ./scripts/jenkins-build-cleanup.sh --delete --force    # clean up regardless of free space
#   ./scripts/jenkins-build-cleanup.sh --delete --yes      # skip the y/N prompt (e.g. no TTY)
#   KEEP_JENKINS_BUILDS=3 ./scripts/jenkins-build-cleanup.sh --delete
#
# Env vars:
#   JENKINS_JOB          - Jenkins job name to clean up (default: social-comment-bot)
#   KEEP_JENKINS_BUILDS  - how many most-recent builds to keep for that job
#                          (default: 1)
#   MIN_FREE_GB          - host free-disk threshold in GB below which disk is
#                          considered "tight" (default: 50)
#   JENKINS_CONTAINER    - Jenkins container name (default: jenkins-jenkins-1)

set -euo pipefail

DELETE=false
FORCE=false
YES=false
for arg in "$@"; do
  case "$arg" in
    --delete) DELETE=true ;;
    --force) FORCE=true ;;
    --yes|-y) YES=true ;;
  esac
done

JENKINS_JOB="${JENKINS_JOB:-social-comment-bot}"
KEEP_JENKINS_BUILDS="${KEEP_JENKINS_BUILDS:-1}"
MIN_FREE_GB="${MIN_FREE_GB:-50}"
JENKINS_CONTAINER="${JENKINS_CONTAINER:-jenkins-jenkins-1}"

echo "Mode: $([[ "$DELETE" == true ]] && echo 'DELETE (will ask before removing)' || echo 'REPORT ONLY — pass --delete to remove what is found')"
echo

confirm() {
  local msg="$1"
  if $YES; then
    return 0
  fi
  if [[ ! -t 0 ]]; then
    echo "No interactive terminal to confirm '$msg' -- re-run with --yes to skip the prompt, or run this directly in a real terminal." >&2
    return 1
  fi
  read -rp "$msg [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]]
}

# Host free disk in GB.
free_disk_gb() {
  df -g / 2>/dev/null | awk 'NR==2 {print $4}' || \
    df -g /System/Volumes/Data 2>/dev/null | awk 'NR==2 {print $4}'
}

# Old build logs are pure disk cost with no speed benefit to keeping them.
# Still, gating on disk pressure by default keeps this script from
# nagging/deleting on every run when there's no actual need; --force
# bypasses the check.
FREE_GB=$(free_disk_gb)
DISK_TIGHT=false
if [[ -n "$FREE_GB" ]] && [[ "$FREE_GB" -lt "$MIN_FREE_GB" ]]; then
  DISK_TIGHT=true
fi
status_note="fine"
if $DISK_TIGHT; then status_note="tight"; fi
if $FORCE; then status_note="$status_note, --force passed"; fi
echo "Host free disk: ${FREE_GB:-unknown}GB (threshold: ${MIN_FREE_GB}GB) — $status_note"

should_clean() {
  $DISK_TIGHT || $FORCE
}

# ── Old build records/logs for this project's Jenkins job ──────────────────
# Keep only the most recent $KEEP_JENKINS_BUILDS build(s). nextBuildNumber
# is left untouched so numbering keeps incrementing normally. Restarts the
# Jenkins container afterward so it reloads job state fresh from disk
# rather than holding stale in-memory references to deleted builds.
echo
echo "── Old build records/logs for job '$JENKINS_JOB' (keeping last $KEEP_JENKINS_BUILDS) ──"
if docker ps --filter "name=${JENKINS_CONTAINER}" --format '{{.Names}}' | grep -q .; then
  builds=$(docker exec "$JENKINS_CONTAINER" sh -c "ls -1 '/var/jenkins_home/jobs/${JENKINS_JOB}/builds/' 2>/dev/null | grep -E '^[0-9]+\$' | sort -n" || true)
  total=$(echo "$builds" | grep -c . || true)
  if [[ "$total" -eq 0 ]]; then
    echo "Job '$JENKINS_JOB' not found (or has no builds) under $JENKINS_CONTAINER — nothing to do."
  elif [[ "$total" -le "$KEEP_JENKINS_BUILDS" ]]; then
    echo "Nothing to trim — job already has $total build(s), <= $KEEP_JENKINS_BUILDS."
  else
    keep=$(echo "$builds" | tail -n "$KEEP_JENKINS_BUILDS")
    # comm requires both inputs in the *same* sort order to correctly
    # detect matches -- $builds is numerically sorted (1,2,...,9,10,...) so
    # a plain lexicographic re-sort here (not -n) is needed just for this
    # comparison, or comm silently miscompares multi-digit build numbers
    # against single-digit ones and fails to exclude some/all of $keep.
    to_remove=$(comm -23 <(echo "$builds" | sort) <(echo "$keep" | sort))
    size=$(docker exec "$JENKINS_CONTAINER" sh -c "du -sh '/var/jenkins_home/jobs/${JENKINS_JOB}' 2>/dev/null" | cut -f1 || true)
    echo "$total build record(s), ${size} on disk — would remove $(echo "$to_remove" | wc -l | tr -d ' '), keep: $keep"
    if ! should_clean; then
      echo "Disk is fine (${FREE_GB:-unknown}GB >= ${MIN_FREE_GB}GB) — leaving build history as-is. Pass --force to clean up anyway."
    elif $DELETE && confirm "Remove old build records for '$JENKINS_JOB'?"; then
      for b in $to_remove; do
        docker exec "$JENKINS_CONTAINER" sh -c "rm -rf '/var/jenkins_home/jobs/${JENKINS_JOB}/builds/${b}'"
      done
      echo "Restarting Jenkins to reload job state from disk..."
      docker restart "$JENKINS_CONTAINER"
    fi
  fi
else
  echo "Jenkins container '$JENKINS_CONTAINER' not running — skipped."
fi

echo
if $DELETE; then
  echo "Done — review output above for anything skipped/declined."
else
  echo "Re-run with --delete to remove what was found above."
fi

#!/usr/bin/env bash
# Clean up old Jenkins pipeline build records/logs for this project's job(s).
# Adapted from content_my_trip/scripts/jenkins-media-cleanup.sh -- that
# script also cleans up raw video source clips, which don't exist in this
# project, so only the Jenkins-build-cleanup half made the trip over.
#
# social-comment-bot's Jenkinsfile runs on the same shared Jenkins
# controller/Docker Desktop daemon as content_my_trip's (see this repo's
# Jenkinsfile header comment), so this defaults to the same container name
# and will happily trim old builds for every job on that controller, not
# just this project's.
#
# Safe by design: defaults to report-only. Nothing is deleted unless you
# pass --delete, and even then it asks for confirmation before touching
# anything. nextBuildNumber is left untouched so numbering keeps
# incrementing normally.
#
# Usage:
#   ./scripts/jenkins-build-cleanup.sh                    # report only, no changes
#   ./scripts/jenkins-build-cleanup.sh --delete            # delete what's found, but
#                                                           # only offered if disk is tight
#   ./scripts/jenkins-build-cleanup.sh --delete --force    # clean up regardless of free space
#   KEEP_JENKINS_BUILDS=3 ./scripts/jenkins-build-cleanup.sh --delete
#
# Env vars:
#   KEEP_JENKINS_BUILDS  - how many most-recent builds to keep per Jenkins
#                          job (default: 1)
#   MIN_FREE_GB          - host free-disk threshold in GB below which disk is
#                          considered "tight" (default: 50)
#   JENKINS_CONTAINER    - Jenkins container name (default: jenkins-jenkins-1)

set -euo pipefail

DELETE=false
FORCE=false
for arg in "$@"; do
  case "$arg" in
    --delete) DELETE=true ;;
    --force) FORCE=true ;;
  esac
done

KEEP_JENKINS_BUILDS="${KEEP_JENKINS_BUILDS:-1}"
MIN_FREE_GB="${MIN_FREE_GB:-50}"
JENKINS_CONTAINER="${JENKINS_CONTAINER:-jenkins-jenkins-1}"

echo "Mode: $([[ "$DELETE" == true ]] && echo 'DELETE (will ask before each job)' || echo 'REPORT ONLY — pass --delete to remove what is found')"
echo

confirm() {
  local msg="$1"
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

# ── Old Jenkins pipeline build records/logs ─────────────────────────────────
# Keep only the most recent $KEEP_JENKINS_BUILDS build(s) per job.
# nextBuildNumber is left untouched so numbering keeps incrementing
# normally. Restarts the Jenkins container afterward so it reloads job
# state fresh from disk rather than holding stale in-memory references to
# deleted builds.
echo
echo "── Old Jenkins pipeline build records/logs (keeping last $KEEP_JENKINS_BUILDS per job) ──"
if docker ps --filter "name=${JENKINS_CONTAINER}" --format '{{.Names}}' | grep -q .; then
  jobs=$(docker exec "$JENKINS_CONTAINER" sh -c 'ls /var/jenkins_home/jobs/ 2>/dev/null')
  any_old=false
  any_removed=false
  # `ls` output is newline-separated but job names can contain spaces -- a
  # plain `for job in $jobs` word-splits on IFS and silently mangles those
  # into multiple bogus names. `while read` preserves one job name per line
  # regardless of spaces. Reads from fd 3 (not stdin/fd 0) so confirm()'s
  # own interactive `read -rp` inside this loop still gets real stdin
  # instead of being fed leftover job-name lines.
  while IFS= read -r job <&3; do
    [[ -z "$job" ]] && continue
    builds=$(docker exec "$JENKINS_CONTAINER" sh -c "ls -1 '/var/jenkins_home/jobs/${job}/builds/' 2>/dev/null | grep -E '^[0-9]+\$' | sort -n" || true)
    total=$(echo "$builds" | grep -c . || true)
    if [[ "$total" -gt "$KEEP_JENKINS_BUILDS" ]]; then
      any_old=true
      keep=$(echo "$builds" | tail -n "$KEEP_JENKINS_BUILDS")
      to_remove=$(comm -23 <(echo "$builds") <(echo "$keep"))
      size=$(docker exec "$JENKINS_CONTAINER" sh -c "du -sh '/var/jenkins_home/jobs/${job}' 2>/dev/null" | cut -f1 || true)
      echo "Job '$job': $total build record(s), ${size} on disk — would remove $(echo "$to_remove" | wc -l | tr -d ' '), keep: $keep"
      if $DELETE && should_clean && confirm "Remove old build records for '$job'?"; then
        for b in $to_remove; do
          docker exec "$JENKINS_CONTAINER" sh -c "rm -rf '/var/jenkins_home/jobs/${job}/builds/${b}'"
        done
        any_removed=true
      fi
    fi
  done 3<<< "$jobs"
  if ! $any_old; then
    echo "Nothing to trim — every job already has <= $KEEP_JENKINS_BUILDS build(s)."
  elif ! should_clean; then
    echo "Disk is fine (${FREE_GB:-unknown}GB >= ${MIN_FREE_GB}GB) — leaving build history as-is. Pass --force to clean up anyway."
  elif $any_removed; then
    echo "Restarting Jenkins to reload job state from disk..."
    docker restart "$JENKINS_CONTAINER"
  fi
else
  echo "Jenkins container '$JENKINS_CONTAINER' not running — skipped."
fi

echo
if $DELETE; then
  echo "Done — review output above for anything skipped/declined."
else
  echo "Re-run with --delete to remove what was found above (asks per job)."
fi

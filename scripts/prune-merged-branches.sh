#!/usr/bin/env bash
#
# Delete branches whose commits are already in the default branch.
#
# The whole script rests on one rule: a branch is only deleted when every
# commit on it is reachable from the default branch. Deleting such a branch
# removes a *name*, never code — the commits stay in the default branch's
# history. Anything that fails that test is reported, never deleted.
#
# Dry run by default. Nothing is deleted until you pass --yes.
#
#   ./scripts/prune-merged-branches.sh                 # show what would go
#   ./scripts/prune-merged-branches.sh --yes           # delete local + remote
#   ./scripts/prune-merged-branches.sh --yes --local   # local only
#   ./scripts/prune-merged-branches.sh --repo ~/other-project --yes
#
# Exclusions, always applied:
#   - the default branch itself
#   - head branches of OPEN pull requests
#   - branches checked out in any git worktree (someone is working there)
#
set -euo pipefail

APPLY=0 SCOPE=both REPO="" SKIP_PR_CHECK=0 INCLUDE_SQUASHED=0

while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes)           APPLY=1 ;;
    --local)            SCOPE=local ;;
    --remote)           SCOPE=remote ;;
    --repo)             REPO="${2:?--repo needs a path}"; shift ;;
    --skip-pr-check)    SKIP_PR_CHECK=1 ;;
    --include-squashed) INCLUDE_SQUASHED=1 ;;
    -h|--help)          sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

[ -n "$REPO" ] && cd "$REPO"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
  echo "not a git repository: $(pwd)" >&2; exit 1; }
cd "$(git rev-parse --show-toplevel)"

echo "repo: $(pwd)"
git fetch origin --prune --quiet

# --- the default branch, detected not assumed (master here, main elsewhere) ---
DEF="$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||' || true)"
if [ -z "$DEF" ]; then
  for c in main master trunk; do
    git show-ref --verify --quiet "refs/remotes/origin/$c" && { DEF="$c"; break; }
  done
fi
[ -n "$DEF" ] || { echo "could not determine the default branch; set it with:
  git remote set-head origin -a" >&2; exit 1; }
echo "default branch: $DEF"

KEEP="$(mktemp)"; DEL_R="$(mktemp)"; DEL_L="$(mktemp)"; REVIEW="$(mktemp)"
trap 'rm -f "$KEEP" "$DEL_R" "$DEL_L" "$REVIEW"' EXIT

# --- never delete a branch an open PR points at ---
if [ "$SKIP_PR_CHECK" -eq 0 ] && command -v gh >/dev/null 2>&1; then
  if gh pr list --state open --json headRefName -q '.[].headRefName' > "$KEEP" 2>/dev/null; then
    echo "open PR head branches (protected): $(wc -l < "$KEEP" | tr -d ' ')"
  else
    echo "WARNING: could not query open PRs (not authenticated?). Re-run with" >&2
    echo "         --skip-pr-check only if you are sure no PRs are open." >&2
    exit 1
  fi
else
  [ "$SKIP_PR_CHECK" -eq 1 ] && echo "PR check SKIPPED by request"
  command -v gh >/dev/null 2>&1 || echo "gh not installed — PR check skipped"
fi

# --- never delete a branch that is checked out in a worktree ---
git worktree list --porcelain | awk '/^branch /{sub("refs/heads/","",$2); print $2}' >> "$KEEP"

protected() { grep -qxF "$1" "$KEEP"; }

# --- classify remote branches -------------------------------------------------
for ref in $(git for-each-ref --format='%(refname)' refs/remotes/origin | grep -v '/origin/HEAD$'); do
  name="${ref#refs/remotes/origin/}"
  [ "$name" = "$DEF" ] && continue
  protected "$name" && continue
  if git merge-base --is-ancestor "$ref" "origin/$DEF" 2>/dev/null; then
    echo "$name" >> "$DEL_R"
  else
    # Not an ancestor is NOT the same as unmerged: a squash-merged branch never
    # becomes an ancestor. Compare by patch content instead.
    uniq_n="$(git cherry "origin/$DEF" "$ref" 2>/dev/null | grep -c '^+' || true)"
    if [ "${uniq_n:-1}" -eq 0 ] && [ "$INCLUDE_SQUASHED" -eq 1 ]; then
      echo "$name" >> "$DEL_R"
    else
      printf '%-62s %s unique commit(s)\n' "$name" "${uniq_n:-?}" >> "$REVIEW"
    fi
  fi
done

# --- classify local branches --------------------------------------------------
for b in $(git for-each-ref --format='%(refname:short)' refs/heads); do
  [ "$b" = "$DEF" ] && continue
  protected "$b" && continue
  git merge-base --is-ancestor "$b" "origin/$DEF" 2>/dev/null && echo "$b" >> "$DEL_L"
done

r=$(wc -l < "$DEL_R" | tr -d ' '); l=$(wc -l < "$DEL_L" | tr -d ' ')
v=$(wc -l < "$REVIEW" | tr -d ' ')

echo
echo "remote branches fully merged into $DEF : $r"
echo "local  branches fully merged into $DEF : $l"
echo "needs review (not fully merged)        : $v"

if [ "$v" -gt 0 ]; then
  echo
  echo "--- NOT deleted, review these yourself ---"
  sort "$REVIEW" | sed 's/^/  /'
  echo "  inspect one with:  git cherry -v origin/$DEF <branch>"
  echo "  '0 unique commit(s)' means it was squash-merged and is safe;"
  echo "  re-run with --include-squashed to delete those too."
fi

# sanity: the default branch must never be in a delete list
for f in "$DEL_R" "$DEL_L"; do
  if grep -qxF "$DEF" "$f"; then
    echo "ABORT: default branch '$DEF' ended up in a delete list — refusing." >&2
    exit 1
  fi
done

if [ "$APPLY" -eq 0 ]; then
  echo
  echo "DRY RUN — nothing deleted. Branches that would be deleted:"
  [ "$SCOPE" != local ]  && [ "$r" -gt 0 ] && sed 's/^/  remote  /' "$DEL_R"
  [ "$SCOPE" != remote ] && [ "$l" -gt 0 ] && sed 's/^/  local   /' "$DEL_L"
  echo
  echo "Re-run with --yes to delete."
  exit 0
fi

# --- delete -------------------------------------------------------------------
# xargs, deliberately: `git push` inside a `while read ... done < file` loop
# swallows the loop's stdin and silently deletes nothing. Errors are NOT
# suppressed — a failure here needs to be visible.
if [ "$SCOPE" != local ] && [ "$r" -gt 0 ]; then
  echo; echo "deleting $r remote branches..."
  xargs -n 25 git push origin --delete < "$DEL_R"
fi

if [ "$SCOPE" != remote ] && [ "$l" -gt 0 ]; then
  echo; echo "deleting $l local branches..."
  # -d (not -D): git refuses if it is somehow not merged after all.
  xargs -n 50 git branch -d < "$DEL_L"
fi

echo
git fetch origin --prune --quiet
echo "remaining remote branches:"; git branch -r | grep -v 'origin/HEAD' | sed 's/^/  /'
echo "$DEF is unchanged at: $(git log --oneline -1 "origin/$DEF")"

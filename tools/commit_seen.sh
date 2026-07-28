#!/usr/bin/env bash
# Record content/seen.json on main, separately from any review PR.
#
# Why seen.json is committed directly rather than riding along in a review
# branch: it is pure dedup bookkeeping, not editorial content. If it travelled
# inside review PRs, two pending PRs could each carry their own seen.json diff
# and be merged out of order — and git's line-based text merge has no concept
# of JSON structure, so it can "cleanly" combine two edits into something that
# is a valid text diff but invalid JSON. That happened here once already.
#
# The merge is semantic (pipeline.merge_seen_with_remote), never a git text
# merge: every run rewrites the same `last_run` line to a different value,
# which is a genuine unresolvable conflict that text merging cannot paper over.
set -euo pipefail

if [ ! -f content/seen.json ]; then
  echo "[seen] no content/seen.json — nothing to record."
  exit 0
fi

git config user.name "good-news-bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

REMOTE_SEEN="$(mktemp)"
WORKTREE="$(mktemp -d)/seen-wt"

cleanup() {
  git worktree remove --force "$WORKTREE" >/dev/null 2>&1 || true
  rm -f "$REMOTE_SEEN"
}
trap cleanup EXIT

for attempt in 1 2 3; do
  git fetch -q origin main

  # Semantic merge of this run's ids with whatever is on main right now.
  # Done in the real checkout so pipeline.py stays the single source of truth
  # for the merge rule, and so this job's working tree ends up holding the
  # merged result too.
  git show origin/main:content/seen.json > "$REMOTE_SEEN" 2>/dev/null \
    || echo '{"ids":[]}' > "$REMOTE_SEEN"
  python3 -c "import sys; sys.path.insert(0, '.'); import pipeline; pipeline.merge_seen_with_remote('$REMOTE_SEEN')"

  # Build the commit in a throwaway worktree checked out at the CURRENT tip of
  # main — not on this job's branch, which may be behind.
  #
  # This is the part that makes a retry meaningful. The previous version reset
  # the local commit but never moved the branch, so every attempt re-committed
  # onto the same stale parent and all three were rejected identically, losing
  # the ids without saying so.
  #
  # A worktree rather than `git reset --hard origin/main` on this checkout,
  # because the working tree may hold new or rewritten articles that are not
  # committed yet — a hard reset would silently destroy a whole rewrite run.
  git worktree remove --force "$WORKTREE" >/dev/null 2>&1 || true
  git worktree add --detach --quiet "$WORKTREE" origin/main
  cp content/seen.json "$WORKTREE/content/seen.json"

  git -C "$WORKTREE" add content/seen.json
  if git -C "$WORKTREE" diff --cached --quiet; then
    echo "[seen] already up to date on main."
    exit 0
  fi

  git -C "$WORKTREE" commit -q -m "Update seen-articles tracking $(date -u +'%Y-%m-%d %H:%M')"
  if git -C "$WORKTREE" push -q origin HEAD:main; then
    echo "[seen] recorded on attempt ${attempt}."
    exit 0
  fi

  echo "[seen] push rejected (attempt ${attempt}/3) — main moved; retrying against the new tip…"
done

# Deliberately not fatal: failing to record dedup state must never block the
# review PR that carries the actual articles. The cost of giving up is that
# some already-judged candidates may be reconsidered on the next run.
echo "[seen] could not record seen.json after 3 attempts — some candidates may be reconsidered next run." >&2
exit 0

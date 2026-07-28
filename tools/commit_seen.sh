#!/usr/bin/env bash
# Commit content/seen.json straight to main, separately from any review PR.
#
# Why this is its own script: it was previously duplicated verbatim in six
# places in publish.yml, which meant six places to fix whenever the merge
# logic changed.
#
# Why seen.json is committed directly rather than riding along in a review
# branch: it is pure dedup bookkeeping, not editorial content. If it travelled
# inside review PRs, two pending PRs could each carry their own seen.json diff
# and be merged out of order — and git's line-based text merge has no concept
# of JSON structure, so it can "cleanly" combine two edits into something that
# is a valid text diff but invalid JSON. That is a real corruption that
# happened here once already.
#
# The merge itself is semantic (pipeline.merge_seen_with_remote), not a git
# text merge: every run rewrites the same `last_run` line to a different
# value, which is a genuine unresolvable conflict that automatic text merging
# cannot paper over. Retries with a fresh fetch+merge if another run pushes in
# between.
set -euo pipefail

if [ ! -f content/seen.json ]; then
  echo "[seen] no content/seen.json — nothing to commit."
  exit 0
fi

git config user.name "good-news-bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

for attempt in 1 2 3; do
  git fetch origin main
  git show origin/main:content/seen.json > /tmp/remote_seen.json 2>/dev/null \
    || echo '{"ids":[]}' > /tmp/remote_seen.json

  python3 -c "import sys; sys.path.insert(0, '.'); import pipeline; pipeline.merge_seen_with_remote('/tmp/remote_seen.json')"

  git add content/seen.json
  if git diff --cached --quiet; then
    echo "[seen] nothing changed."
    exit 0
  fi

  git commit -m "Update seen-articles tracking $(date -u +'%Y-%m-%d %H:%M')"
  if git push origin main; then
    echo "[seen] pushed on attempt ${attempt}."
    exit 0
  fi

  echo "[seen] push failed (attempt ${attempt}/3) — another run likely pushed in between; retrying with a fresh merge…"
  git reset --soft HEAD~1
done

echo "[seen] could not push after 3 attempts. The run continues; the next run will merge it." >&2
exit 0

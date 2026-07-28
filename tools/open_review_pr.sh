#!/usr/bin/env bash
# Open a review PR for freshly written content, so nothing goes live until a
# human merges it. Replaces five near-identical copies of this logic in
# publish.yml.
#
# Usage: tools/open_review_pr.sh <branch-prefix> <pr-title-prefix>
#   e.g. tools/open_review_pr.sh daily "daily good news"
#
# Requires GH_TOKEN in the environment. Always ends back on main, so later
# steps (like the site build) only ever see already-approved content.
set -euo pipefail

PREFIX="${1:?usage: open_review_pr.sh <branch-prefix> <pr-title-prefix>}"
TITLE_PREFIX="${2:?usage: open_review_pr.sh <branch-prefix> <pr-title-prefix>}"

git config user.name "good-news-bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

# seen.json is committed separately, straight to main — see tools/commit_seen.sh.
git add content ':!content/seen.json'
if [ -d assets/articles ]; then git add assets/articles; fi

if git diff --cached --quiet; then
  echo "[review] no content changes to review."
  exit 0
fi

STAMP="$(date -u +'%Y-%m-%d %H:%M')"
BRANCH="review/${PREFIX}-$(date -u +%Y%m%d-%H%M%S)"

git checkout -b "$BRANCH"
git commit -m "${TITLE_PREFIX} ${STAMP} — pending review"
git push origin "$BRANCH"

MANUAL_URL="https://github.com/${GITHUB_REPOSITORY}/pull/new/${BRANCH}"
FALLBACK_NOTE="Could not auto-create the PR (often a repo setting — Settings > Actions > General > Workflow permissions > allow PR creation). The branch and commit are safe regardless — open this URL to create the PR manually: ${MANUAL_URL}"

if [ -f pr_description.md ]; then
  gh pr create --base main --head "$BRANCH" \
    --title "Review: ${TITLE_PREFIX} — ${STAMP}" \
    --body-file pr_description.md || echo "$FALLBACK_NOTE"
else
  gh pr create --base main --head "$BRANCH" \
    --title "Review: ${TITLE_PREFIX} — ${STAMP}" \
    --body "Ready for review — see Files changed." || echo "$FALLBACK_NOTE"
fi

git checkout main

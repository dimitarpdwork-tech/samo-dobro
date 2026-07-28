name: Publish selected editorial candidates

on:
  issue_comment:
    types: [created]

permissions:
  contents: write
  pull-requests: write
  issues: write

concurrency:
  group: editorial-publish-${{ github.event.issue.number }}
  cancel-in-progress: false

jobs:
  publish-selected:
    if: >-
      !github.event.issue.pull_request &&
      startsWith(github.event.comment.body, '/publish ')

    runs-on: ubuntu-latest

    steps:
      - name: Validate commenter and issue
        id: validate
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          REPOSITORY: ${{ github.repository }}
          ISSUE_NUMBER: ${{ github.event.issue.number }}
          COMMENT_BODY: ${{ github.event.comment.body }}
          AUTHOR_ASSOCIATION: ${{ github.event.comment.author_association }}
        shell: bash
        run: |
          set -euo pipefail

          case "$AUTHOR_ASSOCIATION" in
            OWNER|MEMBER|COLLABORATOR) ;;
            *)
              echo "Only the repository owner/member/collaborator may publish candidates."
              exit 1
              ;;
          esac

          HAS_LABEL="$(gh issue view "$ISSUE_NUMBER" \
            --repo "$REPOSITORY" \
            --json labels \
            --jq '[.labels[].name] | any(. == "daily-candidates")')"

          if [ "$HAS_LABEL" != "true" ]; then
            echo "This is not a DobroDelo daily-candidates issue."
            exit 1
          fi

          STATE="$(gh issue view "$ISSUE_NUMBER" \
            --repo "$REPOSITORY" \
            --json state \
            --jq '.state')"

          if [ "$STATE" != "OPEN" ]; then
            echo "This shortlist is already closed."
            exit 1
          fi

          python3 - <<'PY'
          import os, re, sys
          body = os.environ["COMMENT_BODY"].strip()
          if re.fullmatch(r"/publish\s+none\s*", body, re.I):
              raise SystemExit(0)
          if not re.fullmatch(r"/publish\s+[0-9,\s]+\s*", body, re.I):
              print("Use /publish 1 3 6, /publish 1,3,6 or /publish none", file=sys.stderr)
              raise SystemExit(2)
          PY

      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Download shortlist issue body
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          REPOSITORY: ${{ github.repository }}
          ISSUE_NUMBER: ${{ github.event.issue.number }}
        run: |
          gh issue view "$ISSUE_NUMBER" \
            --repo "$REPOSITORY" \
            --json body \
            --jq '.body' > /tmp/dobrodelo-candidate-issue.md

      - name: Write only the selected articles
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          PEXELS_API_KEY: ${{ secrets.PEXELS_API_KEY }}
          FAL_API_KEY: ${{ secrets.FAL_API_KEY }}
          COMMENT_BODY: ${{ github.event.comment.body }}
        run: |
          python tools/publish_selected.py \
            --issue-body /tmp/dobrodelo-candidate-issue.md \
            --command "$COMMENT_BODY"

      - name: Make helper scripts executable
        run: chmod +x tools/*.sh

      - name: Commit editorial seen-state directly to main
        run: tools/commit_seen.sh

      - name: Open review PR for written articles
        id: review
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          REPOSITORY: ${{ github.repository }}
        shell: bash
        run: |
          set -euo pipefail

          SAVED="$(python3 - <<'PY'
          import json
          with open("publish_selection_result.json", encoding="utf-8") as f:
              print(json.load(f).get("saved_count", 0))
          PY
          )"

          echo "saved=$SAVED" >> "$GITHUB_OUTPUT"

          if [ "$SAVED" = "0" ]; then
            echo "No article files were written; no review PR needed."
            exit 0
          fi

          git add content ':!content/seen.json'
          if [ -d assets/articles ]; then git add assets/articles; fi

          if git diff --cached --quiet; then
            echo "No content changes to review."
            exit 0
          fi

          BRANCH="review/selected-$(date -u +%Y%m%d-%H%M%S)"
          git checkout -b "$BRANCH"
          git commit -m "Selected good news $(date -u +'%Y-%m-%d %H:%M') — pending review"
          git push origin "$BRANCH"

          PR_URL="$(gh pr create \
            --repo "$REPOSITORY" \
            --base main \
            --head "$BRANCH" \
            --title "Review: selected good news — $(date -u +'%Y-%m-%d %H:%M')" \
            --body-file pr_description.md)"

          echo "pr_url=$PR_URL" >> "$GITHUB_OUTPUT"

      - name: Report result and close shortlist
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          REPOSITORY: ${{ github.repository }}
          ISSUE_NUMBER: ${{ github.event.issue.number }}
          PR_URL: ${{ steps.review.outputs.pr_url }}
        shell: bash
        run: |
          set -euo pipefail

          python3 - <<'PY' > /tmp/editorial-result.md
          import json, os

          with open("publish_selection_result.json", encoding="utf-8") as f:
              r = json.load(f)

          saved = r.get("saved_count", 0)
          selected = r.get("selected_count", 0)
          failed = r.get("failed") or []
          pr_url = os.environ.get("PR_URL", "").strip()

          print(f"✅ Редакционният избор е обработен: **{saved}** написани от **{selected}** избрани.")

          if pr_url:
              print()
              print(f"Review PR: {pr_url}")
          elif selected == 0:
              print()
              print("Днес избра да не се публикува нито една новина.")
          elif saved == 0:
              print()
              print("Нито една от избраните не мина safety/writing проверките, затова няма PR.")

          if failed:
              print()
              print("Неуспели избрани кандидати:")
              for item in failed:
                  print(f"- #{item.get('number')}: {item.get('reason')}")
          PY

          gh issue comment "$ISSUE_NUMBER" \
            --repo "$REPOSITORY" \
            --body-file /tmp/editorial-result.md

          gh issue close "$ISSUE_NUMBER" \
            --repo "$REPOSITORY" \
            --reason completed

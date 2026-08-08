name: Refresh and release idle articles

# Run this from the Actions tab when articles are written but not yet live.
#
# action:
#   redraft-only          Rewrite the bodies from the real source. Nothing goes live.
#   release-only          Clear the embargo. Text is left as it is.
#   redraft-and-release   Both. This is normally what you want.
#
# release_timing:
#   stagger   Spread the articles from now, using the interval in config.json,
#             so they don't all land on the homepage at the same second.
#   now       Publish every one of them immediately.
#
# Nothing goes live from this workflow directly — it opens a review PR.
# The articles appear on the site once you merge it.
#
# Results appear on the run's summary page; no need to read the raw log.

on:
  workflow_dispatch:
    inputs:
      action:
        description: "What to do"
        required: true
        default: redraft-and-release
        type: choice
        options:
          - redraft-only
          - release-only
          - redraft-and-release
      release_timing:
        description: "When released articles go live"
        required: true
        default: stagger
        type: choice
        options:
          - stagger
          - now
      slugs:
        description: "Optional: specific slugs, comma-separated. Blank = every idle article."
        required: false
        default: ""

permissions:
  contents: write
  pull-requests: write

jobs:
  refresh:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Refresh and/or release
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          set -o pipefail

          case "${{ inputs.action }}" in
            redraft-only)        ARGS="--redraft" ;;
            release-only)        ARGS="--release" ;;
            redraft-and-release) ARGS="--redraft --release" ;;
          esac

          ARGS="$ARGS --release-mode ${{ inputs.release_timing }}"

          if [ -n "${{ inputs.slugs }}" ]; then
            ARGS="$ARGS --slugs '${{ inputs.slugs }}'"
          fi

          eval python tools/refresh_and_release.py $ARGS 2>&1 \
            | tee /tmp/refresh-report.txt

      - name: Rebuild the site to confirm nothing broke
        run: python build.py

      - name: Show the result on the summary page
        if: always()
        run: |
          {
            echo "## Refresh and release"
            echo
            echo "Action: \`${{ inputs.action }}\` · timing: \`${{ inputs.release_timing }}\`"
            echo
            echo '```'
            cat /tmp/refresh-report.txt || echo "(the run produced no output)"
            echo '```'
          } >> "$GITHUB_STEP_SUMMARY"

      - name: Open a review PR
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          REPOSITORY: ${{ github.repository }}
        shell: bash
        run: |
          set -euo pipefail

          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

          git add content/articles

          if git diff --cached --quiet; then
            echo "Nothing changed, so there is no PR to open." \
              >> "$GITHUB_STEP_SUMMARY"
            exit 0
          fi

          BRANCH="review/refresh-$(date -u +%Y%m%d-%H%M%S)"
          git checkout -b "$BRANCH"
          git commit -m "Refresh and release idle articles ($(date -u +'%Y-%m-%d %H:%M'))"
          git push origin "$BRANCH"

          PR_URL="$(gh pr create \
            --repo "$REPOSITORY" \
            --base main \
            --head "$BRANCH" \
            --title "Review: refreshed articles — $(date -u +'%Y-%m-%d %H:%M')" \
            --body-file /tmp/refresh-report.txt)"

          echo "Review PR: $PR_URL" >> "$GITHUB_STEP_SUMMARY"

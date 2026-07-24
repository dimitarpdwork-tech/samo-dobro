name: Publish good news

on:
  schedule:
    - cron: "17 5,11,17 * * *"   # three times a day (UTC)
  workflow_dispatch:              # run manually from the Actions tab
    inputs:
      backfill_photos:
        description: "One-off: add real photos to existing articles that don't have one yet (instead of publishing new stories)"
        type: boolean
        default: false
      regenerate_image:
        description: "One-off: regenerate the image for ONE specific article — enter its slug (the part of the URL after /novina/), e.g. atp-wta-ranglista-obyasnena-9825. Opens a review PR so you can look at the new image before it ships."
        type: string
        default: ""
      regenerate_all_images:
        description: "One-off: bulk-convert existing Pexels photos to AI-generated images (config.json's image_provider). Use image_limit below to cap cost/size — recommend starting small."
        type: boolean
        default: false
      image_limit:
        description: "Only used with regenerate_all_images: cap how many articles this run touches. Leave blank to process ALL Pexels-sourced articles at once — not recommended until you've tested a small batch."
        type: string
        default: ""
      check_feeds:
        description: "One-off: just test every RSS feed and report which work (no publishing)"
        type: boolean
        default: false
      recover:
        description: "One-time: recover good stories missed over the last ~72h (may create a few duplicates to delete)"
        type: boolean
        default: false
      list_candidates:
        description: "Diagnostic: list every story the pipeline sees in the last 72h (no AI, no publishing) — just so we can read them"
        type: boolean
        default: false
      rewrite_articles:
        description: "One-time: rewrite existing articles to professional length from their full source (preserves URLs & dates)"
        type: boolean
        default: false
      rewrite_force:
        description: "Only used with rewrite_articles: also reprocess articles already marked rewritten (use when the writing prompt itself changed, e.g. added-value paragraphs, and older rewrites should be brought up to the current standard)"
        type: boolean
        default: false
      rewrite_limit:
        description: "Only used with rewrite_articles: cap how many articles this run processes. Leave blank to process all eligible articles — recommended to test with a small number (e.g. 15) first."
        type: string
        default: ""
      generate_guide:
        description: "One-off: generate one original, web-search-grounded evergreen guide (наръчник) instead of publishing daily news. Targets the thinnest category unless you set guide_category below."
        type: boolean
        default: false
      guide_category:
        description: "Optional, only used with generate_guide: category id to target (e.g. zdrave, priroda, sport). Leave blank to auto-pick the thinnest category."
        type: string
        default: ""
      guide_count:
        description: "Optional, only used with generate_guide: how many guides to generate in this run. Default 1 — each one costs real API time/money, and quality matters more than volume here."
        type: string
        default: "1"
  push:
    branches: [main]              # deploys design/content edits without the AI step

permissions:
  contents: write
  pull-requests: write
  pages: write
  id-token: write

concurrency:
  group: pages
  cancel-in-progress: false

jobs:
  publish:
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Test all RSS feeds (one-off)
        if: github.event.inputs.check_feeds == 'true'
        run: python pipeline.py --check-feeds

      - name: Rewrite existing articles (one-time)
        if: github.event.inputs.rewrite_articles == 'true'
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          PEXELS_API_KEY: ${{ secrets.PEXELS_API_KEY }}
          FAL_API_KEY: ${{ secrets.FAL_API_KEY }}
        run: |
          ARGS="--rewrite-articles"
          if [ "${{ github.event.inputs.rewrite_force }}" = "true" ]; then
            ARGS="$ARGS --rewrite-force"
          fi
          if [ -n "${{ github.event.inputs.rewrite_limit }}" ]; then
            ARGS="$ARGS --rewrite-limit ${{ github.event.inputs.rewrite_limit }}"
          fi
          python pipeline.py $ARGS

      - name: Open review PR (after rewrite)
        # Opens a PR instead of committing to main directly — nothing goes
        # live until a human merges it. Does nothing if there's nothing new
        # to review. Ends back on main so later steps (like the site build)
        # only ever see already-approved content, never anything pending.
        if: github.event.inputs.rewrite_articles == 'true'
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          git config user.name "good-news-bot"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          # seen.json is pure dedup bookkeeping, not editorial content — commit
          # it directly and immediately, separate from the reviewed article
          # content. This prevents a real corruption found tonight: if
          # seen.json rode along inside a review-PR branch, two separate
          # pending PRs each carrying their own seen.json diff could get
          # merged out of order, and git's line-based text merge has no
          # concept of JSON structure — it can "cleanly" combine two edits
          # into something that reads as a valid text diff but invalid JSON.
          if [ -f content/seen.json ]; then
            git add content/seen.json
            if ! git diff --cached --quiet; then
              git commit -m "Update seen-articles tracking $(date -u +'%Y-%m-%d %H:%M')"
              git pull --rebase origin main
              git push
            fi
          fi
          git add content ':!content/seen.json'
          if [ -d assets/articles ]; then git add assets/articles; fi
          if ! git diff --cached --quiet; then
            BRANCH="review/rewrite-$(date -u +%Y%m%d-%H%M%S)"
            git checkout -b "$BRANCH"
            git commit -m "Rewrite pass $(date -u +'%Y-%m-%d %H:%M') — pending review"
            git push origin "$BRANCH"
            PR_URL="https://github.com/${{ github.repository }}/pull/new/$BRANCH"
            if [ -f pr_description.md ]; then
              gh pr create --base main --head "$BRANCH" \
                --title "Review: rewritten articles — $(date -u +'%Y-%m-%d %H:%M')" \
                --body-file pr_description.md \
                || echo "Could not auto-create the PR (often a repo setting — Settings > Actions > General > Workflow permissions > allow PR creation). The branch and commit are safe regardless — open this URL to create the PR manually: $PR_URL"
            else
              gh pr create --base main --head "$BRANCH" \
                --title "Review: rewritten articles — $(date -u +'%Y-%m-%d %H:%M')" \
                --body "Rewritten articles ready for review — see Files changed." \
                || echo "Could not auto-create the PR — open this URL to create it manually: $PR_URL"
            fi
            git checkout main
          fi

      - name: List candidates (diagnostic, no publishing)
        if: github.event.inputs.list_candidates == 'true'
        run: python pipeline.py --list-candidates

      - name: Recover missed stories (one-time)
        if: github.event.inputs.recover == 'true'
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          PEXELS_API_KEY: ${{ secrets.PEXELS_API_KEY }}
          FAL_API_KEY: ${{ secrets.FAL_API_KEY }}
        run: python pipeline.py --recover

      - name: Open review PR (after recover)
        if: github.event.inputs.recover == 'true'
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          git config user.name "good-news-bot"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          # seen.json is pure dedup bookkeeping, not editorial content — commit
          # it directly and immediately, separate from the reviewed article
          # content. This prevents a real corruption found tonight: if
          # seen.json rode along inside a review-PR branch, two separate
          # pending PRs each carrying their own seen.json diff could get
          # merged out of order, and git's line-based text merge has no
          # concept of JSON structure — it can "cleanly" combine two edits
          # into something that reads as a valid text diff but invalid JSON.
          if [ -f content/seen.json ]; then
            git add content/seen.json
            if ! git diff --cached --quiet; then
              git commit -m "Update seen-articles tracking $(date -u +'%Y-%m-%d %H:%M')"
              git pull --rebase origin main
              git push
            fi
          fi
          git add content ':!content/seen.json'
          if [ -d assets/articles ]; then git add assets/articles; fi
          if ! git diff --cached --quiet; then
            BRANCH="review/recover-$(date -u +%Y%m%d-%H%M%S)"
            git checkout -b "$BRANCH"
            git commit -m "Recovered stories $(date -u +'%Y-%m-%d %H:%M') — pending review"
            git push origin "$BRANCH"
            PR_URL="https://github.com/${{ github.repository }}/pull/new/$BRANCH"
            if [ -f pr_description.md ]; then
              gh pr create --base main --head "$BRANCH" \
                --title "Review: recovered stories — $(date -u +'%Y-%m-%d %H:%M')" \
                --body-file pr_description.md \
                || echo "Could not auto-create the PR (often a repo setting — Settings > Actions > General > Workflow permissions > allow PR creation). The branch and commit are safe regardless — open this URL to create the PR manually: $PR_URL"
            else
              gh pr create --base main --head "$BRANCH" \
                --title "Review: recovered stories — $(date -u +'%Y-%m-%d %H:%M')" \
                --body "Recovered stories ready for review — see Files changed." \
                || echo "Could not auto-create the PR — open this URL to create it manually: $PR_URL"
            fi
            git checkout main
          fi

      - name: Generate an evergreen guide article (one-off)
        if: github.event.inputs.generate_guide == 'true'
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          PEXELS_API_KEY: ${{ secrets.PEXELS_API_KEY }}
          FAL_API_KEY: ${{ secrets.FAL_API_KEY }}
        run: |
          if [ -n "${{ github.event.inputs.guide_category }}" ]; then
            python pipeline.py --generate-guide --guide-category "${{ github.event.inputs.guide_category }}" --guide-count "${{ github.event.inputs.guide_count }}"
          else
            python pipeline.py --generate-guide --guide-count "${{ github.event.inputs.guide_count }}"
          fi

      - name: Open review PR (after guide generation)
        if: github.event.inputs.generate_guide == 'true'
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          git config user.name "good-news-bot"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          # seen.json is pure dedup bookkeeping, not editorial content — commit
          # it directly and immediately, separate from the reviewed article
          # content. This prevents a real corruption found tonight: if
          # seen.json rode along inside a review-PR branch, two separate
          # pending PRs each carrying their own seen.json diff could get
          # merged out of order, and git's line-based text merge has no
          # concept of JSON structure — it can "cleanly" combine two edits
          # into something that reads as a valid text diff but invalid JSON.
          if [ -f content/seen.json ]; then
            git add content/seen.json
            if ! git diff --cached --quiet; then
              git commit -m "Update seen-articles tracking $(date -u +'%Y-%m-%d %H:%M')"
              git pull --rebase origin main
              git push
            fi
          fi
          git add content ':!content/seen.json'
          if [ -d assets/articles ]; then git add assets/articles; fi
          if ! git diff --cached --quiet; then
            BRANCH="review/guide-$(date -u +%Y%m%d-%H%M%S)"
            git checkout -b "$BRANCH"
            git commit -m "New guide article $(date -u +'%Y-%m-%d %H:%M') — pending review"
            git push origin "$BRANCH"
            PR_URL="https://github.com/${{ github.repository }}/pull/new/$BRANCH"
            if [ -f pr_description.md ]; then
              gh pr create --base main --head "$BRANCH" \
                --title "Review: new guide article — $(date -u +'%Y-%m-%d %H:%M')" \
                --body-file pr_description.md \
                || echo "Could not auto-create the PR (often a repo setting — Settings > Actions > General > Workflow permissions > allow PR creation). The branch and commit are safe regardless — open this URL to create the PR manually: $PR_URL"
            else
              gh pr create --base main --head "$BRANCH" \
                --title "Review: new guide article — $(date -u +'%Y-%m-%d %H:%M')" \
                --body "New guide ready for review — see Files changed." \
                || echo "Could not auto-create the PR — open this URL to create it manually: $PR_URL"
            fi
            git checkout main
          fi

      - name: Fetch and write new stories (AI pipeline)
        if: github.event_name != 'push' && github.event.inputs.backfill_photos != 'true' && github.event.inputs.regenerate_image == '' && github.event.inputs.regenerate_all_images != 'true' && github.event.inputs.check_feeds != 'true' && github.event.inputs.recover != 'true' && github.event.inputs.list_candidates != 'true' && github.event.inputs.rewrite_articles != 'true' && github.event.inputs.generate_guide != 'true'
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          PEXELS_API_KEY: ${{ secrets.PEXELS_API_KEY }}
          FAL_API_KEY: ${{ secrets.FAL_API_KEY }}
        run: python pipeline.py

      - name: Backfill photos on existing articles (one-off)
        if: github.event.inputs.backfill_photos == 'true'
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          PEXELS_API_KEY: ${{ secrets.PEXELS_API_KEY }}
          FAL_API_KEY: ${{ secrets.FAL_API_KEY }}
        run: python pipeline.py --backfill-photos

      - name: Commit photo backfill (direct — no review needed)
        # Adding a photo to an already-approved article isn't new editorial
        # content, so this stays a direct commit rather than a review PR.
        if: github.event.inputs.backfill_photos == 'true'
        run: |
          git config user.name "good-news-bot"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add content
          if [ -d assets/articles ]; then git add assets/articles; fi
          if ! git diff --cached --quiet; then
            git commit -m "Photo backfill $(date -u +'%Y-%m-%d %H:%M')"
            git pull --rebase origin main
            git push
          fi

      - name: Regenerate one article's image (one-off)
        if: github.event.inputs.regenerate_image != ''
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          PEXELS_API_KEY: ${{ secrets.PEXELS_API_KEY }}
          FAL_API_KEY: ${{ secrets.FAL_API_KEY }}
        run: python pipeline.py --regenerate-image "${{ github.event.inputs.regenerate_image }}"

      - name: Open review PR (after image regeneration)
        # Routed through review, not direct commit — the whole point of this
        # mode is looking at the new image before it goes live.
        if: github.event.inputs.regenerate_image != ''
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          git config user.name "good-news-bot"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          # seen.json is pure dedup bookkeeping, not editorial content — commit
          # it directly and immediately, separate from the reviewed article
          # content. This prevents a real corruption found tonight: if
          # seen.json rode along inside a review-PR branch, two separate
          # pending PRs each carrying their own seen.json diff could get
          # merged out of order, and git's line-based text merge has no
          # concept of JSON structure — it can "cleanly" combine two edits
          # into something that reads as a valid text diff but invalid JSON.
          if [ -f content/seen.json ]; then
            git add content/seen.json
            if ! git diff --cached --quiet; then
              git commit -m "Update seen-articles tracking $(date -u +'%Y-%m-%d %H:%M')"
              git pull --rebase origin main
              git push
            fi
          fi
          git add content ':!content/seen.json'
          if [ -d assets/articles ]; then git add assets/articles; fi
          if ! git diff --cached --quiet; then
            BRANCH="review/image-$(date -u +%Y%m%d-%H%M%S)"
            git checkout -b "$BRANCH"
            git commit -m "Regenerated image for review — $(date -u +'%Y-%m-%d %H:%M')"
            git push origin "$BRANCH"
            PR_URL="https://github.com/${{ github.repository }}/pull/new/$BRANCH"
            if [ -f pr_description.md ]; then
              gh pr create --base main --head "$BRANCH" \
                --title "Review: regenerated image — $(date -u +'%Y-%m-%d %H:%M')" \
                --body-file pr_description.md \
                || echo "Could not auto-create the PR (often a repo setting — Settings > Actions > General > Workflow permissions > allow PR creation). The branch and commit are safe regardless — open this URL to create the PR manually: $PR_URL"
            else
              gh pr create --base main --head "$BRANCH" \
                --title "Review: regenerated image — $(date -u +'%Y-%m-%d %H:%M')" \
                --body "Image regenerated for review — see Files changed." \
                || echo "Could not auto-create the PR — open this URL to create it manually: $PR_URL"
            fi
            git checkout main
          fi

      - name: Bulk-regenerate existing photos (one-off)
        if: github.event.inputs.regenerate_all_images == 'true'
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          PEXELS_API_KEY: ${{ secrets.PEXELS_API_KEY }}
          FAL_API_KEY: ${{ secrets.FAL_API_KEY }}
        run: |
          if [ -n "${{ github.event.inputs.image_limit }}" ]; then
            python pipeline.py --regenerate-all-images --image-limit "${{ github.event.inputs.image_limit }}"
          else
            python pipeline.py --regenerate-all-images
          fi

      - name: Open review PR (after bulk image regeneration)
        if: github.event.inputs.regenerate_all_images == 'true'
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          git config user.name "good-news-bot"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          # seen.json is pure dedup bookkeeping, not editorial content — commit
          # it directly and immediately, separate from the reviewed article
          # content. This prevents a real corruption found tonight: if
          # seen.json rode along inside a review-PR branch, two separate
          # pending PRs each carrying their own seen.json diff could get
          # merged out of order, and git's line-based text merge has no
          # concept of JSON structure — it can "cleanly" combine two edits
          # into something that reads as a valid text diff but invalid JSON.
          if [ -f content/seen.json ]; then
            git add content/seen.json
            if ! git diff --cached --quiet; then
              git commit -m "Update seen-articles tracking $(date -u +'%Y-%m-%d %H:%M')"
              git pull --rebase origin main
              git push
            fi
          fi
          git add content ':!content/seen.json'
          if [ -d assets/articles ]; then git add assets/articles; fi
          if ! git diff --cached --quiet; then
            BRANCH="review/bulk-images-$(date -u +%Y%m%d-%H%M%S)"
            git checkout -b "$BRANCH"
            git commit -m "Bulk photo regeneration for review — $(date -u +'%Y-%m-%d %H:%M')"
            git push origin "$BRANCH"
            PR_URL="https://github.com/${{ github.repository }}/pull/new/$BRANCH"
            if [ -f pr_description.md ]; then
              gh pr create --base main --head "$BRANCH" \
                --title "Review: bulk photo regeneration — $(date -u +'%Y-%m-%d %H:%M')" \
                --body-file pr_description.md \
                || echo "Could not auto-create the PR (often a repo setting — Settings > Actions > General > Workflow permissions > allow PR creation). The branch and commit are safe regardless — open this URL to create the PR manually: $PR_URL"
            else
              gh pr create --base main --head "$BRANCH" \
                --title "Review: bulk photo regeneration — $(date -u +'%Y-%m-%d %H:%M')" \
                --body "Photos regenerated for review — see Files changed." \
                || echo "Could not auto-create the PR — open this URL to create it manually: $PR_URL"
            fi
            git checkout main
          fi
          if [ -d assets/articles ]; then git add assets/articles; fi
          if ! git diff --cached --quiet; then
            git commit -m "Photo backfill $(date -u +'%Y-%m-%d %H:%M')"
            git pull --rebase origin main
            git push
          fi

      - name: Open review PR (daily pipeline)
        if: github.event_name != 'push' && github.event.inputs.backfill_photos != 'true' && github.event.inputs.regenerate_image == '' && github.event.inputs.regenerate_all_images != 'true' && github.event.inputs.check_feeds != 'true' && github.event.inputs.recover != 'true' && github.event.inputs.list_candidates != 'true' && github.event.inputs.rewrite_articles != 'true' && github.event.inputs.generate_guide != 'true'
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          git config user.name "good-news-bot"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          # seen.json is pure dedup bookkeeping, not editorial content — commit
          # it directly and immediately, separate from the reviewed article
          # content. This prevents a real corruption found tonight: if
          # seen.json rode along inside a review-PR branch, two separate
          # pending PRs each carrying their own seen.json diff could get
          # merged out of order, and git's line-based text merge has no
          # concept of JSON structure — it can "cleanly" combine two edits
          # into something that reads as a valid text diff but invalid JSON.
          if [ -f content/seen.json ]; then
            git add content/seen.json
            if ! git diff --cached --quiet; then
              git commit -m "Update seen-articles tracking $(date -u +'%Y-%m-%d %H:%M')"
              git pull --rebase origin main
              git push
            fi
          fi
          git add content ':!content/seen.json'
          if [ -d assets/articles ]; then git add assets/articles; fi
          if ! git diff --cached --quiet; then
            BRANCH="review/daily-$(date -u +%Y%m%d-%H%M%S)"
            git checkout -b "$BRANCH"
            git commit -m "New good news $(date -u +'%Y-%m-%d %H:%M') — pending review"
            git push origin "$BRANCH"
            PR_URL="https://github.com/${{ github.repository }}/pull/new/$BRANCH"
            if [ -f pr_description.md ]; then
              gh pr create --base main --head "$BRANCH" \
                --title "Review: daily good news — $(date -u +'%Y-%m-%d %H:%M')" \
                --body-file pr_description.md \
                || echo "Could not auto-create the PR (often a repo setting — Settings > Actions > General > Workflow permissions > allow PR creation). The branch and commit are safe regardless — open this URL to create the PR manually: $PR_URL"
            else
              gh pr create --base main --head "$BRANCH" \
                --title "Review: daily good news — $(date -u +'%Y-%m-%d %H:%M')" \
                --body "New articles ready for review — see Files changed." \
                || echo "Could not auto-create the PR — open this URL to create it manually: $PR_URL"
            fi
            git checkout main
          fi

      - name: Build the site
        run: python build.py

      - uses: actions/configure-pages@v5

      - uses: actions/upload-pages-artifact@v3
        with:
          path: dist

      - id: deployment
        uses: actions/deploy-pages@v4

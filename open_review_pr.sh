name: Publish good news

on:
  workflow_dispatch:
    inputs:
      backfill_photos:
        description: "One-off: add images to existing articles that don't have one yet (instead of publishing new stories)"
        type: boolean
        default: false
      regenerate_image:
        description: "One-off: regenerate the image for ONE article — enter its slug (the part after /novina/), e.g. atp-wta-ranglista-obyasnena-9825. Opens a review PR so you can see the new image first."
        type: string
        default: ""
      regenerate_all_images:
        description: "One-off: bulk-convert existing Pexels photos to AI-generated images. Use image_limit to cap cost — start small."
        type: boolean
        default: false
      image_limit:
        description: "Only with regenerate_all_images: cap how many articles this run touches. Blank = ALL of them (not recommended until tested)."
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
        description: "Diagnostic: list every story the pipeline sees in the last 72h (no AI, no publishing)"
        type: boolean
        default: false
      rewrite_articles:
        description: "One-time: rewrite existing articles to professional length from their full source (preserves URLs & dates)"
        type: boolean
        default: false
      rewrite_force:
        description: "Only with rewrite_articles: also reprocess articles already marked rewritten (use when the writing prompt itself changed)"
        type: boolean
        default: false
      rewrite_limit:
        description: "Only with rewrite_articles: cap how many articles this run processes. Blank = all eligible. Test with e.g. 15 first."
        type: string
        default: ""
      generate_guide:
        description: "One-off: generate an original, web-search-grounded evergreen guide (наръчник) instead of daily news."
        type: boolean
        default: false
      guide_category:
        description: "Only with generate_guide: category id to target (e.g. zdrave, priroda, sport). Blank = thinnest category."
        type: string
        default: ""
      guide_count:
        description: "Only with generate_guide: how many guides this run. Default 1 — quality matters more than volume."
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

env:
  # Inputs are read into the environment rather than interpolated directly
  # into `run:` blocks, so a value can never be parsed as shell syntax.
  IN_BACKFILL: ${{ github.event.inputs.backfill_photos }}
  IN_REGEN_IMAGE: ${{ github.event.inputs.regenerate_image }}
  IN_REGEN_ALL: ${{ github.event.inputs.regenerate_all_images }}
  IN_IMAGE_LIMIT: ${{ github.event.inputs.image_limit }}
  IN_CHECK_FEEDS: ${{ github.event.inputs.check_feeds }}
  IN_RECOVER: ${{ github.event.inputs.recover }}
  IN_LIST_CANDIDATES: ${{ github.event.inputs.list_candidates }}
  IN_REWRITE: ${{ github.event.inputs.rewrite_articles }}
  IN_REWRITE_FORCE: ${{ github.event.inputs.rewrite_force }}
  IN_REWRITE_LIMIT: ${{ github.event.inputs.rewrite_limit }}
  IN_GUIDE: ${{ github.event.inputs.generate_guide }}
  IN_GUIDE_CATEGORY: ${{ github.event.inputs.guide_category }}
  IN_GUIDE_COUNT: ${{ github.event.inputs.guide_count }}

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

      - name: Make helper scripts executable
        run: chmod +x tools/*.sh

      # ---------------------------------------------------------------- one-offs

      - name: Test all RSS feeds (one-off)
        if: github.event.inputs.check_feeds == 'true'
        run: python pipeline.py --check-feeds

      - name: List candidates (diagnostic, no publishing)
        if: github.event.inputs.list_candidates == 'true'
        run: python pipeline.py --list-candidates

      - name: Rewrite existing articles (one-time)
        if: github.event.inputs.rewrite_articles == 'true'
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          PEXELS_API_KEY: ${{ secrets.PEXELS_API_KEY }}
          FAL_API_KEY: ${{ secrets.FAL_API_KEY }}
        run: |
          ARGS="--rewrite-articles"
          if [ "$IN_REWRITE_FORCE" = "true" ]; then ARGS="$ARGS --rewrite-force"; fi
          if [ -n "$IN_REWRITE_LIMIT" ]; then ARGS="$ARGS --rewrite-limit $IN_REWRITE_LIMIT"; fi
          python pipeline.py $ARGS

      - name: Open review PR (after rewrite)
        if: github.event.inputs.rewrite_articles == 'true'
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          tools/commit_seen.sh
          tools/open_review_pr.sh rewrite "rewritten articles"

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
          tools/commit_seen.sh
          tools/open_review_pr.sh recover "recovered stories"

      - name: Generate an evergreen guide article (one-off)
        if: github.event.inputs.generate_guide == 'true'
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          PEXELS_API_KEY: ${{ secrets.PEXELS_API_KEY }}
          FAL_API_KEY: ${{ secrets.FAL_API_KEY }}
        run: |
          ARGS="--generate-guide --guide-count ${IN_GUIDE_COUNT:-1}"
          if [ -n "$IN_GUIDE_CATEGORY" ]; then ARGS="$ARGS --guide-category $IN_GUIDE_CATEGORY"; fi
          python pipeline.py $ARGS

      - name: Open review PR (after guide generation)
        if: github.event.inputs.generate_guide == 'true'
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          tools/commit_seen.sh
          tools/open_review_pr.sh guide "new guide article"

      - name: Backfill images on existing articles (one-off)
        if: github.event.inputs.backfill_photos == 'true'
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          PEXELS_API_KEY: ${{ secrets.PEXELS_API_KEY }}
          FAL_API_KEY: ${{ secrets.FAL_API_KEY }}
        run: python pipeline.py --backfill-photos

      - name: Commit image backfill (direct — no review needed)
        # Adding an image to an already-approved article isn't new editorial
        # content, so this stays a direct commit rather than a review PR.
        if: github.event.inputs.backfill_photos == 'true'
        run: |
          git config user.name "good-news-bot"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add content
          if [ -d assets/articles ]; then git add assets/articles; fi
          if ! git diff --cached --quiet; then
            git commit -m "Image backfill $(date -u +'%Y-%m-%d %H:%M')"
            git pull --rebase --autostash origin main
            git push
          fi

      - name: Regenerate one article's image (one-off)
        if: github.event.inputs.regenerate_image != ''
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          PEXELS_API_KEY: ${{ secrets.PEXELS_API_KEY }}
          FAL_API_KEY: ${{ secrets.FAL_API_KEY }}
        run: python pipeline.py --regenerate-image "$IN_REGEN_IMAGE"

      - name: Open review PR (after image regeneration)
        # Routed through review, not direct commit — the whole point of this
        # mode is looking at the new image before it goes live.
        if: github.event.inputs.regenerate_image != ''
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          tools/commit_seen.sh
          tools/open_review_pr.sh image "regenerated image"

      - name: Bulk-regenerate existing photos (one-off)
        if: github.event.inputs.regenerate_all_images == 'true'
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          PEXELS_API_KEY: ${{ secrets.PEXELS_API_KEY }}
          FAL_API_KEY: ${{ secrets.FAL_API_KEY }}
        run: |
          ARGS="--regenerate-all-images"
          if [ -n "$IN_IMAGE_LIMIT" ]; then ARGS="$ARGS --image-limit $IN_IMAGE_LIMIT"; fi
          python pipeline.py $ARGS

      - name: Open review PR (after bulk image regeneration)
        if: github.event.inputs.regenerate_all_images == 'true'
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          tools/commit_seen.sh
          tools/open_review_pr.sh bulk-images "bulk photo regeneration"

      # ------------------------------------------------------------ daily path

      - name: Fetch and write new stories (AI pipeline)
        if: >-
          github.event_name != 'push'
          && github.event.inputs.backfill_photos != 'true'
          && github.event.inputs.regenerate_image == ''
          && github.event.inputs.regenerate_all_images != 'true'
          && github.event.inputs.check_feeds != 'true'
          && github.event.inputs.recover != 'true'
          && github.event.inputs.list_candidates != 'true'
          && github.event.inputs.rewrite_articles != 'true'
          && github.event.inputs.generate_guide != 'true'
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          PEXELS_API_KEY: ${{ secrets.PEXELS_API_KEY }}
          FAL_API_KEY: ${{ secrets.FAL_API_KEY }}
        run: python pipeline.py

      - name: Open review PR (daily pipeline)
        if: >-
          github.event_name != 'push'
          && github.event.inputs.backfill_photos != 'true'
          && github.event.inputs.regenerate_image == ''
          && github.event.inputs.regenerate_all_images != 'true'
          && github.event.inputs.check_feeds != 'true'
          && github.event.inputs.recover != 'true'
          && github.event.inputs.list_candidates != 'true'
          && github.event.inputs.rewrite_articles != 'true'
          && github.event.inputs.generate_guide != 'true'
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          tools/commit_seen.sh
          tools/open_review_pr.sh daily "daily good news"

      # ------------------------------------------------------------ build/deploy

      - name: Build the site
        run: python build.py

      - uses: actions/configure-pages@v5

      - uses: actions/upload-pages-artifact@v3
        with:
          path: dist

      - id: deployment
        uses: actions/deploy-pages@v4

      # ---------------------------------------------------------------- facebook

      - name: Post newly approved articles to Facebook
        # Fires ONLY on push-to-main — i.e. only after a human merges a review
        # PR — never at write-time. Runs AFTER the Pages deploy so article URLs
        # are already live when Facebook's crawler scrapes them; posting before
        # deploy would make Facebook cache a 404 link preview. This ordering
        # also means a Facebook failure can never block a deploy.
        #
        # Anything beyond facebook.max_per_run is left queued and picked up by
        # facebook-queue.yml on its schedule. It is NOT picked up by a
        # re-trigger of this workflow: pushes made with the default
        # GITHUB_TOKEN deliberately do not trigger `on: push`, so the queue
        # needs its own scheduled runner.
        if: github.event_name == 'push'
        env:
          FACEBOOK_PAGE_TOKEN: ${{ secrets.FACEBOOK_PAGE_TOKEN }}
          FACEBOOK_PAGE_ID: ${{ secrets.FACEBOOK_PAGE_ID }}
        run: |
          # Give Pages/Cloudflare a moment to settle after the deploy —
          # Facebook caches whatever its first scrape of each URL sees.
          sleep 30
          python pipeline.py --post-facebook

      - name: Commit Facebook bookkeeping (direct — no review needed)
        # fb_posted flags are dedup bookkeeping, not editorial content — same
        # reasoning as seen.json. Direct commit.
        if: github.event_name == 'push'
        run: |
          git config user.name "good-news-bot"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add content/articles
          if ! git diff --cached --quiet; then
            git commit -m "Mark articles as posted to Facebook $(date -u +'%Y-%m-%d %H:%M')"
            git pull --rebase --autostash origin main
            git push
          fi

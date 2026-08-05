name: Make Reels

# Renders vertical Reel videos from published articles and attaches them as a
# downloadable artifact.
#
# It stops at generating the file rather than posting: publishing to Instagram
# needs a Business account linked to the Page plus the Instagram Graph API, and
# that API will only accept a video from a public URL, not an upload. Getting
# there is a real setup job. Until it is worth doing, downloading a finished
# MP4 and posting it by hand takes about a minute, and you keep control of the
# caption — which is the part that actually decides whether a Reel travels.
#
# Videos are NOT committed to the repo: a 2 MB binary per article would bloat
# the history permanently for files you use once.

on:
  schedule:
    # 05:40 UTC = 08:40 Sofia. One fresh video waiting each morning, picked
    # from the previous 24h — batching three and posting them across the week
    # means Friday's Reel carries Monday's news.
    - cron: "40 5 * * *"
  workflow_dispatch:
    inputs:
      slug:
        description: "Article slug (leave blank for the most recent articles)"
        type: string
        default: ""
      count:
        description: "How many recent articles to render (ignored if slug or best is set)"
        type: string
        default: "1"
      best_hours:
        description: "Auto-pick the most shareable article from the last N hours (blank = off)"
        type: string
        default: ""
      duration:
        description: "Seconds per video (single style only)"
        type: string
        default: "12"
      style:
        description: "single = one photo card | story = photo + fact cards + CTA (needs quick_facts)"
        type: choice
        options: ["single", "story"]
        default: "single"

permissions:
  contents: read

jobs:
  reels:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install ffmpeg
        # Installed explicitly rather than assumed. ffmpeg used to ship in the
        # ubuntu-latest runner image, but GitHub has been trimming those images
        # and it is no longer guaranteed to be present — the first run of this
        # workflow failed with "ffmpeg is not installed" for exactly that
        # reason. Takes ~20s and makes the job independent of image contents.
        run: |
          sudo apt-get update -qq
          sudo apt-get install -y -qq ffmpeg
          ffmpeg -version | head -1

      - name: Install Python dependencies
        # requirements.txt is installed in full because make_reel.py imports
        # pipeline.py (for is_animal_story, so "animal story" means the same
        # thing here as in the editorial ranking), and pipeline.py imports
        # requests. Installing only the reel's own dependencies left that
        # import failing with ModuleNotFoundError.
        #
        # fonttools+brotli are extra: they convert the site's own .woff2 into a
        # TTF that Pillow can draw with, so Reels match the site's typography
        # rather than falling back to a generic font.
        run: pip install -r requirements.txt fonttools brotli

      - name: Render Reels
        env:
          IN_SLUG: ${{ github.event.inputs.slug }}
          IN_COUNT: ${{ github.event.inputs.count }}
          IN_DURATION: ${{ github.event.inputs.duration }}
          IN_BEST: ${{ github.event.inputs.best_hours }}
          IN_STYLE: ${{ github.event.inputs.style }}
        run: |
          ARGS="--duration ${IN_DURATION:-12} --style ${IN_STYLE:-single}"
          if [ -n "$IN_SLUG" ]; then
            ARGS="$ARGS --slug $IN_SLUG"
          elif [ -n "$IN_BEST" ]; then
            ARGS="$ARGS --best-hours $IN_BEST"
          elif [ "${{ github.event_name }}" = "schedule" ]; then
            # Scheduled runs always auto-pick the day's best story.
            ARGS="$ARGS --best-hours 24"
          else
            ARGS="$ARGS --latest ${IN_COUNT:-1}"
          fi
          python tools/make_reel.py $ARGS

      - name: Upload videos
        uses: actions/upload-artifact@v4
        with:
          name: reel-${{ github.run_number }}
          path: reels/*.mp4
          retention-days: 14
          # named per-run so the daily videos don't overwrite each other
          if-no-files-found: error

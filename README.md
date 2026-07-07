# Само Добро — a fully automated good-news website

Only good news. An AI editor reads trusted sources several times a day, keeps
**only** the genuinely positive stories, writes original summaries in Bulgarian
with credit + link to every source, and publishes them to a fast, SEO-optimized
static site. No servers to maintain. Hosting is free.

## How it works

    RSS feeds  ──▶  pipeline.py  ──▶  Claude API (editor)  ──▶  content/articles/*.json
                                                                      │
    GitHub Actions (3×/day)  ──▶  build.py  ──▶  dist/  ──▶  GitHub Pages (your domain)

- `pipeline.py` — fetches feeds, asks Claude to select only positive stories and
  write original Bulgarian summaries (never invented facts, never copied text).
- `build.py` — turns the article files into the full website: pages, category
  archives, `sitemap.xml`, RSS `feed.xml`, `robots.txt`, NewsArticle structured
  data, Open Graph tags, favicons.
- `.github/workflows/publish.yml` — runs everything on a schedule and deploys.

## Go live in ~15 minutes

1. **Create a GitHub repository** (e.g. `samo-dobro`) and push this folder to it:

       git init && git add -A && git commit -m "launch"
       git branch -M main
       git remote add origin https://github.com/YOURNAME/samo-dobro.git
       git push -u origin main

2. **Add your API key.** Get one at https://console.anthropic.com → then in the
   repo: *Settings → Secrets and variables → Actions → New repository secret*,
   name it `ANTHROPIC_API_KEY`.

3. **Enable Pages.** *Settings → Pages → Source: GitHub Actions.*

4. **Run it.** *Actions → "Publish good news" → Run workflow.* The pipeline
   fetches real news, the site builds and deploys. From then on it runs
   automatically three times a day.

5. **Set your real address.** In `config.json` change `base_url` to your final
   URL. Until you attach a custom domain, that is
   `https://YOURNAME.github.io/samo-dobro` — and also set `base_path` to
   `/samo-dobro` in that case. With a custom domain, `base_path` stays `""`.

### Custom domain (recommended for SEO)

Buy a domain (e.g. `samodobro.bg`), then: repo *Settings → Pages → Custom domain*,
and at your DNS provider add a `CNAME` record pointing `www` to
`YOURNAME.github.io` (plus the four GitHub Pages `A` records for the apex —
GitHub shows them in the Pages settings). Update `base_url` in `config.json`,
set `base_path` to `""`, commit, done. HTTPS is automatic.

## First-run checklist

- `python pipeline.py --check-feeds` — tests every RSS source and reports
  OK/FAIL. Remove or replace failing ones in `config.json` (media sometimes
  move their feeds). Works locally or as a one-off in Actions.
- Delete the launch seed stories whenever you like:
  `find content -name "seed-*.json" -delete` (then commit).
- Edit `contact_email`, `site_name`, `tagline` in `config.json` if you rebrand.

## Run it locally (optional)

    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=sk-ant-...
    python pipeline.py            # fetch + write new stories
    python build.py               # build the site into dist/
    cd dist && python -m http.server 8000   # open http://localhost:8000

## Costs

- Hosting: **free** (GitHub Pages), automation: **free** (GitHub Actions).
- Claude API: the pipeline uses one small batched call per run with the fast
  Haiku model — typically a few cents per run, roughly **$3–15/month** at
  3 runs/day. Current rates: https://docs.claude.com/en/docs/about-claude/pricing
  You can set a hard monthly spend limit in the Anthropic Console.
- Domain: ~€10–30/year.

## Tune it

- **Sources** — edit `feeds` in `config.json` (any RSS feed works).
- **Frequency** — edit the `cron` line in `.github/workflows/publish.yml`.
- **Volume** — `max_new_per_run` in `config.json`.
- **Taste** — the editorial rules live in `build_prompt()` inside
  `pipeline.py`. Want stricter positivity, more nature, no sports? Say so there.
- **Model** — `model` in `config.json`. `claude-haiku-4-5-20251001` is the
  budget default; switch to `claude-sonnet-4-6` for noticeably nicer prose at
  higher cost.
- **Look** — colors and fonts in `config.json`; layout/CSS in `build.py`.

## SEO launch checklist

1. Verify the site in **Google Search Console** and submit
   `https://yourdomain/sitemap.xml`. Do the same in **Bing Webmaster Tools**.
2. Keep the schedule running — fresh content on a steady rhythm is the single
   biggest ranking factor for a news site.
3. The tech is already handled: NewsArticle structured data, canonical URLs,
   Open Graph/Twitter cards, RSS, mobile-first, no render-blocking JS.
4. After a few weeks of consistent publishing, consider applying to
   **Google Publisher Center** to appear in Google News.

## Editorial honesty (please keep this)

Every story is an *original* AI-written summary of real reporting, with the
source named and linked under the article, and an AI-disclosure note on every
page. The prompt forbids inventing facts. This transparency is not just
ethics — search engines and readers reward it. If a source reports something
wrong, your correction policy is simple: delete the JSON file and commit.

## Troubleshooting

- **Action fails at the pipeline step** → is the `ANTHROPIC_API_KEY` secret set?
- **Feeds return nothing** → run `python pipeline.py --check-feeds`.
- **Site deployed but unstyled at github.io** → set `base_path` (step 5).
- **Want to force a fresh run** → Actions tab → Run workflow.

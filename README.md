# Добро Дело — a human-edited, AI-assisted good-news site

Only good news from Bulgaria. A pipeline reads trusted sources, shortlists the
genuinely positive stories, and a human — not a schedule — decides which ones
get written. Selected stories are drafted in Bulgarian with the source credited
and linked, then reviewed again in a pull request before anything goes live.

Static site, free hosting, no servers.

## How it actually works

    RSS + scraped listings
             │
             ▼
    tools/prepare_candidate_issue.py   ← one cheap Claude call: shortlist only
             │
             ▼
    GitHub Issue: "📰 Кандидати"       ← you comment: /publish 1 3 6
             │                            (or /dismiss 4 to kill one for good)
             │
             ▼
    tools/publish_selected.py          ← writes ONLY the chosen stories,
             │                            generates ONLY their images
             ▼
    Review PR (article text + safety table)   ← you read it; /reject <slug> to drop one
             │
             ▼ merge
    build.py → dist/ → GitHub Pages → Facebook post

Three human checkpoints: choosing the shortlist, reviewing the PR, merging.
Nothing publishes itself.

### Why two phases

Selection is a small, cheap call over many headlines. Writing is an expensive
call per story. Splitting them means you only pay to write what you actually
chose — and a truncated response can never silently drop good picks, because
the selection output is tiny.

## The files

| File | What it does |
|---|---|
| `pipeline.py` | Feeds, candidate collection, Claude calls, article writing, images, Facebook, guides |
| `build.py` | The entire site: pages, categories, tags, city hubs, sitemaps, feed, search, schema |
| `config.json` | Everything editable: feeds, colours, copy, categories, cities, model |
| `tools/prepare_candidate_issue.py` | Builds the daily shortlist issue |
| `tools/publish_selected.py` | Writes only what you picked |
| `tools/dismiss_candidate.py` | Permanently rejects a shortlist candidate (`/dismiss <n>`) |
| `tools/reject_article.py` | Removes one article from a review PR (`/reject <slug>`) |
| `tools/backlog_integrity.py` | Audits historical source attribution and city tags |
| `tools/weekly_roundup.py` | Editorial brief for a human-written weekly roundup |
| `tools/commit_seen.sh` | Semantic JSON merge + commit of `content/seen.json` |
| `tools/open_review_pr.sh` | Opens a review PR for pending content |
| `make_assets.py` | Regenerates favicons and the default OG image from config colours |

## Workflows

| Workflow | Trigger | Does |
|---|---|---|
| `candidate-shortlist.yml` | daily 13:17 UTC + manual | Opens the shortlist issue. Skips if one is still open. |
| `publish-selected.yml` | `/publish …` comment | Writes the chosen stories, opens the review PR, closes the issue. |
| `publish.yml` | manual + push to main | One-off maintenance modes; builds and deploys; posts to Facebook. |
| `dismiss-candidate.yml` | `/dismiss <n>` comment | Permanently rejects a candidate; issue stays open. |
| `reject-article.yml` | `/reject <slug>` comment | Removes one article from an open review PR. |
| `facebook-queue.yml` | every 3h | Drains Facebook posts deferred by `max_per_run`. |
| `backlog-integrity.yml` | manual | Audits source attribution; can open a fix PR. |
| `weekly-roundup.yml` | manual | Opens a weekly editorial brief issue. |

Only owners/members/collaborators can run `/publish`, `/dismiss` or `/reject` —
all three workflows check `author_association` before doing anything.

### Why `/dismiss` exists

Shortlisted candidates are only marked seen at the *end* of a successful
`/publish`. If the writing step fails, or you just close the issue, nothing is
recorded — so a story you keep skipping keeps coming back. `/dismiss 4` records
the rejection immediately and permanently, and leaves the issue open.

Dismissals go into `seen.json`'s `dismissed` list, which — unlike the rolling
`ids` window — is never capped or evicted. A dismissal is forever.

## Setup

1. **Secrets** (*Settings → Secrets and variables → Actions*):
   - `ANTHROPIC_API_KEY` — required.
   - `FAL_API_KEY` — required while `image_provider` is `"fal"`.
   - `PEXELS_API_KEY` — optional stock-photo fallback.
   - `FACEBOOK_PAGE_TOKEN`, `FACEBOOK_PAGE_ID` — optional; without them the
     Facebook steps are a silent no-op.
2. **Pages**: *Settings → Pages → Source: GitHub Actions.*
3. **PR creation**: *Settings → Actions → General → Workflow permissions →*
   allow GitHub Actions to create pull requests.
4. **Domain**: set `base_url` in `config.json`. With a custom domain leave
   `base_path` as `""`; on `USERNAME.github.io/repo` set it to `/repo`.

## Running locally

    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=sk-ant-...
    python pipeline.py --list-candidates    # what the feeds see; no AI, no writes
    python pipeline.py --dry                # candidates only
    python build.py                         # build into dist/
    cd dist && python -m http.server 8000

`--check-feeds` tests every source and reports OK/FAIL. Feeds move and break;
run it when something looks thin.

## Costs

- Hosting and Actions: free.
- Claude: one small shortlist call per day, plus one writing call per story you
  actually select. The model is set in `config.json` (`claude-sonnet-5`).
- Images: FLUX via fal.ai, roughly a fraction of a cent each at 1200×675.
- Domain: ~€10–30/year.

Set a hard monthly spend cap in the Anthropic Console.

## Tuning

- **Sources** — `feeds` in `config.json`. Keep the `name` honest: it becomes the
  public source label, and `backlog_integrity.py` will flag it if the name and
  the URL's domain disagree.
- **Shortlist size** — the `shortlist_size` input on the shortlist workflow.
- **Editorial taste** — `build_editorial_prompt()` in
  `tools/prepare_candidate_issue.py` (what gets shortlisted) and
  `build_writing_prompt()` in `pipeline.py` (how it's written).
- **Animal stories** — `animal_keywords` in `config.json` flags candidates as
  `[ANIMAL PRIORITY]` before the shortlist call, and the shortlist prompt gives
  them clear preference. The prompt deliberately still rejects lost-pet notices
  and donation appeals: an animal story needs a real outcome, not just a plea.
  Animal articles get the `животни` tag, aliased to the `zhivotni` slug.
- **Scraping org sites** — a `scrape` feed can set `link_exclude` (URL
  substrings that mark navigation) and `min_title_chars`, for sites whose
  article URLs are bare root-level slugs.
- **Look** — colours and fonts in `config.json`; layout and CSS in `build.py`.
- **City hubs** — `known_cities` in `config.json`; a city gets a page at
  `MIN_CITY_ARTICLES` (3) articles. Cities are served *only* by
  `/gradove/<city>/`, never also by `/tag/<city>/`.

## Editorial honesty

Every article is an original summary written from one credited, linked source,
selected by a human and reviewed by a human before publication. Article images
are AI-generated illustrations, not photographs of the events, and are labelled
as such. The AI involvement is disclosed on every article, in the About page,
in `llms.txt`, and in the page's structured data.

If something is wrong: delete the JSON file and commit, or email the address in
`config.json`.

## Troubleshooting

- **Shortlist issue didn't appear** → an earlier one is still open. Close it or
  comment `/publish none`.
- **`/publish` did nothing** → check `author_association`; only
  owner/member/collaborator can run it.
- **Feeds returning nothing** → `python pipeline.py --check-feeds`.
- **Facebook posts stalled** → check `FACEBOOK_PAGE_TOKEN` hasn't expired
  (error code 190 in the logs says so explicitly), then run `facebook-queue.yml`
  manually.
- **Site unstyled on github.io** → set `base_path`.

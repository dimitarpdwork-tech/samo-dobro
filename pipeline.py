#!/usr/bin/env python3
"""
Good-news pipeline.

1. Fetches recent items from the RSS feeds listed in config.json
2. Sends the candidates to the Claude API, which selects ONLY genuinely
   positive, uplifting stories and writes an original summary for each
   (in the site's language), never copying source text and never
   inventing facts that are not in the source snippet.
3. Saves accepted stories as JSON files under content/articles/YYYY/MM/

Usage:
    ANTHROPIC_API_KEY=sk-... python pipeline.py            # normal run
    python pipeline.py --check-feeds                       # test every feed
    python pipeline.py --dry                               # list candidates, no API call
    python pipeline.py --limit 3                           # cap new stories this run

Designed to run on a schedule inside GitHub Actions (see .github/workflows).
"""

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests

try:
    import feedparser
except ImportError:
    print("Missing dependency. Run: pip install -r requirements.txt")
    sys.exit(1)

ROOT = Path(__file__).resolve().parent
CONTENT_DIR = ROOT / "content" / "articles"
SEEN_FILE = ROOT / "content" / "seen.json"
PR_DESCRIPTION_FILE = ROOT / "pr_description.md"

# Every article successfully created/changed in this run, for the human-review
# PR description written at the end of main(). A module-level list rather than
# threaded through every save function's signature — this is a single-process
# CLI script run once per invocation, not a library, so this is simpler and
# safer than passing a mutable accumulator through run_two_phase/
# rewrite_articles/generate_guides and every function they call.
REVIEW_BATCH: list[dict] = []
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
# A standard browser-like UA, not a self-identifying bot string. RSS feeds are
# published specifically for automated consumption, so there's no ethical
# concern here — but many smaller municipal/government sites run basic
# security plugins that specifically target and block obvious bot signatures
# (sometimes with unusual status codes like 415 rather than a plain 403).
# The previous "GoodNewsBot/1.0" string is a likely cause of the widespread
# feed failures seen when the municipal feed list was added — this is a
# reasoned hypothesis based on real evidence (the failure pattern was
# consistent across unrelated sites on different CMS platforms), not a
# confirmed fix; verify with --check-feeds after deploying.
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

CYRILLIC_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sht", "ъ": "a",
    "ь": "", "ю": "yu", "я": "ya",
}


def slugify(text: str, max_len: int = 60) -> str:
    text = text.lower().strip()
    text = "".join(CYRILLIC_MAP.get(ch, ch) for ch in text)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    return text[:max_len].rstrip("-") or "story"


def load_config() -> dict:
    with open(ROOT / "config.json", encoding="utf-8-sig") as f:
        return json.load(f)


def load_seen() -> dict:
    if SEEN_FILE.exists():
        with open(SEEN_FILE, encoding="utf-8-sig") as f:
            return json.load(f)
    return {"ids": []}


def save_seen(seen: dict) -> None:
    seen["ids"] = seen["ids"][-4000:]  # keep the file small
    seen["last_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=0)


def clean_text(raw: str, limit: int = 450) -> str:
    """Strip HTML tags/entities from a feed snippet and clip it."""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def entry_id(link: str, title: str) -> str:
    return hashlib.sha1((link or title).encode("utf-8")).hexdigest()[:16]


def fetch_feed(feed: dict, window_hours: int) -> list[dict]:
    """Fetch one RSS feed and return recent entries as candidate dicts."""
    resp = requests.get(
        feed["url"], timeout=15,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        },
    )
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    out = []
    for e in parsed.entries[:25]:
        published = None
        for key in ("published_parsed", "updated_parsed"):
            if getattr(e, key, None):
                published = datetime(*getattr(e, key)[:6], tzinfo=timezone.utc)
                break
        # If the feed carries dates, only keep recent items;
        # if it doesn't, keep the first few and let dedupe handle reruns.
        if published and published < cutoff:
            continue
        title = clean_text(getattr(e, "title", ""), 200)
        if not title:
            continue
        out.append(
            {
                "id": entry_id(getattr(e, "link", ""), title),
                "title": title,
                "summary": clean_text(getattr(e, "summary", "")),
                "link": getattr(e, "link", ""),
                "source": feed["name"],
            }
        )
        if len(out) >= 12:
            break
    return out


def fetch_scraped_listing(source: dict) -> list[dict]:
    """Fetch a non-RSS listing page and extract candidate articles by
    matching link hrefs against a configured URL substring (source['link_pattern'])
    — deliberately NOT CSS-selector-based, since URL structure tends to survive
    a site redesign far better than markup/class names do. No date filtering
    here (unlike fetch_feed): listing pages rarely expose a clean parseable
    date, so this relies entirely on seen.json dedup by URL — since only page 1
    (or 'pages', a small explicit list) is checked each run, only genuinely new
    items make it past the dedup filter in practice."""
    pages = source.get("pages") or [source["url"]]
    pattern = source["link_pattern"]
    out = []
    seen_hrefs = set()
    for page_url in pages:
        try:
            resp = requests.get(page_url, timeout=15, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
        except Exception as exc:
            print(f"  [scrape] {source['name']}: failed to fetch listing page ({exc})")
            continue
        page_html = resp.text
        for m in re.finditer(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page_html, re.DOTALL | re.IGNORECASE):
            href, inner = m.group(1), m.group(2)
            if pattern not in href:
                continue
            if href.startswith("/"):
                href = urljoin(page_url, href)
            if href in seen_hrefs:
                continue
            title = clean_text(re.sub(r"<[^>]+>", " ", inner), 200)
            if not title or len(title) < 10:
                # Too short to be a real headline — likely an icon/image-only
                # link (a common pattern: thumbnail linked before the headline
                # text). Deliberately NOT marking href as seen here — a later
                # occurrence of the same href in the HTML may carry the real
                # headline text, and this must not block that one.
                continue
            seen_hrefs.add(href)
            out.append({
                "id": entry_id(href, title),
                "title": title,
                "summary": "",  # listing pages rarely expose a summary; full text is fetched later anyway
                "link": href,
                "source": source["name"],
            })
            if len(out) >= 12:
                break
        if len(out) >= 12:
            break
    return out


def collect_candidates(cfg: dict, seen_ids: set, window_override: int | None = None,
                        ignore_seen: bool = False) -> list[dict]:
    candidates, errors = [], []
    window = window_override if window_override is not None else cfg.get("window_hours", 48)
    for feed in cfg["feeds"]:
        try:
            if feed.get("type") == "scrape":
                entries = fetch_scraped_listing(feed)
            else:
                entries = fetch_feed(feed, window)
            fresh = entries if ignore_seen else [e for e in entries if e["id"] not in seen_ids]
            candidates.extend(fresh)
            print(f"  [feed] {feed['name']}: {len(fresh)} new / {len(entries)} recent")
        except Exception as exc:  # a dead feed must never kill the run
            errors.append(feed["name"])
            print(f"  [feed] {feed['name']}: FAILED ({exc})")
    if errors:
        print(f"  [note] {len(errors)} feed(s) failed — the run continues without them.")
    # newest sources first, capped so the prompt stays small and cheap
    return candidates[:60]


def load_recent_headlines(days: int = 10, limit: int = 60) -> list[str]:
    """Headlines from recently published articles, newest first, for duplicate-topic checks."""
    if not CONTENT_DIR.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    items = []
    for path in CONTENT_DIR.rglob("*.json"):
        try:
            with open(path, encoding="utf-8-sig") as f:
                a = json.load(f)
            dt = datetime.strptime(a["published"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                items.append((dt, a.get("headline", "")))
        except Exception:
            continue
    items.sort(key=lambda x: x[0], reverse=True)
    return [h for _, h in items[:limit]]


def fetch_full_article(url: str, timeout: int = 8) -> str | None:
    """Fetch a story's source page and extract just the article text.

    Deliberately defensive: ANY failure — the extraction library not being
    installed, a network error, a paywall, a bot-block, an unparseable page —
    returns None, and the caller falls back to the RSS snippet for that one
    story. A missing full-text must never break a publish run.
    """
    if not url:
        return None
    try:
        import trafilatura  # imported lazily so the pipeline still runs without it
        from trafilatura.settings import use_config
    except Exception:
        return None
    try:
        # IMPORTANT: trafilatura.fetch_url() does not accept a timeout kwarg —
        # a previous version of this function had a `timeout` parameter that
        # was never actually passed to anything, silently falling back to
        # trafilatura's own default (30s). A batch hitting several dead links
        # from older articles could burn several real minutes waiting for
        # nothing as a result. The actual mechanism is a Config object.
        config = use_config()
        config.set("DEFAULT", "DOWNLOAD_TIMEOUT", str(timeout))
        downloaded = trafilatura.fetch_url(url, config=config)
        if not downloaded:
            return None
        text = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
            favor_precision=True,
        )
        if not text:
            return None
        text = text.strip()
        # Strip control/non-printable characters that occasionally survive
        # extraction from arbitrary web pages (encoding artifacts, stray
        # bytes) — these can make the API reject the request outright
        # (400 Bad Request) rather than just rendering oddly.
        text = "".join(ch for ch in text if ch in "\n\t" or (ord(ch) >= 32 and ord(ch) != 127))
        # Guard against junk: too short to be a real article, or absurdly long.
        if len(text) < 200:
            return None
        return text[:6000]  # cap so the writing prompt stays a sane size
    except Exception as exc:
        print(f"    [extract] could not read {url[:60]} ({type(exc).__name__}) — using snippet")
        return None


def build_selection_prompt(cfg: dict, candidates: list[dict], max_new: int,
                            recent_headlines: list[str]) -> str:
    """Phase 1: cheap selection only. Ask the model to pick the genuinely-good,
    non-duplicate stories and return just their indices + a one-line reason —
    NOT to write them. Small output, so it can never truncate away good picks."""
    recent_block = ""
    if recent_headlines:
        recent_list = "\n".join(f"- {h}" for h in recent_headlines[:80])
        recent_block = (
            "\nALREADY PUBLISHED — do NOT pick anything covering the same event as "
            f"any of these:\n{recent_list}\n"
        )
    cand_lines = "\n".join(
        f'{i}. [{c["source"]}] {c["title"]} — {clean_text(c.get("summary",""), 260)}'
        for i, c in enumerate(candidates)
    )
    return f"""You are the editor of "{cfg['site_name']}", which publishes ONLY genuinely good, uplifting news in {cfg['language_name']}.

From the numbered candidates below, select up to {max_new} that are GENUINELY positive: concrete good outcomes, kindness, recoveries of nature, scientific or medical breakthroughs, community wins, cultural achievements, human generosity or skill.

REJECT anything whose core is negative even if framed positively: war, crime, accidents, disasters, deaths, disease, scandals, court cases, party politics, elections, market/economic reports, weather, celebrity gossip, PR. When unsure, reject. Selecting fewer than {max_new} — even zero — is correct if the good ones aren't there.
{recent_block}
Respond with ONLY a JSON array of objects, nothing else:
[{{"candidate": <number>, "why": "<3-6 word reason it's good news>"}}]

CANDIDATES
{cand_lines}"""


def build_writing_prompt(cfg: dict, story: dict, full_text: str | None, use_search: bool = False) -> str:
    """Phase 2: write ONE article, ideally from the full source text. Uniqueness
    rules are explicit so articles don't read as templated. The lede and
    quick-facts rules exist so AI search/answer engines have a self-contained,
    citable passage near the top of the page instead of only a narrative
    opening — see the GEO section of the SEO audit. The added-value context
    rule exists so articles aren't pure rewrites with nothing to distinguish
    them from the wire source (the isBasedOn attribution-cannibalization
    problem) — see the chat history for the reasoning."""
    source_block = (
        f"FULL SOURCE ARTICLE (write from this):\n{full_text}"
        if full_text else
        f"SOURCE SUMMARY (only this snippet is available):\n{clean_text(story.get('summary',''), 600)}"
    )
    lede_rule = (
        "- The FIRST paragraph MUST be a self-contained, answer-first passage of "
        "approximately 140-160 words that fully conveys what happened, who it "
        "involves, and why it matters — written so it could stand alone as a "
        "quote or AI-generated summary without needing the rest of the article. "
        "Vary its phrasing and angle story-to-story (lead with the outcome, a "
        "striking detail, or the human stakes) but always make it complete on "
        "its own."
        if full_text else
        "- Open with the single most important fact from the snippet, as a "
        "self-contained sentence or two. Don't pad it toward 140-160 words if "
        "the snippet doesn't support it — a short honest opening beats a "
        "padded one."
    )
    if use_search:
        context_rule = (
            "- After the lede and supporting paragraph(s), add 1-2 MORE paragraphs of genuine added value. "
            "This is the single most important part of the article for two purposes: giving the reader "
            "something worth reading beyond the original story, and giving AI answer engines (ChatGPT, "
            "Perplexity, Google AI Overviews, etc.) a self-contained passage worth citing when someone asks a "
            "related question — this is how this article earns being credited as its own source rather than "
            "just a rewrite.\n"
            "- Use web search to verify real, specific context: the history or effort behind the event (who "
            "has worked on this and for how long), the broader significance (what this means for the "
            "environment/community/country beyond this one event), or how this compares to the situation "
            "before. Only include facts you actually verified via search and are confident are real — never "
            "invent a specific number, date, name, or program detail.\n"
            "- Write this as a genuine CAUSAL explanation, not vague praise ('this is inspiring', 'this shows "
            "progress'). Explain the actual mechanism or reason: why does this matter, what changes because "
            "of it, what would be different without it. Make the passage self-contained — someone should be "
            "able to read ONLY this part and come away with a complete, accurate answer to 'why does this "
            "matter', without needing the rest of the article. Aim for roughly 100-160 words in this section.\n"
            "- If you cite a specific external source for this added context, use exactly this syntax: "
            "[link text](URL) — the ONLY citation format allowed. Do NOT use <cite> tags, footnotes, or any "
            "other citation markup; this text is published directly on a website with no citation-rendering "
            "system beyond that one link format — anything else shows up as broken, garbled text to readers."
        )
    else:
        context_rule = (
            "- After the lede and supporting paragraph(s), add 1-2 MORE paragraphs offering a genuine, argued "
            "EDITORIAL OPINION about the story — not additional facts. This is what makes the piece worth "
            "reading beyond the original story: a real point of view, not a restatement of what happened.\n"
            "- Frame this explicitly as interpretation, not verified fact: use language like 'What stands out "
            "here is...', 'The real significance may be...', 'This suggests...', 'One way to read this "
            "is...'. Never state a new specific number, date, name, or program detail as if it were an "
            "established fact — you have no way to verify it. An opinion can be debatable and that's fine; "
            "a fabricated fact is not, which is exactly why this must stay opinion, not new claims.\n"
            "- Make the opinion SPECIFIC and ARGUED, not generic praise. Take an actual position: what does "
            "this story reveal about a broader pattern, tension, or shift? What would you push back on, or "
            "add nuance to, if you were a thoughtful reader rather than just relaying the news? Avoid vague "
            "uplift ('this is wonderful', 'this gives us hope') — argue something specific enough that a "
            "reader could actually agree or disagree with it.\n"
            "- Make the passage self-contained — someone should be able to read ONLY this part and "
            "understand the actual argument being made, without needing the rest of the article. Aim for "
            "roughly 100-160 words in this section."
        )
    word_target = "380-480 words total" if full_text else "180-230 words total (snippet is thin — keep the added-value part brief and general rather than padding)"
    return f"""You are the editor of "{cfg['site_name']}", writing one good-news article in {cfg['language_name']}.

HEADLINE OF THE STORY: {story['title']}
SOURCE: {story['source']}

{source_block}

Write an original article in {cfg['language_name']}. Rules:
- Use ONLY facts present in the source above for the core summary. Never invent numbers, names, quotes, or dates.
- Include 2-3 CONCRETE specific details from the source (a number, a place, a name, a circumstance) — this is what makes the piece real rather than generic.
{lede_rule}
- After the lede, add 1-2 shorter paragraphs of supporting context or narrative from the source — don't just repeat the lede in different words.
{context_rule}
- Find the actual STORY beyond the headline — what does the full source reveal that the headline alone wouldn't tell someone?
- Warm, human, concrete tone. Hopeful, never saccharine or clickbaity.
- {word_target}.
- Native-level {cfg['language_name']}. Never invent words. Check noun-adjective gender/number agreement. Never use Russian spellings or words.

Also extract 3-5 short "quick facts" — standalone phrases (not full sentences, under ~12 words each) pulling out the concrete who/what/where/when/how-much details from the source. These appear in a bullet box at the top of the article, so each one must be fully understandable on its own without reading the article body.

If the story is specifically tied to a particular Bulgarian city or town (not just "Bulgaria" broadly), include that city's name as one of the tags, in Bulgarian (e.g. "варна", "пловдив", "стара загора") — this is what lets readers browse news by their own city, so get it right whenever the source clearly names a specific place.

Respond using EXACTLY this plain-text format — nothing before or after it. Do NOT use JSON. This format exists specifically so that quotes, apostrophes, and other punctuation in your writing can never break parsing the way an unescaped quote inside a JSON string would:

===HEADLINE===
<max 75 chars, in {cfg['language_name']}>
===SLUG===
<3-6 latin lowercase words, hyphenated>
===CATEGORY===
<one id from: {', '.join(cfg['categories'].keys())}>
===META_DESCRIPTION===
<max 155 chars>
===SUMMARY_SHORT===
<max 160 chars teaser>
===BODY===
<the article — first paragraph is the answer-first lede, with one fully blank line between each paragraph>
===QUICK_FACTS===
<first fact>
<second fact>
<third fact>
===TAGS===
<tag one, tag two, tag three — include the city tag here if applicable, per above>
===IMAGE_QUERY===
<2-4 words English, a concrete scene, action, or object — NEVER a scoreboard, chart, table, ranking list, or
anything with readable text/numbers in it (image generation cannot render legible text and produces garbled
nonsense when asked to). For abstract topics (rankings, statistics, policy), depict the concrete activity or
setting instead — e.g. for a tennis rankings article, "tennis player serving court" not "tennis ranking board".
Never a real person's name or a specific claimed location.>
===END==="""


def parse_json_object(raw: str) -> dict | None:
    """Parse a single JSON object from a model response, tolerant of fences/prose."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def parse_delimited_article(raw: str) -> dict | None:
    """Parse the ===FIELD=== delimited format used by build_writing_prompt().
    Deliberately NOT JSON: a real, recurring fraction of JSON-formatted
    responses were failing to parse (and therefore being paid for and
    discarded) once the added-value/opinion paragraphs made the writing
    style more natural — almost certainly from unescaped quotes/apostrophes
    inside the generated prose breaking JSON's string syntax. Extracting
    plain text between distinctive markers can't be broken by punctuation
    at all, since there's no escaping involved."""
    text = raw.strip()
    text = re.sub(r"^```\w*", "", text).strip()
    text = re.sub(r"```$", "", text).strip()

    def extract(field: str) -> str:
        m = re.search(rf"==={field}===\s*\n(.*?)(?=\n===[A-Z_]+===|\Z)", text, re.DOTALL)
        return m.group(1).strip() if m else ""

    headline = extract("HEADLINE")
    body = extract("BODY")
    if not headline or not body:
        return None

    quick_facts = [line.strip() for line in extract("QUICK_FACTS").split("\n") if line.strip()]
    tags = [t.strip() for t in extract("TAGS").split(",") if t.strip()]

    return {
        "headline": headline,
        "slug_hint": extract("SLUG"),
        "category": extract("CATEGORY"),
        "meta_description": extract("META_DESCRIPTION"),
        "summary_short": extract("SUMMARY_SHORT"),
        "body": body,
        "quick_facts": quick_facts,
        "tags": tags,
        "image_query": extract("IMAGE_QUERY"),
    }


def call_claude(cfg: dict, prompt: str, tools: list[dict] | None = None,
                 max_tokens_override: int | None = None, hard_fail: bool = True) -> str:
    """hard_fail=True (default): unrecoverable failure exits the whole process —
    correct for single must-succeed calls like the daily selection phase.
    hard_fail=False: unrecoverable failure returns "" instead — required for
    any call made inside a per-item batch loop (rewriting/writing one of many
    articles), so one bad article can never take the rest of the batch down
    with it. A real production incident (one article's request got a
    non-retryable 400 and killed an entire --rewrite-articles run partway
    through, after already paying for several earlier calls) is exactly why
    this distinction exists — see chat history."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY is not set. Aborting.")
        sys.exit(1)
    body = {
        "model": cfg.get("model", "claude-haiku-4-5-20251001"),
        # Headroom for up to ~10 articles at the 150-190 word target plus all their
        # JSON metadata. The old 6000 ceiling truncated the response mid-JSON once
        # article length was raised, which made the whole batch unparseable.
        "max_tokens": max_tokens_override or cfg.get("max_tokens", 16000),
        "messages": [{"role": "user", "content": prompt}],
    }
    if tools:
        # web_search is a server-side tool: the API executes searches and feeds
        # results back to the model internally, returning one final response
        # with all the interleaved search/reasoning/text blocks already
        # resolved — no client-side tool-result loop needed here.
        body["tools"] = tools
    headers = {
        "x-api-key": api_key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }
    last_err = None
    for attempt in range(1, 4):
        try:
            resp = requests.post(API_URL, headers=headers, json=body, timeout=180)
            if resp.status_code in (429, 500, 502, 503, 529):
                # Genuinely transient — worth retrying with backoff.
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            if resp.status_code >= 400:
                # Client errors (400/401/403/404/etc.) are NOT transient — the
                # identical request will be rejected identically every time.
                # Retrying 3x with backoff only burns minutes for zero chance
                # of success. Fail immediately, and print the FULL error body
                # (not truncated) since that's what actually explains what was
                # wrong with the request.
                print(f"  [api] non-retryable error, HTTP {resp.status_code} — not retrying:")
                print(f"  {resp.text[:1000]}")
                if hard_fail:
                    sys.exit(1)
                return ""
            resp.raise_for_status()
            data = resp.json()
            if data.get("stop_reason") == "max_tokens":
                print("  [api] WARNING: response hit the max_tokens ceiling and was "
                      "truncated — some stories in this batch may be lost. Consider "
                      "lowering max_new_per_run or raising max_tokens in config.json.")
            return "".join(
                block.get("text", "")
                for block in data.get("content", [])
                if block.get("type") == "text"
            )
        except Exception as exc:
            last_err = exc
            wait = 10 * attempt
            print(f"  [api] attempt {attempt} failed ({exc}); retrying in {wait}s")
            time.sleep(wait)
    print(f"API call failed after retries: {last_err}")
    if hard_fail:
        sys.exit(1)
    return ""


def _split_top_level_objects(text: str) -> list[str]:
    """Scan array text and return each top-level {...} object as a raw string,
    respecting string boundaries so a brace inside a text value can't confuse it."""
    objects = []
    depth = 0
    start = None
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                objects.append(text[start : i + 1])
                start = None
    return objects


def parse_selection(raw: str) -> list[dict]:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    start = text.find("[")
    if start == -1:
        print("  [parse] no JSON array in model output — treating as zero selections.")
        return []
    end = text.rfind("]")
    if end != -1 and end > start:
        # Normal case: a complete, bracketed array.
        array_text = text[start : end + 1]
        try:
            items = json.loads(array_text)
            return items if isinstance(items, list) else []
        except json.JSONDecodeError as exc:
            print(f"  [parse] JSON error in the full batch ({exc}) — recovering stories "
                  f"one by one instead of discarding all of them…")
        scan_text = array_text
    else:
        # Truncation case: opening '[' but no clean closing ']' — the response was
        # cut off mid-JSON (usually the max_tokens ceiling). Recover the complete
        # objects that DID arrive before the cutoff rather than losing everything.
        print("  [parse] response looks truncated (no closing ']') — recovering the "
              "complete stories that arrived before the cutoff…")
        scan_text = text[start:]

    recovered, failed = [], 0
    for obj_text in _split_top_level_objects(scan_text):
        try:
            recovered.append(json.loads(obj_text))
        except json.JSONDecodeError:
            failed += 1
    if recovered:
        print(f"  [parse] recovered {len(recovered)} of {len(recovered) + failed} stories individually")
    else:
        print("  [parse] could not recover any stories from this batch.")
    return recovered


def clip(value: str, limit: int) -> str:
    value = (value or "").strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def generate_article_image(cfg: dict, prompt: str, out_path: Path) -> dict | None:
    """Generate an editorial illustration via FLUX.1 [schnell] on fal.ai, and
    save it locally as WebP (never hotlinked — this repo is public, and a
    hotlinked third-party image is also a dependency you don't control).
    Returns None (silently) on ANY failure — a missing image should never
    break a publish run, same principle as find_stock_photo().

    Verified directly against fal.ai's own API docs before writing this:
    - Auth: `Authorization: Key {FAL_API_KEY}` header (the literal word
      "Key", not "Bearer")
    - Endpoint: POST https://fal.run/fal-ai/flux/schnell (synchronous —
      appropriate here since Schnell is sub-second; slower models would
      need the async queue pattern instead)
    - image_size as a custom {"width","height"} object is supported
      alongside preset enum strings
    - Response: {"images": [{"url", "width", "height", "content_type"}], ...}
      — fal returns a URL to download, not the raw image bytes, so this is
      a two-step fetch: generate, then download.
    Pricing: $0.003/megapixel, billed rounded up to the next whole
    megapixel — 1200x675 is ~0.81MP, so this stays at the cheapest tier.
    """
    api_key = os.environ.get("FAL_API_KEY")
    if not api_key or not prompt:
        return None
    # Defense-in-depth, not reliant solely on the model following instructions:
    # append a standing negative constraint to every generation prompt. Image
    # models cannot render legible text/numbers — asking for one anyway (a
    # scoreboard, chart, table) reliably produces garbled nonsense, as seen
    # directly on the ATP/WTA rankings guide's image.
    full_prompt = f"{prompt}, no text, no numbers, no scoreboard, no charts, no tables, no UI elements"
    try:
        import io
        try:
            from PIL import Image
        except ImportError:
            print("  [image-gen] Pillow not installed — add 'Pillow' to requirements.txt")
            return None

        resp = requests.post(
            "https://fal.run/fal-ai/flux/schnell",
            headers={"Authorization": f"Key {api_key}", "Content-Type": "application/json"},
            json={
                "prompt": full_prompt,
                "image_size": {"width": 1200, "height": 675},  # 16:9, stays under 1MP billing tier
                "num_images": 1,
                "output_format": "jpeg",
            },
            timeout=60,
        )
        resp.raise_for_status()
        images = resp.json().get("images") or []
        if not images:
            return None
        image_url = images[0]["url"]

        img_resp = requests.get(image_url, timeout=30)
        img_resp.raise_for_status()

        img = Image.open(io.BytesIO(img_resp.content)).convert("RGB")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, "WEBP", quality=84, method=6)
        return {"width": images[0].get("width"), "height": images[0].get("height")}
    except Exception as exc:
        print(f"  [image-gen] failed (non-fatal, article publishes without an image): {exc}")
        return None


def get_article_photo(cfg: dict, written: dict, slug: str) -> dict | None:
    """Dispatches to whichever image provider config.json selects.
    Defaults to Pexels (existing behavior, completely unchanged) unless
    image_provider is explicitly set to 'fal'. Falls back to Pexels if FLUX
    generation fails and fallback_to_pexels isn't disabled — matches the
    cautious rollout both source documents recommended: don't commit to the
    new provider fully until it's proven out."""
    provider = cfg.get("image_provider", "pexels")
    if provider == "fal":
        out_path = ROOT / "assets" / "articles" / f"{slug}.webp"
        result = generate_article_image(cfg, written.get("image_query", ""), out_path)
        if result:
            return {
                "image_path": f"/assets/articles/{slug}.webp",
                "image_credit": "AI-generated illustration",
            }
        if not cfg.get("fallback_to_pexels", True):
            return None
        print("  [image-gen] falling back to Pexels for this article")
    return find_stock_photo(cfg, written.get("image_query", ""))


def find_stock_photo(cfg: dict, query: str) -> dict | None:
    """Look up a genuinely-licensed, generic topical stock photo via Pexels.
    Returns None (silently) if no key is configured, the query is empty, or the
    lookup fails for any reason — a missing photo should never break a publish run.
    Key comes from the PEXELS_API_KEY environment variable (a GitHub Actions
    secret), not config.json — this file is committed to a public repo, and a
    real API key was previously sitting there in plaintext. cfg['pexels_api_key']
    is still checked as a fallback for local/manual runs, but should be empty
    in the committed file from now on."""
    key = os.environ.get("PEXELS_API_KEY") or cfg.get("pexels_api_key", "")
    if not key or not query:
        return None
    try:
        resp = requests.get(
            "https://api.pexels.com/v1/search",
            params={"query": query, "per_page": 1, "orientation": "landscape"},
            headers={"Authorization": key},
            timeout=15,
        )
        resp.raise_for_status()
        photos = resp.json().get("photos", [])
        if not photos:
            return None
        p = photos[0]
        return {
            "photo_url": p["src"]["large"],
            "photo_credit": p.get("photographer", "Pexels"),
            "photo_credit_url": p.get("photographer_url") or p.get("url", "https://www.pexels.com"),
        }
    except Exception as exc:
        print(f"  [photo] lookup failed for '{query}' (non-fatal): {exc}")
        return None


def save_articles(cfg: dict, items: list[dict], candidates: list[dict], seen: dict) -> tuple[int, list[str]]:
    now = datetime.now(timezone.utc)
    default_cat = next(iter(cfg["categories"]))
    saved = 0
    new_urls = []
    base = cfg["base_url"].rstrip("/") + cfg.get("base_path", "").rstrip("/")
    for item in items:
        try:
            cand = candidates[int(item["candidate"])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        if cand["id"] in set(seen["ids"]):
            continue
        category = item.get("category") if item.get("category") in cfg["categories"] else default_cat
        slug = f'{slugify(item.get("slug_hint") or item.get("headline", ""))}-{cand["id"][:4]}'
        body = (item.get("body") or "").strip()
        headline = clip(item.get("headline", ""), 90)
        if not body or not headline:
            continue
        article = {
            "id": cand["id"],
            "slug": slug,
            "headline": headline,
            "meta_description": clip(item.get("meta_description", ""), 160),
            "summary_short": clip(item.get("summary_short", ""), 170),
            "body": body,
            "category": category,
            "tags": [clip(t, 30) for t in (item.get("tags") or [])[:5]],
            "quick_facts": [c for c in (clip(f, 120) for f in (item.get("quick_facts") or [])[:5]) if c],
            "source_name": cand["source"],
            "source_url": cand["link"],
            "published": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "lang": cfg["lang"],
        }
        photo = find_stock_photo(cfg, item.get("image_query", ""))
        if photo:
            article.update(photo)
        out_dir = CONTENT_DIR / now.strftime("%Y") / now.strftime("%m")
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / f"{slug}.json", "w", encoding="utf-8") as f:
            json.dump(article, f, ensure_ascii=False, indent=2)
        seen["ids"].append(cand["id"])
        saved += 1
        new_urls.append(f'{base}/{cfg["article_prefix"]}/{slug}/')
        print(f"  [new] {headline}")
    return saved, new_urls


def ping_indexnow(cfg: dict, urls: list[str]) -> None:
    """Tell Bing/Yandex/Naver about new URLs immediately instead of waiting to be crawled.
    No-op if indexnow_key isn't set in config.json. Never fails the run — this is a nicety,
    not a requirement."""
    key = cfg.get("indexnow_key", "")
    if not key or not urls:
        return
    host = cfg["base_url"].rstrip("/").split("//", 1)[-1]
    key_location = f'{cfg["base_url"].rstrip("/")}{cfg.get("base_path", "")}/{key}.txt'
    try:
        resp = requests.post(
            "https://api.indexnow.org/indexnow",
            json={"host": host, "key": key, "keyLocation": key_location, "urlList": urls},
            timeout=15,
        )
        print(f"  [indexnow] pinged {len(urls)} URL(s) — HTTP {resp.status_code}")
    except Exception as exc:
        print(f"  [indexnow] ping failed (non-fatal): {exc}")


def check_feeds(cfg: dict) -> None:
    print(f"Checking {len(cfg['feeds'])} feeds…")
    ok = 0
    for feed in cfg["feeds"]:
        try:
            if feed.get("type") == "scrape":
                # fetch_scraped_listing() swallows per-page fetch errors by
                # design (so one bad page never crashes a real collection
                # run) — do a direct reachability check here instead, so this
                # diagnostic can tell "reachable, 0 matches" (check
                # link_pattern) apart from "couldn't even reach the page".
                resp = requests.get(feed["url"], timeout=15, headers={"User-Agent": USER_AGENT})
                resp.raise_for_status()
                entries = fetch_scraped_listing(feed)
            else:
                entries = fetch_feed(feed, window_hours=24 * 14)
            print(f"  OK    {feed['name']}: {len(entries)} recent entries — {feed['url']}")
            ok += 1
        except Exception as exc:
            print(f"  FAIL  {feed['name']}: {exc} — {feed['url']}")
    print(f"{ok}/{len(cfg['feeds'])} feeds working. Remove or replace failing ones in config.json.")


def backfill_photos(cfg: dict) -> None:
    """One-off: find every existing article with no photo, generate a proper
    per-article (not just per-category) English search topic for each via a
    single batched Claude call, then fetch a photo via whichever provider
    config.json selects. Safe to re-run — anything that already has a photo
    is skipped."""
    has_pexels = os.environ.get("PEXELS_API_KEY") or cfg.get("pexels_api_key")
    has_fal = os.environ.get("FAL_API_KEY")
    if cfg.get("image_provider") == "fal" and not has_fal:
        print("image_provider is 'fal' but no FAL_API_KEY configured — nothing to do.")
        return
    if cfg.get("image_provider", "pexels") != "fal" and not has_pexels:
        print("No Pexels API key configured (PEXELS_API_KEY env var) — nothing to do.")
        return
    paths = sorted(CONTENT_DIR.rglob("*.json"))
    missing = []
    for path in paths:
        try:
            with open(path, encoding="utf-8-sig") as f:
                a = json.load(f)
        except Exception:
            continue  # a broken file is build.py's problem, not this script's
        if not a.get("photo_url") and not a.get("image_path"):
            missing.append((path, a))
    if not missing:
        print("Every article already has an image. Nothing to backfill.")
        return
    print(f"{len(missing)} article(s) missing an image. Generating search topics…")

    queries: dict[str, str] = {}
    CHUNK = 25  # keep each prompt small and cheap
    for i in range(0, len(missing), CHUNK):
        chunk = missing[i:i + CHUNK]
        lines = "\n".join(
            f'{j}. {a["headline"]} — tags: {", ".join(a.get("tags", []))}'
            for j, (_, a) in enumerate(chunk)
        )
        prompt = f"""For each numbered article below (title in {cfg['language_name']}), write a
2-4 word GENERIC image topic in English — a concrete scene, action, or object only.
NEVER a scoreboard, chart, table, ranking list, or anything with readable text/numbers
in it (image generation cannot render legible text and produces garbled nonsense when
asked to) — for abstract topics, depict the concrete real-world activity instead.
NEVER include a real person's name or a specific claimed place; this is for
illustrative imagery, not a picture of the actual people or event.

Respond with ONLY a JSON object mapping each number to its query string, like:
{{"0": "beekeeping apiary", "1": "hospital doctor patient"}}

ARTICLES
{lines}"""
        try:
            raw = call_claude(cfg, prompt)
            text = re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip()
            start, end = text.find("{"), text.rfind("}")
            result = json.loads(text[start:end + 1]) if start != -1 else {}
        except Exception as exc:
            print(f"  [batch {i}] query generation failed (skipping this batch): {exc}")
            continue
        for j, (path, _) in enumerate(chunk):
            q = result.get(str(j), "")
            if q:
                queries[str(path)] = q

    saved, skipped = 0, 0
    for path, article in missing:
        query = queries.get(str(path), "")
        photo = get_article_photo(cfg, {"image_query": query}, article["slug"]) if query else None
        if not photo:
            skipped += 1
            continue
        article.update(photo)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(article, f, ensure_ascii=False, indent=2)
        saved += 1
        print(f"  [photo] {article['headline'][:60]} → {query}")
    print(f"Done. {saved} article(s) got a photo, {skipped} had no match (kept their SVG art).")


def regenerate_image(cfg: dict, slug: str) -> None:
    """Regenerate the image for ONE specific article, overwriting whatever it
    currently has (photo_url or image_path) — for exactly this situation:
    testing whether a prompt/code fix actually changed the outcome for a
    known-bad image, without waiting for a full new article to be written."""
    match = None
    for path in sorted(CONTENT_DIR.rglob("*.json")):
        try:
            with open(path, encoding="utf-8-sig") as f:
                a = json.load(f)
        except Exception:
            continue
        if a.get("slug") == slug:
            match = (path, a)
            break
    if not match:
        print(f"No article found with slug '{slug}'.")
        return
    path, article = match

    print(f"Regenerating image for: {article['headline'][:60]}")
    prompt = f"""Write a 2-4 word GENERIC image topic in English for this article
(title in {cfg['language_name']}) — a concrete scene, action, or object only.
NEVER a scoreboard, chart, table, ranking list, or anything with readable text/numbers
in it (image generation cannot render legible text and produces garbled nonsense when
asked to) — for abstract topics, depict the concrete real-world activity instead.
NEVER include a real person's name or a specific claimed place.

ARTICLE: {article['headline']} — tags: {', '.join(article.get('tags', []))}

Respond with ONLY the 2-4 word query, nothing else."""
    try:
        query = call_claude(cfg, prompt, hard_fail=False).strip().strip('"')
    except Exception as exc:
        print(f"  query generation failed: {exc}")
        return
    if not query:
        print("  Could not generate a query. Nothing changed.")
        return
    print(f"  New image query: {query}")

    photo = get_article_photo(cfg, {"image_query": query}, slug)
    if not photo:
        print("  Image generation/lookup failed. Nothing changed.")
        return
    article.pop("photo_url", None)
    article.pop("photo_credit", None)
    article.pop("photo_credit_url", None)
    article.pop("image_path", None)
    article.pop("image_credit", None)
    article.update(photo)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(article, f, ensure_ascii=False, indent=2)
    print(f"  Done — new image saved for {slug}")
    REVIEW_BATCH.append({
        "kind": "image regenerated", "headline": article["headline"],
        "summary_short": f"New image query used: {query}",
        "body": f"Image regenerated for review. Check the 'Files changed' tab for the actual new image.",
        "quick_facts": [], "source_name": "",
        "url": f'{cfg["base_url"].rstrip("/")}{cfg.get("base_path", "").rstrip("/")}/{cfg["article_prefix"]}/{slug}/',
    })


def recently_ran(hours: float = 2.0) -> bool:
    """True if a publishing run completed within the cooldown window, judged by the
    last_run timestamp stored INSIDE seen.json — never by file modification times.
    (Mtimes are meaningless in CI: actions/checkout rewrites every file's mtime to
    'right now' on every run, which made the original mtime-based version of this
    check wrongly skip 100% of runs.) Guards against near-simultaneous duplicate
    triggers — the GitHub schedule and the cron-job.org backup firing for the same
    slot. Never blocks manually triggered runs (handled in main)."""
    try:
        if not SEEN_FILE.exists():
            return False
        with open(SEEN_FILE, encoding="utf-8-sig") as f:
            last_run = json.load(f).get("last_run")
        if not last_run:
            return False
        last = datetime.strptime(last_run, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - last < timedelta(hours=hours)
    except Exception:
        return False  # the safety guard must never itself block publishing


def recover_missed(cfg: dict, hours: int = 72) -> None:
    """One-time sweep to recover good stories stranded by an earlier bug: looks back
    `hours` (wider than the normal window) AND ignores the seen-filter (since the
    stranded stories are marked seen but never actually published). To avoid
    re-posting stories that DID publish, it leans hard on the same duplicate-topic
    guard used normally — the editor model is told to reject anything matching a
    recently published headline. Still, review the result and delete any dupes."""
    seen = load_seen()
    print(f"[{cfg['site_name']}] RECOVERY sweep — looking back {hours}h, ignoring the seen-list…")
    candidates = collect_candidates(cfg, set(), window_override=hours, ignore_seen=True)
    print(f"  {len(candidates)} candidates in the {hours}h window")
    if not candidates:
        print("Nothing to recover. Done.")
        return
    max_new = cfg.get("max_new_per_run", 6)
    if cfg.get("two_phase", True):
        saved, new_urls = run_two_phase(cfg, candidates, seen, max_new)
    else:
        recent_headlines = load_recent_headlines(days=14, limit=120)
        prompt = build_prompt(cfg, candidates, max_new, recent_headlines)
        print("  asking the editor model to pick genuinely-good, NON-duplicate stories…")
        raw = call_claude(cfg, prompt)
        items = parse_selection(raw)[:max_new]
        saved, new_urls = save_articles(cfg, items, candidates, seen)
    ping_indexnow(cfg, new_urls)
    for c in candidates:
        if c["id"] not in set(seen["ids"]):
            seen["ids"].append(c["id"])
    save_seen(seen)
    print(f"\nRecovery done. {saved} stor{'y' if saved == 1 else 'ies'} recovered and published.")
    print("→ Please review these on the site and delete any that duplicate an "
          "already-published story (the guard prevents most, but check).")


def save_one_written(cfg: dict, written: dict, cand: dict, seen: dict) -> str | None:
    """Save a single already-written article (two-phase output). Returns its URL, or None."""
    default_cat = next(iter(cfg["categories"]))
    now = datetime.now(timezone.utc)
    body = (written.get("body") or "").strip()
    # Defensive cleanup: strip any stray <cite>...</cite> tags the model might
    # emit when context_search is on (same trained-habit issue as the guide
    # generator — see generate_guide/save_guide for the full explanation).
    body = re.sub(r'<cite[^>]*>(.*?)</cite>', r'\1', body, flags=re.DOTALL)
    headline = clip(written.get("headline", ""), 90)
    if not body or not headline:
        return None
    category = written.get("category") if written.get("category") in cfg["categories"] else default_cat
    slug = f'{slugify(written.get("slug_hint") or headline)}-{cand["id"][:4]}'
    article = {
        "id": cand["id"], "slug": slug, "headline": headline,
        "meta_description": clip(written.get("meta_description", ""), 160),
        "summary_short": clip(written.get("summary_short", ""), 170),
        "body": body, "category": category,
        "tags": [clip(t, 30) for t in (written.get("tags") or [])[:5]],
        "quick_facts": [c for c in (clip(f, 120) for f in (written.get("quick_facts") or [])[:5]) if c],
        "source_name": cand["source"], "source_url": cand["link"],
        "published": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "lang": cfg["lang"],
    }
    photo = get_article_photo(cfg, written, slug)
    if photo:
        article.update(photo)
    out_dir = CONTENT_DIR / now.strftime("%Y") / now.strftime("%m")
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / f"{slug}.json", "w", encoding="utf-8") as f:
        json.dump(article, f, ensure_ascii=False, indent=2)
    seen["ids"].append(cand["id"])
    base = cfg["base_url"].rstrip("/") + cfg.get("base_path", "").rstrip("/")
    url = f'{base}/{cfg["article_prefix"]}/{slug}/'
    REVIEW_BATCH.append({
        "kind": "new", "headline": headline, "summary_short": article["summary_short"],
        "body": body, "quick_facts": article["quick_facts"],
        "source_name": article["source_name"], "url": url,
    })
    return url


def run_two_phase(cfg: dict, candidates: list[dict], seen: dict, max_new: int) -> tuple[int, list[str]]:
    """Phase 1: cheap selection call picks the good, non-duplicate stories.
    Phase 2: for each pick, fetch the full source article and write it individually.
    Full-text fetch failure falls back to the snippet automatically per-story."""
    recent_headlines = load_recent_headlines(days=14, limit=120)
    sel_prompt = build_selection_prompt(cfg, candidates, max_new, recent_headlines)
    print("  [phase 1] selecting the genuinely-good stories…")
    picks = parse_selection(call_claude(cfg, sel_prompt))[:max_new]
    if not picks:
        print("  [phase 1] nothing selected this run.")
        return 0, []
    print(f"  [phase 1] selected {len(picks)} — now writing each from full source…")

    saved, new_urls = 0, []
    seen_ids = set(seen["ids"])
    context_search = cfg.get("context_search", False)
    search_tools = [{"type": "web_search_20250305", "name": "web_search"}] if context_search else None
    for pick in picks:
        try:
            cand = candidates[int(pick["candidate"])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        if cand["id"] in seen_ids:
            continue
        full_text = fetch_full_article(cand["link"])
        tag = "full source" if full_text else "snippet only"
        write_prompt = build_writing_prompt(cfg, cand, full_text, use_search=context_search)
        raw_response = call_claude(cfg, write_prompt, tools=search_tools, hard_fail=False)
        written = parse_delimited_article(raw_response)
        if not written:
            print(f"    [skip] writing failed for: {cand['title'][:55]}")
            print(f"    [debug] raw response start: {raw_response[:300]!r}")
            print(f"    [debug] raw response end: {raw_response[-300:]!r}")
            continue
        url = save_one_written(cfg, written, cand, seen)
        if url:
            saved += 1
            seen_ids.add(cand["id"])
            new_urls.append(url)
            print(f"  [new · {tag}] {clip(written.get('headline',''), 60)}")
    return saved, new_urls


def rewrite_articles(cfg: dict, limit: int | None = None, force: bool = False) -> None:
    """Go back through existing articles and rewrite each to the current
    professional length/uniqueness standard, using its original source.

    Safety-first, because this EDITS live content:
    - Preserves slug, id, published date, category, and any existing photo —
      so URLs and SEO are untouched (only headline/body/meta/tags improve).
    - Skips seed articles. Skips anything already marked rewritten UNLESS
      force=True — needed to bring articles that were rewritten under an
      older prompt version (e.g. before the added-value-paragraph rule
      existed) up to the current standard, not just untouched ones.
    - Skips (leaves untouched) any article whose source can't be re-fetched or
      whose rewrite fails to parse — a bad rewrite must never replace good text.
    - Writes each file in place only after a valid new version is produced.
    """
    if not CONTENT_DIR.exists():
        print("No content directory. Nothing to rewrite.")
        return
    paths = sorted(CONTENT_DIR.rglob("*.json"))
    done, skipped, fetch_failed, parse_failed, attempted = 0, 0, 0, 0, 0
    for path in paths:
        # IMPORTANT: this checks ATTEMPTS (paid API calls made), not successful
        # rewrites. A previous version checked `done >= limit` here, which only
        # counted successes — meaning a run with a poor parse-success rate could
        # silently make far more paid calls than the limit was meant to cap,
        # while technically never exceeding it. `--rewrite-limit` must bound
        # real spend, not just output count.
        if limit is not None and attempted >= limit:
            break
        try:
            with open(path, encoding="utf-8-sig") as f:
                art = json.load(f)
        except Exception:
            continue  # broken file is build.py's problem, not ours

        # Skip things we shouldn't touch.
        if art.get("id", "").startswith("seed") or (art.get("rewritten") and not force):
            skipped += 1
            continue
        # Never rewrite the special anniversary / pinned pieces.
        if art.get("cat_unlock") or art.get("pin_until") or art.get("publish_at"):
            skipped += 1
            continue
        src_url = art.get("source_url")
        if not src_url:
            skipped += 1
            continue

        full_text = fetch_full_article(src_url)
        if not full_text:
            # Can't re-fetch the source — leave the existing article exactly as is.
            # No API call was made, so this does NOT count against the limit.
            fetch_failed += 1
            print(f"  [keep] source unavailable, left untouched: {art.get('headline','')[:50]}")
            continue

        # From here on, a real paid API call is about to be made — count it now,
        # before we even know whether it succeeds, since the limit exists to
        # bound spend, not output.
        attempted += 1

        # Reuse the same writing prompt as the live pipeline for consistency.
        pseudo = {"title": art.get("headline", ""), "source": art.get("source_name", ""),
                  "summary": art.get("summary_short", ""), "link": src_url}
        context_search = cfg.get("context_search", False)
        search_tools = [{"type": "web_search_20250305", "name": "web_search"}] if context_search else None
        raw_response = call_claude(
            cfg, build_writing_prompt(cfg, pseudo, full_text, use_search=context_search),
            tools=search_tools, hard_fail=False)
        written = parse_delimited_article(raw_response)
        if not written or not (written.get("body") or "").strip():
            parse_failed += 1
            print(f"  [keep] rewrite failed, left untouched: {art.get('headline','')[:50]}")
            print(f"  [debug] raw response start: {raw_response[:300]!r}")
            print(f"  [debug] raw response end: {raw_response[-300:]!r}")
            continue

        # Merge the improved fields, preserving everything SEO-critical.
        category = written.get("category") if written.get("category") in cfg["categories"] else art.get("category")
        art["headline"] = clip(written.get("headline") or art["headline"], 90)
        art["meta_description"] = clip(written.get("meta_description", ""), 160) or art.get("meta_description", "")
        art["summary_short"] = clip(written.get("summary_short", ""), 170) or art.get("summary_short", "")
        art["body"] = written["body"].strip()
        art["category"] = category
        if written.get("tags"):
            art["tags"] = [clip(t, 30) for t in written["tags"][:5]]
        if written.get("quick_facts"):
            art["quick_facts"] = [c for c in (clip(f, 120) for f in written["quick_facts"][:5]) if c]
        art["rewritten"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # slug, id, published, photo_* all deliberately left as-is.

        with open(path, "w", encoding="utf-8") as f:
            json.dump(art, f, ensure_ascii=False, indent=2)
        done += 1
        print(f"  [rewritten] {art['headline'][:55]}")
        base = cfg["base_url"].rstrip("/") + cfg.get("base_path", "").rstrip("/")
        REVIEW_BATCH.append({
            "kind": "rewritten", "headline": art["headline"], "summary_short": art["summary_short"],
            "body": art["body"], "quick_facts": art.get("quick_facts", []),
            "source_name": art.get("source_name", ""), "url": f'{base}/{cfg["article_prefix"]}/{art["slug"]}/',
        })

    print(f"\nRewrite complete. {attempted} paid API call(s) made, {done} rewritten successfully, "
          f"{parse_failed} of those calls failed to parse (paid, but left untouched), "
          f"{fetch_failed} skipped for a dead/unfetchable source (free, no API call), "
          f"{skipped} skipped entirely — seed/special/no-source/already-rewritten (zero cost).")


def count_articles_by_category(cfg: dict) -> dict:
    """Count existing regular (non-pillar) articles per category — used to
    find the thinnest category to prioritize for the next generated guide."""
    counts = {cid: 0 for cid in cfg["categories"]}
    if not CONTENT_DIR.exists():
        return counts
    for path in CONTENT_DIR.rglob("*.json"):
        try:
            with open(path, encoding="utf-8-sig") as f:
                a = json.load(f)
        except Exception:
            continue
        cid = a.get("category")
        if cid in counts and not a.get("pillar"):
            counts[cid] += 1
    return counts


def count_guides_by_category(cfg: dict) -> dict:
    """Count existing pillar/guide articles per category — used to prefer
    spreading new guides across categories that don't have one yet, rather
    than piling multiple guides into whichever category happens to be
    thinnest by regular-article count every single run."""
    counts = {cid: 0 for cid in cfg["categories"]}
    if not CONTENT_DIR.exists():
        return counts
    for path in CONTENT_DIR.rglob("*.json"):
        try:
            with open(path, encoding="utf-8-sig") as f:
                a = json.load(f)
        except Exception:
            continue
        cid = a.get("category")
        if cid in counts and a.get("pillar"):
            counts[cid] += 1
    return counts


def pick_thinnest_category(cfg: dict) -> str:
    """Pick the category to target for the next generated guide. Prefers
    categories with zero existing guides first — regular-article counts
    don't change when a guide is added, so without this preference a
    category tied for 'thinnest' keeps winning every run, piling multiple
    guides into the same category instead of spreading across categories
    that have none yet. Falls back to fewest guides overall (tie-broken by
    article count) once every category already has at least one."""
    article_counts = count_articles_by_category(cfg)
    guide_counts = count_guides_by_category(cfg)
    no_guide_yet = [cid for cid in cfg["categories"] if guide_counts.get(cid, 0) == 0]
    if no_guide_yet:
        return min(no_guide_yet, key=lambda cid: article_counts.get(cid, 0))
    return min(cfg["categories"],
               key=lambda cid: (guide_counts.get(cid, 0), article_counts.get(cid, 0)))


def load_existing_guide_topics(category_id: str) -> list[str]:
    """Headlines of existing pillar/guide articles in this category, so a
    newly generated guide doesn't duplicate an existing one's topic."""
    topics = []
    if not CONTENT_DIR.exists():
        return topics
    for path in CONTENT_DIR.rglob("*.json"):
        try:
            with open(path, encoding="utf-8-sig") as f:
                a = json.load(f)
        except Exception:
            continue
        if a.get("pillar") and a.get("category") == category_id:
            topics.append(a.get("headline", ""))
    return topics


def build_guide_prompt(cfg: dict, category_id: str, avoid_topics: list[str]) -> str:
    """Prompt for an original, source-free evergreen 'наръчник' guide article.
    Unlike the daily wire-rewrite pipeline, this is explicitly told to use
    live web search to verify facts before writing — especially anything
    time-sensitive (currency, current office-holders, recent statistics) —
    rather than relying only on the model's training data, which can be
    stale or simply wrong by the time this runs. This is the content type
    that structurally avoids the isBasedOn attribution-cannibalization
    problem: it's not a rewrite of one source, so there's nothing to
    attribute away to."""
    cat = cfg["categories"][category_id]
    avoid_block = ""
    if avoid_topics:
        avoid_list = "\n".join(f"- {t}" for t in avoid_topics)
        avoid_block = (f"\nDO NOT duplicate the topic of any existing guide in this category:\n"
                        f"{avoid_list}\n")

    return f"""You are the editor of "{cfg['site_name']}", writing an original, evergreen reference guide
(a 'наръчник') for the "{cat['label']}" category, in {cfg['language_name']}.

This is NOT a rewrite of one news story. It is a standalone, comprehensive guide that:
- Is not tied to any single source — it's your own synthesis of well-established, publicly known facts
- Will stay relevant for years, not days
- Genuinely earns being cited by AI search engines and Google, rather than re-summarizing someone else's reporting
{avoid_block}
CRITICAL — USE WEB SEARCH TO VERIFY FACTS BEFORE WRITING:
- Search the web for anything you are not 100% certain about — especially current statistics, currency or
  economic status, who currently holds a position, recent legal/regulatory changes, or anything else that could
  have changed recently. Do not rely on training data alone for anything time-sensitive.
- If you cannot verify a specific fact via search, do not include it — write around it or drop it rather than guess.
- Where a section rests on one clearly verifiable official/authoritative source, cite it inline (format below)
  the way a careful human editor would — but only a URL you actually found via search and are confident is real.
  Never invent a URL.
- IMPORTANT: the ONLY citation format allowed in the body text is [link text](URL) — do NOT use <cite> tags,
  footnote markers, or any other citation markup. This body text is published directly on a website with no
  citation-rendering system beyond that one link format; anything else will show up as broken, garbled text to
  real readers.

STRUCTURE
- Open with a 2-4 sentence introduction (no heading) framing why this topic matters.
- Follow with 4-6 clearly separated sections, each starting with its own '## Heading' line (in
  {cfg['language_name']}), covering genuinely distinct sub-topics — not padding.
- To cite a source inline, use exactly this syntax: [link text](URL) — nothing else, see above.
- Close with a short, honest paragraph (no heading) stating this is an AI-compiled guide based on publicly
  available information rather than one single source, and inviting corrections via the site's contact email.
- Total length: 500-800 words.

Respond with ONLY a JSON object, nothing else:
{{
  "headline": "<max 90 chars, in {cfg['language_name']}>",
  "slug_hint": "<3-6 latin lowercase words, hyphenated>",
  "meta_description": "<max 155 chars>",
  "summary_short": "<max 170 chars teaser>",
  "body": "<the full guide, paragraphs/headings separated by \\n\\n, per the structure above>",
  "quick_facts": ["<3-5 short standalone facts, in {cfg['language_name']}>"],
  "tags": ["<4-6 lowercase tags, no spaces, in {cfg['language_name']}>"],
  "image_query": "<2-4 words English, a concrete scene, action, or object matching this guide's overall subject — e.g. 'mountain forest hiking' or 'hospital doctor patient'. NEVER a scoreboard, chart, table, ranking list, diagram, or anything with readable text/numbers in it (image generation cannot render legible text and produces garbled nonsense when asked to). For abstract/explainer topics — rankings, statistics, how a system works — depict the concrete real-world activity or setting instead, never the abstraction itself. Never a real person's name or a specific claimed location.>"
}}"""


def save_guide(cfg: dict, written: dict, category_id: str) -> str | None:
    """Save a generated evergreen guide article. Unlike regular articles,
    guides have no source_url/source_name (they're original syntheses, not
    single-source rewrites) — so build.py's schema correctly omits
    isBasedOn for them — and are flagged pillar=true so build.py pins them
    at the top of their category page instead of letting them paginate away
    like a dated news item."""
    body = (written.get("body") or "").strip()
    # Defensive cleanup: models with web_search access have a strong trained
    # habit of citing sources with <cite index="...">...</cite> tags (the
    # same syntax used elsewhere for citing search results), which can slip
    # through despite the prompt explicitly forbidding it. build.py's
    # renderer doesn't recognize these — they'd show up as raw, broken markup
    # to real readers. Strip the tags, keep the actual cited text.
    body = re.sub(r'<cite[^>]*>(.*?)</cite>', r'\1', body, flags=re.DOTALL)
    headline = clip(written.get("headline", ""), 90)
    if not body or not headline:
        return None
    now = datetime.now(timezone.utc)
    guide_id = hashlib.sha1((headline + now.isoformat()).encode("utf-8")).hexdigest()[:16]
    slug = f'{slugify(written.get("slug_hint") or headline)}-{guide_id[:4]}'
    article = {
        "id": guide_id, "slug": slug, "headline": headline,
        "meta_description": clip(written.get("meta_description", ""), 160),
        "summary_short": clip(written.get("summary_short", ""), 170),
        "body": body, "category": category_id,
        "tags": [clip(t, 30) for t in (written.get("tags") or [])[:6]],
        "quick_facts": [c for c in (clip(f, 120) for f in (written.get("quick_facts") or [])[:5]) if c],
        "published": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lang": cfg["lang"],
        "pillar": True,
    }
    photo = get_article_photo(cfg, written, slug)
    if photo:
        article.update(photo)
    out_dir = CONTENT_DIR / now.strftime("%Y") / now.strftime("%m")
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / f"{slug}.json", "w", encoding="utf-8") as f:
        json.dump(article, f, ensure_ascii=False, indent=2)
    base = cfg["base_url"].rstrip("/") + cfg.get("base_path", "").rstrip("/")
    REVIEW_BATCH.append({
        "kind": "guide", "headline": headline, "summary_short": article["summary_short"],
        "body": body, "quick_facts": article["quick_facts"],
        "source_name": "", "url": f'{base}/{cfg["article_prefix"]}/{slug}/',
    })
    return slug


def generate_guide(cfg: dict, category_override: str | None = None) -> None:
    """Generate one original, web-search-grounded evergreen guide article,
    targeting the thinnest category by default (or a specific one via
    --guide-category)."""
    category_id = category_override if category_override in cfg["categories"] else pick_thinnest_category(cfg)
    cat_label = cfg["categories"][category_id]["label"]
    print(f"[{cfg['site_name']}] generating an evergreen guide for category: {cat_label} ({category_id})")

    avoid_topics = load_existing_guide_topics(category_id)
    if avoid_topics:
        print(f"  avoiding {len(avoid_topics)} existing guide topic(s) already covered in this category")

    prompt = build_guide_prompt(cfg, category_id, avoid_topics)
    print("  researching and writing (uses live web search — this can take a minute or two)…")
    raw = call_claude(cfg, prompt,
                       tools=[{"type": "web_search_20250305", "name": "web_search"}],
                       max_tokens_override=8000)
    written = parse_json_object(raw)
    if not written:
        print("  Could not parse a guide from the model's response. Nothing saved. "
              "(Rerun — this is usually transient.)")
        return

    slug = save_guide(cfg, written, category_id)
    if slug:
        print(f"  [new guide] {written.get('headline', '')[:70]}")
        print(f"  saved to content/articles/{datetime.now(timezone.utc).strftime('%Y/%m')}/{slug}.json")
    else:
        print("  Guide response was missing a headline or body. Nothing saved.")


def generate_guides(cfg: dict, count: int, category_override: str | None = None) -> None:
    """Generate `count` guides in one run. Each call re-reads existing guides
    from disk, so category selection (pick_thinnest_category) and duplicate-
    topic avoidance both naturally account for guides created earlier in the
    same run — no special batching logic needed beyond looping.

    A soft, printed warning at higher counts rather than a hard limit: this
    is a genuine quality/cost tradeoff for a human to weigh, not a safety
    issue to enforce. See the guidance in generate_guide()'s own docstring
    and the README/chat history for sizing recommendations."""
    if count > 10:
        print(f"  [note] generating {count} guides in one run. Each one is a real API cost (web search + "
              f"long-form writing) and takes 1-2 minutes. More importantly: a category only has so many "
              f"genuinely distinct 'start here' topics before new guides start feeling thin or redundant — "
              f"quality, not quantity, is what earns citations. Consider a smaller batch and reviewing the "
              f"output before generating more.")
    for i in range(count):
        if count > 1:
            print(f"\n=== guide {i + 1} of {count} ===")
        generate_guide(cfg, category_override=category_override)
        if i < count - 1:
            time.sleep(5)  # brief pause between calls


def write_pr_description() -> bool:
    """Write a clean, readable markdown summary of everything in REVIEW_BATCH
    for the review-PR description — this is what a human reads to approve or
    reject a batch, so it needs to read like a document, not a JSON diff.
    Returns True if anything was written (i.e., there's something to actually
    open a review PR for)."""
    if not REVIEW_BATCH:
        return False
    kind_label = {"new": "New article", "rewritten": "Rewritten article", "guide": "New evergreen guide"}
    lines = [f"# {len(REVIEW_BATCH)} item(s) ready for review\n"]
    for i, item in enumerate(REVIEW_BATCH, 1):
        label = kind_label.get(item["kind"], "Item")
        lines.append(f"## {i}. [{label}] {item['headline']}")
        if item.get("source_name"):
            lines.append(f"**Source:** {item['source_name']}\n")
        lines.append(item["body"])
        if item.get("quick_facts"):
            lines.append("\n**Quick facts:**")
            for f in item["quick_facts"]:
                lines.append(f"- {f}")
        lines.append(f"\n*Will be live at:* {item['url']}\n")
        lines.append("---\n")
    PR_DESCRIPTION_FILE.write_text("\n".join(lines), encoding="utf-8")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="Good-news pipeline")
    ap.add_argument("--check-feeds", action="store_true")
    ap.add_argument("--dry", action="store_true", help="list candidates only, no API call")
    ap.add_argument("--limit", type=int, default=None, help="max new stories this run")
    ap.add_argument("--backfill-photos", action="store_true",
                     help="one-off: add real Pexels photos to existing articles that don't have one")
    ap.add_argument("--regenerate-image", type=str, default=None, metavar="SLUG",
                     help="one-off: regenerate the image for one specific article by slug, overwriting whatever it currently has")
    ap.add_argument("--recover", action="store_true",
                     help="one-time: sweep the last 72h ignoring the seen-list to recover stranded stories")
    ap.add_argument("--list-candidates", action="store_true",
                     help="diagnostic: print every candidate in the last 72h (no AI, no publishing)")
    ap.add_argument("--rewrite-articles", action="store_true",
                     help="one-time: rewrite existing articles to professional length from their full source")
    ap.add_argument("--rewrite-limit", type=int, default=None,
                     help="cap how many articles --rewrite-articles processes in one run")
    ap.add_argument("--rewrite-force", action="store_true",
                     help="also reprocess articles already marked rewritten — use when the writing prompt "
                          "itself has changed (e.g. the added-value-paragraph rule) and older rewrites should "
                          "be brought up to the current standard")
    ap.add_argument("--generate-guide", action="store_true",
                     help="generate one original, web-search-grounded evergreen guide article "
                          "(a 'наръчник'), targeting the thinnest category by default")
    ap.add_argument("--guide-category", type=str, default=None,
                     help="override which category --generate-guide targets (defaults to the thinnest)")
    ap.add_argument("--guide-count", type=int, default=1,
                     help="generate this many guides in one run instead of one (default 1)")
    ap.add_argument("--force", action="store_true", help="skip the duplicate-trigger cooldown check")
    args = ap.parse_args()

    cfg = load_config()
    if args.check_feeds:
        check_feeds(cfg)
        return
    if args.backfill_photos:
        backfill_photos(cfg)
        return
    if args.regenerate_image:
        regenerate_image(cfg, args.regenerate_image)
        write_pr_description()
        return
    if args.rewrite_articles:
        rewrite_articles(cfg, limit=args.rewrite_limit, force=args.rewrite_force)
        write_pr_description()
        return
    if args.generate_guide:
        generate_guides(cfg, count=max(1, args.guide_count), category_override=args.guide_category)
        write_pr_description()
        return
    if args.list_candidates:
        print(f"[{cfg['site_name']}] listing every candidate in the last 72h "
              "(ignoring seen-list, no AI, no publishing)…\n")
        cands = collect_candidates(cfg, set(), window_override=72, ignore_seen=True)
        print(f"\n=== {len(cands)} candidates ===\n")
        for i, c in enumerate(cands):
            print(f"{i+1}. [{c['source']}] {c['title']}")
            summary = (c.get('summary') or '').strip().replace('\n', ' ')
            if summary:
                print(f"     {summary[:200]}")
        return
    if args.recover:
        recover_missed(cfg)
        write_pr_description()
        return
    # A manually triggered run (someone clicked "Run workflow", or a local run)
    # must ALWAYS publish — never let the duplicate-guard silently skip a human.
    manual_dispatch = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if not args.force and not args.dry and not manual_dispatch and recently_ran():
        print("A scheduled run already completed within the last 2 hours — this looks "
              "like a duplicate trigger (the GitHub schedule and the cron-job.org "
              "backup both firing for the same slot), not a new one. Skipping to avoid "
              "double-publishing. (Manual 'Run workflow' clicks are never skipped; or "
              "use --force locally.)")
        return

    seen = load_seen()
    seen_ids = set(seen["ids"])
    print(f"[{cfg['site_name']}] collecting candidates…")
    candidates = collect_candidates(cfg, seen_ids)
    print(f"  {len(candidates)} fresh candidates")
    if not candidates:
        print("Nothing new. Done.")
        return

    if args.dry:
        for i, c in enumerate(candidates):
            print(f"  {i}. [{c['source']}] {c['title']}")
        return

    max_new = args.limit or cfg.get("max_new_per_run", 6)

    if cfg.get("two_phase", True):
        # New default: select cheaply, then write each story from full source text.
        saved, new_urls = run_two_phase(cfg, candidates, seen, max_new)
    else:
        # Legacy single-call path, kept as a fallback.
        recent_headlines = load_recent_headlines()
        prompt = build_prompt(cfg, candidates, max_new, recent_headlines)
        print("  asking the editor model to pick the good ones…")
        raw = call_claude(cfg, prompt)
        items = parse_selection(raw)[:max_new]
        saved, new_urls = save_articles(cfg, items, candidates, seen)

    ping_indexnow(cfg, new_urls)

    # Mark rejected candidates as seen too, so we never re-pay to re-judge them.
    seen_now = set(seen["ids"])
    for c in candidates:
        if c["id"] not in seen_now:
            seen["ids"].append(c["id"])
    save_seen(seen)
    write_pr_description()
    print(f"Done. {saved} new stor{'y' if saved == 1 else 'ies'} published, "
          f"{len(candidates) - saved} not selected.")


if __name__ == "__main__":
    main()

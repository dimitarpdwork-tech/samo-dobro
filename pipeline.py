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
from urllib.parse import urljoin, urlparse

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
# Honest-first fetching: identify as ourselves by default (a transparency
# site should be transparent in its User-Agent too — site + contact address,
# so any webmaster can reach a human), and fall back to a plain browser UA
# only when a request with the bot UA fails. Many smaller municipal sites
# run security plugins that blanket-block anything bot-shaped (sometimes
# with odd status codes like 415 rather than a plain 403) — the fallback
# keeps those feeds working without making impersonation the default.
# If the site is ever rebranded/moved, update BOT_UA to match config.json.
BOT_UA = "DobroDeloBot/1.0 (+https://dobrodelo.com/za-nas/; redaktsia@dobrodelo.com)"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# A COMPLETE browser header profile for the fallback, not just a browser UA.
#
# This distinction matters and was the cause of a real, large failure: an
# earlier version swapped only the User-Agent on retry while still sending
# just two headers. A request announcing itself as Chrome but omitting
# Accept-Language, Accept-Encoding and the rest is a well-known bot
# signature — real Chrome never does that — and many WAFs reject it. The
# tell in the logs was ~37 feeds answering 415 "Unsupported Media Type" to a
# GET, which is meaningless for a bodyless request (415 is about a request
# BODY's Content-Type) and therefore proof that a security layer, not the
# CMS, was replying. The same URLs opened fine in a normal browser.
#
# Also note the Accept header here is the ordinary browser one rather than a
# feed-specific "application/rss+xml, ..." list. An Accept header advertising
# only XML types is itself a scraper signature to some WAFs, and the browser
# Accept still includes application/xml, so feeds are served correctly.
#
# Accept-Encoding deliberately omits "br": brotli decoding needs an optional
# extra package, and claiming support we might not have would yield
# undecodable bytes rather than a clean error.
BROWSER_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "bg-BG,bg;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
}
# Deliberately NOT setting "Connection": urllib3 owns connection lifecycle, and
# Connection is a hop-by-hop header (illegal in HTTP/2, rejected by some
# fronting proxies). Setting it by hand made gov.bg answer 421 Misdirected
# Request on a URL that had been fetching fine — a regression introduced
# alongside the browser-profile fix, caught by the next --check-feeds run.


def http_get(url: str, timeout: int = 15, accept: str | None = None) -> "requests.Response":
    """GET with the identifying bot UA first; on any failure (network error or
    HTTP >= 400) retry once as a full browser. Raises on final failure so
    callers keep their existing error handling.

    The retry sends the entire BROWSER_HEADERS profile — swapping only the
    User-Agent is not enough, see the comment on that constant."""
    headers = {"User-Agent": BOT_UA}
    if accept:
        headers["Accept"] = accept
    try:
        resp = requests.get(url, timeout=timeout, headers=headers)
        resp.raise_for_status()
        return resp
    except Exception:
        resp = requests.get(url, timeout=timeout, headers=dict(BROWSER_HEADERS))
        resp.raise_for_status()
        return resp

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
            seen = json.load(f)
        seen.setdefault("ids", [])
        seen.setdefault("dismissed", [])
        return seen
    return {"ids": [], "dismissed": []}


def all_seen_ids(seen: dict) -> set:
    """Every id that should be excluded from new candidate collection:
    the rolling 'ids' window PLUS the permanent 'dismissed' list.

    Always use this rather than set(seen["ids"]) — 'ids' is capped and evicts
    old entries, so a story dismissed by hand would otherwise come back once it
    aged out, which is exactly what /dismiss exists to prevent."""
    return set(seen.get("ids", [])) | set(seen.get("dismissed", []))


def save_seen(seen: dict) -> None:
    seen["ids"] = seen["ids"][-4000:]  # keep the file small
    # 'dismissed' is deliberately NOT capped. It only grows through explicit
    # human /dismiss commands (a handful a day at most), and its whole purpose
    # is to be permanent — capping it would resurrect stories the editor
    # already rejected, which is the bug this list exists to fix.
    if seen.get("dismissed"):
        seen["dismissed"] = sorted(set(seen["dismissed"]))
    seen["last_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: write to a temp file, then os.replace() into place — the
    # file on disk is always either the complete old version or the complete
    # new one, never a partial/interleaved mix, even if two processes race to
    # write it at the same time.
    tmp_path = SEEN_FILE.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=0)
    os.replace(tmp_path, SEEN_FILE)


def merge_seen_with_remote(remote_path: str) -> None:
    """Merge the local (just-written) seen.json with a remote copy at the
    JSON/semantic level — a union of both 'ids' lists, keeping the newer of
    the two 'last_run' timestamps — instead of relying on git's line-based
    text merge for this file. That was the actual root cause of a real
    production conflict: every run's save_seen() rewrites the 'last_run'
    line to a new value, so two runs close together each produce a commit
    changing that same line differently — a genuine, unresolvable git
    conflict, not something automatic merging can paper over. A semantic
    JSON merge sidesteps this entirely, since it never diffs the two files
    as text."""
    try:
        with open(remote_path, encoding="utf-8-sig") as f:
            remote = json.load(f)
    except Exception:
        remote = {"ids": []}
    local = load_seen()
    merged_ids = sorted(set(local.get("ids", [])) | set(remote.get("ids", [])))[-4000:]
    # Dismissals are permanent and must survive the merge uncapped, or a
    # concurrent run could drop a rejection the editor made by hand.
    merged_dismissed = sorted(
        set(local.get("dismissed", [])) | set(remote.get("dismissed", []))
    )
    # save_seen() always overwrites last_run with the current wall-clock
    # time regardless of what's in the dict passed to it, so there's no
    # need to reconcile the two input timestamps here.
    save_seen({"ids": merged_ids, "dismissed": merged_dismissed})
    print(f"  [seen] merged: {len(merged_ids)} ids, "
          f"{len(merged_dismissed)} permanently dismissed "
          f"(local had {len(local.get('ids', []))}, remote had {len(remote.get('ids', []))})")


def clean_text(raw: str, limit: int = 450) -> str:
    """Strip HTML tags/entities from a feed snippet and clip it."""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def entry_id(link: str, title: str) -> str:
    return hashlib.sha1((link or title).encode("utf-8")).hexdigest()[:16]

def normalize_host(url: str) -> str:
    """Return a comparable lowercase hostname without www."""
    if not url:
        return ""
    try:
        host = (urlparse(url).hostname or "").lower().strip(".")
    except Exception:
        return ""

    if host.startswith("www."):
        host = host[4:]

    return host


def hosts_match(expected: str, actual: str) -> bool:
    """Allow exact domains and normal subdomain relationships."""
    expected = (expected or "").lower().strip(".")
    actual = (actual or "").lower().strip(".")

    if not expected or not actual:
        return False

    return (
        expected == actual
        or actual.endswith("." + expected)
        or expected.endswith("." + actual)
    )


def validate_candidate_source(candidate: dict) -> tuple[bool, str]:
    """Check whether article URL matches the configured source domain."""
    if candidate.get("allow_external_links"):
        return True, ""

    expected = candidate.get("expected_source_host", "")
    actual = normalize_host(candidate.get("link", ""))

    if not expected:
        return True, ""

    if not actual:
        return False, "article URL has no valid hostname"

    if hosts_match(expected, actual):
        return True, ""

    return False, f"source host mismatch: expected {expected}, got {actual}"


SENSITIVE_TOPIC_PATTERNS = (
    r"\bhpv\b",
    r"\bваксин",
    r"\bимуниз",
    r"\bлекар",
    r"\bмедицин",
    r"\bболест",
    r"\bзаболяв",
    r"\bрак\b",
    r"\bтерап",
    r"\bлечение",
    r"\bпациент",
    r"\bздраве",
    r"\bvirus\b",
    r"\bвирус",
    r"\bучен",
    r"\bизследван",
    r"\bнаучн",
    r"\bоткрити",
    r"\bднк\b",
    r"\bdna\b",
    r"\bгеном",
    r"\bархеолог",
    r"\bфосил",
    r"\bhomo\b",
    r"\bпроцент",
    r"\bстатист",
    r"\bпроучван",
    r"\bпърв(?:ият|ата|ото|ите)\b",
    r"\bрекорд",
)


def is_sensitive_candidate(candidate: dict) -> bool:
    haystack = " ".join(
        [
            candidate.get("title", ""),
            candidate.get("summary", ""),
        ]
    ).lower()

    return any(
        re.search(pattern, haystack, re.IGNORECASE)
        for pattern in SENSITIVE_TOPIC_PATTERNS
    )

def fetch_feed(feed: dict, window_hours: int) -> list[dict]:
    """Fetch one RSS feed and return recent entries as candidate dicts."""
    resp = http_get(
        feed["url"], timeout=15,
        accept="application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    )
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
        article_link = getattr(e, "link", "")

        out.append(
            {
                "id": entry_id(article_link, title),
                "title": title,
                "summary": clean_text(getattr(e, "summary", "")),
                "link": article_link,
                "source": feed["name"],
                "expected_source_host": normalize_host(feed["url"]),
                "allow_external_links": bool(feed.get("allow_external_links", False)),
                "source_published": (
                    published.strftime("%Y-%m-%dT%H:%M:%SZ")
                    if published
                    else None
                ),
                "source_fetched_at": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
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
    # Optional extra filters, for org sites whose article URLs are bare
    # root-level slugs with no distinguishing path segment. On those, a
    # link_pattern broad enough to catch articles also catches every nav link,
    # so two cheap discriminators are available:
    #   link_exclude    — substrings that mark a URL as navigation/boilerplate
    #   min_title_chars — nav labels are short ("Дари сега"); real headlines
    #                     are sentences. Defaults to the existing 10.
    excludes = source.get("link_exclude") or []
    min_title_chars = int(source.get("min_title_chars", 10))
    out = []
    seen_hrefs = set()
    for page_url in pages:
        try:
            resp = http_get(page_url, timeout=15)
        except Exception as exc:
            print(f"  [scrape] {source['name']}: failed to fetch listing page ({exc})")
            continue
        page_html = resp.text
        for m in re.finditer(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page_html, re.DOTALL | re.IGNORECASE):
            href, inner = m.group(1), m.group(2)
            # Resolve to an absolute URL BEFORE matching. Listing pages very
            # often use relative hrefs ("/article-slug"), so a link_pattern
            # containing the hostname would never match one and the source
            # would silently yield zero items — exactly what the Green Balkans
            # source did on its first live run ("0 recent entries" despite the
            # page being reachable). Matching after resolution also makes
            # host-based patterns work regardless of how the site writes links.
            href = urljoin(page_url, href)
            if pattern not in href:
                continue
            if any(x in href for x in excludes):
                continue
            if href in seen_hrefs:
                continue
            title = clean_text(re.sub(r"<[^>]+>", " ", inner), 200)
            if not title or len(title) < min_title_chars:
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
                "summary": "",
                "link": href,
                "source": source["name"],
                "expected_source_host": normalize_host(source["url"]),
                "allow_external_links": bool(source.get("allow_external_links", False)),
                "source_published": None,
                "source_fetched_at": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
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
    source_block = (
        f"FULL SOURCE ARTICLE (write from this):\n{full_text}"
        if full_text else
        f"SOURCE SUMMARY (only this snippet is available):\n{clean_text(story.get('summary',''), 600)}"
    )

    if full_text:
        lede_rule = (
            "- Open with an answer-first first paragraph of roughly 80-110 words: "
            "what happened, who was involved, where it happened, and the concrete positive outcome. "
            "Do not stretch the paragraph if the facts do not support that length."
        )
        word_target = "250-340 words total"
        support_rule = (
            "- Add 1-2 short supporting paragraphs using concrete details from the source.\n"
            "- Add ONE short 'why it matters' paragraph (roughly 50-90 words) ONLY if the source "
            "supports a real significance, consequence, background, or human angle. If there is no "
            "meaningful extra point to make, omit it instead of padding the story."
        )
    else:
        lede_rule = (
            "- Open with the single most important fact from the snippet in 1-2 clear sentences. "
            "Do not pretend the snippet contains more detail than it does."
        )
        word_target = "140-200 words total"
        support_rule = (
            "- Add only the supporting details that are actually present in the snippet. "
            "Do not add an analysis/opinion section and do not pad the article."
        )

    if use_search and full_text:
        context_rule = (
            "- You MAY add one short verified-context paragraph (roughly 60-100 words) when web search "
            "finds genuinely useful background that helps explain why the story matters. Use only facts "
            "you verified. If you cite an external source, use exactly [link text](URL). "
            "Do not add context merely to make the article longer."
        )
    else:
        context_rule = (
            "- Do NOT invent an editorial thesis, counterargument, generic praise, or broad social analysis. "
            "The article should end when the useful facts and a source-supported significance are covered."
        )

    return f'''You are the editor of "{cfg['site_name']}", writing one concise good-news article in {cfg['language_name']}.

HEADLINE OF THE STORY: {story['title']}
SOURCE: {story['source']}

{source_block}

Write an original article in {cfg['language_name']}. Rules:
- Use ONLY facts present in the source above for the core story. Never invent numbers, names, quotes, dates, motives, rankings, or claims.
- Include 2-4 CONCRETE details from the source when available: names, places, numbers, circumstances, actions or outcomes.
{lede_rule}
{support_rule}
{context_rule}
- Find the actual story beyond the headline, but do not manufacture depth that is not there.
- Warm, human, concrete tone. Positive without becoming sugary or promotional.
- {word_target}.
- Native-level {cfg['language_name']}. Never invent words. Check noun-adjective gender/number agreement. Never use Russian spellings or words.
- Avoid repetitive AI phrases such as "това показва", "това е доказателство", "вдъхновяващ пример", unless genuinely necessary.

Also extract 3-4 short "quick facts" — standalone phrases under ~12 words each.

If the story is specifically tied to a particular Bulgarian city or town, include that city's name as one of the tags, in Bulgarian. Add a city tag ONLY when the story itself is actually about that place — never infer a city from the source/publisher name.

Respond using EXACTLY this plain-text format — nothing before or after it:
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
<the concise article, with one fully blank line between paragraphs>
===QUICK_FACTS===
<first fact>
<second fact>
<third fact>
===TAGS===
<tag one, tag two, tag three — include a city only when clearly supported by the story; ALWAYS include the tag "животни" when the story is genuinely about animals (rescue, treatment, adoption, release, shelters, wildlife), so these stories are collectable>
===IMAGE_QUERY===
<2-4 words English, a concrete scene, action, or object — NEVER a scoreboard, chart, table, ranking list, readable text/numbers, a real person's name, or a falsely claimed specific location>
===END=>'''


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
    """Generate an editorial illustration via FLUX on fal.ai (schnell or dev,
    per cfg['fal_model']), and save it locally as WebP (never hotlinked —
    this repo is public, and a hotlinked third-party image is also a
    dependency you don't control). Returns None (silently) on ANY failure —
    a missing image should never break a publish run, same principle as
    find_stock_photo().

    Verified directly against fal.ai's own API docs before writing this:
    - Auth: `Authorization: Key {FAL_API_KEY}` header (the literal word
      "Key", not "Bearer")
    - Endpoint: POST https://fal.run/fal-ai/flux/{schnell|dev} (synchronous
      — appropriate for schnell, which is sub-second; dev is slower but
      still uses the same synchronous endpoint pattern)
    - image_size as a custom {"width","height"} object is supported
      alongside preset enum strings, for both models
    - Response: {"images": [{"url", "width", "height", "content_type"}], ...}
      — fal returns a URL to download, not the raw image bytes, so this is
      a two-step fetch: generate, then download.
    - IMPORTANT, confirmed directly from fal.ai's own prompting guide: base
      FLUX does NOT support a real negative_prompt parameter, and appending
      'no X, no Y' text is explicitly called out by fal.ai as LESS reliable
      than positive phrasing — describe what the image SHOULD contain, not
      what to avoid. An earlier version of this function used the weaker
      negative-listing technique; fixed after it demonstrably still
      produced a garbled fake scoreboard on a tennis rankings guide.
    Pricing: schnell $0.003/MP, dev ~$0.025/MP, both billed rounded up to
    the next whole megapixel — 1200x675 is ~0.81MP, so this stays at the
    cheapest tier for whichever model is selected.
    """
    api_key = os.environ.get("FAL_API_KEY")
    if not api_key or not prompt:
        return None
    # Positive framing, not a negative list — per fal.ai's own guidance above.
    full_prompt = f"{prompt}, clean candid documentary photograph, natural unobstructed composition, plain simple background"
    model = cfg.get("fal_model", "schnell")
    if model not in ("schnell", "dev"):
        model = "schnell"
    try:
        import io
        try:
            from PIL import Image
        except ImportError:
            print("  [image-gen] Pillow not installed — add 'Pillow' to requirements.txt")
            return None

        resp = requests.post(
            f"https://fal.run/fal-ai/flux/{model}",
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
        # A unique suffix per generation, not just the bare slug — a
        # regeneration must get a genuinely new filename/URL, or a CDN in
        # front of the site (Cloudflare here) can keep serving the old
        # cached bytes at the old URL indefinitely, exactly what happened
        # with the ATP/WTA rankings image: the new file was correctly
        # generated and committed, but sat at the same URL as the stale
        # cached version. This makes that entire class of problem
        # impossible rather than requiring a manual cache purge each time.
        suffix = hashlib.sha1(f"{slug}{time.time()}".encode()).hexdigest()[:8]
        filename = f"{slug}-{suffix}.webp"
        out_path = ROOT / "assets" / "articles" / filename
        result = generate_article_image(cfg, written.get("image_query", ""), out_path)
        if result:
            photo = {
                "image_path": f"/assets/articles/{filename}",
                "image_credit": "AI-generated illustration",
            }
            # Persist the real generated dimensions so build.py can emit
            # accurate width/height attributes and ImageObject schema instead
            # of hardcoding the requested 1200x675 (fal may snap sizes).
            if result.get("width") and result.get("height"):
                photo["image_width"] = result["width"]
                photo["image_height"] = result["height"]
            return photo
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
            "photo_width": p.get("width"),
            "photo_height": p.get("height"),
        }
    except Exception as exc:
        print(f"  [photo] lookup failed for '{query}' (non-fatal): {exc}")
        return None


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


FB_GRAPH = "https://graph.facebook.com/v25.0"


def post_new_articles_to_facebook(cfg: dict) -> None:
    """Share newly approved articles on the site's Facebook Page.

    Runs ONLY from the push-to-main workflow step — i.e. only after a human has
    merged a review PR — so nothing is ever announced at write-time. Reads
    FACEBOOK_PAGE_TOKEN / FACEBOOK_PAGE_ID from the environment (GitHub
    Secrets); with either missing it's a silent no-op, so this code is safe to
    ship before the secrets exist.

    Guards, in order:
    - facebook.start in config.json: articles published before this date are
      never posted. This is what keeps the pre-existing back catalog (hundreds
      of articles with no fb_posted field) from flooding a brand-new Page the
      first time the feature activates.
    - fb_posted flag on each article JSON: once truthy, never posted again —
      re-runs and re-triggered workflows can't double-post.
    - facebook.max_per_run (default 5) + a courtesy pause between posts: a
      giant merge trickles out across successive runs instead of firing a
      burst that looks like spam from a day-old Page.

    Failures print the full Graph API error into the Actions log and never
    block the site build; a dead token (code 190) prints an explicit
    regenerate-the-secret hint and stops early, since every remaining post
    would fail identically."""
    token = os.environ.get("FACEBOOK_PAGE_TOKEN")
    page_id = os.environ.get("FACEBOOK_PAGE_ID")
    if not token or not page_id:
        print("[facebook] FACEBOOK_PAGE_TOKEN / FACEBOOK_PAGE_ID not set — skipping.")
        return
    fb_cfg = cfg.get("facebook", {})
    start_raw = fb_cfg.get("start", "2026-07-27")
    try:
        start = datetime.strptime(start_raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        print(f"[facebook] bad facebook.start value {start_raw!r} (expected YYYY-MM-DD) — skipping.")
        return
    max_per_run = int(fb_cfg.get("max_per_run", 5))
    base = cfg["base_url"].rstrip("/") + cfg.get("base_path", "").rstrip("/")

    pending = []
    for path in sorted(CONTENT_DIR.rglob("*.json")):
        try:
            with open(path, encoding="utf-8-sig") as f:
                a = json.load(f)
        except Exception:
            continue  # a broken file is build.py's problem, not this step's
        if a.get("fb_posted"):
            continue
        try:
            published = datetime.strptime(a["published"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        if published < start:
            continue
        pending.append((published, path, a))

    if not pending:
        print("[facebook] nothing new to post.")
        return
    pending.sort(key=lambda t: t[0])  # oldest first, so the Page feed reads chronologically
    queue, later = pending[:max_per_run], pending[max_per_run:]
    print(f"[facebook] {len(pending)} unposted article(s); posting {len(queue)} this run"
          + (f", {len(later)} deferred to the next run" if later else "") + "…")

    posted = failed = 0
    for i, (published, path, a) in enumerate(queue):
        url = f'{base}/{cfg["article_prefix"]}/{a["slug"]}/'
        message = (a.get("headline") or "").strip()
        summary = (a.get("summary_short") or "").strip()
        if summary:
            message = f"{message}\n\n{summary}"
        try:
            resp = requests.post(
                f"{FB_GRAPH}/{page_id}/feed",
                data={"message": message, "link": url, "access_token": token},
                timeout=20,
            )
            data = resp.json()
        except Exception as exc:
            failed += 1
            print(f"  [fail] {a.get('headline', '?')}: request error {exc}")
            continue
        if resp.ok and data.get("id"):
            a["fb_posted"] = True
            a["fb_post_id"] = data["id"]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(a, f, ensure_ascii=False, indent=2)
            posted += 1
            print(f"  [posted] {a['headline']} → {data['id']}")
        else:
            failed += 1
            err = data.get("error") or {}
            print(f"  [fail] {a.get('headline', '?')}: HTTP {resp.status_code} — "
                  f"{json.dumps(err, ensure_ascii=False)}")
            if err.get("code") == 190:
                print("  → the Page token is invalid or revoked. Regenerate it "
                      "(Graph API Explorer → debugger 'Extend Access Token' → GET me/accounts) "
                      "and update the FACEBOOK_PAGE_TOKEN repository secret.")
                break  # every remaining post would fail the same way
        if i < len(queue) - 1:
            time.sleep(3)  # be gentle with a young Page
    print(f"[facebook] done: {posted} posted, {failed} failed"
          + (f", {len(later)} still queued for the next run." if later else "."))


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
                http_get(feed["url"], timeout=15)
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
    # The cache-busting suffix means every regeneration gets a NEW filename —
    # correct for CDNs, but it also means the superseded file would sit in
    # the public repo forever unless removed here. Delete it only after the
    # replacement definitely exists, and only if it's a repo-local asset.
    old_image_path = article.get("image_path", "")
    if (old_image_path.startswith("/assets/articles/")
            and old_image_path != photo.get("image_path")):
        old_file = ROOT / old_image_path.lstrip("/")
        try:
            if old_file.exists():
                old_file.unlink()
                print(f"  Removed superseded image: {old_image_path}")
        except OSError as exc:
            print(f"  Could not remove old image {old_image_path} ({exc}) — "
                  f"delete it manually to keep the repo tidy.")
    article.pop("photo_url", None)
    article.pop("photo_credit", None)
    article.pop("photo_credit_url", None)
    article.pop("image_path", None)
    article.pop("image_credit", None)
    article.pop("image_width", None)
    article.pop("image_height", None)
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


def regenerate_all_images(cfg: dict, limit: int | None = None) -> None:
    """Bulk-convert existing Pexels-sourced article photos to AI-generated
    images via the configured provider (config.json's image_provider/
    fal_model). Only touches articles currently using photo_url (Pexels) —
    articles that already have image_path (already AI-generated) are left
    alone, use --regenerate-image for those individually.
    `limit` caps how many articles this run touches, applied BEFORE any
    processing — a predictable, bounded cost regardless of hit rate, since
    image generation either succeeds or fails cleanly with no complex
    parsing step that could otherwise inflate real attempts beyond limit."""
    paths = sorted(CONTENT_DIR.rglob("*.json"))
    targets = []
    for path in paths:
        try:
            with open(path, encoding="utf-8-sig") as f:
                a = json.load(f)
        except Exception:
            continue
        if a.get("photo_url"):
            targets.append((path, a))
    if not targets:
        print("No Pexels-sourced articles found. Nothing to regenerate.")
        return
    print(f"{len(targets)} article(s) currently use a Pexels photo.")
    to_process = targets[:limit] if limit else targets
    print(f"Processing {len(to_process)} of them this run"
          f"{f' (capped by --image-limit {limit})' if limit else ''}.")

    queries: dict[str, str] = {}
    CHUNK = 25  # keep each query-generation prompt small and cheap
    for i in range(0, len(to_process), CHUNK):
        chunk = to_process[i:i + CHUNK]
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

    base = cfg["base_url"].rstrip("/") + cfg.get("base_path", "").rstrip("/")
    done, failed = 0, 0
    for path, article in to_process:
        query = queries.get(str(path), "")
        photo = get_article_photo(cfg, {"image_query": query}, article["slug"]) if query else None
        if not photo:
            failed += 1
            continue
        article.pop("photo_url", None)
        article.pop("photo_credit", None)
        article.pop("photo_credit_url", None)
        article.update(photo)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(article, f, ensure_ascii=False, indent=2)
        done += 1
        print(f"  [image] {article['headline'][:50]} → {query}")
        REVIEW_BATCH.append({
            "kind": "image updated", "headline": article["headline"],
            "summary_short": f"New image query: {query}", "body": "", "quick_facts": [],
            "source_name": "", "url": f'{base}/{cfg["article_prefix"]}/{article["slug"]}/',
        })
    print(f"Done. {done} regenerated, {failed} failed (left with their previous photo).")


def recently_ran(hours: float = 2.0) -> bool:
    """True if a publishing run completed within the cooldown window, judged by the
    last_run timestamp stored INSIDE seen.json — never by file modification times.
    (Mtimes are meaningless in CI: actions/checkout rewrites every file's mtime to
    'right now' on every run, which made the original mtime-based version of this
    check wrongly skip 100% of runs.)

    NOTE: with publish.yml currently dispatch-only (no `schedule:` trigger),
    every CI run is a workflow_dispatch and main() exempts those — so this
    guard is dormant. It's kept (and kept correct) so that re-adding a
    schedule trigger later gets duplicate-run protection for free rather
    than silently double-publishing. Never blocks manual runs."""
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
    saved, new_urls = run_two_phase(cfg, candidates, seen, max_new)
    ping_indexnow(cfg, new_urls)
    for c in candidates:
        if c["id"] not in all_seen_ids(seen):
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
        "id": cand["id"],
        "slug": slug,
        "headline": headline,
        "meta_description": clip(written.get("meta_description", ""), 160),
        "summary_short": clip(written.get("summary_short", ""), 170),
        "body": body,
        "category": category,
        "tags": [clip(t, 30) for t in (written.get("tags") or [])[:5]],
        "quick_facts": [
            c
            for c in (clip(f, 120) for f in (written.get("quick_facts") or [])[:5])
            if c
        ],
        "source_name": cand["source"],
        "source_url": cand["link"],
        "published": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_published": cand.get("source_published"),
        "source_fetched_at": cand.get("source_fetched_at"),
        "source_host": normalize_host(cand["link"]),
        "expected_source_host": cand.get("expected_source_host", ""),
        "full_source_extracted": bool(cand.get("_full_source_extracted")),
        "sensitive_topic": bool(cand.get("_sensitive_topic")),
        "lang": cfg["lang"],
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
        "kind": "new",
        "headline": headline,
        "slug": slug,
        "summary_short": article["summary_short"],
        "body": body,
        "quick_facts": article["quick_facts"],
        "source_name": article["source_name"],
        "source_url": article["source_url"],
        "source_host": article.get("source_host", ""),
        "expected_source_host": article.get("expected_source_host", ""),
        "source_published": article.get("source_published"),
        "source_fetched_at": article.get("source_fetched_at"),
        "full_source_extracted": article.get("full_source_extracted", False),
        "sensitive_topic": article.get("sensitive_topic", False),
        "url": url,
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
    seen_ids = all_seen_ids(seen)
    context_search = cfg.get("context_search", False)
    search_tools = [{"type": "web_search_20250305", "name": "web_search"}] if context_search else None
    for pick in picks:
        try:
            cand = candidates[int(pick["candidate"])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        if cand["id"] in seen_ids:
            continue

        source_ok, source_error = validate_candidate_source(cand)
        if not source_ok:
            print(
                f"    [skip · source mismatch] {cand['title'][:55]} "
                f"— {source_error}"
            )
            continue

        full_text = fetch_full_article(cand["link"])
        sensitive = is_sensitive_candidate(cand)

        cand["_sensitive_topic"] = sensitive
        cand["_full_source_extracted"] = bool(full_text)

        if sensitive and not full_text:
            print(
                f"    [skip · sensitive + no full source] "
                f"{cand['title'][:65]}"
            )
            continue

        tag = "full source" if full_text else "snippet only"
        write_prompt = build_writing_prompt(
            cfg, cand, full_text, use_search=context_search
        )
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
    """Write a human-friendly editorial review report for the PR."""
    if not REVIEW_BATCH:
        return False

    kind_label = {
        "new": "New article",
        "rewritten": "Rewritten article",
        "guide": "New evergreen guide",
        "image updated": "Image updated",
        "image regenerated": "Image regenerated",
    }

    lines = [
        f"# {len(REVIEW_BATCH)} item(s) ready for review",
        "",
        "> Nothing below is live until this PR is merged.",
        "",
    ]

    for i, item in enumerate(REVIEW_BATCH, 1):
        label = kind_label.get(item.get("kind"), "Item")

        lines.append(
            f"## {i}. [{label}] {item.get('headline', '')}"
        )
        lines.append("")

        if item.get("kind") == "new":
            source_name = item.get("source_name") or "Unknown"
            source_url = item.get("source_url") or ""

            actual_host = item.get("source_host") or "unknown"
            expected_host = item.get("expected_source_host") or "unknown"

            full_source = bool(
                item.get("full_source_extracted")
            )

            sensitive = bool(
                item.get("sensitive_topic")
            )

            source_match = (
                actual_host != "unknown"
                and expected_host != "unknown"
                and hosts_match(expected_host, actual_host)
            )

            lines.append("### Editorial safety check")
            lines.append("")
            lines.append("| Check | Result |")
            lines.append("|---|---|")

            lines.append(
                f"| Source domain | "
                f"{'✅ Match' if source_match else '⚠️ Check manually'} "
                f"(`{actual_host}` / expected `{expected_host}`) |"
            )

            lines.append(
                f"| Full source extracted | "
                f"{'✅ Yes' if full_source else '⚠️ No - snippet only'} |"
            )

            lines.append(
                f"| Sensitive topic | "
                f"{'⚠️ Yes - review carefully' if sensitive else '✅ No'} |"
            )

            source_date = item.get("source_published")

            lines.append(
                f"| Source publication date | "
                f"{source_date or 'ℹ️ Not supplied by source'} |"
            )

            lines.append("")
            lines.append(f"**Source:** {source_name}")

            if source_url:
                lines.append(
                    f"**Original URL:** {source_url}"
                )

            reject_slug = item.get("slug")
            if reject_slug:
                lines.append("")
                lines.append(
                    f"**Reject this article:** `/reject {reject_slug}`"
                )

            lines.append("")

        elif item.get("source_name"):
            lines.append(
                f"**Source:** {item['source_name']}"
            )
            lines.append("")

        summary = (
            item.get("summary_short") or ""
        ).strip()

        if summary:
            lines.append(f"**Summary:** {summary}")
            lines.append("")

        body = (
            item.get("body") or ""
        ).strip()

        if body:
            lines.append("### Article text")
            lines.append("")
            lines.append(body)
            lines.append("")

        facts = item.get("quick_facts") or []

        if facts:
            lines.append("**Quick facts:**")

            for fact in facts:
                lines.append(f"- {fact}")

            lines.append("")

        lines.append(
            f"*Will be live at:* {item.get('url', '')}"
        )
        lines.append("")
        lines.append("---")
        lines.append("")

    PR_DESCRIPTION_FILE.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )

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
    ap.add_argument("--regenerate-all-images", action="store_true",
                     help="one-off: bulk-convert existing Pexels photos to AI-generated images (via config.json's image_provider)")
    ap.add_argument("--image-limit", type=int, default=None,
                     help="cap how many articles --regenerate-all-images processes in one run — leave unset to process all of them")
    ap.add_argument("--post-facebook", action="store_true",
                     help="share newly approved (merged-to-main) articles on the Facebook Page, then exit")
    ap.add_argument("--merge-seen-with", type=str, default=None, metavar="REMOTE_FILE",
                     help="internal: merge local seen.json with a fetched remote copy at the JSON level, avoiding git's line-based text merge")
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
    if args.merge_seen_with:
        merge_seen_with_remote(args.merge_seen_with)
        return
    if args.post_facebook:
        post_new_articles_to_facebook(cfg)
        return
    if args.regenerate_all_images:
        regenerate_all_images(cfg, limit=args.image_limit)
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
        print("A run already completed within the last 2 hours — this looks like a "
              "duplicate trigger, not a new one. Skipping to avoid double-publishing. "
              "(Manual 'Run workflow' clicks are never skipped; use --force locally.)")
        return

    seen = load_seen()
    seen_ids = all_seen_ids(seen)
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

    # Select cheaply first, then write each picked story from its full source
    # text. (The old single-call "legacy" path was removed: it referenced a
    # build_prompt() that no longer existed and bypassed the fal image
    # provider — dead code that would have crashed the moment it ran.)
    saved, new_urls = run_two_phase(cfg, candidates, seen, max_new)

    ping_indexnow(cfg, new_urls)

    # Mark rejected candidates as seen too, so we never re-pay to re-judge them.
    seen_now = all_seen_ids(seen)
    for c in candidates:
        if c["id"] not in seen_now:
            seen["ids"].append(c["id"])
    save_seen(seen)
    write_pr_description()
    print(f"Done. {saved} new stor{'y' if saved == 1 else 'ies'} published, "
          f"{len(candidates) - saved} not selected.")


if __name__ == "__main__":
    main()

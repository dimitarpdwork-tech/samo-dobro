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

import requests

try:
    import feedparser
except ImportError:
    print("Missing dependency. Run: pip install -r requirements.txt")
    sys.exit(1)

ROOT = Path(__file__).resolve().parent
CONTENT_DIR = ROOT / "content" / "articles"
SEEN_FILE = ROOT / "content" / "seen.json"
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
USER_AGENT = "GoodNewsBot/1.0 (+https://github.com/; polite RSS reader)"

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
    with open(ROOT / "config.json", encoding="utf-8") as f:
        return json.load(f)


def load_seen() -> dict:
    if SEEN_FILE.exists():
        with open(SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"ids": []}


def save_seen(seen: dict) -> None:
    seen["ids"] = seen["ids"][-4000:]  # keep the file small
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
        feed["url"], timeout=15, headers={"User-Agent": USER_AGENT}
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


def collect_candidates(cfg: dict, seen_ids: set) -> list[dict]:
    candidates, errors = [], []
    for feed in cfg["feeds"]:
        try:
            entries = fetch_feed(feed, cfg.get("window_hours", 48))
            fresh = [e for e in entries if e["id"] not in seen_ids]
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
            with open(path, encoding="utf-8") as f:
                a = json.load(f)
            dt = datetime.strptime(a["published"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                items.append((dt, a.get("headline", "")))
        except Exception:
            continue
    items.sort(key=lambda x: x[0], reverse=True)
    return [h for _, h in items[:limit]]


def build_prompt(cfg: dict, candidates: list[dict], max_new: int, recent_headlines: list[str]) -> str:
    cat_lines = "\n".join(
        f'- "{cid}": {c["label"]}' for cid, c in cfg["categories"].items()
    )
    cand_lines = "\n".join(
        f'{i}. [{c["source"]}] {c["title"]} — {c["summary"] or "(no summary)"}'
        for i, c in enumerate(candidates)
    )
    recent_block = ""
    if recent_headlines:
        recent_list = "\n".join(f"- {h}" for h in recent_headlines[:60])
        recent_block = f"""
ALREADY PUBLISHED — DO NOT DUPLICATE
These headlines were already published recently. If a candidate below covers the same real-world event or story as any of these (even from a different source outlet), REJECT it rather than writing it again:
{recent_list}
"""
    return f"""You are the sole editor of "{cfg['site_name']}", a news site that publishes ONLY genuinely good, uplifting news, written in {cfg['language_name']}.

Below is a numbered list of raw news candidates pulled from RSS feeds.

YOUR TASK
1. Select at most {max_new} candidates that are GENUINELY positive: concrete good outcomes, kindness, recoveries of nature, scientific or medical breakthroughs, community wins, cultural achievements, records of human generosity or skill.
2. REJECT anything whose core is negative even if framed positively: war, crime, accidents, disasters, deaths, disease outbreaks, scandals, court cases, party politics, election results, celebrity gossip, advertising/PR, financial speculation. When in doubt, reject. Selecting zero is a valid answer.
3. For each selected story, write an ORIGINAL article in {cfg['language_name']}.
{recent_block}
STRICT WRITING RULES
- Use ONLY facts present in the candidate's title/summary above. Never invent numbers, names, quotes, dates or details. If the snippet is too thin to write 2 short paragraphs honestly, reject it.
- Write completely in your own words. Do not copy or closely paraphrase the source phrasing.
- Tone: warm, human, concrete. Hopeful but never saccharine or clickbaity.
- The reader should finish the story feeling lighter.

LANGUAGE QUALITY BAR — THIS IS NON-NEGOTIABLE
- Write in fully correct, natural, native-level {cfg['language_name']}, as a professional native-speaking editor would.
- NEVER invent a word that does not exist in {cfg['language_name']}. If you are not completely certain a word is real and correctly spelled, use a simpler word you are certain of instead.
- Check every noun-adjective pair for correct grammatical gender/number agreement before finalizing (this is a common failure point — verify it explicitly).
- Do not borrow spellings or vocabulary from a closely related language (e.g. when writing Bulgarian, never use Russian spellings or words — the two are related but distinct, and mixing them is a real, disqualifying error).
- After drafting each article, re-read it once specifically to check for grammar and invented words before including it in your output. If any sentence feels uncertain, simplify it rather than risk an error.
- Respectful, neutral phrasing for gender and identity: describe achievements plainly (e.g. "first woman [role]") — never use a gendered adjective to modify a person's professional title or role in a way that could read as diminishing.

OUTPUT FORMAT
Respond with ONLY a JSON array (no markdown fences, no commentary). Each element:
{{
  "candidate": <number from the list>,
  "headline": "<max 75 characters, in {cfg['language_name']}>",
  "slug_hint": "<3-6 latin lowercase words separated by hyphens>",
  "category": "<one id from the category list below>",
  "meta_description": "<max 155 characters, in {cfg['language_name']}>",
  "summary_short": "<max 160 characters teaser, in {cfg['language_name']}>",
  "body": "<3-4 short paragraphs separated by \\n\\n, aim for 150-190 words total — this length matters, don't undershoot it, in {cfg['language_name']}>",
  "tags": ["<3-5 short tags in {cfg['language_name']}, lowercase, single words or hyphenated phrases, NEVER containing spaces>"]
}}

CATEGORY IDS
{cat_lines}

CANDIDATES
{cand_lines}"""


def call_claude(cfg: dict, prompt: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY is not set. Aborting.")
        sys.exit(1)
    body = {
        "model": cfg.get("model", "claude-haiku-4-5-20251001"),
        "max_tokens": 6000,
        "messages": [{"role": "user", "content": prompt}],
    }
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
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            resp.raise_for_status()
            data = resp.json()
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
    sys.exit(1)


def parse_selection(raw: str) -> list[dict]:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        print("  [parse] no JSON array in model output — treating as zero selections.")
        return []
    try:
        items = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        print(f"  [parse] JSON error: {exc} — treating as zero selections.")
        return []
    return items if isinstance(items, list) else []


def clip(value: str, limit: int) -> str:
    value = (value or "").strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


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
            "source_name": cand["source"],
            "source_url": cand["link"],
            "published": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "lang": cfg["lang"],
        }
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
            entries = fetch_feed(feed, window_hours=24 * 14)
            print(f"  OK    {feed['name']}: {len(entries)} recent entries — {feed['url']}")
            ok += 1
        except Exception as exc:
            print(f"  FAIL  {feed['name']}: {exc} — {feed['url']}")
    print(f"{ok}/{len(cfg['feeds'])} feeds working. Remove or replace failing ones in config.json.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Good-news pipeline")
    ap.add_argument("--check-feeds", action="store_true")
    ap.add_argument("--dry", action="store_true", help="list candidates only, no API call")
    ap.add_argument("--limit", type=int, default=None, help="max new stories this run")
    args = ap.parse_args()

    cfg = load_config()
    if args.check_feeds:
        check_feeds(cfg)
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
    recent_headlines = load_recent_headlines()
    prompt = build_prompt(cfg, candidates, max_new, recent_headlines)
    print("  asking the editor model to pick the good ones…")
    raw = call_claude(cfg, prompt)
    items = parse_selection(raw)[:max_new]
    saved, new_urls = save_articles(cfg, items, candidates, seen)
    ping_indexnow(cfg, new_urls)

    # Mark rejected candidates as seen too, so we never re-pay to re-judge them.
    for c in candidates:
        if c["id"] not in set(seen["ids"]):
            seen["ids"].append(c["id"])
    save_seen(seen)
    print(f"Done. {saved} new stor{'y' if saved == 1 else 'ies'} published, "
          f"{len(candidates) - saved} rejected as not-good-enough news.")


if __name__ == "__main__":
    main()

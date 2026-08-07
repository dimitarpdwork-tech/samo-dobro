#!/usr/bin/env python3
"""Miss audit — measure what the site is failing to publish.

The site currently learns about its blind spots by accident: the editor
stumbles on a story elsewhere and notices it never came through. That is not a
signal you can act on, because it has no denominator. This turns it into one.

It re-runs the Google News topic queries over a long window, throws away
everything that was published or explicitly dismissed, and reports what is
left, ranked by the same local scorer the pipeline uses. Whatever is near the
top of that list is a story the system saw, or could have seen, and dropped.

READ-ONLY. It never writes seen.json and never marks anything as dismissed —
running the audit must not change what tomorrow's shortlist offers.

Usage:
    python tools/miss_audit.py                 # last 30 days
    python tools/miss_audit.py --days 60
    python tools/miss_audit.py --out audit.md
"""

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pipeline  # noqa: E402

ARTICLES = ROOT / "content" / "articles"

# Google News encodes the recency window in the query itself as `when:2d`.
# The audit needs a much longer one, so rewrite it rather than keeping a
# second copy of every query in the config that could drift out of sync.
_WHEN_RE = re.compile(r"when%3A\d+[dhm]|when:\d+[dhm]")


def _norm_title(text: str) -> str:
    """Fold a headline for comparison. Published headlines are rewritten from
    the source, so exact matching finds nothing — compare on a bag of the
    longer words instead."""
    text = unicodedata.normalize("NFKD", (text or "").lower())
    # Crude prefix stemming, not a nicety: Bulgarian inflects heavily, so
    # "спасиха" and "спасено" are the same event told twice and whole-word
    # matching scores them at 0.33 — low enough that the audit would report a
    # published story as a miss. Truncating to a 4-char prefix collapses the
    # inflection. It over-collapses occasionally; the 0.55 overlap threshold
    # and the requirement of several shared stems absorb that.
    words = re.findall(r"[а-яa-z]{4,}", text)
    return " ".join(sorted({w[:4] for w in words}))


def _title_overlap(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def load_published() -> tuple[set, list, Counter]:
    """Return (source urls, normalised headlines, per-host counts)."""
    urls, titles, hosts = set(), [], Counter()
    for f in ARTICLES.rglob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("source_url"):
            urls.add(d["source_url"].split("?")[0].rstrip("/"))
        titles.append(_norm_title(d.get("headline", "")))
        if d.get("source_host"):
            hosts[d["source_host"].replace("www.", "")] += 1
    return urls, titles, hosts


def widen(url: str, days: int) -> str:
    return _WHEN_RE.sub(f"when%3A{days}d", url)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", default="miss_audit.md")
    ap.add_argument("--top", type=int, default=40, help="how many misses to list")
    args = ap.parse_args()

    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    seen = pipeline.load_seen()
    dismissed = set(seen.get("dismissed", []))
    pub_urls, pub_titles, pub_hosts = load_published()

    aggregators = [f for f in cfg["feeds"] if isinstance(f, dict) and f.get("aggregator")]
    if not aggregators:
        print("[audit] няма aggregator feeds в config.json — няма какво да одитирам.")
        return 1

    seen_ids, candidates, failed = set(), [], []
    for feed in aggregators:
        probe = dict(feed, url=widen(feed["url"], args.days), window_hours=args.days * 24)
        try:
            entries = pipeline.fetch_feed(probe, window_hours=args.days * 24)
        except Exception as exc:
            failed.append((feed["name"], str(exc)[:80]))
            continue
        for e in entries:
            if e["id"] in seen_ids:
                continue
            seen_ids.add(e["id"])
            candidates.append(e)

    misses = []
    for c in candidates:
        if c.get("id") in dismissed:
            continue  # the editor looked at it and said no — not a miss
        link = (c.get("link") or "").split("?")[0].rstrip("/")
        if link and link in pub_urls:
            continue
        norm = _norm_title(c.get("title", ""))
        if any(_title_overlap(norm, t) >= 0.55 for t in pub_titles):
            continue  # published from another source
        score, hard_drop, reasons = pipeline.score_candidate(cfg, c)
        if hard_drop:
            continue
        misses.append({**c, "_score": score, "_reasons": reasons})

    misses.sort(key=lambda m: -m["_score"])
    total = len(candidates)
    rate = (len(misses) * 100 // total) if total else 0

    lines = [
        f"# Одит на изпуснатото — последните {args.days} дни",
        "",
        f"- Кандидати от тематичните заявки: **{total}**",
        f"- Непубликувани и неотхвърлени: **{len(misses)}** ({rate}%)",
        f"- Отхвърлени от теб (не се броят за изпуснати): "
        f"**{sum(1 for c in candidates if c.get('id') in dismissed)}**",
        "",
    ]
    if failed:
        lines += ["> Заявки, които не се заредиха: "
                  + "; ".join(f"{n} ({e})" for n, e in failed), ""]

    by_host = Counter(c.get("expected_source_host", "?") for c in misses)
    if by_host:
        lines += ["## Откъде идва изпуснатото", ""]
        for host, n in by_host.most_common(10):
            have = pub_hosts.get(host, 0)
            flag = "  ← няма нито една публикувана оттам" if have == 0 else ""
            lines.append(f"- `{host}` — {n} изпуснати, {have} публикувани{flag}")
        lines.append("")

    lines += [f"## Топ {min(args.top, len(misses))} по локален скор", ""]
    for m in misses[: args.top]:
        why = f" _({', '.join(m['_reasons'][:3])})_" if m.get("_reasons") else ""
        lines.append(f"- **{m['_score']}** · [{m['title']}]({m['link']}) "
                     f"— {m.get('source', '?')}{why}")
    lines.append("")
    lines.append(
        "Висок скор тук значи, че системата е видяла или е могла да види "
        "историята и я е изпуснала. Ако един и същ хост се повтаря, добави "
        "го като собствен feed вместо да разчиташ на Google News."
    )

    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    print(f"[audit] {len(misses)} изпуснати от {total} кандидата ({rate}%) → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

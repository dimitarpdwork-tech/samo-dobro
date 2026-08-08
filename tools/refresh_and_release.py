#!/usr/bin/env python3
"""Re-draft and/or release articles that are written but not yet live.

Articles written from a Google News redirect only ever had a two-sentence RSS
snippet to work from, so their bodies are roughly half the length of the rest
of the archive. Now that source_url points at the real publisher, the source
can actually be fetched and the story rewritten properly.

Two independent actions, either or both:

  --redraft   Re-fetch each article's source and rewrite the body against the
              current writing standard. Preserves slug, id, image, category
              and source — only the text improves. An article whose source
              cannot be fetched, or whose rewrite fails to parse, is left
              EXACTLY as it was. A bad rewrite never replaces good text.

  --release   Clear the embargo so the article goes live. Either all at once
              (--release-mode now) or spread out from now using the interval
              in config.json (--release-mode stagger, the default), so six
              stories don't land on the homepage in the same second.

By default it acts on every article with a future publish_at. Pass --slugs to
target specific ones instead.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow scripts launched as `python tools/<script>.py` to import pipeline.py
# from the repository root in GitHub Actions and local runs.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pipeline

ARTICLES = ROOT / "content" / "articles"
STAMP = "%Y-%m-%dT%H:%M:%SZ"


def load(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def save(path: Path, art: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(art, f, ensure_ascii=False, indent=2)


def select(slugs: list[str]) -> list[tuple[Path, dict]]:
    """Target the requested slugs, or every article still under embargo."""
    now = datetime.now(timezone.utc)
    out = []
    for path in sorted(ARTICLES.rglob("*.json")):
        art = load(path)
        if not art:
            continue
        if slugs:
            if art.get("slug") in slugs:
                out.append((path, art))
            continue
        if not pipeline.is_live(art, now):
            out.append((path, art))
    return out


def redraft_one(cfg: dict, path: Path, art: dict) -> str:
    """Rewrite one article from its source. Returns a short status string."""
    src_url = art.get("source_url") or ""
    if not src_url:
        return "no source_url — left untouched"
    if pipeline.is_aggregator_url(src_url):
        return "source is still an aggregator link — left untouched"

    full_text = pipeline.fetch_full_article(src_url)
    if not full_text:
        return "source could not be fetched — left untouched"

    before = len(art.get("body", ""))

    pseudo = {
        "title": art.get("headline", ""),
        "source": art.get("source_name", ""),
        "summary": art.get("summary_short", ""),
        "link": src_url,
    }
    context_search = cfg.get("context_search", False)
    tools = (
        [{"type": "web_search_20250305", "name": "web_search"}]
        if context_search else None
    )
    raw = pipeline.call_claude(
        cfg,
        pipeline.build_writing_prompt(cfg, pseudo, full_text, use_search=context_search),
        tools=tools,
        hard_fail=False,
    )
    written = pipeline.parse_delimited_article(raw)
    if not written or not (written.get("body") or "").strip():
        return "rewrite failed to parse — left untouched"

    # Merge only the text. slug, id, published, publish_at, image_* and every
    # source_* field are deliberately preserved: the URL is already public and
    # must not move, and the source is what we just verified.
    category = (
        written.get("category")
        if written.get("category") in cfg["categories"]
        else art.get("category")
    )
    art["headline"] = pipeline.clip(written.get("headline") or art["headline"], 90)
    art["meta_description"] = (
        pipeline.clip(written.get("meta_description", ""), 160)
        or art.get("meta_description", "")
    )
    art["summary_short"] = (
        pipeline.clip(written.get("summary_short", ""), 170)
        or art.get("summary_short", "")
    )
    art["body"] = written["body"].strip()
    art["category"] = category
    if written.get("tags"):
        art["tags"] = [pipeline.clip(t, 30) for t in written["tags"][:5]]
    if written.get("quick_facts"):
        art["quick_facts"] = [
            c for c in (pipeline.clip(f, 120) for f in written["quick_facts"][:5]) if c
        ]
    art["full_source_extracted"] = True
    art["rewritten"] = datetime.now(timezone.utc).strftime(STAMP)

    save(path, art)
    return f"rewritten, body {before} -> {len(art['body'])} chars"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--redraft", action="store_true",
                    help="rewrite bodies from the real source")
    ap.add_argument("--release", action="store_true",
                    help="clear the embargo so the articles go live")
    ap.add_argument("--release-mode", choices=["now", "stagger"], default="stagger",
                    help="publish all at once, or spread from now (default)")
    ap.add_argument("--slugs", default="",
                    help="comma-separated slugs; default is every embargoed article")
    args = ap.parse_args()

    if not (args.redraft or args.release):
        print("Nothing to do: pass --redraft, --release, or both.")
        return 0

    cfg = pipeline.load_config()
    slugs = [s.strip() for s in args.slugs.split(",") if s.strip()]
    targets = select(slugs)

    if not targets:
        print("No matching articles. Nothing to do.")
        return 0

    print(f"{len(targets)} article(s) selected.\n")

    if args.redraft:
        print("Re-drafting from source:")
        for path, art in targets:
            status = redraft_one(cfg, path, art)
            print(f"  {art.get('slug', path.stem)}\n      {status}")
        print()

    if args.release:
        # Re-read: redraft_one wrote to disk, and we must not clobber it.
        targets = select(slugs)
        now = datetime.now(timezone.utc)
        if args.release_mode == "now":
            slots = [now] * len(targets)
        else:
            slots = pipeline.compute_publish_slots(cfg, len(targets), start=now)

        print(f"Releasing ({args.release_mode}):")
        for (path, art), slot in zip(targets, slots):
            # An article that is ALREADY public must never be re-dated. Its URL
            # is indexed, it may already have been posted to Facebook, and
            # moving `published` forward would present old news as new. This
            # matters because --slugs can legitimately target live articles
            # (to re-draft a thin one), and release must be a no-op for those.
            if pipeline.is_live(art, now):
                print(f"  {art.get('slug', path.stem)}  ->  already live, date untouched")
                continue
            stamp = slot.strftime(STAMP)
            # `published` tracks publish_at by design — an article that becomes
            # visible at 09:40 must carry 09:40, not the moment it was drafted.
            art["publish_at"] = stamp
            art["published"] = stamp
            save(path, art)
            print(f"  {art.get('slug', path.stem)}  ->  {stamp}")
        print()

    print("Done. Review the PR, then merge to publish.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

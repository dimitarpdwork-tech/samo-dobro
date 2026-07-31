#!/usr/bin/env python3
"""
Decide whether the site needs rebuilding because a scheduled article is due.

Writes `due=true` or `due=false` to $GITHUB_OUTPUT so the workflow can skip the
build entirely when there is nothing to publish. Runs on a short cron, so it is
deliberately stdlib-only: the no-op path is a checkout plus this script, with no
pip install.

HOW "DUE" IS DECIDED
--------------------
Not by a time window. The check compares the articles that SHOULD be visible
now against the live sitemap:

    an article is due  <=>  its embargo has passed
                            AND its URL is missing from the deployed sitemap

That is stateless and self-correcting. A time-window heuristic ("did anything
become due in the last 15 minutes?") silently loses an article whenever a run
is skipped, throttled, or fails — and GitHub cron is missed often enough for
that to matter. Comparing against reality instead means a missed run is simply
picked up by the next one.

If the sitemap cannot be fetched, the script reports due=true: a wasted build
is cheap, a permanently unpublished article is not.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTENT = ROOT / "content" / "articles"
CONFIG = ROOT / "config.json"
UA = "DobroDeloScheduler/1.0 (+https://dobrodelo.com/za-nas/)"


def emit(due: bool, reason: str) -> int:
    print(f"[due] {'REBUILD' if due else 'nothing to do'} — {reason}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"due={'true' if due else 'false'}\n")
    return 0


def main() -> int:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    base = cfg["base_url"].rstrip("/") + cfg.get("base_path", "").rstrip("/")
    prefix = cfg.get("article_prefix", "novina")
    now = datetime.now(timezone.utc)

    if not CONTENT.exists():
        return emit(False, "no content directory")

    # Articles whose embargo passed RECENTLY. Anything without publish_at is
    # already live by definition, and anything due more than LOOKBACK ago has
    # certainly been built by some later push — checking those forever would
    # mean one pointless sitemap fetch every 15 minutes for the life of the
    # site. The window still needs to be far wider than the cron interval so
    # that a run of skipped or failed builds is recovered rather than lost.
    LOOKBACK_HOURS = 48
    should_be_live: list[str] = []
    still_waiting = 0
    for path in CONTENT.rglob("*.json"):
        try:
            a = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        raw = a.get("publish_at")
        if not raw:
            continue
        try:
            embargo = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if now >= embargo:
            age_hours = (now - embargo).total_seconds() / 3600
            if age_hours <= LOOKBACK_HOURS:
                should_be_live.append(f'{base}/{prefix}/{a["slug"]}/')
        else:
            still_waiting += 1

    if not should_be_live:
        return emit(False, f"nothing became due in the last {LOOKBACK_HOURS}h ({still_waiting} still scheduled)")

    # Cache-buster: GitHub Pages sits behind a CDN, and a stale sitemap would
    # make an already-published article look missing and trigger a needless
    # rebuild every 15 minutes.
    url = f"{base}/sitemap.xml?t={int(now.timestamp())}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            sitemap = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return emit(True, f"could not fetch sitemap ({exc}) — rebuilding to be safe")

    missing = [u for u in should_be_live if u not in sitemap]
    if missing:
        return emit(True, f"{len(missing)} due article(s) not yet on the site: "
                          + ", ".join(m.rsplit('/', 2)[-2] for m in missing[:3]))
    return emit(False, f"all {len(should_be_live)} due article(s) already live "
                       f"({still_waiting} still scheduled)")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # never let a scheduler bug wedge publishing
        print(f"[due] check failed ({exc}) — rebuilding to be safe", file=sys.stderr)
        raise SystemExit(emit(True, "check errored"))

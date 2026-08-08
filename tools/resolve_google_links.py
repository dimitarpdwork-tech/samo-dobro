#!/usr/bin/env python3
"""Audit and repair published articles whose cited source is an aggregator.

Eight articles went live with source_url pointing at a news.google.com redirect
and source_host set to "news.google.com" while source_name named the real
publisher. This finds them and, where the redirect can still be resolved,
rewrites source_url / source_host to the publisher's own URL.

    python tools/resolve_google_links.py            # report only, no writes
    python tools/resolve_google_links.py --fix      # rewrite what resolves
    python tools/resolve_google_links.py --fix --unpublish-unresolved

Run the report first. It doubles as the live test of the resolver: if
everything comes back "unresolved", Google's batchexecute endpoint has changed
shape again and the aggregator feeds should simply be turned off in config.json
rather than patched around.
"""

import argparse
import json
import sys
from pathlib import Path

# Allow scripts launched as `python tools/<script>.py` to import pipeline.py
# from the repository root in GitHub Actions and local runs.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pipeline

ARTICLES = Path("content/articles")


def find_aggregator_articles() -> list[Path]:
    hits = []
    for path in sorted(ARTICLES.glob("*/*/*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if pipeline.is_aggregator_url(data.get("source_url", "")):
            hits.append(path)
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true",
                    help="rewrite source_url/source_host where resolution succeeds")
    ap.add_argument("--unpublish-unresolved", action="store_true",
                    help="delete articles whose source link cannot be resolved")
    args = ap.parse_args()

    hits = find_aggregator_articles()
    if not hits:
        print("No published article cites an aggregator. Nothing to do.")
        return 0

    print(f"{len(hits)} article(s) cite an aggregator:\n")

    fixed = unresolved = removed = 0

    for path in hits:
        data = json.loads(path.read_text(encoding="utf-8"))
        slug = data.get("slug", path.stem)
        resolved = pipeline.resolve_aggregator_url(data.get("source_url", ""))

        if resolved:
            host = pipeline.normalize_host(resolved)
            print(f"  [resolved]   {slug}")
            print(f"               -> {host}  {resolved[:90]}")
            if args.fix:
                data["source_url"] = resolved
                data["source_host"] = host
                if not data.get("source_name"):
                    data["source_name"] = host
                path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                fixed += 1
        else:
            unresolved += 1
            claimed = data.get("source_name", "?")
            print(f"  [unresolved] {slug}  (source_name claims: {claimed})")
            if args.fix and args.unpublish_unresolved:
                path.unlink()
                removed += 1
                print(f"               removed {path}")

    print(
        f"\n{len(hits)} checked; {fixed} rewritten, "
        f"{unresolved} unresolved, {removed} removed."
    )
    if not args.fix:
        print("Report only — nothing was written. Re-run with --fix to apply.")
    else:
        print("Run `python3 build.py` and check the pages before committing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

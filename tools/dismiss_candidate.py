#!/usr/bin/env python3
"""
Immediately dismiss specific candidates from a Daily Candidates issue.

WHY THIS EXISTS SEPARATELY FROM /publish
----------------------------------------
Candidates the model never shortlisted are marked seen straight away by
prepare_candidate_issue.py. But candidates that DID reach the shortlist are
only marked seen at the very end of a successful publish_selected.py run —
after the writing calls, after the image generation, after the save.

That means a story you keep deliberately skipping keeps coming back if:
  - the writing step fails (API error, parse failure, rate limit), so the
    workflow never reaches the seen-commit step; or
  - you simply close the issue by hand, or ignore it; or
  - you run /publish but the run breaks partway.

In all of those cases the editorial decision is real but nothing records it,
so tomorrow's shortlist happily offers the same story again.

/dismiss records the rejection immediately and independently, without writing
anything, without touching the issue state, and without depending on any later
step succeeding. It is the "no, and stop asking" button.

Usage:
    python tools/dismiss_candidate.py \
        --issue-body-file /tmp/issue.md \
        --command "/dismiss 3 5"
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
import sys

# Allow scripts launched as `python tools/<script>.py` to import pipeline.py
# from the repository root in GitHub Actions and local runs.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# ...and sibling tools, so the queue format has exactly one definition.
TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import pipeline
from publish_selected import decode_queue

RESULT_FILE = ROOT / "dismiss_result.md"


def parse_dismiss(command: str, item_count: int) -> list[int]:
    """Parse `/dismiss 3`, `/dismiss 3 5 7` or `/dismiss 3,5,7` into a list of
    1-based candidate numbers. Deliberately strict: a typo should produce a
    clear error rather than dismissing the wrong story, since dismissal is not
    conveniently reversible from the issue."""
    command = (command or "").strip()

    match = re.fullmatch(r"/dismiss\s+([0-9,\s]+)\s*", command, flags=re.IGNORECASE)
    if not match:
        raise ValueError(
            "Invalid command. Use `/dismiss 3`, `/dismiss 3 5 7` or `/dismiss 3,5,7`."
        )

    raw_numbers = re.findall(r"\d+", match.group(1))
    if not raw_numbers:
        raise ValueError("No candidate numbers were supplied.")

    numbers: list[int] = []
    for raw in raw_numbers:
        number = int(raw)
        if number < 1 or number > item_count:
            raise ValueError(
                f"Candidate {number} is out of range — this shortlist has "
                f"{item_count} item(s)."
            )
        if number not in numbers:
            numbers.append(number)

    return numbers


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue-body-file", required=True)
    ap.add_argument("--command", required=True)
    args = ap.parse_args()

    issue_body = Path(args.issue_body_file).read_text(encoding="utf-8")
    queue = decode_queue(issue_body)
    items = queue["items"]

    numbers = parse_dismiss(args.command, len(items))

    seen = pipeline.load_seen()
    # Check against BOTH lists, but write only into the permanent one: the
    # rolling "ids" window is capped at 4000 and evicts old entries, so a
    # dismissal recorded there would quietly expire and the story would come
    # back — the exact failure /dismiss exists to prevent.
    seen_ids = pipeline.all_seen_ids(seen)
    seen.setdefault("dismissed", [])

    dismissed: list[dict] = []
    already: list[dict] = []

    for number in numbers:
        item = items[number - 1]
        cand = item.get("candidate") or {}
        cand_id = cand.get("id")
        title = cand.get("title") or item.get("headline") or "(untitled)"

        if not cand_id:
            # Nothing stable to record against; skip rather than guess.
            already.append({"number": number, "title": title, "note": "no candidate id"})
            continue

        if cand_id in seen_ids:
            already.append({"number": number, "title": title, "note": "already dismissed"})
            continue

        seen["dismissed"].append(cand_id)
        seen_ids.add(cand_id)
        dismissed.append({"number": number, "title": title})

    if dismissed:
        pipeline.save_seen(seen)

    # Human-readable result for the workflow to post back as a comment.
    lines = []
    if dismissed:
        lines.append(f"🚫 Отхвърлени завинаги ({len(dismissed)}):")
        lines.append("")
        for d in dismissed:
            lines.append(f"- **#{d['number']}** — {d['title'][:110]}")
        lines.append("")
        lines.append("Тези истории няма да се появят отново в бъдещи предложения.")
    if already:
        lines.append("")
        lines.append("ℹ️ Пропуснати:")
        for a in already:
            lines.append(f"- #{a['number']} — {a['title'][:90]} ({a['note']})")
    if not dismissed and not already:
        lines.append("Нищо не беше отхвърлено.")

    lines.append("")
    lines.append(
        "_Списъкът остава отворен — продължи с `/publish …` за избраните истории._"
    )

    RESULT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(
        f"[dismiss] dismissed {len(dismissed)} candidate(s); "
        f"{len(already)} skipped."
    )
    for d in dismissed:
        print(f"  [dismissed] #{d['number']} {d['title'][:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

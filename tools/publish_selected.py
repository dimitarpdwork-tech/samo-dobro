#!/usr/bin/env python3
"""
Publish only the human-selected candidates from a Daily Candidates GitHub Issue.

The issue body contains a hidden base64 queue created by prepare_candidate_issue.py.
The user's command can be:
    /publish 1 3 6
    /publish 1,3,6
    /publish none

There is deliberately NO maximum selection count.

All shortlisted candidates are marked seen after the editorial decision:
- selected items are written;
- unselected items are treated as editorially dismissed and won't reappear tomorrow.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
from pathlib import Path

import pipeline


ROOT = Path(__file__).resolve().parents[1]
RESULT_FILE = ROOT / "publish_selection_result.json"


def decode_queue(issue_body: str) -> dict:
    match = re.search(
        r"<!--\s*DOBRODELO_QUEUE_B64:([A-Za-z0-9_\-=]+)\s*-->",
        issue_body,
    )
    if not match:
        raise ValueError("The issue does not contain a DobroDelo candidate queue.")

    raw = base64.urlsafe_b64decode(match.group(1).encode("ascii"))
    queue = json.loads(raw.decode("utf-8"))

    if queue.get("version") != 1 or not isinstance(queue.get("items"), list):
        raise ValueError("Unsupported or malformed candidate queue.")

    return queue


def parse_selection(command: str, item_count: int) -> list[int]:
    command = (command or "").strip()

    if re.fullmatch(r"/publish\s+none\s*", command, flags=re.IGNORECASE):
        return []

    match = re.fullmatch(
        r"/publish\s+([0-9,\s]+)\s*",
        command,
        flags=re.IGNORECASE,
    )
    if not match:
        raise ValueError(
            "Invalid command. Use `/publish 1 3 6`, `/publish 1,3,6`, "
            "or `/publish none`."
        )

    raw_numbers = re.findall(r"\d+", match.group(1))
    if not raw_numbers:
        raise ValueError("No candidate numbers were supplied.")

    selected = []
    seen = set()

    for raw in raw_numbers:
        number = int(raw)
        if number < 1 or number > item_count:
            raise ValueError(
                f"Candidate {number} does not exist. Valid range: 1-{item_count}."
            )
        if number not in seen:
            selected.append(number)
            seen.add(number)

    return selected


def mark_entire_shortlist_seen(seen: dict, queue: dict) -> None:
    ids = list(seen.get("ids", []))
    present = set(ids)

    for item in queue["items"]:
        candidate_id = item.get("candidate", {}).get("id")
        if candidate_id and candidate_id not in present:
            ids.append(candidate_id)
            present.add(candidate_id)

    seen["ids"] = ids


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue-body", required=True)
    ap.add_argument("--command", required=True)
    args = ap.parse_args()

    issue_body = Path(args.issue_body).read_text(encoding="utf-8")
    queue = decode_queue(issue_body)
    selected_numbers = parse_selection(args.command, len(queue["items"]))

    cfg = pipeline.load_config()
    seen = pipeline.load_seen()
    already_seen = set(seen.get("ids", []))

    selected_items = [queue["items"][n - 1] for n in selected_numbers]

    print(
        f"[editorial] selected {len(selected_items)} of "
        f"{len(queue['items'])} shortlisted candidates."
    )

    saved = 0
    failed = []
    new_urls = []

    context_search = cfg.get("context_search", False)
    search_tools = (
        [{"type": "web_search_20250305", "name": "web_search"}]
        if context_search
        else None
    )

    for human_number, item in zip(selected_numbers, selected_items):
        cand = item["candidate"]

        # An item might have been published through another route since the issue was created.
        if cand.get("id") in already_seen:
            print(f"  [skip · already seen] #{human_number} {cand.get('title','')[:60]}")
            failed.append(
                {"number": human_number, "title": cand.get("title", ""), "reason": "already seen"}
            )
            continue

        if hasattr(pipeline, "validate_candidate_source"):
            source_ok, source_error = pipeline.validate_candidate_source(cand)
            if not source_ok:
                print(
                    f"  [skip · source mismatch] #{human_number} "
                    f"{cand.get('title','')[:55]} — {source_error}"
                )
                failed.append(
                    {
                        "number": human_number,
                        "title": cand.get("title", ""),
                        "reason": source_error,
                    }
                )
                continue

        full_text = pipeline.fetch_full_article(cand.get("link", ""))
        sensitive = (
            pipeline.is_sensitive_candidate(cand)
            if hasattr(pipeline, "is_sensitive_candidate")
            else False
        )

        cand["_sensitive_topic"] = sensitive
        cand["_full_source_extracted"] = bool(full_text)

        if sensitive and not full_text:
            reason = "sensitive topic but full source extraction failed"
            print(f"  [skip · safety] #{human_number} {cand.get('title','')[:60]}")
            failed.append(
                {"number": human_number, "title": cand.get("title", ""), "reason": reason}
            )
            continue

        prompt = pipeline.build_writing_prompt(
            cfg,
            cand,
            full_text,
            use_search=context_search,
        )

        raw = pipeline.call_claude(
            cfg,
            prompt,
            tools=search_tools,
            hard_fail=False,
        )
        written = pipeline.parse_delimited_article(raw)

        if not written:
            reason = "writing response could not be parsed"
            print(f"  [skip · writing failed] #{human_number} {cand.get('title','')[:60]}")
            failed.append(
                {"number": human_number, "title": cand.get("title", ""), "reason": reason}
            )
            continue

        url = pipeline.save_one_written(cfg, written, cand, seen)
        if not url:
            reason = "article save validation failed"
            failed.append(
                {"number": human_number, "title": cand.get("title", ""), "reason": reason}
            )
            continue

        saved += 1
        new_urls.append(url)
        already_seen.add(cand["id"])
        print(f"  [written] #{human_number} {written.get('headline','')[:65]}")

    # The human has made the editorial decision. Everything not selected is
    # deliberately treated as dismissed so it doesn't keep reappearing.
    mark_entire_shortlist_seen(seen, queue)
    pipeline.save_seen(seen)

    if saved and hasattr(pipeline, "write_pr_description"):
        pipeline.write_pr_description()

    result = {
        "shortlist_count": len(queue["items"]),
        "selected_count": len(selected_items),
        "saved_count": saved,
        "failed": failed,
        "new_urls": new_urls,
        "selection": selected_numbers,
    }
    RESULT_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        f"[editorial] done: {saved} article(s) written; "
        f"{len(failed)} selected item(s) failed safety/writing checks."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

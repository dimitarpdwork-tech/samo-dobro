#!/usr/bin/env python3
"""
Build a cheap daily editorial shortlist and write a GitHub Issue body.

This script does NOT write articles and does NOT generate images.
It only:
- collects recent unseen candidates using pipeline.py
- asks Claude to shortlist the strongest positive-news options
- writes candidate_issue.md with titles/sources/scores
- embeds the machine-readable queue in a hidden HTML comment

Usage:
    python tools/prepare_candidate_issue.py
    python tools/prepare_candidate_issue.py --shortlist-size 15
"""

from __future__ import annotations

import argparse
import base64
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

# Allow scripts launched as `python tools/<script>.py` to import pipeline.py
# from the repository root in GitHub Actions and local runs.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pipeline

OUTPUT = ROOT / "candidate_issue.md"


def is_animal_story(cfg: dict, cand: dict) -> bool:
    """True if a candidate's title/summary matches any configured animal
    keyword. Deliberately a cheap substring test on lowercased text rather than
    anything clever: it runs over every candidate before the paid model call,
    and Bulgarian inflection means prefix matching ("осинов" catching
    осиновяване / осиновен / осиновиха) beats exact word matching here.

    This only FLAGS a story for the editor model — it never selects or
    publishes anything on its own."""
    keywords = cfg.get("animal_keywords") or []
    if not keywords:
        return False
    haystack = f'{cand.get("title", "")} {cand.get("summary", "")}'.lower()
    return any(k in haystack for k in keywords)


def build_editorial_prompt(
    cfg: dict,
    candidates: list[dict],
    shortlist_size: int,
    recent_headlines: list[str],
) -> str:
    recent_block = ""
    if recent_headlines:
        recent_block = "\nALREADY PUBLISHED — reject the same event/topic:\n" + "\n".join(
            f"- {h}" for h in recent_headlines[:100]
        )

    category_ids = ", ".join(cfg["categories"].keys())
    # Animal stories are pre-flagged so the editor model can see them at a
    # glance. The flag is a hint about topic, NOT an instruction to select —
    # the quality bar in the prompt still applies to every flagged item.
    candidate_lines = "\n".join(
        f'{i}. {"[ANIMAL PRIORITY] " if is_animal_story(cfg, c) else ""}'
        f'[{c["source"]}] {c["title"]} — '
        f'{pipeline.clean_text(c.get("summary", ""), 300)}'
        for i, c in enumerate(candidates)
    )

    return f"""You are the shortlist editor for "{cfg['site_name']}".

Choose up to {shortlist_size} of the BEST genuinely positive stories from the
candidates below. This is only an editorial shortlist: DO NOT write articles.

Prefer:
- concrete positive outcomes;
- Bulgarian/local relevance;
- strong human, community, nature, science, culture or sports achievements;
- stories with enough substance to become a useful article;
- variety across topics instead of many nearly identical stories.

ANIMAL STORIES — GIVE THESE HIGH PRIORITY:
Readers respond strongly to animal stories, and they are currently
under-represented. Give clear preference to stories about animals that were
rescued, treated, adopted, rehomed or released back into the wild; the
volunteers, vets, shelters and organisations who do that work; and adoption
drives or exhibitions where a reader can concretely help a named animal.
Candidates already flagged [ANIMAL PRIORITY] are topic matches — judge each on
merit, then favour it over a weaker non-animal story.
If at least 3 good animal stories are available, include up to 5 of them.

BUT — the quality bar does not drop for animals:
- do NOT shortlist every lost-pet notice, missing-animal appeal or urgent
  donation plea;
- an animal story still needs a verifiable positive OUTCOME, a strong human
  story, or a concrete adoption opportunity;
- reject animal cruelty, death, poisoning or rescue-failure stories even when
  the organisation's response was admirable — the outcome is what matters.
A shortlist full of appeals for help is a failure, not a success.

Reject:
- war, crime, accidents, deaths, scandals, party politics, elections;
- vague PR announcements with no concrete positive outcome;
- negative stories merely framed optimistically;
- duplicates of an already published event.

For each chosen story return:
- candidate: original candidate number;
- score: integer 1-10 for how strong it is for Добро Дело;
- category: exactly one of: {category_ids}
- why: a concise Bulgarian reason, maximum 12 words.

Order the shortlist BEST FIRST.
{recent_block}

Respond ONLY with a JSON array:
[
  {{"candidate": 3, "score": 9, "category": "priroda", "why": "Конкретен природозащитен резултат с местно значение"}}
]

CANDIDATES
{candidate_lines}
"""


def category_label(cfg: dict, category_id: str) -> str:
    info = cfg["categories"].get(category_id) or {}
    emoji = info.get("emoji", "")
    label = info.get("label", category_id or "Друго")
    return f"{emoji} {label}".strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shortlist-size", type=int, default=15)
    args = ap.parse_args()

    shortlist_size = max(1, min(args.shortlist_size, 30))

    cfg = pipeline.load_config()
    seen = pipeline.load_seen()
    seen_ids = pipeline.all_seen_ids(seen)

    print(f"[shortlist] collecting recent unseen candidates…")
    candidates = pipeline.collect_candidates(cfg, seen_ids)

    # Cheap integrity filter before paying for the selection call.
    if hasattr(pipeline, "validate_candidate_source"):
        clean_candidates = []
        for cand in candidates:
            ok, reason = pipeline.validate_candidate_source(cand)
            if ok:
                clean_candidates.append(cand)
            else:
                print(f"  [drop · source mismatch] {cand.get('title','')[:60]} — {reason}")
        candidates = clean_candidates

    if not candidates:
        OUTPUT.write_text(
            "# Няма достатъчно нови кандидати днес\n\n"
            "Pipeline-ът не намери нови непубликувани истории в текущия прозорец.\n",
            encoding="utf-8",
        )
        print("[shortlist] no candidates.")
        return 0

    recent = pipeline.load_recent_headlines(days=14, limit=120)
    prompt = build_editorial_prompt(cfg, candidates, shortlist_size, recent)

    print(f"[shortlist] asking Claude for up to {shortlist_size} options…")
    raw = pipeline.call_claude(
        cfg,
        prompt,
        max_tokens_override=2500,
        hard_fail=True,
    )
    picks = pipeline.parse_selection(raw)

    selected = []
    used_candidate_indexes = set()

    for pick in picks:
        try:
            original_index = int(pick["candidate"])
            cand = candidates[original_index]
        except (KeyError, TypeError, ValueError, IndexError):
            continue

        if original_index in used_candidate_indexes:
            continue
        used_candidate_indexes.add(original_index)

        try:
            score = int(pick.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        score = max(1, min(score or 1, 10))

        category = str(pick.get("category", "")).strip()
        if category not in cfg["categories"]:
            category = ""

        selected.append(
            {
                "candidate": cand,
                "score": score,
                "category": category,
                "why": str(pick.get("why", "")).strip()[:180],
            }
        )

        if len(selected) >= shortlist_size:
            break

    if not selected:
        OUTPUT.write_text(
            "# Няма достатъчно силни добри новини днес\n\n"
            "Claude не избра нито един кандидат като достатъчно добър за редакционен преглед.\n",
            encoding="utf-8",
        )
        print("[shortlist] model selected nothing.")
        return 0

    queue = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "site": cfg.get("site_name", "Добро Дело"),
        "items": selected,
    }

    encoded = base64.urlsafe_b64encode(
        json.dumps(queue, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")

    lines = [
        "# 📰 Кандидати за Добро Дело",
        "",
        f"Подбрах **{len(selected)}** възможни истории. Тук няма написани статии и няма генерирани картинки — това е само евтиният редакционен подбор.",
        "",
        "Избери колкото искаш, като оставиш **един коментар**:",
        "",
        "```text",
        "/publish 1 3 6 8",
        "```",
        "",
        "Може да избереш **1, 2, 5, 10 или колкото прецениш**. Няма фиксиран лимит.",
        "",
        "Ако днес не искаш нито една:",
        "",
        "```text",
        "/publish none",
        "```",
        "",
        "Ако някоя история не ти харесва и **не искаш да я виждаш повече**:",
        "",
        "```text",
        "/dismiss 3",
        "```",
        "",
        "`/dismiss` записва отказа **веднага** и списъкът остава отворен. "
        "Ползвай го, когато една и съща история продължава да се появява — "
        "затварянето на issue-то само по себе си не записва отказ.",
        "",
        "---",
        "",
    ]

    for human_index, item in enumerate(selected, start=1):
        cand = item["candidate"]
        category = category_label(cfg, item["category"])
        title = cand.get("title", "Без заглавие")
        source = cand.get("source", "Неизвестен източник")
        url = cand.get("link", "")
        why = item["why"] or "Силен кандидат за положителна новина"
        score = item["score"]

        lines.append(f"## {human_index}. {title}")
        lines.append("")
        lines.append(f"**Оценка:** {score}/10 · **Категория:** {category}")
        if url:
            lines.append(f"**Източник:** [{source}]({url})")
        else:
            lines.append(f"**Източник:** {source}")
        lines.append(f"**Защо е тук:** {why}")
        lines.append("")

    lines.extend(
        [
            "---",
            "",
            "След `/publish ...` системата ще напише **само избраните** истории, "
            "ще генерира **само техните** изображения и ще отвори нормалния review PR.",
            "",
            f"<!-- DOBRODELO_QUEUE_B64:{encoded} -->",
            "",
        ]
    )

    OUTPUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"[shortlist] wrote {OUTPUT.name} with {len(selected)} options.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

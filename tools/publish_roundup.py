#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pipeline

CONTENT = ROOT / "content" / "articles"
RESULT = ROOT / "roundup_result.json"
PR_DESCRIPTION = ROOT / "pr_description.md"


def decode_queue(issue_body: str) -> dict:
    match = re.search(
        r"<!--\s*DOBRODELO_ROUNDUP_QUEUE_B64:([A-Za-z0-9_\-=]+)\s*-->",
        issue_body,
    )
    if not match:
        raise ValueError(
            "This issue was created by an older weekly workflow and has no "
            "machine-readable roundup queue. Run Weekly roundup brief again."
        )

    raw = base64.urlsafe_b64decode(match.group(1).encode("ascii"))
    queue = json.loads(raw.decode("utf-8"))

    if queue.get("version") != 1 or not isinstance(queue.get("options"), list):
        raise ValueError("Malformed or unsupported roundup queue.")

    return queue


def parse_command(command: str, option_count: int) -> list[int]:
    match = re.fullmatch(
        r"/roundup\s+([0-9,\s]+)\s*",
        (command or "").strip(),
        flags=re.IGNORECASE,
    )
    if not match:
        raise ValueError("Use `/roundup 1` or `/roundup 1 3 5`.")

    selected = []
    seen = set()

    for raw in re.findall(r"\d+", match.group(1)):
        number = int(raw)
        if number < 1 or number > option_count:
            raise ValueError(
                f"Roundup option {number} does not exist. Valid range: 1-{option_count}."
            )
        if number not in seen:
            selected.append(number)
            seen.add(number)

    if not selected:
        raise ValueError("No roundup option was selected.")

    return selected


def load_article(slug: str) -> dict | None:
    for path in CONTENT.rglob("*.json"):
        try:
            article = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if article.get("slug") == slug:
            return article
    return None


def build_prompt(cfg: dict, option: dict, source_articles: list[dict]) -> str:
    blocks = []

    for index, article in enumerate(source_articles, 1):
        base = cfg["base_url"].rstrip("/") + cfg.get("base_path", "").rstrip("/")
        url = f'{base}/{cfg["article_prefix"]}/{article["slug"]}/'
        facts = "\n".join(f"- {fact}" for fact in (article.get("quick_facts") or []))
        blocks.append(
            f"""SOURCE ARTICLE {index}
Title: {article.get("headline", "")}
URL: {url}
Summary: {article.get("summary_short", "")}
Key facts:
{facts or "- No separate quick facts stored"}
Article text:
{article.get("body", "")}
"""
        )

    return f"""Ти си редактор на „Добро Дело“.

Напиши ОРИГИНАЛНА обзорна статия на български със заглавие около идеята:

{option["title"]}

Период: {option["week_start"]} – {option["week_end"]}

Използвай САМО предоставените вътрешни статии. Не добавяй външни факти,
имена, числа или заключения, които не са подкрепени от тях.

Задължителна структура:
- Кратко въведение от 80–130 думи, което обединява седмицата без общ AI filler.
- Отделна секция за ВСЯКА предоставена история.
- Всяка секция има естествено подзаглавие и 70–130 думи.
- Във всяка секция сложи точно един вътрешен Markdown линк към пълната новина,
  използвайки предоставения URL.
- Не копирай цели изречения от изходните статии. Синтезирай фактите.
- Завърши с кратък естествен финал и покана хората да изпращат добри новини.
- Не наричай текста „AI обзор“ и не обяснявай процеса.
- Не използвай думата „сензация“ и не преувеличавай.
- Общата дължина трябва да следва естествено броя истории; не пълни текст.

Отговори ТОЧНО в този формат:

===HEADLINE===
<естествено заглавие, което запазва реалния брой истории>
===SLUG===
<кратък slug на латиница>
===META_DESCRIPTION===
<до 155 знака>
===SUMMARY_SHORT===
<до 160 знака>
===BODY===
<готовата обзорна статия в Markdown>
===QUICK_FACTS===
<периодът на обзора>
<реалният брой включени истории>
<основната тема или град>
===IMAGE_QUERY===
<2-4 английски думи за обща реалистична сцена, без текст и числа>
===END===

ИЗХОДНИ СТАТИИ:

{chr(10).join(blocks)}
"""


def save_roundup(cfg: dict, option: dict, written: dict, source_articles: list[dict]) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    base_slug = pipeline.slugify(written.get("slug_hint") or written["headline"])
    signature = "|".join(
        [option["week_start"], option["week_end"]]
        + [article["slug"] for article in source_articles]
    )
    suffix = hashlib.sha1(signature.encode("utf-8")).hexdigest()[:4]
    slug = f"{base_slug}-{suffix}"

    for path in CONTENT.rglob("*.json"):
        try:
            existing = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if existing.get("slug") == slug:
            raise ValueError(f"Roundup '{slug}' already exists.")

    category = option.get("category")
    if category not in cfg["categories"]:
        category = next(iter(cfg["categories"]))

    body = (written.get("body") or "").strip()
    headline = pipeline.clip(written.get("headline", ""), 90)

    if not body or not headline:
        raise ValueError("Claude response had no headline or body.")

    base = cfg["base_url"].rstrip("/") + cfg.get("base_path", "").rstrip("/")
    source_urls = [
        f'{base}/{cfg["article_prefix"]}/{article["slug"]}/'
        for article in source_articles
    ]
    article = {
        "id": f"roundup-{suffix}-{now.strftime('%Y%m%d')}",
        "slug": slug,
        "headline": headline,
        "meta_description": pipeline.clip(written.get("meta_description", ""), 160),
        "summary_short": pipeline.clip(written.get("summary_short", ""), 170),
        "body": body,
        "category": category,
        "tags": [pipeline.clip(tag, 30) for tag in (option.get("tags") or [])[:5]],
        "quick_facts": [
            pipeline.clip(fact, 120)
            for fact in (written.get("quick_facts") or [])[:5]
            if fact
        ],
        "source_name": "Добро Дело — седмичен обзор",
        "source_url": cfg["base_url"].rstrip("/") + cfg.get("base_path", "").rstrip("/") + "/",
        "published": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_published": None,
        "source_fetched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_host": pipeline.normalize_host(cfg["base_url"]),
        "expected_source_host": pipeline.normalize_host(cfg["base_url"]),
        "full_source_extracted": True,
        "sensitive_topic": False,
        "lang": cfg["lang"],
        "content_type": "weekly_roundup",
        "roundup_week_start": option["week_start"],
        "roundup_week_end": option["week_end"],
        "roundup_source_slugs": [article["slug"] for article in source_articles],
        "roundup_source_urls": source_urls,
    }

    photo = pipeline.get_article_photo(cfg, written, slug)
    if photo:
        article.update(photo)

    out_dir = CONTENT / now.strftime("%Y") / now.strftime("%m")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{slug}.json"
    out_path.write_text(
        json.dumps(article, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    base = cfg["base_url"].rstrip("/") + cfg.get("base_path", "").rstrip("/")
    url = f'{base}/{cfg["article_prefix"]}/{slug}/'
    return slug, url


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-body", required=True)
    parser.add_argument("--command", required=True)
    args = parser.parse_args()

    issue_body = Path(args.issue_body).read_text(encoding="utf-8")
    queue = decode_queue(issue_body)
    selected_numbers = parse_command(args.command, len(queue["options"]))
    cfg = pipeline.load_config()

    saved = []
    failed = []

    for number in selected_numbers:
        option = queue["options"][number - 1]
        source_articles = []
        missing = []

        for ref in option["articles"]:
            article = load_article(ref["slug"])
            if article:
                source_articles.append(article)
            else:
                missing.append(ref["slug"])

        if missing:
            failed.append({
                "number": number,
                "title": option["title"],
                "reason": f"missing source articles: {', '.join(missing)}",
            })
            continue

        try:
            prompt = build_prompt(cfg, option, source_articles)
            raw = pipeline.call_claude(
                cfg,
                prompt,
                max_tokens_override=6000,
                hard_fail=False,
            )
            written = pipeline.parse_delimited_article(raw)
            if not written:
                raise ValueError("Claude response could not be parsed.")

            written["category"] = option.get("category", "")
            written["tags"] = option.get("tags") or []
            slug, url = save_roundup(cfg, option, written, source_articles)
            saved.append({
                "number": number,
                "title": written["headline"],
                "slug": slug,
                "url": url,
                "source_count": len(source_articles),
            })
        except Exception as exc:
            failed.append({
                "number": number,
                "title": option["title"],
                "reason": str(exc),
            })

    lines = [
        f"# {len(saved)} weekly roundup article(s) ready for review",
        "",
        "> Nothing below is live until this PR is merged.",
        "",
    ]

    for index, item in enumerate(saved, 1):
        lines.extend([
            f"## {index}. [Weekly roundup] {item['title']}",
            "",
            f"**Included stories:** {item['source_count']}",
            f"**Preview URL after merge:** {item['url']}",
            "",
            f"**Reject this article:** `/reject {item['slug']}`",
            "",
            "---",
            "",
        ])

    PR_DESCRIPTION.write_text("\n".join(lines), encoding="utf-8")
    RESULT.write_text(
        json.dumps(
            {
                "selected_count": len(selected_numbers),
                "saved_count": len(saved),
                "saved": saved,
                "failed": failed,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"Saved {len(saved)} roundup article(s); {len(failed)} failed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pipeline

CONTENT = ROOT / "content" / "articles"
OUTPUT = ROOT / "weekly_roundup.md"
SOFIA = ZoneInfo("Europe/Sofia")


def most_recent_saturday(today: date) -> date:
    days_back = (today.weekday() - 5) % 7
    return today - timedelta(days=days_back)


def load_articles(start_local: datetime, end_local: datetime) -> list[dict]:
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    out = []

    for path in CONTENT.rglob("*.json"):
        try:
            article = json.loads(path.read_text(encoding="utf-8-sig"))
            published = datetime.strptime(
                article["published"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
        except Exception:
            continue

        if start_utc <= published <= end_utc:
            article["_dt"] = published
            out.append(article)

    out.sort(key=lambda article: article["_dt"])
    return out


def article_url(cfg: dict, article: dict) -> str:
    base = cfg["base_url"].rstrip("/") + cfg.get("base_path", "").rstrip("/")
    return f'{base}/{cfg["article_prefix"]}/{article["slug"]}/'


def article_ref(cfg: dict, article: dict) -> dict:
    return {
        "slug": article["slug"],
        "headline": article.get("headline", ""),
        "summary_short": article.get("summary_short", ""),
        "category": article.get("category", ""),
        "tags": article.get("tags") or [],
        "url": article_url(cfg, article),
        "published": article.get("published", ""),
    }


def diverse_week_selection(articles: list[dict], limit: int = 10) -> list[dict]:
    """Prefer category variety before filling remaining slots by recency."""
    by_category: dict[str, list[dict]] = defaultdict(list)
    for article in reversed(articles):
        by_category[article.get("category", "other")].append(article)

    selected = []
    used = set()

    for group in by_category.values():
        if group:
            article = group[0]
            selected.append(article)
            used.add(article["slug"])
            if len(selected) >= limit:
                return selected

    for article in reversed(articles):
        if article["slug"] not in used:
            selected.append(article)
            used.add(article["slug"])
            if len(selected) >= limit:
                break

    return selected


def build_options(cfg: dict, articles: list[dict], week_start: date, saturday: date) -> list[dict]:
    options = []

    broad = diverse_week_selection(articles, min(10, len(articles)))
    if len(broad) >= 3:
        options.append({
            "type": "week",
            "title": f"{len(broad)} добри новини от България тази седмица",
            "description": "Разнообразен общ обзор на най-силните теми от седмицата.",
            "category": "obshtestvo" if "obshtestvo" in cfg["categories"] else next(iter(cfg["categories"])),
            "tags": ["седмичен обзор", "добри новини", "България"],
            "week_start": week_start.isoformat(),
            "week_end": saturday.isoformat(),
            "articles": [article_ref(cfg, article) for article in broad],
        })

    known_cities = cfg.get("known_cities", {})
    aliases = cfg.get("tag_aliases", {})
    city_groups: dict[str, list[dict]] = defaultdict(list)

    for article in articles:
        normalized = set()
        for tag in article.get("tags") or []:
            slug = pipeline.slugify(str(tag))
            normalized.add(aliases.get(slug, slug))

        for city_slug in known_cities:
            if city_slug in normalized:
                city_groups[city_slug].append(article)

    ranked_cities = sorted(
        city_groups.items(),
        key=lambda item: (-len(item[1]), known_cities.get(item[0], item[0])),
    )

    for city_slug, grouped in ranked_cities:
        if len(grouped) < 2:
            continue
        city_name = known_cities.get(city_slug, city_slug)
        options.append({
            "type": "city",
            "title": f"{len(grouped)} добри неща, които се случиха в {city_name} тази седмица",
            "description": f"Местен обзор с всички {len(grouped)} публикувани истории за {city_name}.",
            "category": "obshtestvo" if "obshtestvo" in cfg["categories"] else next(iter(cfg["categories"])),
            "tags": [city_name, "седмичен обзор", "добри новини"],
            "city": city_name,
            "week_start": week_start.isoformat(),
            "week_end": saturday.isoformat(),
            "articles": [article_ref(cfg, article) for article in grouped],
        })

    category_groups: dict[str, list[dict]] = defaultdict(list)
    for article in articles:
        category_groups[article.get("category", "")].append(article)

    ranked_categories = sorted(
        category_groups.items(),
        key=lambda item: -len(item[1]),
    )

    for category_id, grouped in ranked_categories:
        if category_id not in cfg["categories"] or len(grouped) < 3:
            continue
        category_info = cfg["categories"][category_id]
        label = category_info.get("label", category_id)
        options.append({
            "type": "category",
            "title": f"{len(grouped)} добри новини от света на {label.lower()} тази седмица",
            "description": f"Тематичен обзор на седмичните публикации в категория „{label}“.",
            "category": category_id,
            "tags": [label, "седмичен обзор", "добри новини"],
            "category_label": label,
            "week_start": week_start.isoformat(),
            "week_end": saturday.isoformat(),
            "articles": [article_ref(cfg, article) for article in grouped[:12]],
        })

    return options[:15]


def build_prompt(
    cfg: dict,
    articles: list[dict],
    week_start: date,
    saturday: date,
    options: list[dict],
) -> str:
    rows = []
    for index, article in enumerate(articles, 1):
        rows.append(
            f'{index}. {article.get("headline", "")}\n'
            f'   Категория: {article.get("category", "")}\n'
            f'   Тагове: {", ".join(article.get("tags") or [])}\n'
            f'   Резюме: {article.get("summary_short", "")}\n'
            f'   URL: {article_url(cfg, article)}'
        )

    option_rows = "\n".join(
        f'{index}. {option["title"]} — {option["description"]}'
        for index, option in enumerate(options, 1)
    )

    return f"""Ти си редакционен помощник на „Добро Дело“.

Направи редакционен бриф за периода {week_start.isoformat()} – {saturday.isoformat()}.
Използвай САМО информацията по-долу. Не добавяй външни факти.

Това НЕ е готова статия. След брифа човекът ще може да генерира готов обзор
чрез команда `/roundup <номер>`.

Върни Markdown със следните секции:

# Седмичен редакционен бриф

## 1. Седмицата в 5 изречения
Обобщи основните позитивни теми.

## 2. Най-силните истории
До 10 истории с по едно изречение защо са силни и URL.

## 3. Какви обзорни статии могат да бъдат генерирани
Опиши накратко предложенията от точния номериран списък по-долу.
Запази същите номера и заглавия. Не добавяй нови номера.

ПРЕДЛОЖЕНИЯ:
{option_rows}

## 4. Идеи за човешки follow-up
До 5 идеи за интервю, коментар или оригинален follow-up.

## 5. Всички публикации от седмицата
Компактен списък с всички заглавия и URL.

ПУБЛИКУВАНИ СТАТИИ:
{chr(10).join(rows)}
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--week-ending", default="")
    args = parser.parse_args()

    if args.week_ending:
        saturday = datetime.strptime(args.week_ending, "%Y-%m-%d").date()
        if saturday.weekday() != 5:
            raise SystemExit("--week-ending must be a Saturday (YYYY-MM-DD).")
    else:
        saturday = most_recent_saturday(datetime.now(SOFIA).date())

    week_start = saturday - timedelta(days=6)
    start_local = datetime.combine(week_start, time.min, SOFIA)
    end_local = datetime.combine(saturday, time.max, SOFIA)

    cfg = pipeline.load_config()
    articles = load_articles(start_local, end_local)

    if not articles:
        OUTPUT.write_text(
            f"# Седмичен редакционен бриф\n\n"
            f"Няма намерени статии за {week_start} – {saturday}.\n",
            encoding="utf-8",
        )
        return 0

    options = build_options(cfg, articles, week_start, saturday)
    prompt = build_prompt(cfg, articles, week_start, saturday, options)
    brief = pipeline.call_claude(
        cfg,
        prompt,
        max_tokens_override=3500,
        hard_fail=True,
    ).strip()

    queue = {
        "version": 1,
        "week_start": week_start.isoformat(),
        "week_end": saturday.isoformat(),
        "options": options,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(queue, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")

    command_help = [
        "",
        "---",
        "",
        "## Генериране на готова обзорна статия",
        "",
        "Остави коментар с номера на желаното предложение:",
        "",
        "```text",
        "/roundup 1",
        "```",
        "",
        "Можеш да генерираш и няколко отделни обзорни статии в един PR:",
        "",
        "```text",
        "/roundup 1 3 5",
        "```",
        "",
        "### Точни налични предложения",
        "",
    ]

    for index, option in enumerate(options, 1):
        command_help.append(f"**{index}. {option['title']}**")
        command_help.append("")
        command_help.append(
            f"{option['description']} Използва {len(option['articles'])} съществуващи публикации."
        )
        command_help.append("")

    command_help.extend([
        "Системата ще напише само избраните обзори, ще добави вътрешни линкове към",
        "всяка включена новина, ще генерира изображение и ще отвори review PR.",
        "",
        f"<!-- DOBRODELO_ROUNDUP_QUEUE_B64:{encoded} -->",
        "",
    ])

    header = (
        f"> Период: **{week_start.strftime('%d.%m.%Y')} – "
        f"{saturday.strftime('%d.%m.%Y')}**  \n"
        f"> Публикувани материали: **{len(articles)}**  \n"
        "> Това е редакционен бриф. Нищо не се публикува автоматично.\n\n"
    )

    OUTPUT.write_text(
        header + brief + "\n" + "\n".join(command_help),
        encoding="utf-8",
    )
    print(f"Wrote {OUTPUT} with {len(options)} roundup option(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pipeline

ROOT = Path(__file__).resolve().parents[1]
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
            a = json.loads(path.read_text(encoding="utf-8-sig"))
            dt = datetime.strptime(a["published"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except Exception:
            continue

        if start_utc <= dt <= end_utc:
            a["_dt"] = dt
            out.append(a)

    out.sort(key=lambda a: a["_dt"])
    return out


def build_prompt(cfg: dict, articles: list[dict], monday: date, saturday: date) -> str:
    rows = []
    base = cfg["base_url"].rstrip("/") + cfg.get("base_path", "").rstrip("/")
    prefix = cfg["article_prefix"]

    for i, a in enumerate(articles, 1):
        url = f'{base}/{prefix}/{a["slug"]}/'
        rows.append(
            f'{i}. {a.get("headline","")}\n'
            f'   Категория: {a.get("category","")}\n'
            f'   Тагове: {", ".join(a.get("tags") or [])}\n'
            f'   Резюме: {a.get("summary_short","")}\n'
            f'   URL: {url}'
        )

    joined = "\n".join(rows)

    return f"""Ти си редакционен помощник на "Добро Дело".

Направи РЕДАКЦИОНЕН БРИФ за седмичен обзор за периода {monday.isoformat()} – {saturday.isoformat()}.
Използвай САМО информацията в списъка по-долу. Не добавяй външни факти и не измисляй подробности.

Това НЕ е готова статия. Целта е Димитър да може да избере идея и сам да направи оригинален обзор.

Върни Markdown със следните секции:

# Седмичен редакционен бриф

## 1. Седмицата в 5 изречения
Кратък обзор на основните позитивни теми.

## 2. Най-силните истории
До 10 истории. За всяка: заглавие + едно изречение защо е подходяща за обзор + URL.

## 3. Възможни обзори по градове
Покажи само градове, за които има ПОНЕ 2 релевантни истории според таговете/заглавията.
За всеки град изброи историите и предложи заглавие от типа:
"5 добри неща, които се случиха във Варна тази седмица".
Не твърди число, по-голямо от реалния брой налични истории.

## 4. Възможни тематични обзори
Предложи 3-6 теми (природа, спорт, култура, общество, наука и др.) само ако има достатъчно материал.
Към всяка тема посочи кои номера от списъка влизат.

## 5. Пет идеи за оригинален материал
Това са идеи за човешки написан roundup/follow-up, НЕ готов текст.
Всяка идея да има:
- работно заглавие;
- кои истории да използва;
- какъв оригинален ъгъл може да добави редакторът;
- на кого би си струвало да пише за кратък коментар/интервю, ако е приложимо.

## 6. Всички публикации от седмицата
Компактен списък с всички заглавия и URL.

ПУБЛИКУВАНИ СТАТИИ:
{joined}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--week-ending", default="")
    args = ap.parse_args()

    if args.week_ending:
        saturday = datetime.strptime(args.week_ending, "%Y-%m-%d").date()
        if saturday.weekday() != 5:
            raise SystemExit("--week-ending must be a Saturday (YYYY-MM-DD).")
    else:
        saturday = most_recent_saturday(datetime.now(SOFIA).date())

    monday = saturday - timedelta(days=5)
    start_local = datetime.combine(monday, time.min, SOFIA)
    end_local = datetime.combine(saturday, time.max, SOFIA)

    cfg = pipeline.load_config()
    articles = load_articles(start_local, end_local)

    if not articles:
        OUTPUT.write_text(
            f"# Седмичен редакционен бриф\n\nНяма намерени статии за {monday} – {saturday}.\n",
            encoding="utf-8",
        )
        print("No articles found for selected week.")
        return 0

    prompt = build_prompt(cfg, articles, monday, saturday)
    raw = pipeline.call_claude(
        cfg,
        prompt,
        max_tokens_override=3500,
        hard_fail=True,
    ).strip()

    header = (
        f"> Период: **{monday.strftime('%d.%m.%Y')} – {saturday.strftime('%d.%m.%Y')}**  \n"
        f"> Публикувани материали: **{len(articles)}**  \n"
        "> Това е редакционен бриф, не автоматично публикувана статия.\n\n"
    )
    OUTPUT.write_text(header + raw + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT} from {len(articles)} article(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

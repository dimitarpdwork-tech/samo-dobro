#!/usr/bin/env python3
"""Editorial calendar reminder.

Most good news is not discovered — it is scheduled. Exam results, migrations,
award ceremonies and charity drives happen on dates known a year in advance,
so the only thing standing between the site and the story is remembering to
prepare it. This reads content/editorial-calendar.json, finds what lands
inside its lead time, and writes a markdown body for a GitHub issue.

It deliberately does NOT create the issue itself — the workflow does that, so
the same idempotency check used by the daily shortlist applies here too.

Usage:
    python tools/calendar_reminder.py [--on YYYY-MM-DD] [--out FILE]

Exit codes:
    0  something is due, body written
    1  nothing due (the workflow skips issue creation)
"""

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CALENDAR = ROOT / "content" / "editorial-calendar.json"

CATEGORY_LABELS = {
    "obshtestvo": "Общество",
    "priroda": "Природа",
    "zdrave": "Здраве",
    "nauka": "Наука",
    "kultura": "Култура",
    "sport": "Спорт",
    "ikonomika": "Икономика",
}


def _event_date(when: dict, year: int) -> date:
    """The date an entry is anchored to. Windows anchor on their start, so the
    reminder lands before the window opens rather than halfway through it."""
    if when.get("type") == "window":
        month, day = when["from"]
    else:
        month, day = when["month"], when["day"]
    # 29 Feb in a non-leap year would raise; clamp instead of crashing.
    try:
        return date(year, month, day)
    except ValueError:
        return date(year, month, 28)


def _window_label(when: dict, year: int) -> str:
    if when.get("type") != "window":
        return _event_date(when, year).strftime("%d.%m")
    fm, fd = when["from"]
    tm, td = when["to"]
    return f"{fd:02d}.{fm:02d} – {td:02d}.{tm:02d}"


def due_events(cal: dict, today: date) -> list[dict]:
    """Events whose anchor date is exactly lead_days away.

    Exactly, not 'within' — the workflow runs daily, so a range would re-fire
    every day for a week. One reminder per event per year is the point.
    """
    default_lead = int(cal.get("default_lead_days", 7))
    out = []
    for ev in cal.get("events", []):
        lead = int(ev.get("lead_days", default_lead))
        for year in (today.year, today.year + 1):  # covers a December -> January wrap
            when = _event_date(ev["when"], year)
            if when - timedelta(days=lead) == today:
                out.append({**ev, "_date": when, "_year": year, "_lead": lead})
                break
    return out


def render(events: list[dict], today: date) -> str:
    lines = [
        "Предстоящи събития от редакционния календар. Това са новини с "
        "известна дата — те не се откриват, а се подготвят.",
        "",
    ]
    for ev in events:
        cat = CATEGORY_LABELS.get(ev.get("category", ""), ev.get("category", ""))
        label = _window_label(ev["when"], ev["_year"])
        kind = "прозорец" if ev["when"].get("type") == "window" else "дата"
        lines.append(f"## {ev['title']}")
        lines.append("")
        lines.append(f"- **{kind.capitalize()}:** {label} ({ev['_year']})")
        lines.append(f"- **Рубрика:** {cat}")
        lines.append(f"- **Остават:** {ev['_lead']} дни")
        if ev.get("note"):
            lines.append(f"- **Бележка:** {ev['note']}")
        if ev.get("look_at"):
            lines.append(f"- **Къде да гледам:** {', '.join(ev['look_at'])}")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "Прозорците се местят всяка година. Където бележката казва ПРОВЕРИ, "
        "датата в календара е ориентир, не факт — потвърди я от институцията, "
        "преди да планираш публикация."
    )
    lines.append("")
    lines.append(
        f"_Генерирано на {today.strftime('%d.%m.%Y')} от "
        "`content/editorial-calendar.json`._"
    )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--on", default=None, help="pretend today is this date (YYYY-MM-DD)")
    ap.add_argument("--out", default="calendar_issue.md")
    ap.add_argument("--list-all", action="store_true",
                    help="print every event with its reminder date and exit")
    args = ap.parse_args()

    cal = json.loads(CALENDAR.read_text(encoding="utf-8"))
    today = (
        datetime.strptime(args.on, "%Y-%m-%d").date() if args.on else date.today()
    )

    if args.list_all:
        default_lead = int(cal.get("default_lead_days", 7))
        rows = []
        for ev in cal["events"]:
            lead = int(ev.get("lead_days", default_lead))
            d = _event_date(ev["when"], today.year)
            rows.append((d - timedelta(days=lead), d, lead, ev["title"]))
        for remind, when, lead, title in sorted(rows):
            print(f"{remind:%d.%m}  ->  {when:%d.%m}  (-{lead}d)  {title}")
        return 0

    events = due_events(cal, today)
    if not events:
        print("[calendar] нищо предстоящо днес.")
        return 1

    body = render(events, today)
    Path(args.out).write_text(body, encoding="utf-8")
    titles = ", ".join(e["title"] for e in events)
    print(f"[calendar] {len(events)} предстоящо(и): {titles}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

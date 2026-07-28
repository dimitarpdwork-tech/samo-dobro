#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
CONTENT = ROOT / "content" / "articles"
CONFIG = ROOT / "config.json"
REPORT = ROOT / "backlog_audit.md"

KNOWN_BRANDS = {
    "presstv.bg": "PressTV.bg",
    "vesti.bg": "Vesti.bg",
    "actualno.com": "Actualno.com",
    "bnr.bg": "БНР",
    "bta.bg": "БТА",
    "nova.bg": "NOVA",
    "btvnovinite.bg": "bTV Новините",
    "dnevnik.bg": "Дневник",
    "capital.bg": "Капитал",
    "offnews.bg": "OFFNews",
    "mediapool.bg": "Mediapool",
    "dariknews.bg": "DarikNews.bg",
    "24chasa.bg": "24 часа",
    "trud.bg": "Труд",
}


def host(url: str) -> str:
    try:
        h = (urlparse(url).hostname or "").lower().strip(".")
    except Exception:
        return ""
    return h[4:] if h.startswith("www.") else h


def host_related(a: str, b: str) -> bool:
    return bool(a and b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def publisher_from_host(h: str) -> str:
    if h in KNOWN_BRANDS:
        return KNOWN_BRANDS[h]
    if not h:
        return "Неизвестен източник"
    parts = h.split(".")
    base = parts[-2] if len(parts) >= 2 else parts[0]
    label = re.sub(r"[-_]+", " ", base).strip()
    return f"{label[:1].upper()}{label[1:]}.{parts[-1]}" if label else h


def feed_host_map(cfg: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for feed in cfg.get("feeds", []):
        h = host(feed.get("url", ""))
        name = (feed.get("name") or "").strip()
        if h and name:
            out.setdefault(h, []).append(name)
    return out


def best_source_name(article: dict, mapping: dict[str, list[str]]) -> tuple[str | None, str]:
    actual = host(article.get("source_url", ""))
    current = (article.get("source_name") or "").strip()
    expected_saved = (article.get("expected_source_host") or "").strip().lower()

    if not actual:
        return None, "missing/invalid source_url"

    if expected_saved and not host_related(expected_saved, actual):
        return publisher_from_host(actual), (
            f"saved expected host {expected_saved} does not match actual {actual}"
        )

    matching_names = []
    for configured_host, names in mapping.items():
        if host_related(configured_host, actual):
            matching_names.extend(names)
    matching_names = list(dict.fromkeys(matching_names))

    if matching_names:
        if current in matching_names:
            return None, ""
        if current.startswith("Община "):
            return matching_names[0], (
                f"municipality label '{current}' conflicts with URL host {actual}"
            )
        if len(matching_names) == 1 and current and current != matching_names[0]:
            return matching_names[0], (
                f"configured source for {actual} is '{matching_names[0]}', not '{current}'"
            )

    if current.startswith("Община "):
        return publisher_from_host(actual), (
            f"municipality label '{current}' has no matching configured host for {actual}"
        )

    return None, ""


def suspicious_city_tags(article: dict, cfg: dict) -> list[str]:
    known = cfg.get("known_cities", {})
    tags = article.get("tags") or []
    if not isinstance(tags, list):
        return []

    text = " ".join([
        str(article.get("headline", "")),
        str(article.get("summary_short", "")),
        str(article.get("body", "")),
    ]).lower()

    suspicious = []
    for slug, display in known.items():
        display_l = display.lower()
        stem = display_l[: max(4, min(len(display_l), 6))]
        for tag in tags:
            tag_l = str(tag).lower()
            if tag_l in {slug.lower(), display_l} or tag_l.replace(" ", "-") == slug.lower():
                if stem not in text:
                    suspicious.append(f"{tag} ({display})")
                break
    return suspicious


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true")
    args = ap.parse_args()

    cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    mapping = feed_host_map(cfg)

    source_findings = []
    city_findings = []
    changed_files = 0

    for path in sorted(CONTENT.rglob("*.json")):
        try:
            article = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            source_findings.append((path, "BROKEN JSON", str(exc), "", ""))
            continue

        actual = host(article.get("source_url", ""))
        old_name = article.get("source_name", "")
        new_name, reason = best_source_name(article, mapping)

        changed = False
        if actual and article.get("source_host") != actual:
            article["source_host"] = actual
            changed = True

        if new_name and new_name != old_name:
            source_findings.append((path, reason, old_name, new_name, actual))
            if args.fix:
                article["source_name"] = new_name
                changed = True

        for warning in suspicious_city_tags(article, cfg):
            city_findings.append((path, warning, article.get("headline", "")))

        if changed and args.fix:
            path.write_text(
                json.dumps(article, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            changed_files += 1

    lines = [
        "# Backlog integrity audit",
        "",
        f"- Mode: **{'FIX' if args.fix else 'AUDIT ONLY'}**",
        f"- Source attribution findings: **{len(source_findings)}**",
        f"- Suspicious city-tag warnings: **{len(city_findings)}**",
        f"- Files changed: **{changed_files}**",
        "",
        "## Source attribution",
        "",
    ]

    if source_findings:
        lines += ["| File | Reason | Old | Suggested/new | Host |", "|---|---|---|---|---|"]
        for path, reason, old, new, actual in source_findings:
            rel = path.relative_to(ROOT)
            lines.append(
                f"| `{rel}` | {reason} | {old or '—'} | {new or '—'} | `{actual or '—'}` |"
            )
    else:
        lines.append("No clear source-attribution problems detected.")

    lines += ["", "## Suspicious city tags (manual review only)", ""]
    if city_findings:
        lines += ["| File | City tag | Headline |", "|---|---|---|"]
        for path, warning, headline in city_findings:
            lines.append(f"| `{path.relative_to(ROOT)}` | {warning} | {headline} |")
    else:
        lines.append("No suspicious city tags detected.")

    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Source findings: {len(source_findings)}")
    print(f"City-tag warnings: {len(city_findings)}")
    print(f"Changed files: {changed_files}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

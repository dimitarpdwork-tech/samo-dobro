#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTENT_DIR = ROOT / "content" / "articles"
ASSET_DIR = (ROOT / "assets" / "articles").resolve()


def normalize_slug(raw: str) -> str:
    value = (raw or "").strip().strip("/")
    if "/" in value:
        value = value.rstrip("/").split("/")[-1]
    if not value:
        raise ValueError("Empty slug.")
    if not all(ch.islower() or ch.isdigit() or ch == "-" for ch in value):
        raise ValueError(
            "Invalid slug. Use only lowercase latin letters, numbers and hyphens."
        )
    return value


def local_image_path(article: dict) -> Path | None:
    image_path = article.get("image_path")
    if not isinstance(image_path, str):
        return None
    if not image_path.startswith("/assets/articles/"):
        return None

    candidate = (ROOT / image_path.lstrip("/")).resolve()
    try:
        candidate.relative_to(ASSET_DIR)
    except ValueError:
        return None
    return candidate


def find_article(slug: str) -> tuple[Path, dict] | None:
    for path in CONTENT_DIR.rglob("*.json"):
        try:
            with path.open(encoding="utf-8-sig") as f:
                article = json.load(f)
        except Exception:
            continue
        if article.get("slug") == slug:
            return path, article
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("slug")
    args = parser.parse_args()

    try:
        slug = normalize_slug(args.slug)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    match = find_article(slug)
    if not match:
        print(
            f"ERROR: No article with slug '{slug}' exists on this review branch.",
            file=sys.stderr,
        )
        return 3

    article_path, article = match
    title = article.get("headline") or slug
    image_path = local_image_path(article)

    article_path.unlink()

    removed_image = False
    if image_path and image_path.exists():
        image_path.unlink()
        removed_image = True

    print(f"Rejected: {title}")
    print(f"Removed article: {article_path.relative_to(ROOT)}")
    if removed_image:
        print(f"Removed image: {image_path.relative_to(ROOT)}")
    else:
        print("No local generated image needed removal.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

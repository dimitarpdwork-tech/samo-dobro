#!/usr/bin/env python3
"""
Turn a published article into a vertical video ready to upload as a Reel.

Output is 1080x1920 MP4, ~12 seconds, with a slow zoom on the article's own
image, the headline over it, and the site's branding. No narration and no
stock footage — this cannot compete with a person talking to camera, but it
costs nothing per video and Reels reach for new accounts is currently the
highest of anything on Meta.

    python tools/make_reel.py --slug my-article-1a2b
    python tools/make_reel.py --latest 3      # the 3 most recent articles
    python tools/make_reel.py --slug x --duration 15

Output goes to reels/<slug>.mp4.

WHY THE TEXT IS BURNED IN WITH PILLOW RATHER THAN ffmpeg's drawtext
-------------------------------------------------------------------
drawtext needs careful escaping and cannot wrap lines, and Bulgarian headlines
are long enough that wrapping is the whole job. Pillow measures each word and
wraps to the real pixel width, so nothing overflows the frame regardless of
headline length.

The font is the site's own Sofia Sans, converted from the .woff2 in assets/ at
runtime via fontTools, so a Reel looks like the site rather than like a generic
template. DejaVu Sans is the fallback if that conversion fails — it also covers
Cyrillic, which most default fonts do not.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import pipeline
CONTENT = ROOT / "content" / "articles"
ASSETS = ROOT / "assets"
OUT_DIR = ROOT / "reels"

W, H = 1080, 1920          # Reels / TikTok / Shorts native size
SAFE_BOTTOM = 420          # Instagram overlays its UI here — keep text clear of it


def load_config() -> dict:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))


def get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Site font if we can convert it, DejaVu otherwise (both cover Cyrillic)."""
    candidates = [
        ASSETS / "sofia-sans-v20-cyrillic_latin-700.woff2",
        ASSETS / "sofia-sans-v20-cyrillic_latin-regular.woff2",
    ]
    for src in candidates:
        if not src.exists():
            continue
        try:
            from fontTools.ttLib import TTFont
            tmp = Path(tempfile.gettempdir()) / (src.stem + ".ttf")
            if not tmp.exists():
                f = TTFont(str(src))
                f.flavor = None
                f.save(str(tmp))
            return ImageFont.truetype(str(tmp), size)
        except Exception:
            continue
    fallback = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
                else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    try:
        return ImageFont.truetype(fallback, size)
    except OSError:
        return ImageFont.load_default()


def wrap(draw, text: str, font, max_width: int) -> list[str]:
    """Greedy wrap measured in real pixels, so long Bulgarian words never
    overflow the frame."""
    words, lines, cur = text.split(), [], ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def hex_rgb(value: str) -> tuple:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def build_frame(cfg: dict, article: dict) -> Image.Image:
    """Compose the still that the video zooms across."""
    colors = cfg["colors"]
    ink, primary = hex_rgb(colors["ink"]), hex_rgb(colors["primary"])

    canvas = Image.new("RGB", (W, H), hex_rgb(colors["bg"]))

    # --- background: the article's own image, cropped to fill ---
    img_rel = (article.get("image_path") or "").lstrip("/")
    src = ROOT / img_rel if img_rel else ASSETS / "og-default.png"
    if not src.exists():
        src = ASSETS / "og-default.png"
    photo = Image.open(src).convert("RGB")

    # cover-fit: scale so both dimensions fill, then centre-crop
    scale = max(W / photo.width, H / photo.height)
    photo = photo.resize((int(photo.width * scale) + 2, int(photo.height * scale) + 2),
                         Image.LANCZOS)
    left = (photo.width - W) // 2
    top = (photo.height - H) // 3      # bias upward; faces sit high in most photos
    canvas.paste(photo.crop((left, top, left + W, top + H)), (0, 0))

    # --- gradient scrim so text stays readable over any image ---
    scrim = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(scrim)
    for y in range(H):
        if y < H * 0.35:
            alpha = int(150 * (1 - y / (H * 0.35)))
        elif y > H * 0.45:
            alpha = int(225 * min(1.0, (y - H * 0.45) / (H * 0.35)))
        else:
            alpha = 0
        sd.line([(0, y), (W, y)], fill=(8, 20, 30, alpha))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), scrim).convert("RGB")

    draw = ImageDraw.Draw(canvas)

    # --- brand, top ---
    brand_font = get_font(52, bold=True)
    draw.text((70, 90), cfg["site_name"], font=brand_font, fill=(255, 255, 255))
    draw.rounded_rectangle([70, 170, 70 + 120, 182], radius=6, fill=primary)

    # --- headline, above Instagram's UI zone ---
    head_font = get_font(78, bold=True)
    lines = wrap(draw, article.get("headline", ""), head_font, W - 140)
    lines = lines[:5]
    line_h = 96
    block_h = len(lines) * line_h
    y = H - SAFE_BOTTOM - block_h
    for line in lines:
        # soft shadow first, for contrast against bright image areas
        draw.text((72, y + 3), line, font=head_font, fill=(0, 0, 0))
        draw.text((70, y), line, font=head_font, fill=(255, 255, 255))
        y += line_h

    # --- call to action, bottom ---
    cta_font = get_font(40)
    domain = cfg["base_url"].replace("https://", "").replace("http://", "").rstrip("/")
    draw.text((70, H - SAFE_BOTTOM + 40), f"➜ {domain}", font=cta_font, fill=primary)

    # --- AI-image disclosure, matching the site's practice ---
    if article.get("image_path"):
        small = get_font(26)
        draw.text((70, H - SAFE_BOTTOM + 100),
                  article.get("image_credit", "AI-generated illustration"),
                  font=small, fill=(210, 220, 228))
    return canvas


def render(frame_path: Path, out_path: Path, duration: int) -> bool:
    """Slow zoom (Ken Burns) via ffmpeg. Static video reads as a dead post;
    even a subtle push keeps a viewer for the second or two that decides
    whether the algorithm shows it to anyone else."""
    fps = 30
    frames = duration * fps
    vf = (
        f"scale={W*2}:{H*2},"
        f"zoompan=z='min(zoom+0.0006,1.12)':d={frames}:x='iw/2-(iw/zoom/2)':"
        f"y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={fps},"
        f"format=yuv420p"
    )
    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-i", str(frame_path),
        "-vf", vf, "-t", str(duration),
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-movflags", "+faststart", "-r", str(fps),
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr[-1200:], file=sys.stderr)
        return False
    return True


def shareability(cfg: dict, article: dict) -> int:
    """Rank recent articles by how well they travel on Reels.

    This is NOT the same judgement as the editorial shortlist. A story can be
    worth publishing and still be a poor video: a culture listing reads fine
    on the site and dies in a vertical feed. What travels is animals, a named
    local place, and a concrete number.

    Reuses pipeline.is_animal_story so 'animal story' means one thing across
    the whole system."""
    score = 0
    probe = {"title": article.get("headline", ""),
             "summary": article.get("summary_short", "")}
    if pipeline.is_animal_story(cfg, probe):
        score += 10
    hay = f'{article.get("headline","")} {article.get("summary_short","")}'
    if any(city in hay for city in (cfg.get("known_cities") or {}).values()):
        score += 6
    if re.search(r"\d", article.get("headline", "")):
        score += 3
    # A real generated image beats the fallback logo every time in a feed.
    if article.get("image_path"):
        score += 5
    else:
        score -= 8
    weights = {"priroda": 5, "zdrave": 4, "obshtestvo": 3,
               "nauka": 2, "sport": 1, "kultura": 0, "ikonomika": 0}
    score += weights.get(article.get("category", ""), 0)
    return score


def find_articles(cfg: dict, slug: str, latest: int, best_hours: int) -> list[dict]:
    arts = []
    for path in sorted(CONTENT.rglob("*.json")):
        try:
            arts.append(json.loads(path.read_text(encoding="utf-8-sig")))
        except Exception:
            continue
    if slug:
        return [a for a in arts if a.get("slug") == slug]

    if best_hours:
        # Pick the single most shareable article published recently, rather
        # than simply the newest. Generating a batch and posting it across the
        # week means Friday's Reel carries Monday's news; one fresh video a day
        # keeps the feed current.
        cutoff = datetime.now(timezone.utc) - timedelta(hours=best_hours)
        recent = []
        for a in arts:
            try:
                when = datetime.strptime(a.get("published", ""), "%Y-%m-%dT%H:%M:%SZ")
            except (TypeError, ValueError):
                continue
            if when.replace(tzinfo=timezone.utc) >= cutoff:
                recent.append(a)
        if not recent:
            return []
        recent.sort(key=lambda a: shareability(cfg, a), reverse=True)
        top = recent[0]
        print(f"[reel] picked from {len(recent)} article(s) in the last "
              f"{best_hours}h (score {shareability(cfg, top)}): {top.get('headline','')[:60]}")
        return [top]

    arts.sort(key=lambda a: a.get("published", ""), reverse=True)
    return arts[:latest]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", default="")
    ap.add_argument("--latest", type=int, default=1)
    ap.add_argument("--duration", type=int, default=12)
    ap.add_argument("--best-hours", type=int, default=0,
                    help="pick the single most shareable article from the last N hours")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        print("ffmpeg is not installed — it renders the video and there is no "
              "fallback.\n"
              "  In GitHub Actions: the workflow's 'Install ffmpeg' step should "
              "handle this; if you see this message there, that step is missing "
              "or failed.\n"
              "  Locally: 'sudo apt install ffmpeg' (Linux), "
              "'brew install ffmpeg' (Mac), or download from ffmpeg.org (Windows).",
              file=sys.stderr)
        return 1

    cfg = load_config()
    articles = find_articles(cfg, args.slug, args.latest, args.best_hours)
    if not articles:
        print(f"No article found for --slug '{args.slug}'." if args.slug
              else "No articles found.", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(exist_ok=True)
    made = 0
    for article in articles:
        slug = article.get("slug", "reel")
        frame = build_frame(cfg, article)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            frame.save(tmp.name)
            out = OUT_DIR / f"{slug}.mp4"
            if render(Path(tmp.name), out, args.duration):
                size_mb = out.stat().st_size / 1024 / 1024
                print(f"  [reel] {out.relative_to(ROOT)}  ({size_mb:.1f} MB, {args.duration}s)")
                made += 1
            else:
                print(f"  [fail] {slug}", file=sys.stderr)
        Path(tmp.name).unlink(missing_ok=True)

    print(f"[reel] {made} video(s) written to {OUT_DIR.relative_to(ROOT)}/")
    return 0 if made else 1


if __name__ == "__main__":
    raise SystemExit(main())

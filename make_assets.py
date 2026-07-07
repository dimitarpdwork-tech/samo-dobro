#!/usr/bin/env python3
"""Generate favicons and the og-default.png social image from config.json colors."""
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def favicon_svg(cfg):
    c = cfg["colors"]
    if cfg["brand"] == "sun":
        rays = "".join(
            f'<rect x="30.4" y="2" width="3.2" height="10" rx="1.6" fill="{c["primary_deep"]}" transform="rotate({a} 32 32)"/>'
            for a in range(0, 360, 45))
        inner = f'<circle cx="32" cy="32" r="15" fill="{c["primary"]}"/>{rays}'
    else:
        inner = (f'<circle cx="32" cy="32" r="22" fill="none" stroke="{c["ink"]}" stroke-width="4"/>'
                 f'<path d="M10 32h44M32 10c-9 10-9 34 0 44M32 10c9 10 9 34 0 44" fill="none" stroke="{c["ink"]}" stroke-width="2.4" opacity=".7"/>'
                 f'<circle cx="50" cy="15" r="6" fill="{c["tertiary"]}"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
            f'<rect width="64" height="64" rx="14" fill="{c["bg"]}"/>{inner}</svg>')


def favicon_png(cfg, size, out):
    c = cfg["colors"]
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, s, s], radius=s // 5, fill=hex_rgb(c["bg"]))
    cx = cy = s // 2
    if cfg["brand"] == "sun":
        import math
        for a in range(0, 360, 45):
            r1, r2 = s * 0.34, s * 0.46
            x1, y1 = cx + r1 * math.cos(math.radians(a)), cy + r1 * math.sin(math.radians(a))
            x2, y2 = cx + r2 * math.cos(math.radians(a)), cy + r2 * math.sin(math.radians(a))
            d.line([x1, y1, x2, y2], fill=hex_rgb(c["primary_deep"]), width=s // 18)
        r = s * 0.26
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=hex_rgb(c["primary"]))
    else:
        r = s * 0.33
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=hex_rgb(c["ink"]), width=s // 14)
        d.arc([cx - r, cy - r * 0.45, cx + r, cy + r * 0.45], 0, 360, fill=hex_rgb(c["ink"]), width=s // 26)
        d.line([cx, cy - r, cx, cy + r], fill=hex_rgb(c["ink"]), width=s // 26)
        rs = s * 0.09
        d.ellipse([s * 0.72 - rs, s * 0.2 - rs, s * 0.72 + rs, s * 0.2 + rs], fill=hex_rgb(c["tertiary"]))
    img = img.resize((size, size), Image.LANCZOS)
    img.save(out)


def og_image(cfg, out):
    W, H = 1200, 630
    c = cfg["colors"]
    top, bottom = hex_rgb(c["bg"]), hex_rgb(c["hero_glow"])
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        d.line([(0, y), (W, y)], fill=lerp(top, bottom, (y / H) ** 1.6))
    if cfg["brand"] == "sun":
        import math
        cx, cy = 980, 500
        for a in range(0, 360, 30):
            x1 = cx + 150 * math.cos(math.radians(a)); y1 = cy + 150 * math.sin(math.radians(a))
            x2 = cx + 205 * math.cos(math.radians(a)); y2 = cy + 205 * math.sin(math.radians(a))
            d.line([x1, y1, x2, y2], fill=hex_rgb(c["primary"]), width=16)
        d.ellipse([cx - 120, cy - 120, cx + 120, cy + 120], fill=hex_rgb(c["primary"]))
    else:
        d.ellipse([160, 430, 2100, 1600], fill=hex_rgb(c["primary"]))
        d.ellipse([180, 455, 2080, 1580], fill=hex_rgb(c["ink"]))
        d.ellipse([880, 320, 970, 410], fill=hex_rgb(c["tertiary"]))
        d.line([0, 430, W, 430], fill=hex_rgb(c["tertiary"]), width=4)
    name_font = ImageFont.truetype(FONT_BOLD, 92)
    tag_font = ImageFont.truetype(FONT_REG, 40)
    d.text((70, 180), cfg["site_name"], font=name_font, fill=hex_rgb(c["ink"]))
    d.text((72, 300), cfg["tagline"], font=tag_font, fill=hex_rgb(c["muted"]))
    img.save(out, quality=92)


def main():
    with open(ROOT / "config.json", encoding="utf-8") as f:
        cfg = json.load(f)
    ASSETS.mkdir(exist_ok=True)
    (ASSETS / "favicon.svg").write_text(favicon_svg(cfg), encoding="utf-8")
    favicon_png(cfg, 64, ASSETS / "favicon.png")
    favicon_png(cfg, 180, ASSETS / "apple-touch-icon.png")
    og_image(cfg, ASSETS / "og-default.png")
    print(f"[{cfg['site_name']}] assets written to {ASSETS}")


if __name__ == "__main__":
    main()

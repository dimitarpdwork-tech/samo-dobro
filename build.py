#!/usr/bin/env python3
"""
Static site builder for the good-news sites.

Reads config.json + content/articles/**/*.json and generates a complete,
SEO-optimized static site into dist/:

  /                       home (hero + latest, paginated)
  /page/N/                older pages
  /c/<category>/          category archives (paginated)
  /<prefix>/<slug>/       article pages (NewsArticle structured data)
  /<about>/               about + editorial policy + AI disclosure
  /<privacy>/             privacy policy (GDPR/cookies)
  /feed.xml  /sitemap.xml  /robots.txt  /404.html
  /assets/                stylesheet, favicons, social image

GA4 / AdSense / Search Console verification are all off by default: fill in
the matching field in config.json (ga4_measurement_id, adsense_client_id,
google_site_verification, bing_site_verification) and the site activates the
Consent-Mode-v2 cookie banner and the relevant script automatically — no
other code changes needed.

Run:  python build.py
"""

import hashlib
import html
import json
import re
import shutil
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parent
CONTENT = ROOT / "content" / "articles"
ASSETS_SRC = ROOT / "assets"
DIST = ROOT / "dist"
PAGE_SIZE = 12
MIN_TAG_ARTICLES = 5  # a /tag/{slug}/ archive page is only built once a tag
                      # has at least this many articles — below that, the
                      # hashtag stays plain text rather than linking to a
                      # thin, near-empty page.

esc = html.escape

BG_MONTHS = ["януари", "февруари", "март", "април", "май", "юни", "юли",
             "август", "септември", "октомври", "ноември", "декември"]
BG_DAYS = ["понеделник", "вторник", "сряда", "четвъртък", "петък", "събота", "неделя"]
EN_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"]
EN_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ---------------------------------------------------------------- data ----

def load_config() -> dict:
    try:
        with open(ROOT / "config.json", encoding="utf-8-sig") as f:
            text = f.read()
    except FileNotFoundError:
        print("ERROR: config.json is missing entirely from the repo root.")
        raise SystemExit(1)
    if not text.strip():
        print("ERROR: config.json is empty (0 bytes). The full file content didn't "
              "save — re-open it, select all, and paste the complete config back in.")
        raise SystemExit(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"ERROR: config.json has a JSON syntax problem: {exc}\n"
              "Check for a missing comma, quote, or brace near that position.")
        raise SystemExit(1)


def load_articles(cfg) -> list[dict]:
    articles = []
    skipped = []
    now = datetime.now(timezone.utc)
    if CONTENT.exists():
        for path in sorted(CONTENT.rglob("*.json")):
            try:
                with open(path, encoding="utf-8-sig") as f:
                    a = json.load(f)
                if a.get("publish_at"):
                    embargo = datetime.strptime(a["publish_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    if now < embargo:
                        continue  # not time yet — invisible to this build, will appear on its own later
                if a.get("category") not in cfg["categories"]:
                    a["category"] = next(iter(cfg["categories"]))
                a["_dt"] = datetime.strptime(a["published"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                articles.append(a)
            except Exception as exc:
                skipped.append((path, exc))
    if skipped:
        print(f"\n⚠ {len(skipped)} article file(s) skipped due to errors (site still builds without them):")
        for path, exc in skipped:
            print(f"  - {path.relative_to(ROOT)}: {exc}")
        print("  Fix the file(s) above and re-run to bring these articles back.\n")
    articles.sort(key=lambda a: a["_dt"], reverse=True)
    return articles


def fmt_date(dt: datetime, lang: str) -> str:
    if lang == "bg":
        return f"{dt.day} {BG_MONTHS[dt.month - 1]} {dt.year} г."
    return f"{EN_MONTHS[dt.month - 1]} {dt.day}, {dt.year}"


def fmt_today(lang: str) -> str:
    now = datetime.now(timezone.utc)
    if lang == "bg":
        return f"{BG_DAYS[now.weekday()]}, {now.day} {BG_MONTHS[now.month - 1]} {now.year}"
    return f"{EN_DAYS[now.weekday()]}, {EN_MONTHS[now.month - 1]} {now.day}, {now.year}"


def reading_time(body: str) -> int:
    return max(1, round(len(body.split()) / 180))


def hnum(seed: str, lo: int, hi: int, salt: str = "") -> int:
    h = int(hashlib.sha1((seed + salt).encode()).hexdigest()[:8], 16)
    return lo + h % (hi - lo + 1)


# Standard Bulgarian Cyrillic -> Latin transliteration (matches the scheme
# used on Bulgarian road signs / official transliteration law), used only
# for building clean ASCII URL slugs from hashtags. Display text keeps the
# original Cyrillic; only the URL is transliterated.
BG_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sht", "ъ": "a",
    "ь": "y", "ю": "yu", "я": "ya",
}


def tag_slug(tag: str, aliases: dict | None = None) -> str:
    """Transliterate a (typically Cyrillic) hashtag into a clean URL slug.
    Latin input passes through unchanged aside from lowercasing/hyphenation.
    `aliases` (from config.json's tag_aliases) maps a raw computed slug to a
    canonical one — needed because the content pipeline sometimes tags in
    Cyrillic and sometimes in already-Latin/casual transliteration (e.g.
    'София' -> 'sofiya' via BG_TRANSLIT, but a literal 'sofia' tag passes
    through unchanged), which otherwise silently fragments one topic across
    two separate tag pages with zero overlapping articles."""
    out = []
    for ch in tag.strip().lower():
        if ch in BG_TRANSLIT:
            out.append(BG_TRANSLIT[ch])
        elif ch.isalnum():
            out.append(ch)
        elif ch in (" ", "-", "_"):
            out.append("-")
        # anything else (punctuation, emoji, etc.) is simply dropped
    slug = re.sub(r"-+", "-", "".join(out)).strip("-")
    if aliases:
        slug = aliases.get(slug, slug)
    return slug


def build_tag_index(articles: list[dict], aliases: dict | None = None) -> dict:
    """Group articles by (alias-normalized) tag slug: slug -> {'display':
    original_tag_text, 'articles': [...]}. Articles are assumed pre-sorted
    newest-first, so each tag's article list stays newest-first too, and the
    'display' name is whichever spelling appeared on the most recent article
    for that slug. Tags that transliterate to an empty slug (pure
    punctuation/emoji) are skipped."""
    idx: dict[str, dict] = {}
    for a in articles:
        for t in a.get("tags", []):
            slug = tag_slug(t, aliases)
            if not slug:
                continue
            entry = idx.setdefault(slug, {"display": t, "articles": []})
            entry["articles"].append(a)
    return idx


# ---------------------------------------------------------------- css -----

CSS = Template("""
${font_faces}
:root{--bg:${bg};--ink:${ink};--muted:${muted};--card:${card};--line:${line};
--p:${primary};--pd:${primary_deep};--s:${secondary};--t:${tertiary};--glow:${hero_glow};
--fd:${font_display};--fb:${font_body};--fl:${font_label};--r:18px;--maxw:1128px}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--fb);
font-size:16.5px;line-height:1.55;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}img,svg{max-width:100%}
:focus-visible{outline:3px solid var(--p);outline-offset:2px;border-radius:6px}
.wrap{max-width:var(--maxw);margin:0 auto;padding:0 22px}

/* masthead */
.masthead{padding:26px 0 10px}
.mast-row{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.mark{flex:0 0 auto;display:grid;place-items:center}
.brand h1,.brand .h1{font-family:var(--fd);font-weight:800;font-size:1.9rem;margin:0;letter-spacing:-.02em;line-height:1}
.brand p{margin:3px 0 0;color:var(--muted);font-size:.95rem}
.today{margin-left:auto;font-family:var(--fl);text-transform:uppercase;letter-spacing:.14em;
font-size:.72rem;color:var(--muted);border:1px solid var(--line);border-radius:999px;
padding:7px 14px;background:var(--card)}
nav.cats{display:flex;gap:8px;overflow-x:auto;padding:16px 0 6px;scrollbar-width:none;
position:relative;-webkit-mask-image:linear-gradient(90deg,#000 0 92%,transparent);
mask-image:linear-gradient(90deg,#000 0 92%,transparent)}
nav.cats::-webkit-scrollbar{display:none}
.chip{flex:0 0 auto;font-family:var(--fl);font-size:.83rem;font-weight:700;letter-spacing:.04em;
padding:13px 16px;min-height:48px;display:inline-flex;align-items:center;border-radius:999px;
border:1.5px solid var(--line);background:var(--card);color:var(--ink);
transition:transform .15s,border-color .15s}
.chip:hover{border-color:var(--p);transform:translateY(-1px)}
.chip.on{background:var(--ink);border-color:var(--ink);color:var(--card)}

/* hero */
.hero{position:relative;overflow:hidden;border-radius:26px;margin:14px 0 30px;
background:var(--card);border:1px solid var(--line)}
.hero-inner{position:relative;z-index:2;padding:42px 44px;max-width:640px}
.kicker{display:inline-flex;align-items:center;gap:8px;font-family:var(--fl);font-weight:700;
text-transform:uppercase;letter-spacing:.16em;font-size:.72rem;color:var(--pd);margin-bottom:14px}
.kicker .dot{width:9px;height:9px;border-radius:50%;background:var(--p);box-shadow:0 0 0 4px color-mix(in srgb,var(--p) 25%,transparent)}
.hero h2{font-family:var(--fd);font-weight:800;font-size:clamp(1.7rem,4vw,2.7rem);
line-height:1.12;margin:0 0 14px;letter-spacing:-.02em}
.hero p.teaser{font-size:1.08rem;color:var(--muted);margin:0 0 20px;max-width:52ch}
.btn{display:inline-block;font-family:var(--fl);font-weight:700;font-size:.95rem;
background:var(--p);color:var(--ink);padding:12px 22px;border-radius:999px;
box-shadow:0 6px 16px color-mix(in srgb,var(--p) 45%,transparent);transition:transform .15s,box-shadow .15s}
.btn:hover{transform:translateY(-2px);box-shadow:0 10px 22px color-mix(in srgb,var(--p) 55%,transparent)}
.hero-art{position:absolute;inset:0;z-index:1;pointer-events:none}
.meta{display:flex;gap:10px;align-items:center;flex-wrap:wrap;color:var(--muted);
font-family:var(--fl);font-size:.8rem;letter-spacing:.03em}
.meta .cat{font-weight:700;color:var(--pd)}

/* section title */
.sec{display:flex;align-items:baseline;gap:14px;margin:6px 0 18px}
.sec h2,.sec h1{font-family:var(--fd);font-weight:800;font-size:1.35rem;margin:0;letter-spacing:-.01em}
.sec .rule{flex:1;height:5px;border-radius:99px;background:linear-gradient(90deg,var(--p),var(--glow) 55%,transparent)}
body.brand-globe .sec .rule{height:2px;background:linear-gradient(90deg,var(--t) 0 64px,var(--line) 64px);position:relative}
.cat-intro{color:var(--muted);font-size:1.02rem;line-height:1.6;max-width:64ch;margin:4px 0 20px}

/* grid + cards */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(292px,1fr));gap:22px;margin-bottom:34px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;
display:flex;flex-direction:column;transition:transform .18s,box-shadow .18s}
.card:hover{transform:translateY(-4px);box-shadow:0 14px 30px rgba(20,40,60,.10)}
.card.pillar-card{border-color:var(--p);border-width:2px}
.pillar-badge{display:inline-block;font-family:var(--fl);font-size:.7rem;font-weight:700;
letter-spacing:.03em;color:var(--pd);background:color-mix(in srgb,var(--p) 16%,var(--card));
border-radius:999px;padding:3px 10px;align-self:flex-start}
.card .thumb{display:block;line-height:0}
.thumb{position:relative;overflow:hidden;background:var(--line)}
.thumb img{width:100%;height:100%;object-fit:cover;display:block}
.photo-credit{position:absolute;right:6px;bottom:5px;font-family:var(--fl);font-size:.66rem;
color:#fff;background:rgba(0,0,0,.45);padding:2px 7px;border-radius:999px;text-decoration:none}
.cbody{padding:16px 18px 18px;display:flex;flex-direction:column;gap:9px;flex:1}
.cbody h3{font-family:var(--fd);font-weight:800;font-size:1.13rem;line-height:1.28;margin:0;letter-spacing:-.01em}
.cbody p{margin:0;color:var(--muted);font-size:.94rem}
.cbody .meta{margin-top:auto;padding-top:6px}

/* article */
.article{max-width:720px;margin:10px auto 40px}
.article h1{font-family:var(--fd);font-weight:800;font-size:clamp(1.7rem,4.4vw,2.55rem);
line-height:1.14;letter-spacing:-.02em;margin:10px 0 14px}
.ai-badge{display:inline-block;font-family:var(--fl);font-size:.72rem;font-weight:700;
letter-spacing:.04em;color:var(--muted);background:var(--card);border:1px solid var(--line);
border-radius:999px;padding:4px 11px;margin:0 0 14px}
.byline{display:inline-block;font-family:var(--fl);font-size:.85rem;font-weight:700;
color:var(--pd);margin:0 10px 14px 0}
.quick-facts{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
padding:16px 20px 16px 38px;margin:6px 0 20px;list-style:disc}
.quick-facts li{font-size:.98rem;line-height:1.55;margin:4px 0;color:var(--ink)}
.cat-name{color:var(--pd);font-weight:700;border-bottom:2px dotted var(--p);cursor:pointer;
padding:0 1px}
.cat-name.found{color:var(--t);border-bottom-style:solid}
.cat-poem{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
padding:22px 26px;margin:24px 0;animation:catpoem-in .5s ease}
.cat-poem p{font-family:var(--fd);font-style:italic;color:var(--ink);line-height:1.9;
margin:0 0 1em;text-align:center}
@keyframes catpoem-in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.article .banner{border-radius:var(--r);overflow:hidden;margin:20px 0;line-height:0;border:1px solid var(--line)}
.article .body p{font-size:1.07rem;line-height:1.75;margin:0 0 1.2em}
.article .body p a{color:var(--pd);text-decoration:underline;text-underline-offset:2px}
.article .body h2{font-family:var(--fd);font-weight:800;font-size:1.5rem;
letter-spacing:-.01em;margin:1.5em 0 .5em}
.article .body h3{font-family:var(--fd);font-weight:700;font-size:1.2rem;
letter-spacing:-.01em;margin:1.3em 0 .4em}
.article .body h2:first-child,.article .body h3:first-child{margin-top:0}
.tags{display:flex;gap:8px;flex-wrap:wrap;margin:20px 0}
.tag{font-family:var(--fl);font-size:.78rem;font-weight:700;color:var(--pd);
background:color-mix(in srgb,var(--p) 14%,var(--card));border-radius:999px;padding:5px 12px}
.tag[href]{cursor:pointer;transition:background .15s}
.tag[href]:hover{background:color-mix(in srgb,var(--p) 26%,var(--card))}
.srcbox{border-left:4px solid var(--p);background:var(--card);border-radius:0 var(--r) var(--r) 0;
padding:14px 18px;margin:24px 0;border-top:1px solid var(--line);border-right:1px solid var(--line);border-bottom:1px solid var(--line)}
.srcbox a{font-weight:700;color:var(--pd);text-decoration:underline;text-underline-offset:3px}
.ainote{color:var(--muted);font-size:.85rem;font-style:italic;margin:10px 0 0}
.backlink{display:inline-block;margin:8px 0 22px;font-family:var(--fl);font-weight:700;color:var(--pd)}

/* pagination + footer */
.pager{display:flex;justify-content:center;gap:12px;margin:8px 0 40px;font-family:var(--fl);font-weight:700}
.pager a,.pager span{padding:9px 18px;border-radius:999px;border:1.5px solid var(--line);background:var(--card)}
.pager a:hover{border-color:var(--p)}
.pager .cur{background:var(--ink);color:var(--card);border-color:var(--ink)}
footer{border-top:1px solid var(--line);margin-top:20px;padding:30px 0 40px;background:var(--card)}
footer .mission{max-width:56ch;color:var(--muted);margin:8px 0 16px}
footer .fnav{display:flex;gap:18px;flex-wrap:wrap;font-family:var(--fl);font-weight:700;font-size:.9rem}
footer .fine{color:var(--muted);font-size:.8rem;margin-top:18px}
.about{max-width:720px;margin:10px auto 44px}
.about h1{font-family:var(--fd);font-weight:800;font-size:2.1rem;letter-spacing:-.02em}
.editor-card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
padding:20px 22px;margin:20px 0 6px}
.editor-name{font-family:var(--fd);font-weight:800;font-size:1.15rem}
.editor-title{font-family:var(--fl);font-weight:700;font-size:.82rem;color:var(--pd);
text-transform:uppercase;letter-spacing:.05em;margin:2px 0 10px}
.editor-card p{margin:0;color:var(--muted);line-height:1.65}
.about h2{font-family:var(--fd);font-weight:800;font-size:1.25rem;margin:26px 0 8px}
.about p{color:var(--ink);line-height:1.7}
.nf{text-align:center;padding:70px 0}
.nf .big{font-size:4rem}

@media (max-width:700px){
 .hero-inner{padding:22px 20px 20px;max-width:100%}
 .kicker{margin-bottom:8px}
 .hero h2{font-size:clamp(1.35rem,5.5vw,1.85rem);margin-bottom:8px}
 .hero p.teaser{margin:0 0 14px;font-size:.98rem}
 .hero-art svg.side{opacity:.35}
 .today{display:none}
}
@media (prefers-reduced-motion:reduce){
 *{transition:none!important;animation:none!important}html{scroll-behavior:auto}
}

/* cookie consent banner */
.cookie-banner{position:fixed;left:16px;right:16px;bottom:16px;z-index:999;
max-width:640px;margin:0 auto;background:var(--ink);color:var(--bg);
border-radius:16px;padding:18px 20px;box-shadow:0 12px 34px rgba(0,0,0,.28);
display:flex;flex-wrap:wrap;align-items:center;gap:14px;font-size:.92rem}
.cookie-banner[hidden]{display:none}
.cookie-banner p{margin:0;flex:1 1 260px;line-height:1.5}
.cookie-banner a{text-decoration:underline;text-underline-offset:3px;color:var(--bg)}
.cookie-actions{display:flex;gap:10px;flex:0 0 auto}
.cookie-actions button{font-family:var(--fl);font-weight:700;font-size:.86rem;
border-radius:999px;padding:9px 16px;border:1.5px solid color-mix(in srgb,var(--bg) 35%,transparent);
background:transparent;color:var(--bg);cursor:pointer}
.cookie-actions button#cookie-accept{background:var(--p);color:var(--ink);border-color:var(--p)}
@media (max-width:480px){.cookie-banner{padding:14px 16px}
.cookie-actions button{min-height:44px}}
""")


# ------------------------------------------------------------- svg art ----

def mark_svg(cfg) -> str:
    c = cfg["colors"]
    if cfg["brand"] == "sun":
        rays = "".join(
            f'<rect x="22.6" y="1" width="2.8" height="8" rx="1.4" fill="{c["primary_deep"]}" transform="rotate({a} 24 24)"/>'
            for a in range(0, 360, 45))
        return (f'<svg class="mark" width="46" height="46" viewBox="0 0 48 48" aria-hidden="true">'
                f'<circle cx="24" cy="24" r="11.5" fill="{c["primary"]}"/>'
                f'<circle cx="24" cy="24" r="11.5" fill="none" stroke="{c["primary_deep"]}" stroke-width="1.6"/>{rays}</svg>')
    return (f'<svg class="mark" width="46" height="46" viewBox="0 0 48 48" aria-hidden="true">'
            f'<circle cx="24" cy="24" r="17" fill="none" stroke="{c["ink"]}" stroke-width="2.6"/>'
            f'<path d="M7 24h34M24 7c-7 8-7 26 0 34M24 7c7 8 7 26 0 34" fill="none" stroke="{c["ink"]}" stroke-width="1.8" opacity=".65"/>'
            f'<circle cx="38.5" cy="11" r="4.5" fill="{c["tertiary"]}"/></svg>')


def hero_art(cfg) -> str:
    c = cfg["colors"]
    if cfg["brand"] == "sun":
        rays = "".join(
            f'<rect x="-7" y="-150" width="14" height="52" rx="7" fill="{c["primary"]}" opacity=".85" transform="rotate({a})"/>'
            for a in range(0, 360, 30))
        return (
            '<div class="hero-art">'
            f'<div style="position:absolute;inset:0;background:'
            f'radial-gradient(620px 420px at 86% 118%,{c["hero_glow"]} 0%,{c["primary"]}55 34%,transparent 68%)"></div>'
            f'<svg class="side" style="position:absolute;right:-40px;bottom:-70px" width="380" height="380" viewBox="-190 -190 380 380" aria-hidden="true">'
            f'<g>{rays}</g><circle r="86" fill="{c["primary"]}"/><circle r="86" fill="none" stroke="#fff" stroke-opacity=".5" stroke-width="3"/></svg></div>')
    return (
        '<div class="hero-art">'
        f'<svg preserveAspectRatio="none" style="position:absolute;left:0;right:0;bottom:0;width:100%;height:150px" viewBox="0 0 1000 150" aria-hidden="true">'
        f'<defs><linearGradient id="hz" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{c["primary"]}"/><stop offset="1" stop-color="{c["ink"]}"/></linearGradient></defs>'
        f'<circle cx="820" cy="58" r="26" fill="{c["tertiary"]}"/>'
        f'<circle cx="820" cy="58" r="40" fill="{c["hero_glow"]}" opacity=".35"/>'
        f'<ellipse cx="500" cy="330" rx="760" ry="250" fill="url(#hz)"/>'
        f'<line x1="0" y1="86" x2="1000" y2="86" stroke="{c["tertiary"]}" stroke-width="1.4" opacity=".8"/></svg></div>')


def card_art(cfg, article, height=180) -> str:
    cat = cfg["categories"][article["category"]]
    gid = "g" + hashlib.sha1(article["slug"].encode()).hexdigest()[:8]
    s = article["slug"]
    circles = "".join(
        f'<circle cx="{hnum(s, 30, 610, str(i))}" cy="{hnum(s, 20, 300, "y" + str(i))}" '
        f'r="{hnum(s, 26, 90, "r" + str(i))}" fill="#fff" opacity=".{hnum(s, 8, 18, "o" + str(i))}"/>'
        for i in range(3))
    return (f'<svg class="thumb" viewBox="0 0 640 320" width="100%" height="{height}" '
            f'preserveAspectRatio="xMidYMid slice" role="img" aria-label="{esc(cat["label"])}">'
            f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="1" y2="1">'
            f'<stop offset="0" stop-color="{cat["c1"]}"/><stop offset="1" stop-color="{cat["c2"]}"/></linearGradient></defs>'
            f'<rect width="640" height="320" fill="url(#{gid})"/>{circles}'
            f'<circle cx="320" cy="160" r="64" fill="#fff" opacity=".28"/>'
            f'<text x="320" y="160" font-size="72" text-anchor="middle" dominant-baseline="central">{cat["emoji"]}</text></svg>')


def pexels_resize(url: str, width: int) -> str:
    """Return a Pexels image URL requesting `width`, preserving the image's
    natural aspect ratio and any other query parameters. Used to request a
    size that actually matches where the image is displayed (thumbnail vs.
    full-width banner vs. schema/OG image) instead of one fixed width
    everywhere, and to build srcset candidates.
    Both any existing w= AND h= params are stripped before adding the new
    w= — leaving a fixed h= from the pipeline in place while only changing
    w= would make Pexels crop to a distorted aspect ratio at smaller widths.
    Client-side object-fit:cover already handles final cropping to fit each
    container, so no server-side height constraint is needed here."""
    if "images.pexels.com" not in url:
        return url
    base, _, query = url.partition("?")
    params = [p for p in query.split("&") if p and not p.startswith(("w=", "h="))]
    params.append(f"w={width}")
    return f"{base}?{'&'.join(params)}"


def media(cfg, article, ui, height=180, eager=False,
          sizes="(max-width: 700px) 100vw, 292px") -> str:
    """Real stock photo when the pipeline found one, generated SVG art otherwise.
    Photo credit is a hard requirement of the free API's terms, not optional.
    eager=True skips lazy-loading — use this only for the one above-the-fold
    image per page (the article's own banner), never for listing thumbnails.
    `sizes` should describe the actual rendered width in this context so the
    browser can pick the right srcset candidate — pass a wider value for the
    full-width article banner than for grid thumbnails."""
    if article.get("photo_url"):
        base_url = article["photo_url"]
        srcset = ", ".join(f"{esc(pexels_resize(base_url, w))} {w}w" for w in (400, 800, 1200))
        credit = (f'<a class="photo-credit" href="{esc(article["photo_credit_url"])}" '
                  f'target="_blank" rel="noopener">{esc(ui.get("photo_by", "Photo:"))} {esc(article["photo_credit"])} · Pexels</a>')
        loading_attr = '' if eager else ' loading="lazy"'
        return (f'<div class="thumb" style="height:{height}px">'
                f'<img src="{esc(pexels_resize(base_url, 800))}" srcset="{srcset}" sizes="{esc(sizes)}" '
                f'alt="{esc(article["headline"])}"{loading_attr}>'
                f'{credit}</div>')
    return card_art(cfg, article, height)


# ------------------------------------------------------------ helpers -----

class Site:
    def __init__(self, cfg, articles):
        self.cfg = cfg
        self.articles = articles
        self.bp = cfg.get("base_path", "").rstrip("/")
        self.base = cfg["base_url"].rstrip("/")

    def u(self, path: str) -> str:              # site-relative URL
        return f'{self.bp}{path}'

    def abs_(self, path: str) -> str:           # absolute URL
        return f'{self.base}{self.bp}{path}'

    def article_path(self, a) -> str:
        return f'/{self.cfg["article_prefix"]}/{a["slug"]}/'

    def cat_path(self, cid) -> str:
        return f'/c/{cid}/'

    def tag_path(self, slug: str) -> str:
        return f'/tag/{slug}/'


def org_ld(site) -> dict:
    cfg = site.cfg
    return {"@type": "Organization", "name": cfg["site_name"], "url": site.abs_("/"),
            "logo": {"@type": "ImageObject", "url": site.abs_("/assets/apple-touch-icon.png")},
            "foundingDate": cfg.get("founding_date", "2026-07-01"),
            "contactPoint": {"@type": "ContactPoint", "email": cfg["contact_email"],
                              "contactType": "editorial"},
            "sameAs": cfg.get("same_as", [])}


def author_ld(site) -> dict:
    """Article authorship is always attributed to the automated editorial
    system as an Organization, never to a fabricated human Person — see
    /<about_path>/#editorial-process for the disclosed process. Set
    'byline_name' in config.json to customize the displayed name (defaults
    to '<site_name> AI Editorial System')."""
    cfg = site.cfg
    name = cfg.get("byline_name", f'{cfg["site_name"]} AI Editorial System')
    return {"@type": "Organization", "name": name,
            "url": site.abs_(f'/{cfg["about_path"]}/#editorial-process')}


def verification_tags(cfg) -> str:
    """Search-console ownership meta tags. Empty config values render nothing."""
    tags = []
    gsv = cfg.get("google_site_verification", "")
    bsv = cfg.get("bing_site_verification", "")
    if gsv:
        tags.append(f'<meta name="google-site-verification" content="{esc(gsv)}">')
    if bsv:
        tags.append(f'<meta name="msvalidate.01" content="{esc(bsv)}">')
    return "".join(tags)


def analytics_ads_enabled(cfg) -> bool:
    return bool(cfg.get("ga4_measurement_id") or cfg.get("adsense_client_id"))


def head_scripts(cfg) -> str:
    """Google tag loader + Consent Mode v2 default (denied) set BEFORE any tag fires.
    Renders nothing until a GA4 or AdSense id is added to config.json."""
    if not analytics_ads_enabled(cfg):
        return ""
    ga4 = cfg.get("ga4_measurement_id", "")
    ads = cfg.get("adsense_client_id", "")
    loader_id = ga4 or ads
    tags_snippet = ""
    if ga4:
        tags_snippet += f"gtag('config','{esc(ga4)}');"
    ads_script = (
        f'<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?'
        f'client={esc(ads)}" crossorigin="anonymous"></script>' if ads else ""
    )
    return f"""<script async src="https://www.googletagmanager.com/gtag/js?id={esc(loader_id)}"></script>
<script>
window.dataLayer = window.dataLayer || [];
function gtag(){{dataLayer.push(arguments);}}
gtag('consent','default',{{
  'ad_storage':'denied','analytics_storage':'denied',
  'ad_user_data':'denied','ad_personalization':'denied'
}});
gtag('js', new Date());
{tags_snippet}
</script>
{ads_script}"""


def cookie_banner(site) -> str:
    """Simple, equally-weighted accept/reject banner wired to Consent Mode v2.
    Renders nothing until a GA4 or AdSense id is configured."""
    cfg, ui = site.cfg, site.cfg["ui"]
    if not analytics_ads_enabled(cfg):
        return ""
    return f"""<div id="cookie-banner" class="cookie-banner" hidden role="dialog" aria-label="{esc(ui.get('cookie_accept', 'Accept'))}">
<p>{esc(ui.get('cookie_text', 'We use cookies for anonymous analytics.'))} <a href="{site.u('/' + cfg['privacy_path'] + '/')}">{esc(ui.get('privacy', 'Privacy'))}</a></p>
<div class="cookie-actions">
<button id="cookie-reject" type="button">{esc(ui.get('cookie_reject', 'Reject'))}</button>
<button id="cookie-accept" type="button">{esc(ui.get('cookie_accept', 'Accept'))}</button>
</div></div>
<script>
(function(){{
  var KEY='cookie_consent_v1';
  function safeGet(k){{ try {{ return localStorage.getItem(k); }} catch(e) {{ return null; }} }}
  function safeSet(k,v){{ try {{ localStorage.setItem(k,v); }} catch(e) {{ /* storage blocked; consent still applies for this page view */ }} }}
  function apply(state){{
    if (typeof gtag !== 'function') return;
    gtag('consent','update',{{'ad_storage':state,'analytics_storage':state,
      'ad_user_data':state,'ad_personalization':state}});
  }}
  var saved = safeGet(KEY);
  var b = document.getElementById('cookie-banner');
  if (saved) {{ apply(saved); }}
  else if (b) {{ b.hidden = false; }}
  var a = document.getElementById('cookie-accept');
  var r = document.getElementById('cookie-reject');
  if (a) a.addEventListener('click', function(){{
    apply('granted'); safeSet(KEY,'granted'); if (b) b.hidden = true;
  }});
  if (r) r.addEventListener('click', function(){{
    apply('denied'); safeSet(KEY,'denied'); if (b) b.hidden = true;
  }});
}})();
</script>"""


def base_page(site, *, title, description, path, body, jsonld=None, og_type="website",
              og_image="/assets/og-default.png", noindex=False, is_home=False) -> str:
    cfg = site.cfg
    ld = "".join(f'<script type="application/ld+json">{json.dumps(x, ensure_ascii=False)}</script>'
                 for x in (jsonld or []))
    robots = '<meta name="robots" content="noindex">' if noindex else ""
    return f"""<!DOCTYPE html>
<html lang="{cfg['lang']}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<link rel="canonical" href="{site.abs_(path)}">{robots}
<meta property="og:site_name" content="{esc(cfg['site_name'])}">
<meta property="og:type" content="{og_type}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:url" content="{site.abs_(path)}">
<meta property="og:image" content="{og_image if og_image.startswith('http') else site.abs_(og_image)}">
<meta property="og:locale" content="{cfg['locale']}">
{('<link rel="preconnect" href="https://images.pexels.com">' if cfg.get('pexels_api_key') else '')}
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" href="{site.u('/assets/favicon.svg')}" type="image/svg+xml">
<link rel="icon" href="{site.u('/assets/favicon.png')}" sizes="64x64">
<link rel="apple-touch-icon" href="{site.u('/assets/apple-touch-icon.png')}">
<link rel="alternate" type="application/rss+xml" title="{esc(cfg['site_name'])} RSS" href="{site.u('/feed.xml')}">
<link rel="stylesheet" href="{site.u('/assets/style.css')}">
{verification_tags(cfg)}
{head_scripts(cfg)}
{ld}
</head>
<body class="brand-{cfg['brand']}">
{header(site, is_home=is_home)}
<main class="wrap" id="main">
{body}
</main>
{footer(site)}
{cookie_banner(site)}
</body>
</html>"""


def header(site, active: str | None = None, is_home: bool = False) -> str:
    cfg, ui = site.cfg, site.cfg["ui"]
    chips = f'<a class="chip{" on" if active == "home" else ""}" href="{site.u("/")}">{esc(ui["home"])}</a>'
    for cid, cat in cfg["categories"].items():
        on = " on" if active == cid else ""
        chips += f'<a class="chip{on}" href="{site.u(site.cat_path(cid))}">{cat["emoji"]} {esc(cat["label"])}</a>'
    chips += f'<a class="chip{" on" if active == "about" else ""}" href="{site.u("/" + cfg["about_path"] + "/")}">{esc(ui["about"])}</a>'
    brand_name = (f'<h1 class="h1">{esc(cfg["site_name"])}</h1>' if is_home
                  else f'<span class="h1">{esc(cfg["site_name"])}</span>')
    return f"""<header class="masthead wrap">
<div class="mast-row">
{mark_svg(cfg)}
<div class="brand"><a href="{site.u('/')}" aria-label="{esc(cfg['site_name'])}">{brand_name}</a>
<p>{esc(cfg['tagline'])}</p></div>
<span class="today">{esc(fmt_today(cfg['lang']))}</span>
</div>
<nav class="cats" aria-label="categories">{chips}</nav>
</header>"""


def footer(site) -> str:
    cfg, ui = site.cfg, site.cfg["ui"]
    year = datetime.now().year
    return f"""<footer><div class="wrap">
<strong style="font-family:var(--fd);font-size:1.05rem">{esc(cfg['site_name'])}</strong>
<p class="mission">{esc(ui['footer_mission'])}</p>
<nav class="fnav">
<a href="{site.u('/' + cfg['about_path'] + '/')}">{esc(ui['about'])}</a>
<a href="{site.u('/' + cfg['privacy_path'] + '/')}">{esc(ui.get('privacy', 'Privacy'))}</a>
<a href="{site.u('/feed.xml')}">{esc(ui.get('rss', 'RSS'))}</a>
<a href="mailto:{esc(cfg['contact_email'])}">{esc(cfg['contact_email'])}</a>
</nav>
<p class="fine">© {year} {esc(cfg['site_name'])} · ☀</p>
</div></footer>"""


def meta_row(site, a, with_cat=True) -> str:
    cfg, ui = site.cfg, site.cfg["ui"]
    cat = cfg["categories"][a["category"]]
    cat_html = (f'<a class="cat" href="{site.u(site.cat_path(a["category"]))}">'
                f'{cat["emoji"]} {esc(cat["label"])}</a> · ' if with_cat else "")
    return (f'<div class="meta">{cat_html}'
            f'<time datetime="{a["published"]}">{fmt_date(a["_dt"], cfg["lang"])}</time>'
            f' · {reading_time(a["body"])} {ui["min_read"]}</div>')


def card(site, a, eager=False) -> str:
    href = site.u(site.article_path(a))
    ui = site.cfg["ui"]
    badge = (f'<span class="pillar-badge">{esc(ui.get("guide_badge", "📖 Guide"))}</span>'
             if a.get("pillar") else "")
    return f"""<article class="card{' pillar-card' if a.get('pillar') else ''}">
<a href="{href}" aria-label="{esc(a['headline'])}">{media(site.cfg, a, site.cfg['ui'], eager=eager)}</a>
<div class="cbody">
{badge}
<h3><a href="{href}">{esc(a['headline'])}</a></h3>
<p>{esc(a['summary_short'])}</p>
{meta_row(site, a)}
</div></article>"""


def hero(site, a) -> str:
    cfg, ui = site.cfg, site.cfg["ui"]
    cat = cfg["categories"][a["category"]]
    return f"""<section class="hero">
{hero_art(cfg)}
<div class="hero-inner">
<span class="kicker"><span class="dot"></span>{esc(ui['hero_kicker'])} · {cat['emoji']} {esc(cat['label'])}</span>
<h2><a href="{site.u(site.article_path(a))}">{esc(a['headline'])}</a></h2>
<p class="teaser">{esc(a['summary_short'])}</p>
<a class="btn" href="{site.u(site.article_path(a))}">{esc(ui['read_more'])} →</a>
</div></section>"""


def pager(site, base_path: str, page: int, pages: int) -> str:
    if pages <= 1:
        return ""
    ui = site.cfg["ui"]

    def link(p):
        return site.u(base_path if p == 1 else f'{base_path}page/{p}/')

    parts = []
    if page > 1:
        parts.append(f'<a href="{link(page - 1)}">← {ui["newer"]}</a>')
    parts.append(f'<span class="cur">{ui["page"]} {page} / {pages}</span>')
    if page < pages:
        parts.append(f'<a href="{link(page + 1)}">{ui["older"]} →</a>')
    return f'<nav class="pager" aria-label="pagination">{"".join(parts)}</nav>'


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# -------------------------------------------------------------- pages -----

def breadcrumb_ld(site, crumbs: list[tuple[str, str]]) -> dict:
    """crumbs: list of (name, url) from home outward."""
    return {"@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": [
                {"@type": "ListItem", "position": i + 1, "name": name, "item": url}
                for i, (name, url) in enumerate(crumbs)
            ]}


def build_lists(site) -> None:
    cfg, ui = site.cfg, site.cfg["ui"]
    now = datetime.now(timezone.utc)
    pinned = None
    for a in site.articles:
        pin_until = a.get("pin_until")
        if pin_until:
            try:
                until_dt = datetime.strptime(pin_until, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if now < until_dt:
                    pinned = a
                    break  # site.articles is newest-first; first active pin wins
            except Exception:
                pass
    groups = [("home", "/", site.articles, [], cfg["description"], cfg["site_name"] + " — " + cfg["tagline"], "")]
    for cid, cat in cfg["categories"].items():
        cat_arts = [a for a in site.articles if a["category"] == cid]
        # Pillar/guide articles are pulled out of the normal reverse-chronological
        # flow entirely — they're pinned once at the top of page 1 instead of
        # paginating away like a dated news item as new content publishes.
        pillars = [a for a in cat_arts if a.get("pillar")]
        regular = [a for a in cat_arts if not a.get("pillar")]
        intro = cat.get("intro", "")
        desc = intro if intro else f'{cat["label"]} · {cfg["site_name"]} — {cfg["tagline"]}'
        groups.append((cid, site.cat_path(cid), regular, pillars, desc,
                       f'{cat["label"]} · {cfg["site_name"]}', intro))
    for key, base_path, arts, pillars, desc, title, intro in groups:
        pages = max(1, -(-len(arts) // PAGE_SIZE))
        for p in range(1, pages + 1):
            chunk = arts[(p - 1) * PAGE_SIZE: p * PAGE_SIZE]
            body = ""
            rest = chunk
            if key == "home" and p == 1 and chunk:
                hero_article = pinned if pinned else chunk[0]
                body += hero(site, hero_article)
                rest = [a for a in chunk if a["slug"] != hero_article["slug"]]
            label = ui["latest"] if key == "home" else f'{cfg["categories"][key]["emoji"]} {cfg["categories"][key]["label"]}'
            heading_tag = "h2" if key == "home" else "h1"
            body += f'<div class="sec"><{heading_tag}>{esc(label)}</{heading_tag}><span class="rule"></span></div>'
            if intro and p == 1:
                body = f'<p class="cat-intro">{esc(intro)}</p>' + body
            if pillars and p == 1:
                guide_label = ui.get("guides_label", "📖 Наръчници" if cfg["lang"] == "bg" else "📖 Guides")
                body += f'<div class="sec pillar-sec"><h2>{esc(guide_label)}</h2><span class="rule"></span></div>'
                body += '<div class="grid pillar-grid">' + "".join(card(site, a) for a in pillars) + "</div>"
            is_home_p1 = (key == "home" and p == 1)
            body += '<div class="grid">' + "".join(
                card(site, a, eager=(is_home_p1 and i == 0)) for i, a in enumerate(rest)
            ) + "</div>"
            body += pager(site, base_path, p, pages)
            jsonld = None
            if key == "home" and p == 1:
                jsonld = [{"@context": "https://schema.org", "@type": "WebSite",
                           "name": cfg["site_name"], "url": site.abs_("/"),
                           "description": cfg["description"], "inLanguage": cfg["lang"],
                           "publisher": org_ld(site)}]
            elif key != "home":
                cat = cfg["categories"][key]
                crumbs = [(ui["home"], site.abs_("/")), (cat["label"], site.abs_(site.cat_path(key)))]
                # Pillars are included in page 1's ItemList too, since they're
                # genuinely part of this category even though pinned outside
                # the normal chunked pagination.
                schema_items = (pillars + chunk) if p == 1 else chunk
                item_list = {"@type": "ItemList", "itemListElement": [
                    {"@type": "ListItem", "position": i + 1, "url": site.abs_(site.article_path(a))}
                    for i, a in enumerate(schema_items)
                ]}
                jsonld = [
                    breadcrumb_ld(site, crumbs),
                    {"@context": "https://schema.org", "@type": "CollectionPage",
                     "name": f'{cat["label"]} · {cfg["site_name"]}',
                     "description": intro or desc, "url": site.abs_(site.cat_path(key)),
                     "isPartOf": {"@type": "WebSite", "name": cfg["site_name"], "url": site.abs_("/")},
                     "inLanguage": cfg["lang"], "mainEntity": item_list}
                ]
            path = base_path if p == 1 else f'{base_path}page/{p}/'
            out = DIST / path.strip("/") / "index.html" if path != "/" else DIST / "index.html"
            write(out, base_page(site, title=title if p == 1 else f'{title} · {ui["page"]} {p}',
                                 description=desc, path=path, body=body, jsonld=jsonld,
                                 noindex=(p > 1), is_home=(key == "home" and p == 1)))


def render_inline_links(text: str) -> str:
    """Support a minimal [label](url) markdown link syntax within paragraph
    text, for citing official/clinical sources inline — e.g. linking to a
    health ministry's immunization portal from a health article. Everything
    outside the recognized syntax is still fully HTML-escaped; text with no
    such syntax renders identically to plain esc(text)."""
    parts = []
    last = 0
    for m in re.finditer(r'\[([^\]]+)\]\((https?://[^\s)]+)\)', text):
        parts.append(esc(text[last:m.start()]))
        label, url = m.group(1), m.group(2)
        parts.append(f'<a href="{esc(url)}" target="_blank" rel="noopener">{esc(label)}</a>')
        last = m.end()
    parts.append(esc(text[last:]))
    return "".join(parts)


def render_article_body(body: str) -> str:
    """Render article body text into HTML paragraphs, with optional light
    heading support: a block (separated by a blank line, same as any other
    paragraph) that starts with '## ' or '### ' renders as <h2>/<h3> instead
    of <p>. Plain paragraph blocks with no such marker render exactly as
    before — fully backward-compatible with existing short-form articles
    that don't use headings at all. Paragraph text also supports an inline
    [label](url) link, e.g. for citing an official/clinical source."""
    parts = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        if block.startswith("### "):
            parts.append(f"<h3>{esc(block[4:].strip())}</h3>")
        elif block.startswith("## "):
            parts.append(f"<h2>{esc(block[3:].strip())}</h2>")
        else:
            parts.append(f"<p>{render_inline_links(block)}</p>")
    return "".join(parts)


def apply_cat_unlock(paras_html: str, unlock: dict) -> str:
    """Wrap the first mention of each cat name in the article with a tappable
    span. Tapping all of them (any order) reveals the hidden poem below."""
    for name in unlock.get("names", []):
        pattern = re.compile(rf'(?<![\w-])({re.escape(name)})(?![\w-])')
        paras_html = pattern.sub(
            lambda m, n=name: f'<span class="cat-name" data-cat="{esc(n)}" role="button" tabindex="0">{m.group(1)}</span>',
            paras_html, count=1,
        )
    return paras_html


def cat_unlock_block(unlock: dict) -> str:
    stanzas = "".join(
        f"<p>{'<br>'.join(esc(line) for line in stanza.split(chr(10)) if line.strip())}</p>"
        for stanza in unlock["poem"].split("\n\n") if stanza.strip()
    )
    return f"""<div class="cat-poem" id="cat-poem" hidden>{stanzas}</div>
<script>
(function(){{
  var found = new Set();
  var total = {len(unlock.get("names", []))};
  var poem = document.getElementById('cat-poem');
  document.querySelectorAll('.cat-name').forEach(function(s){{
    function reveal(){{
      found.add(s.dataset.cat);
      s.classList.add('found');
      if (found.size >= total && poem) {{
        poem.hidden = false;
        poem.scrollIntoView({{behavior:'smooth', block:'center'}});
      }}
    }}
    s.addEventListener('click', reveal);
    s.addEventListener('keydown', function(e){{ if (e.key==='Enter'||e.key===' ') reveal(); }});
  }});
}})();
</script>"""


def build_tags(site) -> set:
    """Generate /tag/{slug}/ archive pages for tags with at least
    MIN_TAG_ARTICLES articles (paginated identically to category pages;
    page 1 is indexable, page 2+ is noindex and excluded from sitemap.xml,
    same convention as categories). Returns the set of slugs that got a
    page, so build_articles() knows which hashtags to render as links vs.
    plain text."""
    cfg, ui = site.cfg, site.cfg["ui"]
    idx = build_tag_index(site.articles, site.cfg.get("tag_aliases", {}))
    qualifying = {slug: data for slug, data in idx.items()
                  if len(data["articles"]) >= MIN_TAG_ARTICLES}
    for slug, data in qualifying.items():
        arts, display = data["articles"], data["display"]
        base_path = site.tag_path(slug)
        pages = max(1, -(-len(arts) // PAGE_SIZE))
        for p in range(1, pages + 1):
            chunk = arts[(p - 1) * PAGE_SIZE: p * PAGE_SIZE]
            title = f'#{display} · {cfg["site_name"]}'
            body = f'<div class="sec"><h1>#{esc(display)}</h1><span class="rule"></span></div>'
            body += '<div class="grid">' + "".join(card(site, a) for a in chunk) + "</div>"
            body += pager(site, base_path, p, pages)
            jsonld = None
            if p == 1:
                crumbs = [(ui["home"], site.abs_("/")), (f'#{display}', site.abs_(base_path))]
                item_list = {"@type": "ItemList", "itemListElement": [
                    {"@type": "ListItem", "position": i + 1, "url": site.abs_(site.article_path(a))}
                    for i, a in enumerate(chunk)
                ]}
                jsonld = [
                    breadcrumb_ld(site, crumbs),
                    {"@context": "https://schema.org", "@type": "CollectionPage",
                     "name": title, "url": site.abs_(base_path), "inLanguage": cfg["lang"],
                     "mainEntity": item_list}
                ]
            path = base_path if p == 1 else f'{base_path}page/{p}/'
            out = DIST / path.strip("/") / "index.html"
            write(out, base_page(
                site,
                title=title if p == 1 else f'{title} · {ui["page"]} {p}',
                description=f'{ui.get("tags", "Tags")}: #{display}',
                path=path, body=body, jsonld=jsonld, noindex=(p > 1)))
    return set(qualifying.keys())


def build_articles(site, linked_tags: set) -> None:
    cfg, ui = site.cfg, site.cfg["ui"]
    for a in site.articles:
        cat = cfg["categories"][a["category"]]
        paras = render_article_body(a["body"])
        cat_unlock = a.get("cat_unlock")
        if cat_unlock:
            paras = apply_cat_unlock(paras, cat_unlock)
        related = [r for r in site.articles if r["category"] == a["category"] and r["slug"] != a["slug"]][:3]
        if len(related) < 3:
            seen = {r["slug"] for r in related} | {a["slug"]}
            related += [r for r in site.articles if r["slug"] not in seen][: 3 - len(related)]
        rel_html = ""
        if related:
            rel_html = (f'<div class="sec"><h2>{esc(ui["more_good"])}</h2><span class="rule"></span></div>'
                        '<div class="grid">' + "".join(card(site, r) for r in related) + "</div>")
        tags = "".join(
            (f'<a class="tag" href="{site.u(site.tag_path(tag_slug(t, cfg.get("tag_aliases", {}))))}">#{esc(t)}</a>'
             if tag_slug(t, cfg.get("tag_aliases", {})) in linked_tags
             else f'<span class="tag">#{esc(t)}</span>')
            for t in a.get("tags", []))
        src = ""
        if a.get("source_url"):
            src = (f'<aside class="srcbox"><strong>{esc(ui["source"])}:</strong> '
                   f'<a href="{esc(a["source_url"])}" target="_blank" rel="noopener">{esc(a["source_name"])}</a>'
                   f'<p class="ainote">{esc(ui["ai_note"])}</p></aside>')
        quick_facts = [f for f in (a.get("quick_facts") or []) if f][:5]
        quick_facts_html = ""
        if quick_facts:
            items = "".join(f"<li>{esc(f)}</li>" for f in quick_facts)
            quick_facts_html = (f'<ul class="quick-facts" '
                                 f'aria-label="{esc(ui.get("quick_facts_label", "Quick facts"))}">{items}</ul>')
        body = f"""<article class="article">
<a class="backlink" href="{site.u('/')}">← {esc(ui['back_home'])}</a>
{meta_row(site, a)}
<h1>{esc(a['headline'])}</h1>
<span class="byline">{esc(ui.get('byline_label', 'Compiled by'))} {esc(cfg.get('byline_name', cfg['site_name'] + ' AI'))} · <a href="{site.u('/' + cfg['about_path'] + '/#editorial-process')}">{esc(ui.get('how_it_works', 'How this works'))}</a></span>
{f'<span class="ai-badge">{esc(ui.get("ai_badge", "AI-summarized"))}</span>' if not a.get('no_ai_badge') else ''}
{quick_facts_html}
<div class="banner">{media(cfg, a, ui, height=250, eager=True, sizes="(max-width: 760px) 100vw, 720px")}</div>
<div class="body">{paras}</div>
{f'<div class="tags">{tags}</div>' if tags else ''}
{src}
{cat_unlock_block(cat_unlock) if cat_unlock else ''}
</article>
{rel_html}"""
        path = site.article_path(a)
        crumbs = [(ui["home"], site.abs_("/")), (cat["label"], site.abs_(site.cat_path(a["category"]))),
                  (a["headline"], site.abs_(path))]
        rich_image = pexels_resize(a["photo_url"], 1200) if a.get("photo_url") else site.abs_("/assets/og-default.png")
        ld = {"@context": "https://schema.org", "@type": "NewsArticle",
              "headline": a["headline"], "description": a["meta_description"],
              "datePublished": a["published"], "dateModified": a.get("rewritten") or a.get("updated", a["published"]),
              "inLanguage": cfg["lang"], "articleSection": cat["label"],
              "mainEntityOfPage": site.abs_(path),
              "image": [rich_image],
              "author": author_ld(site), "publisher": org_ld(site)}
        if a.get("source_url"):
            ld["isBasedOn"] = a["source_url"]
        write(DIST / path.strip("/") / "index.html",
              base_page(site, title=f'{a["headline"]} · {cfg["site_name"]}',
                        description=a["meta_description"] or a["summary_short"],
                        path=path, body=body, jsonld=[ld, breadcrumb_ld(site, crumbs)], og_type="article",
                        og_image=rich_image))


ABOUT = {
    "bg": [
        ("Защо съществуваме",
         "Отвориш ли новините, светът изглежда черен: катастрофи, скандали, войни, поскъпване. Но това е само половината истина. Всеки ден в България лекари спасяват животи, доброволци садят гори, деца печелят олимпиади, съседи си помагат. {site} събира точно тези истории — само тях."),
        ("Как избираме новините",
         "Наш AI редактор чете водещите български медии няколко пъти дневно и подбира единствено истински добрите новини: конкретни хубави събития, без трагедии „с позитивен привкус“, без политически битки, без криминални хроники. После написва кратко, човешко резюме на български."),
        ("Прозрачност",
         "Всяко резюме е написано от изкуствен интелект по информация от посочения източник и никога не добавя измислени факти. Под всяка новина стои връзка към оригиналната публикация — препоръчваме да я отворите за пълната история. Ако забележите грешка, пишете ни и ще я поправим."),
        ("Свържи се с нас",
         "Знаеш за добра новина, която сме пропуснали? Пиши ни на {email} — най-хубавите истории често идват от читатели."),
    ],
    "en": [
        ("Why we exist",
         "Open any news site and the world looks dark: crashes, scandals, wars, prices. But that is only half the truth. Every single day, somewhere on this planet, a species comes back from the brink, a disease loses ground, a stranger helps a stranger. {site} collects exactly those stories — and only those."),
        ("How stories are chosen",
         "Our AI editor reads trusted international sources several times a day and selects only genuinely good news: concrete positive outcomes, no tragedies dressed up with a silver lining, no partisan politics, no crime. It then writes a short, human summary in plain English."),
        ("Transparency",
         "Every summary is written by an AI from the linked source's reporting and never adds invented facts. Each story credits and links the original publication — we encourage you to read it in full. Spot an error? Tell us and we will fix it."),
        ("Get in touch",
         "Know a good story we missed? Write to {email} — the best finds often come from readers."),
    ],
}


def build_about(site) -> None:
    cfg = site.cfg
    secs = "".join(
        f'<h2>{esc(h)}</h2><p>{esc(t.format(site=cfg["site_name"], email=cfg["contact_email"]))}</p>'
        for h, t in ABOUT[cfg["lang"]])
    editor = (f'<div class="editor-card" id="editorial-process">'
              f'<div class="editor-title">{esc(cfg["ui"].get("editorial_process_label", "Editorial process"))}</div>'
              f'<p>{esc(cfg.get("editorial_process_note", ""))}</p></div>')
    body = f'<div class="about"><h1>{esc(cfg["ui"]["about"])} · {esc(cfg["site_name"])}</h1>{editor}{secs}</div>'
    path = f'/{cfg["about_path"]}/'
    jsonld = [{"@context": "https://schema.org", "@type": "AboutPage",
               "name": f'{cfg["ui"]["about"]} · {cfg["site_name"]}',
               "url": site.abs_(path),
               "description": cfg.get("editorial_process_note", cfg["description"]),
               "mainEntity": org_ld(site)}]
    write(DIST / cfg["about_path"] / "index.html",
          base_page(site, title=f'{cfg["ui"]["about"]} · {cfg["site_name"]}',
                    description=cfg["description"], path=path, body=body, jsonld=jsonld))


PRIVACY = {
    "bg": [
        ("Какво обхваща тази политика",
         "Тази страница обяснява какви данни се събират, когато четете {site}, и с какви инструменти на трети страни (Google Анализ, Google реклами) работим. Не изискваме регистрация и не събираме лични данни за създаване на профил."),
        ("Каква информация се събира",
         "Хостинг доставчикът записва стандартни технически логове (IP адрес, браузър, посетена страница) за всеки сайт в интернет. Ако сте дали съгласие през банера за бисквитки, Google Анализ събира обобщена, анонимизирана статистика за посещенията, а Google реклами може да показва реклами въз основа на бисквитки. Без съгласие тези инструменти не записват нищо, свързано с вас."),
        ("Бисквитки и съгласие",
         "При първо посещение виждате банер, който ви пита дали приемате бисквитки за анализ и реклами. Можете да откажете също толкова лесно, колкото да приемете. По всяко време можете да промените избора си, като изтриете бисквитките на сайта през настройките на браузъра си."),
        ("Вашите права",
         "Съгласно GDPR имате право на достъп, поправка, изтриване и възражение срещу обработката на данните ви. Тъй като не поддържаме профили или бази с лични данни отвъд анонимна статистика, повечето заявки се удовлетворяват автоматично чрез изтриване на бисквитките. За въпроси пишете ни на {email}."),
        ("Трети страни",
         "Google Анализ и Google реклами обработват данни съгласно собствените си политики за поверителност, достъпни на policies.google.com/privacy. Не споделяме данни с други трети страни."),
        ("Промени",
         "Тази политика може да се актуализира при нужда — например когато добавим нов инструмент. Датата на последната промяна винаги ще е видима тук."),
    ],
    "en": [
        ("What this policy covers",
         "This page explains what data is collected when you read {site}, and which third-party tools (Google Analytics, Google ads) we use. No account or sign-up is required, and we don't build personal profiles."),
        ("What information is collected",
         "Our hosting provider logs standard technical data (IP address, browser, page visited) for every website on the internet. If you accept the cookie banner, Google Analytics collects aggregated, anonymized visit statistics, and Google ads may show ads based on cookies. Without consent, neither tool records anything tied to you."),
        ("Cookies and consent",
         "On your first visit you'll see a banner asking whether you accept analytics and advertising cookies. Rejecting is exactly as easy as accepting. You can change your choice at any time by clearing this site's cookies in your browser settings."),
        ("Your rights",
         "Under GDPR you have the right to access, correct, delete, and object to processing of your data. Since we don't maintain accounts or personal databases beyond anonymized statistics, most requests are satisfied simply by clearing your cookies. For questions, write to {email}."),
        ("Third parties",
         "Google Analytics and Google ads process data under their own privacy policies, available at policies.google.com/privacy. We do not share data with any other third party."),
        ("Changes",
         "This policy may be updated as needed — for example, when we add a new tool. The date of the last change will always be visible here."),
    ],
}


def build_privacy(site) -> None:
    cfg = site.cfg
    secs = "".join(
        f'<h2>{esc(h)}</h2><p>{esc(t.format(site=cfg["site_name"], email=cfg["contact_email"]))}</p>'
        for h, t in PRIVACY[cfg["lang"]])
    updated = ("Последна промяна" if cfg["lang"] == "bg" else "Last updated") + \
        f': {datetime.now(timezone.utc).strftime("%Y-%m-%d")}'
    body = (f'<div class="about"><h1>{esc(cfg["ui"]["privacy"])} · {esc(cfg["site_name"])}</h1>'
            f'{secs}<p class="fine">{esc(updated)}</p></div>')
    path = f'/{cfg["privacy_path"]}/'
    write(DIST / cfg["privacy_path"] / "index.html",
          base_page(site, title=f'{cfg["ui"]["privacy"]} · {cfg["site_name"]}',
                    description=cfg["description"], path=path, body=body))


def build_404(site) -> None:
    ui = site.cfg["ui"]
    body = (f'<div class="nf"><div class="big">🌤</div><h1>{esc(ui["not_found_title"])}</h1>'
            f'<p>{esc(ui["not_found_text"])}</p><p><a class="btn" href="{site.u("/")}">{esc(ui["back_home"])}</a></p></div>')
    write(DIST / "404.html", base_page(site, title=f'404 · {site.cfg["site_name"]}',
                                       description=ui["not_found_text"], path="/404.html",
                                       body=body, noindex=True))


def build_feed(site) -> None:
    cfg = site.cfg
    items = ""
    for a in site.articles[:30]:
        items += f"""<item>
<title>{esc(a['headline'])}</title>
<link>{site.abs_(site.article_path(a))}</link>
<guid isPermaLink="true">{site.abs_(site.article_path(a))}</guid>
<pubDate>{format_datetime(a['_dt'])}</pubDate>
<category>{esc(cfg['categories'][a['category']]['label'])}</category>
<description>{esc(a['summary_short'])}</description>
</item>"""
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>{esc(cfg['site_name'])}</title>
<link>{site.abs_('/')}</link>
<description>{esc(cfg['description'])}</description>
<language>{cfg['lang']}</language>
<lastBuildDate>{format_datetime(datetime.now(timezone.utc))}</lastBuildDate>
{items}
</channel></rss>"""
    write(DIST / "feed.xml", feed)


def build_news_sitemap(site) -> None:
    """Publish news-sitemap.xml per Google's News Sitemap protocol — only
    articles published within the last 48 hours. Google explicitly requires
    removing older entries; keeping stale URLs in reduces the sitemap's
    trustworthiness rather than just being harmlessly ignored. Worth having
    given this pipeline publishes 10-40 articles/day. Referenced via a
    second Sitemap: line in robots.txt (multiple Sitemap: directives are
    valid per the Sitemaps protocol)."""
    cfg = site.cfg
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    recent = [a for a in site.articles if a["_dt"] >= cutoff]
    items = ""
    for a in recent:
        pub_date = a["_dt"].strftime("%Y-%m-%dT%H:%M:%S+00:00")
        items += (f"<url><loc>{esc(site.abs_(site.article_path(a)))}</loc>"
                  f"<news:news><news:publication>"
                  f"<news:name>{esc(cfg['site_name'])}</news:name>"
                  f"<news:language>{esc(cfg['lang'])}</news:language>"
                  f"</news:publication>"
                  f"<news:publication_date>{pub_date}</news:publication_date>"
                  f"<news:title>{esc(a['headline'])}</news:title>"
                  f"</news:news></url>")
    write(DIST / "news-sitemap.xml",
          f'<?xml version="1.0" encoding="UTF-8"?>'
          f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
          f'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">{items}</urlset>')


def build_rsl(site) -> None:
    """Publish a minimal RSL 1.0 license (rslstandard.org/rsl) making the
    site's existing, actual stance machine-readable: robots.txt already
    allows all major AI crawlers (GPTBot, ClaudeBot, PerplexityBot,
    Google-Extended, CCBot) and llms.txt already discloses AI involvement —
    so this permits AI/search use broadly, conditioned on attribution,
    rather than inventing a new policy."""
    cfg = site.cfg
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rsl xmlns="https://rslstandard.org/rsl">
  <content url="/">
    <license>
      <permits type="usage">all</permits>
      <payment type="attribution">
        <standard>https://creativecommons.org/licenses/by/4.0/</standard>
      </payment>
    </license>
    <copyright type="organization" contactEmail="{esc(cfg['contact_email'])}">{esc(cfg['site_name'])}</copyright>
    <terms>{esc(site.abs_(f'/{cfg["about_path"]}/'))}</terms>
  </content>
</rsl>
"""
    write(DIST / "rsl.xml", xml)


def build_sitemap(site, tag_slugs: set) -> None:
    cfg = site.cfg
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    urls = [(site.abs_("/"), now), (site.abs_(f'/{cfg["about_path"]}/'), now),
            (site.abs_(f'/{cfg["privacy_path"]}/'), now)]
    # Note: page 2+ (home, category, and tag) are intentionally excluded here
    # — they carry noindex and stay reachable only via in-page pagination
    # links, so the sitemap doesn't send Google a mixed noindex-but-submitted
    # signal.
    for cid in cfg["categories"]:
        urls.append((site.abs_(site.cat_path(cid)), now))
    for slug in sorted(tag_slugs):
        urls.append((site.abs_(site.tag_path(slug)), now))
    def _lastmod(a):
        return (a.get("updated") or "")[:10] or a["_dt"].strftime("%Y-%m-%d")
    urls += [(site.abs_(site.article_path(a)), _lastmod(a)) for a in site.articles]
    body = "".join(f"<url><loc>{esc(u)}</loc><lastmod>{d}</lastmod></url>" for u, d in urls)
    write(DIST / "sitemap.xml",
          f'<?xml version="1.0" encoding="UTF-8"?>'
          f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>')
    write(DIST / "robots.txt",
          f"License: {site.abs_('/rsl.xml')}\nUser-agent: *\nAllow: /\n\n"
          f"Sitemap: {site.abs_('/sitemap.xml')}\n"
          f"Sitemap: {site.abs_('/news-sitemap.xml')}\n")

    key = cfg.get("indexnow_key", "")
    if key:
        write(DIST / f"{key}.txt", key)


# --------------------------------------------------------------- main -----

def build_llms_txt(site) -> None:
    cfg = site.cfg
    recent = "\n".join(f'- {a["headline"]}: {site.abs_(site.article_path(a))}' for a in site.articles[:15])
    txt = f"""# {cfg['site_name']}

> {cfg['tagline']}

{cfg['description']}

{cfg['site_name']} is an independently published, AI-assisted good-news site.
Every article is an original summary written from a single credited source,
never invented, always linked. See {site.abs_('/' + cfg['about_path'] + '/')} for
the full editorial policy and AI-disclosure statement.

## Recent articles
{recent}

## Feeds
- Sitemap: {site.abs_('/sitemap.xml')}
- RSS: {site.abs_('/feed.xml')}
"""
    write(DIST / "llms.txt", txt)


def main() -> None:
    cfg = load_config()
    articles = load_articles(cfg)
    site = Site(cfg, articles)

    if DIST.exists():
        shutil.rmtree(DIST)
    (DIST / "assets").mkdir(parents=True)

    def font_faces_css(cfg) -> str:
        rules = []
        for face in cfg["fonts"].get("faces", []):
            rules.append(
                f"@font-face{{font-family:'{face['family']}';font-style:normal;"
                f"font-weight:{face['weight']};font-display:swap;"
                f"src:url('{face['file']}') format('woff2');}}"
            )
        return "\n".join(rules)

    css_tokens = {**cfg["colors"],
                  "font_display": cfg["fonts"]["display"],
                  "font_body": cfg["fonts"]["body"],
                  "font_label": cfg["fonts"]["label"],
                  "font_faces": font_faces_css(cfg)}
    write(DIST / "assets" / "style.css", CSS.substitute(css_tokens))

    if ASSETS_SRC.exists():
        for f in ASSETS_SRC.iterdir():
            if f.is_dir():
                shutil.copytree(f, DIST / "assets" / f.name, dirs_exist_ok=True)
            else:
                shutil.copy(f, DIST / "assets" / f.name)

    build_lists(site)
    qualifying_tag_slugs = build_tags(site)
    build_articles(site, qualifying_tag_slugs)
    build_about(site)
    build_privacy(site)
    build_404(site)
    build_feed(site)
    build_sitemap(site, qualifying_tag_slugs)
    build_news_sitemap(site)
    build_rsl(site)
    build_llms_txt(site)
    print(f"[{cfg['site_name']}] built {len(articles)} articles → {DIST}")


if __name__ == "__main__":
    main()

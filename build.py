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
MIN_TAG_ARTICLES = 10  # a /tag/{slug}/ archive page is only built once a tag
                      # has at least this many articles — below that, the
                      # hashtag stays plain text rather than linking to a
                      # thin, near-empty page.
MIN_CITY_ARTICLES = 3  # city hubs are curated local landing pages with real
                       # editorial value (and a CTA), so they earn a page
                       # earlier than a generic topic tag does. Cities are
                       # served ONLY by /{cities_path}/{slug}/ — never also by
                       # a /tag/{slug}/ archive, which would put two
                       # overlapping pages in competition for the same query.

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

/* masthead.
   NOTE: the header carries BOTH .masthead and .wrap. A `padding` *shorthand*
   here would silently reset .wrap's `padding:0 22px` to 0 on the left/right
   (same specificity, and .masthead is declared later), gluing the logo mark
   and the search button to the screen edges — very visible on phones, where
   there is no leftover max-width margin to hide it. Longhands only. */
.masthead{padding-top:26px;padding-bottom:10px;
padding-left:max(22px,env(safe-area-inset-left));
padding-right:max(22px,env(safe-area-inset-right))}
.mast-row{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.mark{flex:0 0 auto;display:grid;place-items:center}
.brand h1,.brand .h1{font-family:var(--fd);font-weight:800;font-size:1.9rem;margin:0;letter-spacing:-.02em;line-height:1}
.brand p{margin:3px 0 0;color:var(--muted);font-size:.95rem}
.today{font-family:var(--fl);text-transform:uppercase;letter-spacing:.14em;
font-size:.72rem;color:var(--muted);border:1px solid var(--line);border-radius:999px;
padding:7px 14px;background:var(--card);flex:0 0 auto}
.search-form{display:flex;align-items:center;gap:6px;margin-left:auto;max-width:260px;flex:1 1 200px}
.search-form input{flex:1;min-width:0;min-height:44px;padding:8px 14px;border-radius:999px;border:1.5px solid var(--line);
background:var(--card);color:var(--ink);font-family:var(--fb);font-size:.9rem;box-sizing:border-box}
.search-form input:focus{outline:none;border-color:var(--p)}
.search-form button{flex:0 0 auto;min-width:44px;min-height:44px;padding:8px 12px;border-radius:999px;border:1.5px solid var(--line);
background:var(--card);cursor:pointer;font-size:1rem;line-height:1}
.search-form button:hover{border-color:var(--p)}
/* Category nav.
   The fade was previously a permanent mask on nav.cats, cutting the last 8%
   of the strip at all times — so the final chip looked chopped in half even
   when the row was fully scrolled and there was nothing more to see. The fade
   now lives only on the .cats-more overlay, which the script below hides once
   the end is reached. */
nav.cats{display:flex;gap:8px;overflow-x:auto;padding:16px 0 6px;scrollbar-width:none;
position:relative}
nav.cats::-webkit-scrollbar{display:none}
.cats-wrap{position:relative}
.cats-more{position:absolute;right:-2px;top:0;bottom:6px;display:flex;align-items:center;
pointer-events:none;font-size:1.4rem;font-weight:900;color:var(--p);
background:linear-gradient(90deg,transparent,var(--bg) 62%);padding:0 4px 0 34px;
transition:opacity .2s}
.cats-wrap.at-end .cats-more{opacity:0}
/* On a wide screen nothing should be hidden at all: let the chips wrap onto a
   second line instead of hiding items behind a horizontal scroll a mouse user
   may never think to try. Scrolling stays on narrow screens, where it is the
   expected gesture. */
@media(min-width:940px){
nav.cats{flex-wrap:wrap;overflow-x:visible}
.cats-more{display:none}
}
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
.updated-badge{color:var(--t);font-weight:700}

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
.submit-cta{margin:18px 0;padding:14px 16px;border:1px dashed var(--line,#d8d8d8);border-radius:10px;font-size:.95rem;line-height:1.5}
.submit-cta a{font-weight:600}
.photo-credit{position:absolute;right:6px;bottom:5px;font-family:var(--fl);font-size:.66rem;
color:#fff;background:rgba(0,0,0,.45);padding:2px 7px;border-radius:999px;text-decoration:none}
.ai-credit{position:absolute;right:6px;bottom:5px;font-family:var(--fl);font-size:.66rem;
color:#fff;background:rgba(0,0,0,.45);padding:2px 7px;border-radius:999px;
opacity:0;transition:opacity .2s;pointer-events:none}
.thumb:hover .ai-credit,.thumb:focus-within .ai-credit{opacity:1}
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
.share-row{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:24px 0;padding:16px 0;
border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.share-label{font-family:var(--fl);font-weight:700;font-size:.9rem;color:var(--muted)}
.share-icon,.share-native-btn{font-family:var(--fl);font-size:.83rem;font-weight:700;
min-height:44px;padding:0 16px;display:inline-flex;align-items:center;border-radius:999px;
border:1.5px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer;
text-decoration:none;transition:border-color .15s,transform .15s}
.share-icon:hover,.share-native-btn:hover{border-color:var(--p);transform:translateY(-1px)}
.share-native-btn{background:var(--ink);color:var(--card);border-color:var(--ink)}
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
.pager{display:flex;flex-wrap:wrap;justify-content:center;align-items:center;gap:8px;margin:8px 0 14px;font-family:var(--fl);font-weight:700}
.pager a,.pager span{padding:8px 14px;border-radius:999px;border:1.5px solid var(--line);background:var(--card);min-width:40px;text-align:center}
.pager a.pager-nav,.pager span.pager-nav{padding:8px 16px;min-width:auto}
.pager a:hover{border-color:var(--p)}
.pager .cur{background:var(--ink);color:var(--card);border-color:var(--ink)}
.pager-ellipsis{border:none!important;background:none!important;color:var(--muted);padding:8px 2px!important;min-width:auto!important}
.pager-jump{display:flex;justify-content:center;gap:8px;margin:0 0 40px}
.pager-jump input{width:84px;padding:8px 12px;border-radius:999px;border:1.5px solid var(--line);
background:var(--card);color:var(--ink);font-family:var(--fl);text-align:center}
.pager-jump button{padding:8px 16px;border-radius:999px;border:1.5px solid var(--line);
background:var(--card);font-family:var(--fl);font-weight:700;cursor:pointer}
.pager-jump button:hover{border-color:var(--p)}
.digest-list{display:flex;flex-direction:column;gap:18px;margin:20px 0}
.digest-item{padding-bottom:16px;border-bottom:1px solid var(--line)}
.digest-item:last-child{border-bottom:none}
.digest-item h3{font-family:var(--fd);font-weight:800;font-size:1.15rem;margin:0 0 6px;letter-spacing:-.01em}
.digest-item p{margin:0;color:var(--muted);font-size:.98rem}
.city-grid{display:flex;flex-wrap:wrap;gap:10px;margin:20px 0}
.city-chip{display:inline-flex;align-items:center;gap:8px;font-family:var(--fl);font-weight:700;
font-size:.95rem;padding:10px 16px;border-radius:999px;border:1.5px solid var(--line);
background:var(--card);transition:border-color .15s}
.city-chip:hover{border-color:var(--p)}
.city-count{font-size:.78rem;color:var(--muted);background:var(--bg);border-radius:999px;padding:2px 8px}
footer{border-top:1px solid var(--line);margin-top:20px;padding:30px 0 40px;background:var(--card)}
footer .mission{max-width:56ch;color:var(--muted);margin:8px 0 16px}
footer .fnav{display:flex;gap:18px;flex-wrap:wrap;font-family:var(--fl);font-weight:700;font-size:.9rem}
footer .fine{color:var(--muted);font-size:.8rem;margin-top:18px}
.about{max-width:720px;margin:10px auto 44px}
.about h1{font-family:var(--fd);font-weight:800;font-size:2.1rem;letter-spacing:-.02em}
.search-page{max-width:820px;margin:10px auto 20px}
.search-page h1{font-family:var(--fd);font-weight:800;font-size:2.1rem;letter-spacing:-.02em;margin:0 0 18px}
.search-form-main{margin:0 0 20px}
.search-form-main input{width:100%;padding:14px 20px;border-radius:999px;border:1.5px solid var(--line);
background:var(--card);color:var(--ink);font-family:var(--fb);font-size:1.05rem}
.search-form-main input:focus{outline:none;border-color:var(--p)}
.search-status{color:var(--muted);font-family:var(--fl);font-size:.9rem;margin:0 0 16px}
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
 .search-form{max-width:none;flex:1 1 100%;margin-left:0}
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

/* growth: submit chip, newsletter CTA, city hubs, landing pages */
.growth-submit-chip{border-color:var(--p)!important;background:color-mix(in srgb,var(--p) 14%,var(--card))!important}
.growth-cta-box{display:grid;grid-template-columns:auto 1fr;gap:18px;align-items:center;margin:34px 0;
padding:24px;border:1.5px solid var(--line);background:var(--card);border-radius:var(--r);
box-shadow:0 8px 30px rgba(30,50,64,.06)}
.growth-cta-icon{font-size:2.4rem;line-height:1}
.growth-cta-box h2{font-family:var(--fd);font-weight:800;letter-spacing:-.02em;margin:0 0 6px;font-size:1.35rem}
.growth-cta-box p{margin:0 0 14px;color:var(--muted)}
.growth-btn{display:inline-block;border:0;border-radius:999px;padding:11px 18px;background:var(--p);
color:var(--ink);font-family:var(--fl);font-weight:800;cursor:pointer}
.growth-btn:hover{filter:brightness(.96)}
.growth-kicker{font-family:var(--fl);font-size:.8rem;font-weight:800;letter-spacing:.07em;
color:var(--pd);margin-bottom:8px}
.growth-lead{font-size:1.08rem;color:var(--muted)}
.growth-feature-list{display:grid;gap:8px;margin:20px 0;padding:18px;border-radius:var(--r);
background:var(--card);border:1px solid var(--line)}
.growth-form{display:grid;gap:15px;margin:24px 0}
.growth-form label{display:grid;gap:6px;font-family:var(--fl);font-weight:700}
.growth-form input,.growth-form textarea{width:100%;box-sizing:border-box;border:1.5px solid var(--line);
border-radius:14px;padding:12px 14px;background:var(--card);color:var(--ink);font:inherit}
.growth-form input:focus,.growth-form textarea:focus{outline:none;border-color:var(--p)}
.growth-small{font-size:.88rem;color:var(--muted)}
.growth-city-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:22px 0}
.growth-city-card{display:flex;flex-direction:column;gap:4px;padding:17px 18px;border-radius:var(--r);
background:var(--card);border:1.5px solid var(--line)}
.growth-city-card:hover{border-color:var(--p)}
.growth-city-card strong{font-family:var(--fd);font-size:1.1rem}
.growth-city-card span{font-size:.88rem;color:var(--muted)}
.growth-mini-cta{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;margin:26px 0;
padding:17px 18px;border-radius:var(--r);background:color-mix(in srgb,var(--p) 12%,var(--card));
border:1px solid var(--line)}
.growth-mini-cta a{font-weight:800;color:var(--pd);text-decoration:underline;text-underline-offset:3px}
@media(max-width:640px){.growth-cta-box{grid-template-columns:1fr}.growth-city-grid{grid-template-columns:1fr}}

/* promise banner: tells a first-time visitor what this site IS */
.promise{display:flex;gap:12px;align-items:center;margin:0 0 22px;padding:12px 16px;
border-radius:999px;background:color-mix(in srgb,var(--p) 16%,var(--card));
border:1px solid var(--line);font-family:var(--fl);font-size:.94rem;line-height:1.4}
.promise-icon{font-size:1.3rem;line-height:1;flex:0 0 auto}
.promise strong{font-weight:800}
.promise a{color:var(--pd);text-decoration:underline;text-underline-offset:3px;white-space:nowrap}
@media(max-width:560px){.promise{border-radius:var(--r);font-size:.88rem}}

/* next story: ONE strong follow-on, not a grid of five */
.nextup{display:block;margin:34px 0 10px;border:1.5px solid var(--line);border-radius:var(--r);
overflow:hidden;background:var(--card);box-shadow:0 8px 30px rgba(30,50,64,.06)}
.nextup:hover{border-color:var(--p)}
.nextup-img{width:100%;height:230px;object-fit:cover;display:block}
.nextup-body{padding:18px 20px 20px}
.nextup-kicker{font-family:var(--fl);font-size:.76rem;font-weight:800;letter-spacing:.08em;
text-transform:uppercase;color:var(--pd);margin-bottom:7px}
.nextup h3{font-family:var(--fd);font-weight:800;letter-spacing:-.02em;margin:0 0 6px;
font-size:1.32rem;line-height:1.2}
.nextup p{margin:0;color:var(--muted);font-size:.95rem}
/* newsletter form */
.nl-form{display:flex;gap:10px;flex-wrap:wrap;margin-top:6px}
.nl-form input[type=email]{flex:1 1 220px;min-width:0;border:1.5px solid var(--line);
border-radius:999px;padding:12px 18px;background:var(--card);color:var(--ink);font:inherit}
.nl-form input[type=email]:focus{outline:none;border-color:var(--p)}
.nl-note{margin-top:10px;font-size:.84rem;color:var(--muted)}
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


def build_share_row(cfg, ui, slug: str, url: str, headline: str) -> str:
    """A real share row, not a decorative afterthought — this is one of the
    lowest-effort organic-growth levers available: every reader who likes an
    article can do the promotion the site owner doesn't have to. Facebook,
    Viber, and WhatsApp specifically, since those are the actual dominant
    share channels in Bulgaria, not generic guesses. Works with zero JS via
    real share-intent URLs, then progressively upgrades to the native OS
    share sheet on supported browsers (mostly mobile) — showing whatever
    the reader actually has installed is better than hardcoding icons for
    apps they may not use."""
    import urllib.parse
    enc_url = urllib.parse.quote(url, safe="")
    enc_text = urllib.parse.quote(headline, safe="")
    elem_id = f"share-{slug}"
    label = esc(ui.get("share_label", "Share"))
    copied_label = esc(ui.get("share_copied_label", "Link copied"))
    copy_label = esc(ui.get("share_copy_label", "Copy link"))
    return f"""<div class="share-row" id="{elem_id}">
<span class="share-label">{label}:</span>
<a class="share-icon" href="https://www.facebook.com/sharer/sharer.php?u={enc_url}" target="_blank" rel="noopener" aria-label="Facebook">Facebook</a>
<a class="share-icon" href="viber://forward?text={enc_text}%20{enc_url}" aria-label="Viber">Viber</a>
<a class="share-icon" href="https://wa.me/?text={enc_text}%20{enc_url}" target="_blank" rel="noopener" aria-label="WhatsApp">WhatsApp</a>
<button type="button" class="share-icon share-copy" data-url="{esc(url)}" data-copied="{copied_label}">{copy_label}</button>
</div>
<script>
(function(){{
  var c = document.getElementById('{elem_id}');
  if (!c) return;
  if (navigator.share) {{
    var native = document.createElement('button');
    native.type = 'button';
    native.className = 'share-native-btn';
    native.textContent = '{label}';
    c.innerHTML = '';
    c.appendChild(native);
    native.addEventListener('click', function(){{
      navigator.share({{title: {json.dumps(headline)}, url: {json.dumps(url)}}}).catch(function(){{}});
    }});
    return;
  }}
  var copyBtn = c.querySelector('.share-copy');
  if (copyBtn) {{
    copyBtn.addEventListener('click', function(){{
      navigator.clipboard.writeText(copyBtn.dataset.url).then(function(){{
        var orig = copyBtn.textContent;
        copyBtn.textContent = copyBtn.dataset.copied;
        setTimeout(function(){{ copyBtn.textContent = orig; }}, 2000);
      }});
    }});
  }}
}})();
</script>"""


def media(cfg, article, ui, height=180, eager=False,
          sizes="(max-width: 700px) 100vw, 292px") -> str:
    """Local AI-generated image first, real stock photo second, generated SVG
    art otherwise. Photo credit is a hard requirement of the free Pexels API's
    terms, not optional — AI-generated images get an honest 'AI-generated
    illustration' label instead, consistent with the site's AI-disclosure
    policy elsewhere.
    eager=True skips lazy-loading and adds fetchpriority="high" — use this
    only for the one above-the-fold image per page (the article's own
    banner), never for listing thumbnails.
    `sizes` should describe the actual rendered width in this context so the
    browser can pick the right srcset candidate — pass a wider value for the
    full-width article banner than for grid thumbnails."""
    priority_attr = ' fetchpriority="high"' if eager else ''
    if article.get("image_path"):
        # No srcset variants yet — only one resolution is generated per image
        # currently. A real limitation worth revisiting if this becomes the
        # primary image source rather than a rollout in progress.
        loading_attr = '' if eager else ' loading="lazy"'
        credit = f'<span class="ai-credit">{esc(article.get("image_credit", "AI-generated illustration"))}</span>'
        iw = article.get("image_width") or 1200
        ih = article.get("image_height") or 675
        return (f'<div class="thumb" style="height:{height}px">'
                f'<img src="{esc(article["image_path"])}" width="{iw}" height="{ih}" '
                f'alt="{esc(article["headline"])}"{loading_attr}{priority_attr}>'
                f'{credit}</div>')
    if article.get("photo_url"):
        base_url = article["photo_url"]
        srcset = ", ".join(f"{esc(pexels_resize(base_url, w))} {w}w" for w in (400, 800, 1200))
        credit = (f'<a class="photo-credit" href="{esc(article["photo_credit_url"])}" '
                  f'target="_blank" rel="noopener">{esc(ui.get("photo_by", "Photo:"))} {esc(article["photo_credit"])} · Pexels</a>')
        loading_attr = '' if eager else ' loading="lazy"'
        orig_w, orig_h = article.get("photo_width"), article.get("photo_height")
        dims_attr = f' width="800" height="{round(800 * orig_h / orig_w)}"' if orig_w and orig_h else ''
        return (f'<div class="thumb" style="height:{height}px">'
                f'<img src="{esc(pexels_resize(base_url, 800))}" srcset="{srcset}" sizes="{esc(sizes)}"{dims_attr} '
                f'alt="{esc(article["headline"])}"{loading_attr}{priority_attr}>'
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
    ld = {"@type": "Organization", "name": cfg["site_name"], "url": site.abs_("/"),
          "logo": {"@type": "ImageObject", "url": site.abs_("/assets/apple-touch-icon.png")},
          "foundingDate": cfg.get("founding_date", "2026-07-01"),
          "contactPoint": {"@type": "ContactPoint", "email": cfg["contact_email"],
                            "contactType": "editorial"}}
    same_as = cfg.get("same_as", [])
    if same_as:
        ld["sameAs"] = same_as
    if cfg.get("operator_city"):
        ld["address"] = {"@type": "PostalAddress", "addressLocality": cfg["operator_city"],
                          "addressCountry": cfg.get("operator_country", "BG")}
    if cfg.get("editor_name"):
        ld["founder"] = {"@type": "Person", "name": cfg["editor_name"]}
    return ld


def author_ld(site) -> dict:
    """Article authorship — a real named Person if config.json sets
    'editor_name' (meaning that person genuinely reviews/approves every
    article via the site's real review-PR workflow before it publishes),
    or the honest Organization fallback otherwise. Never a fabricated
    Person with invented credentials — see /<about_path>/#editorial-process
    for the disclosed process. The Person's 'description' explicitly
    discloses AI involvement in structured data itself — not just on the
    visible page — since the on-page byline already says this and the
    schema shouldn't tell a different story than what a reader actually sees."""
    cfg = site.cfg
    editor_name = cfg.get("editor_name", "")
    if editor_name:
        return {"@type": "Person", "name": editor_name,
                "description": "Articles on this site are AI-drafted from a credited source and reviewed by this editor before publication.",
                "url": site.abs_(f'/{cfg["about_path"]}/#editorial-process')}
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
    return f"""<div id="cookie-banner" class="cookie-banner" hidden role="dialog" aria-label="{esc(ui.get('cookie_dialog_label', 'Cookie consent'))}">
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


def truncate_title(title: str, max_len: int = 60) -> str:
    """Keep a rendered <title> near the width search engines display, WITHOUT
    throwing away words.

    The previous version amputated the headline itself — 'Морски орел е
    пациент №2026 в Спасителния це… · Добро Дело' — which is the wrong trade.
    Title length is a DISPLAY limit, not a ranking limit: engines simply cut
    the visible text, but they still read the whole tag. Cutting words out of
    the source removes them from what the page can ever be found for, and on
    a 13-character suffix budget that was costing roughly a quarter of every
    headline, precisely the specific nouns (city names, subjects) a small site
    has any chance of ranking for.

    So: when the full string is too long, drop the ' · SiteName' suffix and
    keep the headline whole. The brand is on the page, in the logo, in the
    schema, and engines routinely append or rewrite site names themselves.
    Only if the bare headline is still absurdly long (>90) is it trimmed, and
    then on a word boundary rather than mid-word."""
    if len(title) <= max_len:
        return title
    main = title.rpartition(" · ")[0] if " · " in title else title
    if len(main) <= 90:
        return main
    cut = main[:89]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:—-") + "…"


# Facebook's crawler rejects WebP og:images and silently substitutes another
# image it finds on the page — observed live on 2026-07-27: article link cards
# showed *sibling articles'* related-thumbnails instead of the declared hero.
# Fix: for locally-hosted .webp heroes, declare a JPEG twin as og:image. The
# twin is encoded straight into dist/ at the end of the build (the repo keeps
# only the WebP — no doubled image storage in git). Humans still get WebP on
# the page; only the og:image URL differs.
OG_JPEG_TWINS: dict = {}  # "/assets/articles/x.webp" -> (width, height)


def og_default_url() -> str:
    """Path to the fallback social image, with a content hash appended.

    Facebook (and LinkedIn, and Slack) cache the image FILE keyed by its URL,
    separately from the page-scrape cache that the Sharing Debugger clears.
    Replacing assets/og-default.png without changing its URL therefore leaves
    every one of them serving the old picture indefinitely — re-scraping the
    page does not help, because the page still points at a URL they believe
    they already have.

    Appending a hash of the file's bytes means the URL changes whenever the
    image does, so a new file is always fetched as a genuinely new image. This
    is the same reasoning behind the cache-busting suffix that
    regenerate_image() puts on article images.
    """
    src = ROOT / "assets" / "og-default.png"
    try:
        digest = hashlib.md5(src.read_bytes()).hexdigest()[:8]
    except OSError:
        return "/assets/og-default.png"
    return f"/assets/og-default.png?v={digest}"


def og_image_for(image_path: str):
    """Return (jpg_rel_path, width, height, mime) for a local WebP hero, or
    (None, None, None, None) when the og:image should stay as-is (non-WebP,
    remote, or unreadable). Registers the source for write_og_jpeg_twins()."""
    if not image_path.endswith(".webp"):
        return None, None, None, None
    src = ROOT / image_path.lstrip("/")
    if not src.exists():
        return None, None, None, None
    try:
        from PIL import Image
        with Image.open(src) as im:
            w, h = im.size
    except Exception:
        return None, None, None, None
    OG_JPEG_TWINS[image_path] = (w, h)
    return image_path[:-len(".webp")] + ".jpg", w, h, "image/jpeg"


def write_og_jpeg_twins() -> None:
    """Encode every registered WebP hero as a JPEG into dist/. Runs after the
    asset copy so nothing overwrites the twins. Per-file failures are skipped
    (a missing twin only degrades that one article's share card, and the
    og:image then points at a 404 the Sharing Debugger will name loudly)."""
    if not OG_JPEG_TWINS:
        return
    try:
        from PIL import Image
    except ImportError:
        print("  [og-jpeg] Pillow not installed — og:image JPEG twins skipped "
              "(Facebook link cards may show the wrong image)")
        return
    written = 0
    for rel in OG_JPEG_TWINS:
        src = ROOT / rel.lstrip("/")
        out = (DIST / rel.lstrip("/")).with_suffix(".jpg")
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(src) as im:
                im.convert("RGB").save(out, "JPEG", quality=85, optimize=True)
            written += 1
        except Exception as exc:
            print(f"  [og-jpeg] failed for {rel} (skipped): {exc}")
    print(f"  [og-jpeg] {written} JPEG og:image twin(s) written to dist")


def base_page(site, *, title, description, path, body, jsonld=None, og_type="website",
              og_image=og_default_url(), og_image_type=None,
              og_image_width=None, og_image_height=None, noindex=False, is_home=False) -> str:
    cfg = site.cfg
    title = truncate_title(title)
    ld = "".join(f'<script type="application/ld+json">{json.dumps(x, ensure_ascii=False)}</script>'
                 for x in (jsonld or []))
    # Indexable pages explicitly opt in to LARGE image previews.
    #
    # This is the single hard requirement for Google Discover: Discover is an
    # image-led surface, and without max-image-preview:large Google is limited
    # to a thumbnail, which effectively excludes the page from being featured.
    # The default when the tag is absent is the small preview, so saying
    # nothing is the same as opting out — which is where this site was.
    #
    # max-snippet:-1 and max-video-preview:-1 remove the equivalent limits on
    # text and video previews. All three are the standard opt-in set for
    # publishers and are what every news site emits.
    robots = ('<meta name="robots" content="noindex">' if noindex else
              '<meta name="robots" content="max-image-preview:large, '
              'max-snippet:-1, max-video-preview:-1">')
    # Explicit og:image metadata helps crawlers validate the image without a
    # second fetch, and lets Facebook render the large card even on the very
    # first share of a URL (without dimensions, the first share often falls
    # back to a small-thumbnail card until the async scrape completes).
    og_image_meta = ""
    if og_image_type:
        og_image_meta += f'\n<meta property="og:image:type" content="{og_image_type}">'
    if og_image_width and og_image_height:
        og_image_meta += (f'\n<meta property="og:image:width" content="{og_image_width}">'
                          f'\n<meta property="og:image:height" content="{og_image_height}">')
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
<meta property="og:image" content="{og_image if og_image.startswith('http') else site.abs_(og_image)}">{og_image_meta}
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
    digest_path = cfg.get("digest_path", "today")
    chips += (f'<a class="chip{" on" if active == "digest" else ""}" href="{site.u("/" + digest_path + "/")}">'
              f'☀ {esc(ui.get("digest_title", "Today"))}</a>')
    for cid, cat in cfg["categories"].items():
        on = " on" if active == cid else ""
        chips += f'<a class="chip{on}" href="{site.u(site.cat_path(cid))}">{cat["emoji"]} {esc(cat["label"])}</a>'
    chips += f'<a class="chip{" on" if active == "about" else ""}" href="{site.u("/" + cfg["about_path"] + "/")}">{esc(ui["about"])}</a>'
    submit_path = cfg.get("submit_path", "")
    if submit_path:
        chips += (f'<a class="chip growth-submit-chip{" on" if active == "submit" else ""}" '
                  f'href="{site.u("/" + submit_path + "/")}">'
                  f'{esc(ui.get("submit_nav_label", "Send good news"))}</a>')
    brand_name = (f'<h1 class="h1">{esc(cfg["site_name"])}</h1>' if is_home
                  else f'<span class="h1">{esc(cfg["site_name"])}</span>')
    return f"""<header class="masthead wrap">
<div class="mast-row">
{mark_svg(cfg)}
<div class="brand"><a href="{site.u('/')}" aria-label="{esc(cfg['site_name'])}">{brand_name}</a>
<p>{esc(cfg['tagline'])}</p></div>
<form class="search-form" action="{site.u('/search/')}" method="get" role="search">
<input type="search" name="q" placeholder="{esc(ui.get('search_placeholder', 'Search'))}" aria-label="{esc(ui.get('search_placeholder', 'Search'))}">
<button type="submit" aria-label="{esc(ui.get('search_button', 'Search'))}">🔍</button>
</form>
<span class="today">{esc(fmt_today(cfg['lang']))}</span>
</div>
<div class="cats-wrap">
<nav class="cats" aria-label="categories">{chips}</nav>
<span class="cats-more" aria-hidden="true">›</span>
</div>
<script>
(function(){{
  var nav = document.querySelector('nav.cats');
  var wrap = document.querySelector('.cats-wrap');
  if (!nav || !wrap) return;
  function check(){{
    if (nav.scrollWidth - nav.scrollLeft - nav.clientWidth < 8) wrap.classList.add('at-end');
    else wrap.classList.remove('at-end');
  }}
  nav.addEventListener('scroll', check, {{passive: true}});
  check();
}})();
</script>
</header>"""


def footer(site) -> str:
    cfg, ui = site.cfg, site.cfg["ui"]
    year = datetime.now().year
    cities_link = ""
    if cfg.get("known_cities"):
        cities_path = cfg.get("cities_path", "cities")
        cities_link = f'<a href="{site.u("/" + cities_path + "/")}">{esc(ui.get("cities_title", "Browse by City"))}</a>\n'
    return f"""<footer><div class="wrap">
<strong style="font-family:var(--fd);font-size:1.05rem">{esc(cfg['site_name'])}</strong>
<p class="mission">{esc(ui['footer_mission'])}</p>
<nav class="fnav">
<a href="{site.u('/' + cfg['about_path'] + '/')}">{esc(ui['about'])}</a>
{cities_link}<a href="{site.u('/' + cfg['privacy_path'] + '/')}">{esc(ui.get('privacy', 'Privacy'))}</a>
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
    updated_html = ""
    modified_raw = a.get("rewritten") or a.get("updated")
    if modified_raw:
        try:
            mod_dt = datetime.strptime(modified_raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            # Only show if genuinely later by calendar date — avoids noise from
            # automated same-day timestamps that are technically a few minutes
            # after publish but not a real, visible "this was later corrected" edit.
            if mod_dt.date() > a["_dt"].date():
                updated_html = (f' · <span class="updated-badge">'
                                 f'{esc(ui.get("updated_label", "Updated"))} '
                                 f'{esc(fmt_date(mod_dt, cfg["lang"]))}</span>')
        except Exception:
            pass
    return (f'<div class="meta">{cat_html}'
            f'<time datetime="{a["published"]}">{fmt_date(a["_dt"], cfg["lang"])}</time>'
            f' · {reading_time(a["body"])} {ui["min_read"]}{updated_html}</div>')


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
        parts.append(f'<a href="{link(page - 1)}" class="pager-nav">← {ui["newer"]}</a>')

    # Compact numbered window: always show first and last page, plus a small
    # window around the current page, collapsing gaps with an ellipsis —
    # e.g. "1 … 8 9 [10] 11 12 … 20" instead of forcing next/prev-only clicks.
    window = 2
    shown = sorted({1, pages, page, *(p for p in range(page - window, page + window + 1))} & set(range(1, pages + 1)))
    last_shown = 0
    for p in shown:
        if last_shown and p - last_shown > 1:
            parts.append('<span class="pager-ellipsis">…</span>')
        if p == page:
            parts.append(f'<span class="cur">{p}</span>')
        else:
            parts.append(f'<a href="{link(p)}">{p}</a>')
        last_shown = p

    if page < pages:
        parts.append(f'<a href="{link(page + 1)}" class="pager-nav">{ui["older"]} →</a>')

    # Jump-to-page input — the numbered window above covers nearby pages and
    # the endpoints in one click, but a direct jump from page 1 to page 14 of
    # 20 needs this rather than several intermediate clicks.
    base_js = site.u(base_path).rstrip("/")
    jump = f"""<form class="pager-jump" onsubmit="event.preventDefault();
var n=parseInt(this.pagenum.value,10);
if(n>=1&&n<={pages}){{ window.location.href = n===1 ? '{site.u(base_path)}' : '{base_js}/page/'+n+'/'; }}">
<input type="number" name="pagenum" min="1" max="{pages}" placeholder="{esc(ui.get('go_to_page', 'Page #'))}" aria-label="{esc(ui.get('go_to_page', 'Go to page'))}">
<button type="submit">{esc(ui.get('go', 'Go'))}</button>
</form>"""

    return f'<nav class="pager" aria-label="pagination">{"".join(parts)}</nav>{jump}'


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
            # On home page 1 the masthead carries the <h1>, so the section
            # heading is an <h2>. Everywhere else — including home page 2+,
            # where the masthead renders a plain span instead — the section
            # heading must be the page's <h1>, or the page has none at all.
            heading_tag = "h2" if (key == "home" and p == 1) else "h1"
            body += f'<div class="sec"><{heading_tag}>{esc(label)}</{heading_tag}><span class="rule"></span></div>'
            if intro and p == 1:
                body = f'<p class="cat-intro">{esc(intro)}</p>' + body
            if pillars and p == 1:
                guide_label = ui.get("guides_label", "📖 Наръчници" if cfg["lang"] == "bg" else "📖 Guides")
                body += f'<div class="sec pillar-sec"><h2>{esc(guide_label)}</h2><span class="rule"></span></div>'
                body += '<div class="grid pillar-grid">' + "".join(card(site, a) for a in pillars) + "</div>"
            is_home_p1 = (key == "home" and p == 1)
            if is_home_p1:
                body = promise_banner(site) + body
            body += '<div class="grid">' + "".join(
                card(site, a, eager=(is_home_p1 and i == 0)) for i, a in enumerate(rest)
            ) + "</div>"
            body += pager(site, base_path, p, pages)
            if is_home_p1:
                body += newsletter_cta(site)
            jsonld = None
            if key == "home" and p == 1:
                jsonld = [{"@context": "https://schema.org", "@type": "WebSite",
                           "name": cfg["site_name"], "url": site.abs_("/"),
                           "description": cfg["description"], "inLanguage": cfg["lang"],
                           "publisher": org_ld(site),
                           "potentialAction": {
                               "@type": "SearchAction",
                               "target": {"@type": "EntryPoint",
                                          "urlTemplate": site.abs_("/search/") + "?q={search_term_string}"},
                               "query-input": "required name=search_term_string"
                           }}]
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


def build_tags(site, city_slugs: set | None = None) -> set:
    """Generate /tag/{slug}/ archive pages for tags with at least
    MIN_TAG_ARTICLES articles (paginated identically to category pages;
    page 1 is indexable, page 2+ is noindex and excluded from sitemap.xml,
    same convention as categories). Returns the set of slugs that get a
    working link, so build_articles() knows which hashtags to render as
    links vs. plain text.

    A tag whose slug exactly matches an existing category ID (e.g. a
    "#култура" hashtag on the same site that also has a "kultura" category)
    does NOT get its own separate page — the category page already covers
    that ground with richer content, and having two similarly-named but
    only-partially-overlapping pages was a real, confusing duplication
    found in an SEO audit. The hashtag still renders as a link (included in
    the returned set) — build_articles() points it at the category page
    instead of generating a redundant tag page for it.

    The same applies to city slugs that have a hub page under
    /{cities_path}/{slug}/: the hub is the canonical local page, so no
    /tag/{city}/ archive is generated and the hashtag links to the hub.
    Without this, /tag/varna/ and /gradove/varna/ both existed with
    overlapping article lists, competing against each other for the same
    local search query."""
    cfg, ui = site.cfg, site.cfg["ui"]
    city_slugs = city_slugs or set()
    idx = build_tag_index(site.articles, site.cfg.get("tag_aliases", {}))
    qualifying = {slug: data for slug, data in idx.items()
                  if len(data["articles"]) >= MIN_TAG_ARTICLES}
    category_ids = set(cfg["categories"].keys())
    for slug, data in qualifying.items():
        if slug in category_ids or slug in city_slugs:
            continue  # covered by the category or city hub page instead
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
    # City slugs are returned as linkable even though no /tag/ page exists for
    # them — build_articles() routes those hashtags to the city hub instead.
    return set(qualifying.keys()) | set(city_slugs)


def qualifying_cities(site) -> dict:
    """Known cities that have enough articles to deserve a hub page:
    {slug: (display_name, [articles])}, ordered by article count desc.
    Cities are matched through the same alias-normalized tag index used
    everywhere else, so a city tagged in Cyrillic and in Latin still lands
    on one page."""
    cfg = site.cfg
    known = cfg.get("known_cities", {})
    if not known:
        return {}
    idx = build_tag_index(site.articles, cfg.get("tag_aliases", {}))
    found = {}
    for slug, display in known.items():
        data = idx.get(slug)
        if data and len(data["articles"]) >= MIN_CITY_ARTICLES:
            found[slug] = (display, data["articles"])
    return dict(sorted(found.items(), key=lambda kv: len(kv[1][1]), reverse=True))


def build_city_index(site, cities: dict) -> list[str]:
    """Build /{cities_path}/ plus one hub page per qualifying city.

    These are the canonical city pages: build_tags() deliberately skips city
    slugs so a city never has both a /tag/{slug}/ archive and a hub page
    competing for the same local query. Returns the list of site-relative
    paths created, for the sitemap."""
    cfg, ui = site.cfg, site.cfg["ui"]
    cities_path = cfg.get("cities_path", "cities")
    index_title = ui.get("cities_title", "Browse by City")
    submit_url = site.u(f'/{cfg.get("submit_path", "izprati-dobra-novina")}/')
    created = []

    for slug, (name, arts) in cities.items():
        path = f'/{cities_path}/{slug}/'
        title = ui.get("city_hub_title", "Good news from {city}").format(city=name)
        lead = ui.get("city_hub_lead", "").format(city=name)
        kicker = f'{ui.get("city_hub_kicker", "📍")} {name.upper()}'
        rows = "".join(
            f'<article class="digest-item"><h3><a href="{site.u(site.article_path(a))}">'
            f'{esc(a["headline"])}</a></h3><p>{esc(a.get("summary_short", ""))}</p></article>'
            for a in arts[:30]
        )
        cta_title = ui.get("city_hub_cta_title", "Know a good story?").format(city=name)
        body = (
            f'<div class="about">'
            f'<div class="growth-kicker">{esc(kicker)}</div>'
            f'<h1>{esc(title)}</h1>'
            f'<p class="growth-lead">{esc(lead)}</p>'
            f'<div class="digest-list">{rows}</div>'
            f'<section class="growth-mini-cta"><strong>{esc(cta_title)}</strong>'
            f'<a href="{submit_url}">{esc(ui.get("submit_cta_link", "Send it to us →"))}</a></section>'
            f'</div>'
        )
        crumbs = [(ui["home"], site.abs_("/")),
                  (index_title, site.abs_(f'/{cities_path}/')),
                  (name, site.abs_(path))]
        item_list = {"@type": "ItemList", "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "url": site.abs_(site.article_path(a))}
            for i, a in enumerate(arts[:30])
        ]}
        jsonld = [
            breadcrumb_ld(site, crumbs),
            {"@context": "https://schema.org", "@type": "CollectionPage",
             "name": f'{title} · {cfg["site_name"]}', "description": lead,
             "url": site.abs_(path), "inLanguage": cfg["lang"], "mainEntity": item_list},
        ]
        write(DIST / cities_path / slug / "index.html",
              base_page(site, title=f'{title} · {cfg["site_name"]}',
                        description=lead or title, path=path, body=body, jsonld=jsonld))
        created.append(path)

    index_path = f'/{cities_path}/'
    if not cities:
        body = (f'<div class="about"><h1>{esc(index_title)}</h1>'
                f'<p>{esc(ui.get("cities_empty", "No city has enough coverage yet."))}</p></div>')
        jsonld = None
    else:
        cards = "".join(
            f'<a class="growth-city-card" href="{site.u(f"/{cities_path}/{slug}/")}">'
            f'<strong>{esc(name)}</strong>'
            f'<span>{len(arts)} {esc(ui.get("city_count_label", "articles"))}</span></a>'
            for slug, (name, arts) in cities.items()
        )
        body = (
            f'<div class="about">'
            f'<div class="growth-kicker">{esc(ui.get("city_index_kicker", ""))}</div>'
            f'<h1>{esc(index_title)}</h1>'
            f'<p class="growth-lead">{esc(ui.get("city_index_lead", ""))}</p>'
            f'<div class="growth-city-grid">{cards}</div>'
            f'<section class="growth-mini-cta">'
            f'<strong>{esc(ui.get("city_index_cta_title", ""))}</strong>'
            f'<span>{esc(ui.get("city_index_cta_text", ""))}</span>'
            f'<a href="{submit_url}">{esc(ui.get("submit_nav_label", "Send good news"))} →</a>'
            f'</section></div>'
        )
        jsonld = [breadcrumb_ld(site, [(ui["home"], site.abs_("/")),
                                       (index_title, site.abs_(index_path))])]
    write(DIST / cities_path / "index.html",
          base_page(site, title=f'{index_title} · {cfg["site_name"]}',
                    description=ui.get("city_index_lead", index_title),
                    path=index_path, body=body, jsonld=jsonld))
    created.append(index_path)
    return created


def build_city_tag_redirects(site, cities: dict) -> list[str]:
    """Cities used to live at /tag/<slug>/ before they moved to dedicated hub
    pages. Those URLs may already be crawled, linked from an old Facebook
    share, or sitting in someone's bookmarks, so leave a forwarding stub rather
    than a bare 404.

    GitHub Pages cannot serve a real HTTP 301, so this is the static
    equivalent: an instant meta refresh, a rel=canonical pointing at the hub so
    search engines consolidate the two URLs, a JS fallback, and a plain visible
    link for anyone whose browser blocks the refresh.

    Deliberately NOT noindex. 'noindex' tells a crawler to drop the URL, while
    'canonical' tells it to fold the URL into the target — contradictory
    instructions, and Google's own guidance is against combining them, since
    the noindex tends to win and the consolidation is lost. The canonical alone
    does what's actually wanted. These stubs are also kept out of sitemap.xml:
    a sitemap should list destinations, not forwarding addresses.

    Safe against collisions because build_tags() skips city slugs entirely, so
    nothing else writes to these paths."""
    cfg, ui = site.cfg, site.cfg["ui"]
    cities_path = cfg.get("cities_path", "cities")
    created = []
    for slug, (name, _arts) in cities.items():
        target_rel = site.u(f'/{cities_path}/{slug}/')
        target_abs = site.abs_(f'/{cities_path}/{slug}/')
        title = ui.get("city_hub_title", "Good news from {city}").format(city=name)
        notice = ui.get("redirect_notice", "This page has moved to:")
        html_doc = f"""<!DOCTYPE html>
<html lang="{cfg['lang']}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · {esc(cfg['site_name'])}</title>
<link rel="canonical" href="{target_abs}">
<meta http-equiv="refresh" content="0; url={target_rel}">
<link rel="stylesheet" href="{site.u('/assets/style.css')}">
</head>
<body class="brand-{cfg['brand']}">
<main class="wrap" style="padding:64px 22px;text-align:center">
<p>{esc(notice)}</p>
<p><a class="btn" href="{target_rel}">{esc(title)} →</a></p>
</main>
<script>location.replace({json.dumps(target_rel)});</script>
</body>
</html>"""
        write(DIST / "tag" / slug / "index.html", html_doc)
        created.append(f'/tag/{slug}/')
    return created


def promise_banner(site) -> str:
    """One line telling a first-time visitor what this site is.

    69% of readers arrive from Facebook, read one article for ~41 seconds and
    leave. They have no idea they have landed somewhere that is different by
    design — an article page looks like any other news page. This states the
    promise at the moment it matters, on the page they actually land on.
    """
    ui = site.cfg["ui"]
    text = ui.get("promise_text", "")
    if not text:
        return ""
    link = ui.get("promise_link", "")
    href = site.u("/" + site.cfg["about_path"] + "/")
    tail = f' <a href="{href}">{esc(link)}</a>' if link else ""
    return (f'<div class="promise"><span class="promise-icon" aria-hidden="true">☀</span>'
            f'<span>{text}{tail}</span></div>')


def next_up(site, article, related) -> str:
    """A single strong follow-on story, image-led.

    Replaces nothing — it sits above the existing related grid. A grid of three
    equal cards asks the reader to choose; at 41 seconds of attention, choosing
    loses to leaving. One large, obvious next story is a decision they can make
    without thinking."""
    if not related:
        return ""
    nxt = related[0]
    ui = site.cfg["ui"]
    img = ""
    if nxt.get("image_path"):
        img = (f'<img class="nextup-img" src="{esc(nxt["image_path"])}" '
               f'alt="{esc(nxt["headline"])}" loading="lazy">')
    return (f'<a class="nextup" href="{site.u(site.article_path(nxt))}">{img}'
            f'<div class="nextup-body">'
            f'<div class="nextup-kicker">{esc(ui.get("next_up", "Още нещо хубаво"))}</div>'
            f'<h3>{esc(nxt["headline"])}</h3>'
            f'<p>{esc(nxt.get("summary_short", ""))}</p></div></a>')


def newsletter_cta(site) -> str:
    """Inline newsletter CTA appended to the home page and article pages."""
    cfg, ui = site.cfg, site.cfg["ui"]
    href = site.u(f'/{cfg.get("newsletter_path", "newsletter")}/')
    # A real form when a provider is configured, otherwise the old link.
    #
    # This matters more than it looks: 41 seconds is far too short to build a
    # reading habit by browsing, so the email is the only durable connection to
    # a reader who liked what they saw. A mailto: link — which is what this was
    # — asks someone to open a mail client and compose a message. Almost nobody
    # does that.
    nl = cfg.get("newsletter") or {}
    action = nl.get("form_action", "")
    if action:
        field = nl.get("email_field", "email")
        # Providers require extra hidden inputs alongside the email. MailerLite
        # needs ml-submit=1 and anticsrf=true; without them the endpoint rejects
        # a plain (non-JavaScript) POST. Keeping them in config means switching
        # provider is a config edit, not a code change.
        hidden = "".join(
            f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">'
            for k, v in (nl.get("hidden_fields") or {}).items()
        )
        # target=_blank so a submit never navigates the reader away from the
        # article they were reading. Providers that answer a raw POST with JSON
        # render that response in the throwaway tab rather than over the site.
        inner = (
            f'<form class="nl-form" action="{esc(action)}" method="post" target="_blank">'
            f'<input type="email" name="{esc(field)}" required '
            f'placeholder="{esc(ui.get("newsletter_placeholder", "твоят имейл"))}" '
            f'aria-label="{esc(ui.get("newsletter_placeholder", "твоят имейл"))}">'
            f'{hidden}'
            f'<button class="growth-btn" type="submit">'
            f'{esc(ui.get("newsletter_cta_button", ""))}</button></form>'
            f'<div class="nl-note">{esc(ui.get("newsletter_note", ""))}</div>'
        )
    else:
        inner = f'<a class="growth-btn" href="{href}">{esc(ui.get("newsletter_cta_button", ""))}</a>'
    return (
        f'<section class="growth-cta-box" aria-labelledby="newsletter-cta-title">'
        f'<div class="growth-cta-icon" aria-hidden="true">☀</div><div>'
        f'<h2 id="newsletter-cta-title">{esc(ui.get("newsletter_cta_title", ""))}</h2>'
        f'<p>{esc(ui.get("newsletter_cta_text", ""))}</p>'
        f'{inner}</div></section>'
    )


def build_newsletter_page(site) -> str:
    cfg, ui = site.cfg, site.cfg["ui"]
    email = cfg["contact_email"]
    import urllib.parse
    # The dedicated newsletter page must carry the real form too. It was the
    # one page still showing "write to us and we'll add you", which is the
    # weakest possible ask on the page most likely to convert.
    nl = cfg.get("newsletter") or {}
    if nl.get("form_action"):
        field = nl.get("email_field", "email")
        hidden = "".join(
            f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">'
            for k, v in (nl.get("hidden_fields") or {}).items()
        )
        signup = (
            f'<form class="nl-form" action="{esc(nl["form_action"])}" method="post" target="_blank">'
            f'<input type="email" name="{esc(field)}" required '
            f'placeholder="{esc(ui.get("newsletter_placeholder", "твоят имейл"))}" '
            f'aria-label="{esc(ui.get("newsletter_placeholder", "твоят имейл"))}">'
            f'{hidden}<button class="growth-btn" type="submit">'
            f'{esc(ui.get("newsletter_cta_button", ""))}</button></form>'
            f'<div class="nl-note">{esc(ui.get("newsletter_note", ""))}</div>'
        )
    else:
        mailto = ("mailto:" + urllib.parse.quote(email, safe="@") + "?subject="
                  + urllib.parse.quote(f'Искам седмичния бюлетин на {cfg["site_name"]}'))
        signup = (f'<p>Пълното записване ще бъде добавено скоро. Дотогава ни пиши '
                  f'и ще те включим сред първите читатели.</p>'
                  f'<a class="growth-btn" href="{esc(mailto)}">Пиши ми за бюлетина</a>')
    body = f"""<div class="about">
<div class="growth-kicker">☀ СЕДМИЧЕН БЮЛЕТИН</div>
<h1>Добрите новини в една кратка неделна селекция</h1>
<p class="growth-lead">Работим по седмичен бюлетин с най-смислените положителни истории от България — без черна хроника, без политически шум и без безкраен поток от известия.</p>
<div class="growth-feature-list">
<div>✓ Най-силните истории от седмицата</div>
<div>✓ Местни добри новини и човешки истории</div>
<div>✓ Един имейл седмично, не всекидневен спам</div>
</div>
{signup}
</div>"""
    path = f'/{cfg.get("newsletter_path", "newsletter")}/'
    write(DIST / path.strip("/") / "index.html",
          base_page(site, title=f'Седмичен бюлетин · {cfg["site_name"]}',
                    description="Седмична селекция с най-добрите положителни новини от България.",
                    path=path, body=body))
    return path


def build_submit_page(site) -> str:
    """Reader submission page. The form deliberately posts nowhere — it opens
    the reader's own mail client with a pre-filled message, so the site stays
    fully static and no reader data ever touches a server."""
    cfg = site.cfg
    email = cfg["contact_email"]
    body = f"""<div class="about">
<div class="growth-kicker">💛 ПОМОГНИ НИ ДА НАМЕРИМ ДОБРОТО</div>
<h1>Изпрати добра новина</h1>
<p class="growth-lead">Знаеш за човек, училище, клуб, доброволци, местна инициатива, природозащитен успех или друго хубаво нещо, което заслужава внимание? Разкажи ни.</p>
<form class="growth-form" id="good-news-form">
<label>Кратко заглавие<input name="title" required placeholder="Какво хубаво се е случило?"></label>
<label>Какво се случи?<textarea name="story" required rows="7" placeholder="Дай ни най-важните факти, имена и контекст."></textarea></label>
<label>Град / място<input name="city" placeholder="Напр. Варна"></label>
<label>Линк към източник (ако има)<input name="source" type="url" placeholder="https://..."></label>
<label>Твоят имейл (по желание)<input name="reply" type="email" placeholder="За уточняващи въпроси"></label>
<button class="growth-btn" type="submit">Подготви имейла</button>
</form>
<p class="growth-small">Формата не качва данните на сървър. Тя отваря твоя email клиент с попълнен текст. Ако искаш да изпратиш снимки, приложи ги към имейла.</p>
<p>Или пиши директно на <a href="mailto:{esc(email)}">{esc(email)}</a>.</p>
</div>
<script>
(function(){{
  var f = document.getElementById('good-news-form');
  if (!f) return;
  f.addEventListener('submit', function(e){{
    e.preventDefault();
    var d = new FormData(f);
    var subject = 'Добра новина: ' + (d.get('title') || '');
    var body = [
      'Заглавие: ' + (d.get('title') || ''), '',
      'Какво се случи:', d.get('story') || '', '',
      'Град / място: ' + (d.get('city') || ''),
      'Източник: ' + (d.get('source') || ''),
      'Email за връзка: ' + (d.get('reply') || '')
    ].join('\\n');
    window.location.href = {json.dumps("mailto:" + email)} + '?subject=' +
      encodeURIComponent(subject) + '&body=' + encodeURIComponent(body);
  }});
}})();
</script>"""
    path = f'/{cfg.get("submit_path", "submit")}/'
    write(DIST / path.strip("/") / "index.html",
          base_page(site, title=f'Изпрати добра новина · {cfg["site_name"]}',
                    description="Сподели положителна история от твоя град или общност с редакцията.",
                    path=path, body=body))
    return path


def build_daily_digest(site) -> None:
    """Public 'Today's Good News' page — lists today's published articles at
    a stable, bookmarkable URL. Serves two purposes: a real SEO landing page
    for 'добри новини днес'-style queries (a Tier 2 keyword from the earlier
    strategy discussion), and ready-to-copy content for composing the daily
    newsletter by hand in MailerLite (or whatever tool) until/unless actual
    sending is automated later."""
    cfg, ui = site.cfg, site.cfg["ui"]
    today = datetime.now(timezone.utc).date()
    todays = [a for a in site.articles if a["_dt"].date() == today]
    digest_path = cfg.get("digest_path", "today")
    title = ui.get("digest_title", "Today's Good News")

    if not todays:
        body = (f'<div class="about"><h1>{esc(title)}</h1>'
                 f'<p>{esc(ui.get("digest_empty", "No new stories published yet today — check back soon."))}</p></div>')
        jsonld = None
    else:
        intro = ui.get("digest_intro", "A summary of the good news published today, {date}.").format(
            date=fmt_today(cfg["lang"]))
        items = "".join(
            f'<article class="digest-item"><h3><a href="{site.u(site.article_path(a))}">{esc(a["headline"])}</a></h3>'
            f'<p>{esc(a["summary_short"])}</p></article>'
            for a in todays
        )
        body = (f'<div class="about"><h1>{esc(title)}</h1><p class="cat-intro">{esc(intro)}</p>'
                f'<div class="digest-list">{items}</div></div>')
        jsonld = [{"@context": "https://schema.org", "@type": "CollectionPage",
                   "name": title, "url": site.abs_(f'/{digest_path}/'), "inLanguage": cfg["lang"],
                   "mainEntity": {"@type": "ItemList", "itemListElement": [
                       {"@type": "ListItem", "position": i + 1, "url": site.abs_(site.article_path(a))}
                       for i, a in enumerate(todays)
                   ]}}]

    path = f'/{digest_path}/'
    write(DIST / digest_path / "index.html",
          base_page(site, title=f'{title} · {cfg["site_name"]}', description=title,
                    path=path, body=body, jsonld=jsonld))


def build_articles(site, linked_tags: set, city_slugs: set | None = None) -> None:
    cfg, ui = site.cfg, site.cfg["ui"]
    city_slugs = city_slugs or set()
    cities_path = cfg.get("cities_path", "cities")
    for a in site.articles:
        cat = cfg["categories"][a["category"]]
        paras = render_article_body(a["body"])
        cat_unlock = a.get("cat_unlock")
        if cat_unlock:
            paras = apply_cat_unlock(paras, cat_unlock)
        if a.get("pillar"):
            # Prioritize sibling pillars in the same category first —
            # cross-linking evergreen guides to each other is a stronger
            # topical-cluster signal than whatever regular news happens to
            # be most recent. Without this, pillars get crowded out of each
            # other's related section entirely once enough regular news
            # accumulates in the same category — confirmed via a real SEO
            # audit finding zero cross-links between the 3 Nature guides
            # despite sharing a category.
            siblings = [r for r in site.articles if r["category"] == a["category"]
                        and r["slug"] != a["slug"] and r.get("pillar")]
            related = siblings[:3]
            if len(related) < 3:
                seen = {r["slug"] for r in related} | {a["slug"]}
                related += [r for r in site.articles if r["category"] == a["category"]
                            and r["slug"] not in seen][: 3 - len(related)]
        else:
            a_tags = set(a.get("tags", []))
            same_cat = [r for r in site.articles if r["category"] == a["category"] and r["slug"] != a["slug"]]
            # Prioritize articles sharing an actual tag — genuinely topical,
            # not just "published in the same broad category around the same
            # time," which could surface an unrelated story just because it's
            # recent. Falls back to the original recency-only behavior when
            # no tag overlap exists, so sparsely-tagged articles are unaffected.
            tag_matches = [r for r in same_cat if a_tags & set(r.get("tags", []))]
            related = tag_matches[:3]
            if len(related) < 3:
                seen = {r["slug"] for r in related} | {a["slug"]}
                related += [r for r in same_cat if r["slug"] not in seen][: 3 - len(related)]
        if len(related) < 3:
            seen = {r["slug"] for r in related} | {a["slug"]}
            related += [r for r in site.articles if r["slug"] not in seen][: 3 - len(related)]
        rel_html = ""
        if related:
            # One big obvious next story first, then the usual grid for anyone
            # still browsing. See next_up() for why the order matters.
            rel_html = next_up(site, a, related)
            rest = related[1:]
            if rest:
                rel_html += (f'<div class="sec"><h2>{esc(ui["more_good"])}</h2>'
                             f'<span class="rule"></span></div>'
                             '<div class="grid">' + "".join(card(site, r) for r in rest) + "</div>")
        def _tag_href(slug):
            if slug in cfg["categories"]:
                return site.cat_path(slug)
            if slug in city_slugs:
                return f'/{cities_path}/{slug}/'
            return site.tag_path(slug)
        tags = "".join(
            (f'<a class="tag" href="{site.u(_tag_href(tag_slug(t, cfg.get("tag_aliases", {}))))}">#{esc(t)}</a>'
             if tag_slug(t, cfg.get("tag_aliases", {})) in linked_tags
             else f'<span class="tag">#{esc(t)}</span>')
            for t in a.get("tags", []))
        src = ""
        if a.get("source_url"):
            added_context_label = ui.get("added_context_label", "Additional context and analysis")
            byline_name = cfg.get("byline_name", cfg["site_name"] + " AI")
            src = (f'<aside class="srcbox"><strong>{esc(ui["source"])}:</strong> '
                   f'<a href="{esc(a["source_url"])}" target="_blank" rel="noopener">{esc(a["source_name"])}</a>'
                   f'<p class="ainote">{esc(added_context_label)}: {esc(byline_name)}</p></aside>')
        # Readers who just finished a story about a local rescue or a school
        # are the single best discovery channel this site has — they know
        # about things before any feed does. The submission page existed only
        # as a nav chip, where nobody in a reading mindset ever looks; putting
        # the ask directly under the story is the whole point of having it.
        submit_cta = ""
        if cfg.get("submit_path"):
            submit_cta = (
                f'<aside class="submit-cta">'
                f'<strong>{esc(ui.get("submit_cta_title", "Знаете добра новина?"))}</strong> '
                f'{esc(ui.get("submit_cta_text", "Разкажете ни за нея."))} '
                f'<a href="{site.u("/" + cfg["submit_path"] + "/")}">'
                f'{esc(ui.get("submit_cta_link", "Изпратете я тук"))}</a>'
                f'</aside>'
            )
        quick_facts = [f for f in (a.get("quick_facts") or []) if f][:5]
        quick_facts_html = ""
        if quick_facts:
            items = "".join(f"<li>{esc(f)}</li>" for f in quick_facts)
            quick_facts_html = (f'<ul class="quick-facts" '
                                 f'aria-label="{esc(ui.get("quick_facts_label", "Quick facts"))}">{items}</ul>')
        editor_name = cfg.get("editor_name", "")
        if editor_name:
            byline_text = (f'{esc(ui.get("ai_written_label", "AI-written"))}, '
                            f'{esc(ui.get("reviewed_by_label", "reviewed by"))} {esc(editor_name)}')
        else:
            byline_text = f'{esc(ui.get("byline_label", "Compiled by"))} {esc(cfg.get("byline_name", cfg["site_name"] + " AI"))}'
        article_url = site.abs_(site.article_path(a))
        share_html = build_share_row(cfg, ui, a["slug"], article_url, a["headline"])
        body = f"""<article class="article">
<a class="backlink" href="{site.u('/')}">← {esc(ui['back_home'])}</a>
{promise_banner(site)}
{meta_row(site, a)}
<h1>{esc(a['headline'])}</h1>
<span class="byline">{byline_text} · <a href="{site.u('/' + cfg['about_path'] + '/#editorial-process')}">{esc(ui.get('how_it_works', 'How this works'))}</a></span>
{f'<span class="ai-badge">{esc(ui.get("ai_badge", "AI-summarized"))}</span>' if not a.get('no_ai_badge') else ''}
{quick_facts_html}
<div class="banner">{media(cfg, a, ui, height=250, eager=True, sizes="(max-width: 760px) 100vw, 720px")}</div>
<div class="body">{paras}</div>
{share_html}
{f'<div class="tags">{tags}</div>' if tags else ''}
{src}
{submit_cta}
{cat_unlock_block(cat_unlock) if cat_unlock else ''}
</article>
{rel_html}
{newsletter_cta(site)}"""
        path = site.article_path(a)
        crumbs = [(ui["home"], site.abs_("/")), (cat["label"], site.abs_(site.cat_path(a["category"]))),
                  (a["headline"], site.abs_(path))]
        og_img = og_w = og_h = og_mime = None
        if a.get("image_path"):
            img_url = site.abs_(a["image_path"])
            rich_image = {"@type": "ImageObject", "url": img_url,
                          "width": a.get("image_width") or 1200,
                          "height": a.get("image_height") or 675}
            og_img, og_w, og_h, og_mime = og_image_for(a["image_path"])
        elif a.get("photo_url"):
            img_url = pexels_resize(a["photo_url"], 1200)
            orig_w, orig_h = a.get("photo_width"), a.get("photo_height")
            if orig_w and orig_h:
                scaled_h = round(1200 * orig_h / orig_w)
                rich_image = {"@type": "ImageObject", "url": img_url, "width": 1200, "height": scaled_h}
            else:
                rich_image = img_url  # no dimensions on file (older article) — fall back to a bare URL
        else:
            img_url = site.abs_(og_default_url())
            rich_image = img_url
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
                        og_image=og_img or img_url, og_image_type=og_mime,
                        og_image_width=og_w, og_image_height=og_h))


ABOUT = {
    "bg": [
        ("Защо съществуваме",
         "Отвориш ли новините, светът изглежда черен: катастрофи, скандали, войни, поскъпване. Но това е само половината истина. Всеки ден в България лекари спасяват животи, доброволци садят гори, деца печелят олимпиади, съседи си помагат. {site} събира точно тези позитивни новини от България — само тях."),
        ("Как избираме новините",
         "Наш AI редактор чете водещите български медии няколко пъти дневно и подбира единствено истински добрите новини: конкретни хубави събития, без трагедии „с позитивен привкус“, без политически битки, без криминални хроники. После написва оригинална статия на български — с точните факти от източника, плюс собствен контекст и анализ, обясняващи защо събитието има значение."),
        ("Прозрачност",
         "Всяка статия комбинира точните факти от посочения източник с допълнителен контекст и анализ, добавени от нашата редакция — обясняваме защо дадено събитие има значение, а не просто го преразказваме. Никога не добавяме измислени факти. Под всяка новина стои връзка към оригиналния репортаж — препоръчваме да го отворите за пълната история. Ако забележите грешка, пишете ни и ще я поправим."),
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
    entity = ""
    if cfg.get("entity_disclosure_note"):
        entity = (f'<div class="editor-card" id="who-runs-this">'
                  f'<div class="editor-title">{esc(cfg["ui"].get("entity_disclosure_label", "Who runs this site"))}</div>'
                  f'<p>{esc(cfg["entity_disclosure_note"])}</p></div>')
    body = f'<div class="about"><h1>{esc(cfg["ui"]["about"])} · {esc(cfg["site_name"])}</h1>{editor}{entity}{secs}</div>'
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
         "Тази страница обяснява какви данни се събират, когато четете {site}, и с какви инструменти на трети страни (Google Анализ, Google реклами, Cloudflare) работим. Не изискваме регистрация и не събираме лични данни за създаване на профил."),
        ("Каква информация се събира",
         "Хостинг доставчикът и Cloudflare (услугата, която доставя сайта и го защитава от атаки) записват стандартни технически логове (IP адрес, браузър, посетена страница) за всеки сайт в интернет. Ако сте дали съгласие през банера за бисквитки, Google Анализ събира обобщена, анонимизирана статистика за посещенията, а Google реклами може да показва реклами въз основа на бисквитки. Без съгласие тези инструменти не записват нищо, свързано с вас."),
        ("Бисквитки и съгласие",
         "При първо посещение виждате банер, който ви пита дали приемате бисквитки за анализ и реклами. Можете да откажете също толкова лесно, колкото да приемете. По всяко време можете да промените избора си, като изтриете бисквитките на сайта през настройките на браузъра си."),
        ("Изтриване на данни",
         "Не поддържаме профили, пароли или лична база данни — няма нищо обвързано с вас, което да \"изтрием\" в традиционния смисъл. Ако сте приели бисквитки за анализ, изтриването на бисквитките на сайта през настройките на браузъра ви спира събирането незабавно. Ако искате конкретна заявка за изтриване на данни (например по GDPR или като част от вход през услуга на трета страна), пишете директно на {email} с темата \"Изтриване на данни\" — отговаряме на всяка заявка лично."),
        ("Вашите права",
         "Съгласно GDPR имате право на достъп, поправка, изтриване и възражение срещу обработката на данните ви. Тъй като не поддържаме профили или бази с лични данни отвъд анонимна статистика, повечето заявки се удовлетворяват автоматично чрез изтриване на бисквитките. За въпроси пишете ни на {email}."),
        ("Трети страни",
         "Google Анализ и Google реклами обработват данни съгласно собствените си политики за поверителност, достъпни на policies.google.com/privacy. Cloudflare обработва технически данни (основно IP адреси) с цел доставка на съдържанието и защита от злонамерен трафик, съгласно политиката им на cloudflare.com/privacypolicy. Не споделяме данни с други трети страни извън тях."),
        ("Промени",
         "Тази политика може да се актуализира при нужда — например когато добавим нов инструмент. Датата на последната промяна винаги ще е видима тук."),
    ],
    "en": [
        ("What this policy covers",
         "This page explains what data is collected when you read {site}, and which third-party tools (Google Analytics, Google ads, Cloudflare) we use. No account or sign-up is required, and we don't build personal profiles."),
        ("What information is collected",
         "Our hosting provider and Cloudflare (the service that delivers this site and protects it from attacks) log standard technical data (IP address, browser, page visited) for every website on the internet. If you accept the cookie banner, Google Analytics collects aggregated, anonymized visit statistics, and Google ads may show ads based on cookies. Without consent, neither tool records anything tied to you."),
        ("Cookies and consent",
         "On your first visit you'll see a banner asking whether you accept analytics and advertising cookies. Rejecting is exactly as easy as accepting. You can change your choice at any time by clearing this site's cookies in your browser settings."),
        ("Data deletion",
         "We don't maintain accounts, passwords, or a personal database — there's nothing tied to you in the traditional sense to \"delete.\" If you accepted analytics cookies, clearing this site's cookies in your browser settings stops that collection immediately. For a specific data deletion request (for example under GDPR, or as part of a third-party login flow), email {email} with the subject \"Data deletion\" — every request gets a personal reply."),
        ("Your rights",
         "Under GDPR you have the right to access, correct, delete, and object to processing of your data. Since we don't maintain accounts or personal databases beyond anonymized statistics, most requests are satisfied simply by clearing your cookies. For questions, write to {email}."),
        ("Third parties",
         "Google Analytics and Google ads process data under their own privacy policies, available at policies.google.com/privacy. Cloudflare processes technical data (mainly IP addresses) to deliver this site's content and protect it from malicious traffic, under their policy at cloudflare.com/privacypolicy. We do not share data with any other third party beyond these."),
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


def build_sitemap(site, tag_slugs: set, extra_paths: list[str] | None = None) -> None:
    cfg = site.cfg

    def _lastmod(a):
        return (a.get("rewritten") or a.get("updated") or "")[:10] or a["_dt"].strftime("%Y-%m-%d")

    # site.articles is newest-first, so the first match in a filtered list is
    # still the most recently touched one in that scope.
    most_recent_overall = _lastmod(site.articles[0]) if site.articles else None

    urls = []
    if most_recent_overall:
        urls.append((site.abs_("/"), most_recent_overall))
    else:
        urls.append((site.abs_("/"), None))
    # About/Privacy are genuinely static pages with no tracked edit date —
    # omitting lastmod is more honest than fabricating one. lastmod is an
    # optional sitemap field; a missing value is not an error.
    urls.append((site.abs_(f'/{cfg["about_path"]}/'), None))
    urls.append((site.abs_(f'/{cfg["privacy_path"]}/'), None))
    # City hubs, newsletter and submit pages, passed in from main() so this
    # function never has to re-derive which ones actually got built.
    for path in (extra_paths or []):
        urls.append((site.abs_(path), most_recent_overall))
    # Note: page 2+ (home, category, and tag) are intentionally excluded here
    # — they carry noindex and stay reachable only via in-page pagination
    # links, so the sitemap doesn't send Google a mixed noindex-but-submitted
    # signal.
    for cid in cfg["categories"]:
        cat_arts = [a for a in site.articles if a["category"] == cid]
        cat_lastmod = _lastmod(cat_arts[0]) if cat_arts else None
        urls.append((site.abs_(site.cat_path(cid)), cat_lastmod))
    for slug in sorted(tag_slugs):
        # tag_slugs includes category and city slugs so hashtags render as
        # links, but those have no /tag/{slug}/ page — listing them here
        # would put 404s in the sitemap.
        if slug in cfg["categories"] or slug in cfg.get("known_cities", {}):
            continue
        tag_arts = [a for a in site.articles if slug in {tag_slug(t, cfg.get("tag_aliases", {})) for t in a.get("tags", [])}]
        tag_lastmod = _lastmod(tag_arts[0]) if tag_arts else None
        urls.append((site.abs_(site.tag_path(slug)), tag_lastmod))
    urls += [(site.abs_(site.article_path(a)), _lastmod(a)) for a in site.articles]
    body = "".join(
        f"<url><loc>{esc(u)}</loc>{f'<lastmod>{d}</lastmod>' if d else ''}</url>" for u, d in urls
    )
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

    yandex_code = cfg.get("yandex_verification", "")
    if yandex_code:
        # Exact format required by Yandex — confirmed against their own docs.
        # Must contain ONLY this content, verbatim, or verification fails.
        write(DIST / f"yandex_{yandex_code}.html",
              '<html>\n<head>\n<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">\n'
              f'</head>\n<body>Verification: {yandex_code}</body>\n</html>')


# --------------------------------------------------------------- main -----

def build_search_index(site) -> None:
    """Lightweight client-side search index — headline, summary, category,
    and tags for every real article. Deliberately excludes full body text to
    keep the file small; enough for readers to find a specific article by
    title/topic/tag. Short keys (t/s/u/c/g/d) keep the JSON compact across
    a few hundred articles."""
    cfg = site.cfg
    items = []
    for a in site.articles:
        cat = cfg["categories"].get(a["category"], {})
        items.append({
            "t": a["headline"], "s": a["summary_short"],
            "u": site.u(site.article_path(a)), "c": cat.get("label", ""),
            "g": a.get("tags", []), "d": a["published"][:10],
        })
    write(DIST / "search-index.json", json.dumps(items, ensure_ascii=False))


def build_search_page(site) -> None:
    """A static page that fetches search-index.json and filters it entirely
    client-side — no backend needed. Supports a shareable ?q= URL and live
    filtering as the user types. Noindexed, since internal search-result
    pages shouldn't be indexed individually (thin/duplicate-content risk)."""
    cfg, ui = site.cfg, site.cfg["ui"]
    index_url = site.u("/search-index.json")
    title_text = ui.get("search_title", "Search")
    placeholder = ui.get("search_placeholder", "Search articles…")
    no_results = ui.get("no_results", "No results found.")
    results_word = ui.get("results_word", "result(s)")
    search_error = ui.get("search_error", "Search is temporarily unavailable.")
    body = f"""<div class="search-page">
<h1>{esc(title_text)}</h1>
<form class="search-form-main" onsubmit="return false;">
<input type="search" id="search-input" placeholder="{esc(placeholder)}" aria-label="{esc(placeholder)}" autofocus>
</form>
<div id="search-status" class="search-status"></div>
<div id="search-results" class="grid"></div>
</div>
<script>
(function(){{
  var input = document.getElementById('search-input');
  var results = document.getElementById('search-results');
  var status = document.getElementById('search-status');
  var index = null;
  var params = new URLSearchParams(window.location.search);
  var initialQ = params.get('q') || '';
  input.value = initialQ;

  function esc(s){{ var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }}

  function render(items, q){{
    if (!q) {{ status.textContent = ''; results.innerHTML = ''; return; }}
    if (!items.length) {{ status.textContent = {json.dumps(no_results)}; results.innerHTML = ''; return; }}
    status.textContent = items.length + ' ' + {json.dumps(results_word)};
    results.innerHTML = items.map(function(a){{
      return '<article class="card"><div class="cbody">' +
        '<h3><a href="' + a.u + '">' + esc(a.t) + '</a></h3>' +
        '<p>' + esc(a.s) + '</p>' +
        '<div class="meta"><span class="cat">' + esc(a.c) + '</span> · <time>' + a.d + '</time></div>' +
        '</div></article>';
    }}).join('');
  }}

  function doSearch(){{
    var q = input.value.trim().toLowerCase();
    var url = new URL(window.location);
    if (q) {{ url.searchParams.set('q', q); }} else {{ url.searchParams.delete('q'); }}
    window.history.replaceState({{}}, '', url);
    if (!index || !q) {{ render([], q); return; }}
    var matches = index.filter(function(a){{
      var hay = (a.t + ' ' + a.s + ' ' + (a.g || []).join(' ')).toLowerCase();
      return hay.indexOf(q) !== -1;
    }});
    render(matches, q);
  }}

  input.addEventListener('input', doSearch);
  fetch('{index_url}').then(function(r){{ return r.json(); }}).then(function(data){{
    index = data;
    if (initialQ) doSearch();
  }}).catch(function(){{ status.textContent = {json.dumps(search_error)}; }});
}})();
</script>"""
    path = "/search/"
    write(DIST / "search" / "index.html",
          base_page(site, title=f'{title_text} · {cfg["site_name"]}',
                    description=placeholder, path=path, body=body, noindex=True))


def build_llms_txt(site) -> None:
    cfg = site.cfg
    recent = "\n".join(f'- {a["headline"]}: {site.abs_(site.article_path(a))}' for a in site.articles[:15])
    categories = "\n".join(
        f'- {cat["label"]}: {site.abs_(site.cat_path(cid))}'
        for cid, cat in cfg["categories"].items()
    )
    txt = f"""# {cfg['site_name']}

> {cfg['tagline']}

{cfg['description']}

{cfg['site_name']} is an independently published, AI-assisted good-news site.
Every article is an original summary written from a single credited source,
never invented, always linked. See {site.abs_('/' + cfg['about_path'] + '/')} for
the full editorial policy and AI-disclosure statement.

## Categories
{categories}

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

    # Cities are computed once and shared: build_tags() must know which slugs
    # to skip, build_articles() must know where to point city hashtags, and
    # the sitemap needs the hub paths. One source of truth for all three.
    cities = qualifying_cities(site)
    city_slugs = set(cities.keys())

    build_lists(site)
    qualifying_tag_slugs = build_tags(site, city_slugs)
    build_articles(site, qualifying_tag_slugs, city_slugs)
    build_about(site)
    build_privacy(site)
    build_404(site)
    build_feed(site)
    city_paths = build_city_index(site, cities)
    # Forwarding stubs for the old /tag/<city>/ URLs. Not added to extra_paths:
    # a sitemap should list destinations, not redirects.
    redirect_paths = build_city_tag_redirects(site, cities)
    newsletter_path = build_newsletter_page(site)
    submit_path = build_submit_page(site)
    build_sitemap(site, qualifying_tag_slugs,
                  extra_paths=city_paths + [newsletter_path, submit_path])
    build_news_sitemap(site)
    build_rsl(site)
    build_search_index(site)
    build_search_page(site)
    build_daily_digest(site)
    build_llms_txt(site)
    write_og_jpeg_twins()
    print(f"[{cfg['site_name']}] built {len(articles)} articles, "
          f"{len(cities)} city hub(s), {len(redirect_paths)} tag redirect(s) → {DIST}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
CONTENT = ROOT / "content" / "articles"
CFG_PATH = ROOT / "config.json"


def esc(value) -> str:
    return html.escape(str(value or ""), quote=True)


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8-sig"))


def load_articles() -> list[dict]:
    out = []
    for path in CONTENT.rglob("*.json"):
        try:
            a = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if a.get("slug") and a.get("headline"):
            out.append(a)
    out.sort(key=lambda a: a.get("published", ""), reverse=True)
    return out


def slugish(value: str) -> str:
    import unicodedata
    table = str.maketrans({
        "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ж":"zh","з":"z","и":"i","й":"y",
        "к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u",
        "ф":"f","х":"h","ц":"ts","ч":"ch","ш":"sh","щ":"sht","ъ":"a","ь":"","ю":"yu","я":"ya",
    })
    s = str(value or "").lower().translate(table)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s)).strip("-")


def bp(cfg: dict) -> str:
    return cfg.get("base_path", "").rstrip("/")


def u(cfg: dict, path: str) -> str:
    return bp(cfg) + path


def abs_url(cfg: dict, path: str) -> str:
    return cfg["base_url"].rstrip("/") + u(cfg, path)


def replace_main(doc: str, body_html: str) -> str:
    pattern = re.compile(r'<main class="wrap" id="main">.*?</main>', re.DOTALL)
    if not pattern.search(doc):
        raise RuntimeError("Could not find main element in dist/index.html")
    return pattern.sub(lambda _: f'<main class="wrap" id="main">{body_html}</main>', doc, count=1)


def set_meta(doc: str, cfg: dict, title: str, description: str, path: str) -> str:
    full_title = f"{title} · {cfg['site_name']}"
    canonical = abs_url(cfg, path)
    doc = re.sub(r"<title>.*?</title>", f"<title>{esc(full_title)}</title>", doc, count=1, flags=re.DOTALL)
    doc = re.sub(r'<meta name="description" content="[^"]*">', f'<meta name="description" content="{esc(description)}">', doc, count=1)
    doc = re.sub(r'<link rel="canonical" href="[^"]*">', f'<link rel="canonical" href="{esc(canonical)}">', doc, count=1)
    doc = re.sub(r'<meta property="og:title" content="[^"]*">', f'<meta property="og:title" content="{esc(full_title)}">', doc, count=1)
    doc = re.sub(r'<meta property="og:description" content="[^"]*">', f'<meta property="og:description" content="{esc(description)}">', doc, count=1)
    doc = re.sub(r'<meta property="og:url" content="[^"]*">', f'<meta property="og:url" content="{esc(canonical)}">', doc, count=1)
    return doc


def write_from_home(cfg: dict, path: str, title: str, description: str, body_html: str) -> None:
    template = (DIST / "index.html").read_text(encoding="utf-8")
    doc = replace_main(template, body_html)
    doc = set_meta(doc, cfg, title, description, path)
    out = DIST / path.strip("/") / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")


def article_url(cfg: dict, a: dict) -> str:
    return u(cfg, f'/{cfg["article_prefix"]}/{a["slug"]}/')


def city_articles(cfg: dict, articles: list[dict]) -> dict[str, tuple[str, list[dict]]]:
    aliases = cfg.get("tag_aliases", {})
    known = cfg.get("known_cities", {})
    result = {}
    for city_slug, display in known.items():
        matches = []
        for a in articles:
            norm = set()
            for tag in (a.get("tags") or []):
                s = slugish(tag)
                s = aliases.get(s, s)
                norm.add(s)
            if city_slug in norm:
                matches.append(a)
        if matches:
            result[city_slug] = (display, matches)
    return result


def inject_nav(doc: str, cfg: dict) -> str:
    if "growth-submit-chip" in doc:
        return doc
    submit = (
        f'<a class="chip growth-submit-chip" '
        f'href="{esc(u(cfg, "/izprati-dobra-novina/"))}">💛 Изпрати добра новина</a>'
    )
    nav_start = doc.find('<nav class="cats"')
    if nav_start == -1:
        return doc
    nav_end = doc.find("</nav>", nav_start)
    if nav_end == -1:
        return doc
    return doc[:nav_end] + submit + doc[nav_end:]


def newsletter_cta(cfg: dict) -> str:
    href = esc(u(cfg, "/byuletin/"))
    return f"""
<section class="growth-cta-box" aria-labelledby="newsletter-cta-title">
  <div class="growth-cta-icon">☀</div>
  <div>
    <h2 id="newsletter-cta-title">Добрите новини веднъж седмично</h2>
    <p>Без черна хроника и без шум. Подбираме най-смисленото от седмицата и го събираме на едно място.</p>
    <a class="growth-btn" href="{href}">Искам седмичния бюлетин</a>
  </div>
</section>"""


def append_cta_to_page(path: Path, cfg: dict) -> None:
    doc = path.read_text(encoding="utf-8")
    if "growth-cta-box" in doc or "</main>" not in doc:
        return
    doc = doc.replace("</main>", newsletter_cta(cfg) + "\n</main>", 1)
    path.write_text(doc, encoding="utf-8")


def update_about(cfg: dict) -> None:
    path = DIST / cfg.get("about_path", "za-nas") / "index.html"
    if not path.exists():
        return
    doc = path.read_text(encoding="utf-8")
    new_note = (
        "Системата на Добро Дело събира потенциални положителни истории от публични източници. "
        "Димитър Иванов лично избира кои от тях си струва да бъдат разработени. AI подпомага "
        "подготовката на текста само за избраните истории, а преди публикуване материалът отново "
        "минава човешки преглед. Не публикуваме автоматично всичко, което системата намери. "
        "Всяка новина посочва основния източник, а при грешка коригираме или премахваме материала."
    )
    pattern = re.compile(
        r'(<div class="editor-card" id="editorial-process">.*?<p>)(.*?)(</p></div>)',
        re.DOTALL
    )
    if pattern.search(doc):
        doc = pattern.sub(lambda m: m.group(1) + esc(new_note) + m.group(3), doc, count=1)
    path.write_text(doc, encoding="utf-8")


def build_newsletter_page(cfg: dict) -> None:
    email = cfg.get("contact_email", "redaktsia@dobrodelo.com")
    mailto = "mailto:" + quote(email, safe="@") + "?subject=" + quote("Искам седмичния бюлетин на Добро Дело")
    body = f"""
<div class="about growth-landing">
  <div class="growth-kicker">☀ СЕДМИЧЕН БЮЛЕТИН</div>
  <h1>Добрите новини в една кратка неделна селекция</h1>
  <p class="growth-lead">Работим по седмичен бюлетин с най-смислените положителни истории от България — без черна хроника, без политически шум и без безкраен поток от известия.</p>
  <div class="growth-feature-list">
    <div>✓ Най-силните истории от седмицата</div>
    <div>✓ Местни добри новини и човешки истории</div>
    <div>✓ Един имейл седмично, не всекидневен спам</div>
  </div>
  <p>Пълното записване ще бъде добавено с newsletter платформата. Дотогава можеш да ни пишеш и ще те включим сред първите читатели.</p>
  <a class="growth-btn" href="{esc(mailto)}">Пиши ми за бюлетина</a>
</div>"""
    write_from_home(cfg, "/byuletin/", "Седмичен бюлетин",
                    "Седмична селекция с най-добрите положителни новини от България.", body)


def build_submit_page(cfg: dict) -> None:
    email = cfg.get("contact_email", "redaktsia@dobrodelo.com")
    body = f"""
<div class="about growth-landing">
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
    window.location.href = 'mailto:{esc(email)}?subject=' + encodeURIComponent(subject) + '&body=' + encodeURIComponent(body);
  }});
}})();
</script>"""
    write_from_home(cfg, "/izprati-dobra-novina/", "Изпрати добра новина",
                    "Сподели положителна история от твоя град или общност с редакцията на Добро Дело.", body)


def build_city_hubs(cfg: dict, articles: list[dict]) -> list[str]:
    grouped = city_articles(cfg, articles)
    ranked = sorted(grouped.items(), key=lambda kv: len(kv[1][1]), reverse=True)
    strong = [(slug, data) for slug, data in ranked if len(data[1]) >= 3][:8]
    if not strong:
        return []

    cards = []
    created_paths = []
    for slug, (name, items) in strong:
        cards.append(
            f'<a class="growth-city-card" href="{esc(u(cfg, f"/gradove/{slug}/"))}">'
            f'<strong>{esc(name)}</strong><span>{len(items)} добри новини</span></a>'
        )
        rows = []
        for a in items[:20]:
            rows.append(
                f'<article class="digest-item"><h3><a href="{esc(article_url(cfg, a))}">'
                f'{esc(a["headline"])}</a></h3><p>{esc(a.get("summary_short",""))}</p></article>'
            )
        body = f"""
<div class="about growth-city-hub">
  <div class="growth-kicker">📍 ДОБРИ НОВИНИ ОТ {esc(name).upper()}</div>
  <h1>Добри новини от {esc(name)}</h1>
  <p class="growth-lead">Последните положителни истории, които сме публикували за {esc(name)} и хората от града.</p>
  <div class="digest-list">{''.join(rows)}</div>
  <section class="growth-mini-cta"><strong>Знаеш добра история от {esc(name)}?</strong>
    <a href="{esc(u(cfg, "/izprati-dobra-novina/"))}">Изпрати ни я →</a></section>
</div>"""
        path = f"/gradove/{slug}/"
        write_from_home(cfg, path, f"Добри новини от {name}",
                        f"Положителни новини, местни успехи и добри истории от {name}.", body)
        created_paths.append(path)

    index_body = f"""
<div class="about growth-city-index">
  <div class="growth-kicker">📍 БЛИЗО ДО ТЕБ</div>
  <h1>Добри новини по градове</h1>
  <p class="growth-lead">Започваме с градовете, за които вече имаме достатъчно реално съдържание. Не създаваме празни страници само за SEO.</p>
  <div class="growth-city-grid">{''.join(cards)}</div>
  <section class="growth-mini-cta"><strong>Не виждаш своя град?</strong>
    <span>Помогни ни да го напълним с истински добри истории.</span>
    <a href="{esc(u(cfg, "/izprati-dobra-novina/"))}">Изпрати добра новина →</a></section>
</div>"""
    write_from_home(cfg, "/gradove/", "Добри новини по градове",
                    "Местни положителни новини от българските градове с най-много покритие в Добро Дело.",
                    index_body)
    return ["/gradove/"] + created_paths


def add_sitemap_urls(cfg: dict, paths: list[str]) -> None:
    sitemap = DIST / "sitemap.xml"
    if not sitemap.exists():
        return
    xml = sitemap.read_text(encoding="utf-8")
    for path in paths:
        loc = abs_url(cfg, path)
        if f"<loc>{loc}</loc>" in xml:
            continue
        xml = xml.replace("</urlset>", f"<url><loc>{esc(loc)}</loc></url></urlset>")
    sitemap.write_text(xml, encoding="utf-8")


def append_css() -> None:
    css_path = DIST / "assets" / "style.css"
    if not css_path.exists():
        return
    css = css_path.read_text(encoding="utf-8")
    if "/* DobroDelo growth layer */" in css:
        return
    css += """

/* DobroDelo growth layer */
.growth-submit-chip{border-color:var(--p)!important;background:#fff7dc!important}
.growth-cta-box{display:grid;grid-template-columns:auto 1fr;gap:18px;align-items:center;margin:34px 0;padding:24px;border:1.5px solid var(--line);background:var(--card);border-radius:var(--r);box-shadow:0 8px 30px rgba(30,50,64,.06)}
.growth-cta-icon{font-size:2.4rem}
.growth-cta-box h2,.growth-landing h1{font-family:var(--fd);font-weight:800;letter-spacing:-.02em}
.growth-cta-box h2{margin:0 0 6px;font-size:1.35rem}
.growth-cta-box p{margin:0 0 14px;color:var(--muted)}
.growth-btn{display:inline-block;border:0;border-radius:999px;padding:11px 18px;background:var(--p);color:var(--ink);font-family:var(--fl);font-weight:800;cursor:pointer}
.growth-btn:hover{filter:brightness(.96)}
.growth-kicker{font-family:var(--fl);font-size:.8rem;font-weight:800;letter-spacing:.07em;color:var(--pd);margin-bottom:8px}
.growth-lead{font-size:1.08rem;color:var(--muted)!important}
.growth-feature-list{display:grid;gap:8px;margin:20px 0;padding:18px;border-radius:var(--r);background:var(--card);border:1px solid var(--line)}
.growth-form{display:grid;gap:15px;margin:24px 0}
.growth-form label{display:grid;gap:6px;font-family:var(--fl);font-weight:700}
.growth-form input,.growth-form textarea{width:100%;box-sizing:border-box;border:1.5px solid var(--line);border-radius:14px;padding:12px 14px;background:var(--card);color:var(--ink);font:inherit}
.growth-form input:focus,.growth-form textarea:focus{outline:none;border-color:var(--p)}
.growth-small{font-size:.88rem;color:var(--muted)!important}
.growth-city-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:22px 0}
.growth-city-card{display:flex;flex-direction:column;gap:4px;padding:17px 18px;border-radius:var(--r);background:var(--card);border:1.5px solid var(--line)}
.growth-city-card:hover{border-color:var(--p)}
.growth-city-card strong{font-family:var(--fd);font-size:1.1rem}
.growth-city-card span{font-size:.88rem;color:var(--muted)}
.growth-mini-cta{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;margin:26px 0;padding:17px 18px;border-radius:var(--r);background:#fff7dc;border:1px solid var(--line)}
.growth-mini-cta a{font-weight:800}
@media(max-width:640px){.growth-cta-box{grid-template-columns:1fr}.growth-city-grid{grid-template-columns:1fr}}
"""
    css_path.write_text(css, encoding="utf-8")


def global_html_pass(cfg: dict) -> None:
    for path in DIST.rglob("*.html"):
        doc = path.read_text(encoding="utf-8")
        doc = doc.replace("Оригинален репортаж", "Основен източник")
        doc = inject_nav(doc, cfg)
        path.write_text(doc, encoding="utf-8")


def main() -> int:
    if not DIST.exists() or not (DIST / "index.html").exists():
        raise SystemExit("dist/ does not exist. Run `python build.py` first.")

    cfg = load_cfg()
    articles = load_articles()

    build_newsletter_page(cfg)
    build_submit_page(cfg)
    city_paths = build_city_hubs(cfg, articles)

    update_about(cfg)
    global_html_pass(cfg)

    append_cta_to_page(DIST / "index.html", cfg)
    article_root = DIST / cfg.get("article_prefix", "novina")
    if article_root.exists():
        for article_page in article_root.rglob("index.html"):
            append_cta_to_page(article_page, cfg)

    append_css()
    add_sitemap_urls(cfg, ["/byuletin/", "/izprati-dobra-novina/"] + city_paths)

    print("DobroDelo growth layer applied:")
    print("- Основен източник wording")
    print("- human-selection About copy")
    print("- newsletter CTA + /byuletin/")
    print("- /izprati-dobra-novina/")
    print(f"- {max(0, len(city_paths)-1)} strong city hub(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

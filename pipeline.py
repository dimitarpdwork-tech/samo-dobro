# How to apply

Copy the files over your repo, preserving paths:

```
pipeline.py
build.py
config.json
README.md
.gitignore
tools/commit_seen.sh          (new)
tools/open_review_pr.sh       (new)
tools/prepare_candidate_issue.py
tools/weekly_roundup.py
.github/workflows/publish.yml
.github/workflows/facebook-queue.yml   (new)
.github/workflows/candidate-shortlist.yml
.github/workflows/publish-selected.yml
```

Then three things the file copy can't do for you:

```bash
# 1. Delete files that no longer exist
git rm tools/postbuild_growth.py
git rm README_REJECT_UPDATE.txt
git rm assets/articles/atp-wta-ranglista-obyasnena-9825.webp   # orphaned image

# 2. Stop tracking dist/ (it's in .gitignore but was committed anyway,
#    and the committed copy is the stale "Само Добро" build)
git rm -r --cached dist

# 3. Mark the new shell scripts executable in git's index
git update-index --chmod=+x tools/commit_seen.sh
git update-index --chmod=+x tools/open_review_pr.sh

git add -A
git commit -m "Consolidate build, fix city page collision, dedupe workflows"
git push
```

The workflows also `chmod +x tools/*.sh` at runtime, so step 3 is belt-and-braces.

## Verify before pushing

```bash
pip install -r requirements.txt
python build.py
```

Expect: `built 406 articles, 13 city hub(s) → dist`.

Then spot-check:

```bash
ls dist/gradove/          # 13 city hubs + index
ls dist/tag/              # 17 topic tags, NO city names
grep -c SearchAction dist/byuletin/index.html    # 0
```

## What changed that you'll see on the site

- Cities now live only at `/gradove/<city>/`. The old `/tag/<city>/` pages are
  gone — if any were indexed, Google will drop them as 404s and the hub pages
  will take over. If you'd rather not lose that link equity, add redirects at
  your CDN from `/tag/<city>/` to `/gradove/<city>/`.
- Source label reads **Основен източник** everywhere.
- The About page's editorial note now says the human-selection version, and the
  page's structured data says the same thing (previously they disagreed).
- Newsletter CTA appears on the home page and under every article.
- 💛 Изпрати добра новина chip appears in the category nav.

## Not done — needs binary files I can't fetch

The CSS asks for font-weights 700 and 800, but `assets/` only ships the 400
weight of Sofia Sans, so every headline is currently faux-bold (browser-
synthesised). Download the real weights and drop them in:

1. Get `sofia-sans-v20-cyrillic_latin-700.woff2` and
   `sofia-sans-condensed-v6-cyrillic_latin-700.woff2` (google-webfonts-helper
   or Google Fonts, Cyrillic + Latin subsets).
2. Put them in `assets/`.
3. Add to `config.json` → `fonts.faces`:

```json
{ "family": "Sofia Sans", "weight": "700", "file": "sofia-sans-v20-cyrillic_latin-700.woff2" },
{ "family": "Sofia Sans Condensed", "weight": "700", "file": "sofia-sans-condensed-v6-cyrillic_latin-700.woff2" }
```

`build.py` generates the `@font-face` rules from that list automatically — no
code change needed.

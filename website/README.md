# The website

[biokids.bilalgurkansanli.com](https://biokids.bilalgurkansanli.com): what BioKids is,
how to install it and how to use it, in Turkish (at `/`) and English (under `/en/`)
(D-256). Static HTML built from the files here; Vercel builds and serves it (D-258).

## Build and look at it

From the repository's root, with the project's virtualenv (only Jinja2 is needed):

```bash
python website/build.py --serve      # http://127.0.0.1:8000
python website/build.py --check      # what CI runs
```

`--check` fails on a broken internal link or anchor, a missing picture or share card,
an image without `alt`, `width` or `height`, a page without exactly one `<h1>`, an id
used twice, JSON-LD that does not parse, a link to a repository file that is not
there, a sitemap that leaves out a page to index or lists one that is not, or a
title (20-65 characters) or description (70-165) out of range. `--out DIR`
builds somewhere other than `website/_site/`, which is ignored by git.

## Where it is served

Vercel builds the site from this repository (`vercel.json` at the root): it runs
`website/vercel-build.sh` (Jinja2, then `build.py --check`), serves `website/_site/`,
deploys every push to `main`, and gives each pull request a preview that is not
indexed. `vercel.json` also sends the security headers (Content-Security-Policy with
`frame-ancestors 'none'`, `nosniff`, a referrer and a permissions policy) and keeps the
addresses' trailing slash, as the canonical links have it. A failed check fails the
deployment, so a broken site never replaces a working one.

`.github/workflows/website.yml` runs the same script on GitHub, so a pull request
shows the site's check beside the product's CI.

## What is where

| | |
|---|---|
| `pages.toml` | every page: its key, address, title, description and short name in both languages; a guide's order, time, difficulty and what it needs |
| `site.toml` | the site's settings (version, email, links) and the words every page shares |
| `faq.toml` | the questions; the FAQ page, the home page's six and the FAQPage data come from it |
| `content/<lang>/<key>.html` | each page's body |
| `templates/` | `base.html` (head, header, footer), `page.html`, `guide.html`, `macros.html` (screenshots, drawings, steps, callouts, commands, cards) |
| `static/` | `css/site.css`, `js/site.js`, the pictures, icons and share cards; served under `/assets/` |
| `root/` | files served at the site's root (`favicon.ico`, `apple-touch-icon.png`) |
| `build.py` | the build, and everything beside the pages: hreflang, JSON-LD, `sitemap.xml`, `robots.txt`, `llms.txt`, `llms-full.txt`, `security.txt`, `site.webmanifest` |
| `vercel-build.sh` | what Vercel and CI run: Jinja2, then `build.py --check` |
| `make_images.py` | the pictures, made once and committed |
| `images.json` | each picture's size, written by `make_images.py` |

The font is not copied here: the build takes Fredoka from
`src/biokids_core/data/static/fonts/`, so the site and the product share one copy.

## Rules

- **Nothing from another host.** No CDN, no Google Fonts, no analytics, no embeds, no
  cookies; the same reasons as the product's (CLAUDE.md §2). The one script,
  `static/js/site.js`, adds conveniences and stores only a per-tab flag. Vercel's Web
  Analytics and Speed Insights stay off: the privacy page says they are not used.
- **Only what the product does.** Every claim must be true of the current release; a
  thing that is coming says so ("Yakında" / "Coming soon"). When a release changes a
  label, a number or a step, change the page.
- **Both languages, always.** A page exists in both or not at all. Write each naturally
  rather than word for word.
- **Real pictures are labelled as real, drawings as drawings** (`m.shot` and `m.illus`).
- **Never change a published address** in `pages.toml`: people and search engines link
  to it.

## Add a page

1. Add it to `pages.toml` with a `key`, and `path`, `title`, `description` and `short`
   under `[page.tr]` and `[page.en]`. A guide also gets `kind = "guide"` and a
   `guide = { order, minutes, level, icon }`.
2. Write `content/tr/<key>.html` and `content/en/<key>.html`. A guide extends
   `guide.html` and puts its steps in `<ol class="steps">` with `m.step(title, id)`:
   `build.py` reads them back for the table of contents and the HowTo data.
3. `python website/make_images.py --og` makes its share card.
4. `python website/build.py --check`.

## Pictures

```bash
python website/make_images.py          # everything
python website/make_images.py --og     # only the share cards (needs Chrome or Edge)
```

Screenshots come from `assets/screenshots/<lang>/` (the READMEs') and from
`dist/website-shots/<lang>/`, which is not in git: the panel pages taken over
`dev/seed.py`'s family with a stand-in daemon and headless Chrome, as
`assets/brand/README.md` describes for the READMEs. Only the WebP copies in
`static/img/` are committed. Never put a real child's name, screen or history in one.

## Once, by the owner

- Vercel: import the repository as a project with the framework preset **Other** and the
  root directory left at the repository's root (`vercel.json` gives the build command
  and the output directory). Add the domain `biokids.bilalgurkansanli.com`, and point
  the project's `*.vercel.app` address at it with a redirect so the site has one
  address. Leave Web Analytics and Speed Insights off.
- DNS: the record Vercel asks for, a `CNAME` `biokids` → `cname.vercel-dns.com`. The
  APT archive keeps its own name, `apt.biokids.bilalgurkansanli.com`, served from the
  `BioKids-apt` repository (D-257); the two do not touch.
- Google Search Console and Bing Webmaster Tools: add the domain and submit
  `https://biokids.bilalgurkansanli.com/sitemap.xml`.
- Every October: renew `security_txt_expires` in `site.toml` (RFC 9116 wants a date
  within a year).
- With every release: `version` in `site.toml`; with the release key, its fingerprint
  on the download page.

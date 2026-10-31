#!/usr/bin/env python3
"""Build the BioKids website into website/_site/.

    python website/build.py              # build
    python website/build.py --serve      # build, then serve http://127.0.0.1:8000
    python website/build.py --check      # build, then check links, images and meta

The site is static HTML, written as Jinja2 templates in two languages: Turkish at
the root, English under /en/. Nothing in it is fetched from another host - no
font, no script, no analytics - for the same reason the product does not
(CLAUDE.md §2): a request to a third party is a tracking request.

What a page is lives in pages.toml (its address, title and description in each
language), the words every page shares in site.toml, the questions in faq.toml,
and each page's body in content/<lang>/<key>.html. Everything a search engine or
an assistant reads beside the page - hreflang pairs, JSON-LD, the sitemap,
llms.txt - is made here from those files, so it cannot drift from the pages.

Images are made once by make_images.py and committed; this script only copies.
Needs Jinja2 (a BioKids dependency already) and nothing else.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import html
import http.server
import json
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, ClassVar

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "_site"
"""Where the site is built; --out builds somewhere else (two builds at once)."""
FONTS = ROOT / "src" / "biokids_core" / "data" / "static" / "fonts"

LANGS = ("tr", "en")


# --------------------------------------------------------------------------- data


@dataclass(frozen=True, slots=True)
class Variant:
    """One page in one language."""

    lang: str
    path: str
    title: str
    description: str
    short: str
    summary: str


@dataclass(frozen=True, slots=True)
class Page:
    key: str
    kind: str
    variants: dict[str, Variant]
    guide: dict[str, Any] = field(default_factory=dict)
    noindex: bool = False

    def v(self, lang: str) -> Variant:
        return self.variants[lang]


def load_toml(name: str) -> dict[str, Any]:
    with (HERE / name).open("rb") as f:
        return tomllib.load(f)


def load_pages() -> list[Page]:
    pages = []
    for raw in load_toml("pages.toml")["page"]:
        variants = {}
        for lang in LANGS:
            v = raw[lang]
            variants[lang] = Variant(
                lang=lang,
                path=v["path"],
                title=v["title"],
                description=v["description"],
                short=v.get("short", v["title"]),
                summary=v.get("summary", v["description"]),
            )
        pages.append(
            Page(
                key=raw["key"],
                kind=raw.get("kind", "page"),
                variants=variants,
                guide=raw.get("guide", {}),
                noindex=raw.get("noindex", False),
            )
        )
    keys = [p.key for p in pages]
    if len(keys) != len(set(keys)):
        sys.exit("pages.toml: a key is used twice")
    return pages


# ------------------------------------------------------------------------ helpers


@functools.cache
def git_date(*paths: Path) -> str:
    """The last commit that touched any of `paths`, as YYYY-MM-DD; today if none.

    CI checks out the whole history (fetch-depth: 0) so this is the real date;
    a shallow clone gets the build date, which is only a little wrong."""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cs", "--", *map(str, paths)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    except OSError:
        out = ""
    return out or datetime.now(UTC).strftime("%Y-%m-%d")


@functools.cache
def asset_version(rel: str) -> str:
    """A short hash of a static file, for its URL: a browser that kept the old
    stylesheet must not use it with a new page."""
    data = (HERE / "static" / rel).read_bytes()
    return hashlib.sha256(data).hexdigest()[:8]


VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "source",
    "wbr",
}
"""Elements that have no end tag."""


class _Text(HTMLParser):
    """The readable text of an HTML fragment, for llms-full.txt and JSON-LD."""

    SKIP: ClassVar = {"script", "style", "svg", "button", "nav", "figure"}
    BLOCK: ClassVar = {"p", "li", "h1", "h2", "h3", "h4", "tr", "pre", "dt", "dd", "summary"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Inside a skipped element, count what opens in it so that its own end
        # tag is known; the build's HTML closes every element it opens.
        if self.skip:
            if tag not in VOID:
                self.skip += 1
        elif tag in self.SKIP or ("aria-hidden", "true") in attrs:
            if tag not in VOID:
                self.skip = 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
            if tag in {"h2", "h3"}:
                self.parts.append("#" * int(tag[1]) + " ")
            elif tag == "li":
                self.parts.append("- ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        pass  # <br/>, <img/>: nothing to read and nothing to close

    def handle_endtag(self, tag: str) -> None:
        if self.skip:
            if tag not in VOID:
                self.skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
        raw = re.sub(r" *\n *", "\n", raw)
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def text_of(fragment: str) -> str:
    p = _Text()
    p.feed(fragment)
    return p.text()


def main_of(page_html: str) -> str:
    m = re.search(r"<main\b[^>]*>(.*)</main>", page_html, re.S)
    return m.group(1) if m else ""


STEP_RE = re.compile(
    r'<li class="step"[^>]*\bid="(?P<id>[^"]+)"[^>]*>\s*<h3[^>]*>(?P<name>.*?)</h3>'
    r'\s*<div class="step-body">(?P<body>.*?)</div><!--/step-->',
    re.S,
)


def howto_steps(main_html: str, url: str) -> list[dict[str, Any]]:
    """The steps a guide marks up with the `step` macro, for HowTo JSON-LD."""
    steps = []
    for m in STEP_RE.finditer(main_html):
        body = text_of(m.group("body")).replace("\n", " ")
        steps.append(
            {
                "@type": "HowToStep",
                "name": text_of(m.group("name")),
                "text": body[:500].rsplit(" ", 1)[0] + "…" if len(body) > 500 else body,
                "url": f"{url}#{m.group('id')}",
            }
        )
    return steps


# ------------------------------------------------------------------------ the build


class Site:
    def __init__(self) -> None:
        self.cfg = load_toml("site.toml")
        self.site = self.cfg["site"]
        self.strings = self.cfg["strings"]
        self.faq = load_toml("faq.toml")
        self.pages = load_pages()
        self.by_key = {p.key: p for p in self.pages}
        self.base = self.site["url"].rstrip("/")
        self.env = Environment(
            loader=FileSystemLoader([HERE / "templates", HERE / "content"]),
            autoescape=select_autoescape(["html"]),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.images = json.loads((HERE / "images.json").read_text(encoding="utf-8"))
        self.env.globals.update(site=self.site, asset=self.asset, image=self.image)
        self.env.filters["longdate"] = self.longdate
        self.built: dict[str, str] = {}

    # -- template helpers

    def image(self, name: str) -> dict[str, int]:
        """Width, height and the small copy's width of static/img/<name>.webp."""
        if name not in self.images:
            raise KeyError(f"no image {name!r} in images.json: run make_images.py")
        return self.images[name]

    def longdate(self, iso: str, lang: str) -> str:
        y, mo, d = (int(x) for x in iso.split("-"))
        month = self.strings[lang]["months"][mo - 1]
        return f"{d} {month} {y}" if lang == "tr" else f"{month} {d}, {y}"

    # -- URLs

    def asset(self, rel: str) -> str:
        return f"/assets/{rel}?v={asset_version(rel)}"

    def abs(self, path: str) -> str:
        return self.base + path

    def url(self, key: str, lang: str, anchor: str = "") -> str:
        page = self.by_key.get(key)
        if page is None:
            raise KeyError(f"no page {key!r}")
        return page.v(lang).path + (f"#{anchor}" if anchor else "")

    def guides(self, lang: str) -> list[Page]:
        """The guides in reading order; templates read `.v(lang)` and `.guide`."""
        return sorted(
            (p for p in self.pages if p.kind == "guide"), key=lambda p: p.guide["order"]
        )

    # -- JSON-LD

    @property
    def author(self) -> dict[str, str]:
        """A reference to the author: the Person of his own site (site.toml)."""
        return {"@id": self.site["author_id"]}

    def person(self) -> dict[str, Any]:
        s = self.site
        return {
            "@type": "Person",
            "@id": s["author_id"],
            "name": s["author"],
            "alternateName": s["author_alt"],
            "url": s["author_url"],
            "email": f"mailto:{s['email']}",
            "sameAs": [s["github_user"]],
            "brand": {"@type": "Brand", "name": s["label"]},
        }

    def software(self, lang: str) -> dict[str, Any]:
        s, t = self.site, self.strings[lang]
        shots = ["hero", "launcher", "panel-live", "assistant", "panel-today", "panel-history"]
        return {
            "@type": "SoftwareApplication",
            "@id": f"{self.base}/#software",
            "name": "BioKids",
            "alternateName": ["BioKids Linux", "BioKids OS"],
            "description": t["software_description"],
            "url": self.abs(self.url("home", lang)),
            "applicationCategory": "UtilitiesApplication",
            "applicationSubCategory": t["software_category"],
            "operatingSystem": "Debian 13 (trixie), amd64",
            "softwareVersion": s["version"],
            "license": "https://opensource.org/licenses/MIT",
            "isAccessibleForFree": True,
            "offers": {"@type": "Offer", "price": "0", "priceCurrency": "TRY"},
            "downloadUrl": s["iso_url"],
            "installUrl": self.abs(self.url("guide-install", lang)),
            "releaseNotes": f"{s['repo']}/blob/main/CHANGELOG.md",
            "screenshot": [self.abs(f"/assets/img/{lang}/{n}.webp") for n in shots],
            "image": self.abs(f"/assets/og/{lang}-home.jpg"),
            "featureList": t["feature_list"],
            "memoryRequirements": t["req_memory"],
            "storageRequirements": t["req_disk"],
            "processorRequirements": "x86-64 (amd64)",
            "inLanguage": ["tr", "en"],
            "audience": {"@type": "Audience", "audienceType": t["audience"]},
            "author": self.author,
            "publisher": self.author,
            # The repository, and BioKids in the author's own list of his work.
            "sameAs": [s["repo"], s["work_id"]],
        }

    def source_code(self) -> dict[str, Any]:
        s = self.site
        return {
            "@type": "SoftwareSourceCode",
            "@id": f"{self.base}/#source",
            "name": "BioKids",
            "codeRepository": s["repo"],
            "programmingLanguage": ["Python", "HTML", "CSS", "JavaScript", "Shell"],
            "runtimePlatform": "Debian 13",
            "license": "https://opensource.org/licenses/MIT",
            "author": self.author,
            "targetProduct": {"@id": f"{self.base}/#software"},
        }

    def graph(
        self, page: Page, lang: str, main_html: str, modified: str
    ) -> list[dict[str, Any]]:
        v = page.v(lang)
        t = self.strings[lang]
        url = self.abs(v.path)
        website = {
            "@type": "WebSite",
            "@id": f"{self.base}/#website",
            "url": self.base + "/",
            "name": "BioKids",
            "alternateName": t["site_alt_name"],
            "description": t["software_description"],
            "inLanguage": ["tr", "en"],
            "publisher": self.author,
            "about": {"@id": f"{self.base}/#software"},
        }
        webpage: dict[str, Any] = {
            "@type": {"faq": "FAQPage", "about": "AboutPage", "support": "ContactPage"}.get(
                page.kind, "WebPage"
            ),
            "@id": f"{url}#webpage",
            "url": url,
            "name": v.title,
            "description": v.description,
            "inLanguage": lang,
            "isPartOf": {"@id": f"{self.base}/#website"},
            "about": {"@id": f"{self.base}/#software"},
            "dateModified": modified,
            "primaryImageOfPage": self.abs(f"/assets/og/{lang}-{page.key}.jpg"),
            "author": self.author,
        }
        nodes: list[dict[str, Any]] = [website, webpage, self.person(), self.software(lang)]
        if page.kind == "home":
            nodes.append(self.source_code())
        crumbs = self.breadcrumbs(page, lang)
        if len(crumbs) > 1:
            webpage["breadcrumb"] = {"@id": f"{url}#breadcrumb"}
            nodes.append(
                {
                    "@type": "BreadcrumbList",
                    "@id": f"{url}#breadcrumb",
                    "itemListElement": [
                        {
                            "@type": "ListItem",
                            "position": i,
                            "name": name,
                            "item": self.abs(path),
                        }
                        for i, (name, path) in enumerate(crumbs, 1)
                    ],
                }
            )
        if page.key == "guides":
            webpage["@type"] = "CollectionPage"
            webpage["mainEntity"] = {
                "@type": "ItemList",
                "itemListElement": [
                    {
                        "@type": "ListItem",
                        "position": i,
                        "name": g.v(lang).short,
                        "url": self.abs(g.v(lang).path),
                    }
                    for i, g in enumerate(self.guides(lang), 1)
                ],
            }
        if page.kind == "faq":
            webpage["mainEntity"] = [
                {
                    "@type": "Question",
                    "name": q["q"],
                    "acceptedAnswer": {"@type": "Answer", "text": text_of(q["a"])},
                }
                for group in self.faq[lang]
                for q in group["items"]
            ]
        if page.kind == "guide":
            steps = howto_steps(main_html, url)
            if steps:
                g = page.guide
                howto: dict[str, Any] = {
                    "@type": "HowTo",
                    "@id": f"{url}#howto",
                    "name": v.short,
                    "description": v.description,
                    "inLanguage": lang,
                    "totalTime": f"PT{g['minutes']}M",
                    "step": steps,
                }
                if g.get("supply", {}).get(lang):
                    howto["supply"] = [
                        {"@type": "HowToSupply", "name": n} for n in g["supply"][lang]
                    ]
                if g.get("tool", {}).get(lang):
                    howto["tool"] = [{"@type": "HowToTool", "name": n} for n in g["tool"][lang]]
                nodes.append(howto)
                webpage["mainEntity"] = {"@id": f"{url}#howto"}
        return nodes

    def breadcrumbs(self, page: Page, lang: str) -> list[tuple[str, str]]:
        t = self.strings[lang]
        crumbs = [(t["crumb_home"], self.url("home", lang))]
        if page.kind == "home" or page.kind == "404":
            return crumbs if page.kind == "home" else []
        if page.kind == "guide":
            hub = self.by_key["guides"].v(lang)
            crumbs.append((hub.short, hub.path))
        crumbs.append((page.v(lang).short, page.v(lang).path))
        return crumbs

    # -- pages

    def modified(self, page: Page, lang: str) -> str:
        """When a page last changed: its body, pages.toml (its title, and a
        guide's facts), and faq.toml for the two pages that show the questions.
        A guide's "last updated", every page's dateModified and the sitemap's
        lastmod are this one date, so a search engine is never told two."""
        paths = [HERE / "content" / lang / f"{page.key}.html", HERE / "pages.toml"]
        if page.kind in {"faq", "home"}:
            paths.append(HERE / "faq.toml")
        return git_date(*paths)

    def render(self, page: Page, lang: str) -> str:
        v = page.v(lang)
        other = "en" if lang == "tr" else "tr"
        modified = self.modified(page, lang)
        ctx = {
            "page": page,
            "v": v,
            "lang": lang,
            "other": other,
            "alt": page.v(other),
            "t": self.strings[lang],
            "url": lambda key, anchor="", lang=lang: self.url(key, lang, anchor),
            "guides": self.guides(lang),
            "faq": self.faq[lang],
            "crumbs": self.breadcrumbs(page, lang),
            "modified": modified,
            "abs": self.abs,
            "pages": self.by_key,
        }
        tpl = self.env.get_template(f"{lang}/{page.key}.html")
        out = tpl.render(**ctx)
        if "<!--TOC-->" in out:
            out = out.replace("<!--TOC-->", self.toc(main_of(out), lang), 1)
        if not page.noindex:
            graph = self.graph(page, lang, main_of(out), modified)
            data = json.dumps(
                {"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False
            )
            # `</` inside a <script> would end it early; JSON allows the escape.
            data = data.replace("</", "<\\/")
            out = out.replace(
                "<!--JSONLD-->", f'<script type="application/ld+json">{data}</script>', 1
            )
        else:
            out = out.replace("<!--JSONLD-->", "", 1)
        return out

    def toc(self, main_html: str, lang: str) -> str:
        """The page's h2s and steps, in order, as a list of links."""
        found = [
            (m.start(), m.group(1), text_of(m.group(2)), "h2")
            for m in re.finditer(r'<h2 id="([^"]+)"[^>]*>(.*?)</h2>', main_html, re.S)
        ]
        found += [
            (m.start(), m.group("id"), text_of(m.group("name")), "step")
            for m in STEP_RE.finditer(main_html)
        ]
        if not found:
            return ""
        items = []
        n = 0
        for _, anchor, name, kind in sorted(found):
            if kind == "step":
                n += 1
                label = f'<span class="toc-n">{n}</span> {html.escape(name)}'
            else:
                n = 0
                label = html.escape(name)
            items.append(f'<li class="toc-{kind}"><a href="#{anchor}">{label}</a></li>')
        title = self.strings[lang]["on_this_page"]
        return (
            '<nav class="toc" aria-labelledby="toc-title">'
            f'<p class="aside-title" id="toc-title">{title}</p>'
            f"<ol>{''.join(items)}</ol></nav>"
        )

    def write(self, rel: str, content: str | bytes) -> None:
        dest = OUT / rel.lstrip("/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            dest.write_bytes(content)
        else:
            dest.write_text(content, encoding="utf-8", newline="\n")

    def build(self) -> None:
        if OUT.exists():
            shutil.rmtree(OUT)
        OUT.mkdir()
        # static files, then the fonts from the product itself: one Fredoka.
        shutil.copytree(HERE / "static", OUT / "assets")
        (OUT / "assets" / "fonts").mkdir(exist_ok=True)
        for f in ("Fredoka-Regular.woff2", "Fredoka-Medium.woff2", "OFL.txt"):
            shutil.copy2(FONTS / f, OUT / "assets" / "fonts" / f)
        shutil.copytree(HERE / "root", OUT, dirs_exist_ok=True)

        for page in self.pages:
            if page.kind == "404":
                # The host serves one /404.html for every address, so it is one
                # page in both languages, written as content/tr/404.html.
                self.write("404.html", self.render(page, "tr"))
                continue
            for lang in LANGS:
                out = self.render(page, lang)
                self.built[f"{lang}:{page.key}"] = out
                self.write(page.v(lang).path + "index.html", out)

        self.write("sitemap.xml", self.sitemap())
        self.write("robots.txt", self.robots())
        self.write("llms.txt", self.llms())
        self.write("llms-full.txt", self.llms_full())
        self.write(".well-known/security.txt", self.security_txt())
        self.write("site.webmanifest", self.manifest())

    # -- the files beside the pages

    def sitemap(self) -> str:
        rows = ['<?xml version="1.0" encoding="UTF-8"?>']
        rows.append(
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            'xmlns:xhtml="http://www.w3.org/1999/xhtml">'
        )
        for page in self.pages:
            if page.noindex:
                continue
            for lang in LANGS:
                rows.append("  <url>")
                rows.append(f"    <loc>{self.abs(page.v(lang).path)}</loc>")
                rows.append(f"    <lastmod>{self.modified(page, lang)}</lastmod>")
                for alt in LANGS:
                    rows.append(
                        f'    <xhtml:link rel="alternate" hreflang="{alt}" '
                        f'href="{self.abs(page.v(alt).path)}"/>'
                    )
                rows.append(
                    '    <xhtml:link rel="alternate" hreflang="x-default" '
                    f'href="{self.abs(page.v("en").path)}"/>'
                )
                rows.append("  </url>")
        rows.append("</urlset>")
        return "\n".join(rows) + "\n"

    def robots(self) -> str:
        return (
            "# BioKids - free, open-source parental controls for Debian.\n"
            "# Every crawler is welcome, search engines and AI assistants alike:\n"
            "# the more accurately BioKids is described, the better.\n"
            "User-agent: *\n"
            "Allow: /\n"
            "\n"
            f"Sitemap: {self.base}/sitemap.xml\n"
        )

    def llms(self) -> str:
        """llms.txt (llmstxt.org): what BioKids is and where each answer lives,
        for an assistant that is asked about it."""
        s = self.site
        t = self.strings["en"]
        lines = [
            "# BioKids",
            "",
            f"> {t['software_description']}",
            "",
            t["llms_intro"].strip(),
            "",
            f"- Version: {s['version']}",
            f"- License: MIT. Source code: {s['repo']}",
            f"- Developer: {s['author']} ({s['label']}), {s['author_url']}",
            f"- Contact and support: {s['email']}",
            f"- Download: {s['iso_url']}",
            f"- Release notes and signed checksums: {s['release_url']}",
            "- Languages: Turkish and English (the site: Turkish at /, English at /en/)",
            "",
        ]
        sections = [
            ("Start here", ["home", "features", "download", "privacy"]),
            ("Guides", [p.key for p in self.guides("en")]),
            ("More", ["faq", "support", "about"]),
        ]
        for title, keys in sections:
            lines.append(f"## {title}")
            lines.append("")
            for key in keys:
                v = self.by_key[key].v("en")
                lines.append(f"- [{v.short}]({self.abs(v.path)}): {v.description}")
            lines.append("")
        lines.append("## Türkçe")
        lines.append("")
        for page in self.pages:
            if page.noindex:
                continue
            v = page.v("tr")
            lines.append(f"- [{v.short}]({self.abs(v.path)}): {v.description}")
        lines.append("")
        lines.append("## Optional")
        lines.append("")
        lines.append(
            f"- [Full text of every page]({self.base}/llms-full.txt): "
            "the English and Turkish pages as plain text"
        )
        lines.append(f"- [README]({s['repo']}#readme)")
        lines.append(f"- [Architecture]({s['repo']}/blob/main/docs/ARCHITECTURE.md)")
        lines.append(f"- [Security and threat model]({s['repo']}/blob/main/docs/SECURITY.md)")
        lines.append(f"- [Every design decision]({s['repo']}/blob/main/docs/DECISIONS.md)")
        return "\n".join(lines) + "\n"

    def llms_full(self) -> str:
        parts = [
            "# BioKids - the full text of biokids.bilalgurkansanli.com",
            "",
            f"> {self.strings['en']['software_description']}",
            "",
        ]
        for lang in ("en", "tr"):
            for page in self.pages:
                if page.noindex:
                    continue
                v = page.v(lang)
                body = text_of(main_of(self.built[f"{lang}:{page.key}"]))
                parts += ["", "---", "", f"URL: {self.abs(v.path)}", "", body, ""]
        return "\n".join(parts).strip() + "\n"

    def security_txt(self) -> str:
        s = self.site
        return (
            f"Contact: mailto:{s['email']}\n"
            f"Expires: {s['security_txt_expires']}\n"
            "Preferred-Languages: tr, en\n"
            f"Canonical: {self.base}/.well-known/security.txt\n"
            f"Policy: {s['repo']}/blob/main/SECURITY.md\n"
        )

    def manifest(self) -> str:
        t = self.strings["tr"]
        data = {
            "name": "BioKids",
            "short_name": "BioKids",
            "description": t["software_description"],
            "lang": "tr",
            "start_url": "/",
            "scope": "/",
            "display": "browser",
            "background_color": "#FBF7F2",
            "theme_color": "#FBF7F2",
            "icons": [
                {"src": "/assets/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/assets/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                {
                    "src": "/assets/icons/icon-maskable-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
            ],
        }
        return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


# ------------------------------------------------------------------------ the check


class _Refs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.imgs: list[dict[str, str | None]] = []
        self.ids: set[str] = set()
        self.dup_ids: set[str] = set()
        self.h1 = 0
        self.noindex = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "meta" and a.get("name") == "robots":
            self.noindex = "noindex" in (a.get("content") or "")
        if a.get("id"):
            if a["id"] in self.ids:
                self.dup_ids.add(a["id"] or "")
            self.ids.add(a["id"] or "")
        if tag == "a" and a.get("href"):
            self.links.append(a["href"] or "")
        if tag in {"link", "script", "source"}:
            ref = a.get("href") or a.get("src") or a.get("srcset")
            if ref and tag != "source":
                self.links.append(ref)
        if tag == "img":
            self.imgs.append(a)
            self.links.append(a.get("src") or "")
            for candidate in (a.get("srcset") or "").split(","):
                if candidate.strip():
                    self.links.append(candidate.split()[0])
        if tag == "h1":
            self.h1 += 1


def check(site: Site) -> int:
    """Every internal link, image and share card resolves, every image says what
    it shows, and every page has one h1 and a title and description of a
    sensible length."""
    problems: list[str] = []
    ids: dict[str, set[str]] = {}
    parsed: dict[str, _Refs] = {}
    for f in OUT.rglob("*.html"):
        rel = "/" + f.relative_to(OUT).as_posix()
        page_html = f.read_text(encoding="utf-8")
        p = _Refs()
        p.feed(page_html)
        parsed[rel] = p
        ids[rel.removesuffix("index.html")] = p.ids
        for card in re.findall(r'property="og:image" content="([^"]+)"', page_html):
            if not (OUT / card.removeprefix(site.base).lstrip("/")).exists():
                problems.append(f"{rel}: no share card {card} (make_images.py --og)")
        for block in re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', page_html, re.S
        ):
            try:
                json.loads(block)
            except ValueError as e:
                problems.append(f"{rel}: JSON-LD does not parse: {e}")
        # A link into the repository names a file that is there.
        for path in re.findall(
            re.escape(site.site["repo"]) + r'/(?:blob|tree)/main/([^"#?]+)', page_html
        ):
            if not (ROOT / path).exists():
                problems.append(f"{rel}: links to {path}, which is not in the repository")
    for rel, p in parsed.items():
        if p.h1 != 1:
            problems.append(f"{rel}: {p.h1} h1 elements")
        for dup in sorted(p.dup_ids):
            problems.append(f"{rel}: id {dup!r} is used twice")
        for img in p.imgs:
            if img.get("alt") is None:
                problems.append(f"{rel}: <img src={img.get('src')}> has no alt")
            if not (img.get("width") and img.get("height")):
                problems.append(f"{rel}: <img src={img.get('src')}> has no width/height")
        for link in p.links:
            if not link.startswith("/") or link.startswith("//"):
                continue
            path, _, anchor = link.partition("#")
            path = path.split("?", 1)[0]
            target = OUT / path.lstrip("/")
            if path.endswith("/"):
                target = target / "index.html"
            if not target.exists():
                problems.append(f"{rel}: broken link {link}")
            elif anchor and anchor not in ids.get(path, set()):
                problems.append(f"{rel}: no #{anchor} on {path}")
    # The sitemap lists every page there is to index, and only those.
    sitemap = (OUT / "sitemap.xml").read_text(encoding="utf-8")
    listed = re.findall(r"<loc>([^<]+)</loc>", sitemap)
    indexable = {
        site.abs(rel.removesuffix("index.html")) for rel, p in parsed.items() if not p.noindex
    }
    for loc in sorted(set(listed) - indexable):
        problems.append(f"sitemap.xml: {loc} is not a page that can be indexed")
    for loc in sorted(indexable - set(listed)):
        problems.append(f"sitemap.xml: {loc} is missing")
    if len(listed) != len(set(listed)):
        problems.append("sitemap.xml: an address is listed twice")
    for page in site.pages:
        for lang in LANGS:
            v = page.v(lang)
            if not 20 <= len(v.title) <= 65:
                problems.append(f"{lang}:{page.key}: title is {len(v.title)} chars (20-65)")
            if not 70 <= len(v.description) <= 165:
                problems.append(
                    f"{lang}:{page.key}: description is {len(v.description)} chars (70-165)"
                )
    for line in problems:
        print("  ✗", line)
    print(f"check: {len(parsed)} pages, {len(problems)} problems")
    return 1 if problems else 0


def serve(port: int) -> None:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(OUT))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        print(f"serving {OUT} on http://127.0.0.1:{port}/")
        httpd.serve_forever()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--serve", action="store_true", help="serve the result on 127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--check", action="store_true", help="check links, images and meta")
    ap.add_argument("--out", type=Path, help="build here instead of website/_site")
    args = ap.parse_args()
    if args.out:
        global OUT
        OUT = args.out.resolve()
    site = Site()
    site.build()
    n = sum(1 for _ in OUT.rglob("*.html"))
    print(f"built {n} pages into {OUT}")
    status = check(site) if args.check else 0
    if args.serve:
        serve(args.port)
    return status


if __name__ == "__main__":
    sys.exit(main())

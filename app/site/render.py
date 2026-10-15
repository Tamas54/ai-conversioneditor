"""Jinja2-renderelés a nyilvános oldalakhoz (szerveroldalon, JS nélkül is teljes tartalommal)."""
from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from app.config import settings
from app.site import i18n, urls

TEMPLATES = Path(__file__).parent / "templates"


@functools.lru_cache(maxsize=1)
def env() -> Environment:
    e = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]),
                    trim_blocks=True, lstrip_blocks=True)
    e.globals.update(urls=urls, i18n=i18n, settings=settings)
    e.filters["jsonld"] = lambda d: Markup(json.dumps(d, ensure_ascii=False).replace("</", "<\\/"))
    e.filters["tojson_attr"] = lambda d: json.dumps(d, ensure_ascii=False)
    return e


def static_v(name: str) -> str:
    from app.routes.product import static_v as sv
    return sv(name)


def render(page: dict[str, Any]) -> str:
    lang = page["lang"]

    def tr(key: str, **kw: Any) -> str:
        return i18n.t(lang, key, **kw)
    ctx = {
        "p": page, "t": tr, "lang": lang, "langs": i18n.langs(), "meta": i18n.meta(lang),
        "base": settings.PUBLIC_BASE_URL.rstrip("/"), "product": settings.PRODUCT_NAME,
        "css": static_v("site/site.css"), "js": static_v("site/site.js"),
        "robots": "index, follow" if page.get("index", True) and _indexing() else "noindex, follow",
        "lang_names": {L: i18n.raw(L, "_meta.native", L) for L in i18n.langs()},
        "nav": nav(lang),
        "product_css": static_v("product.css"),
        "parent": (page.get("crumbs") or [("", "/app")])[-2][1] if len(page.get("crumbs") or []) > 1 else "/app",
    }
    return env().get_template(f"{page['kind']}.html").render(**ctx)


def nav(lang: str) -> list[tuple[str, str]]:
    """A fejléc menüje — ugyanaz az /app-ban és a nyilvános oldalakon."""
    tr = lambda k: i18n.t(lang, k)  # noqa: E731
    return [(tr("nav.make"), "/app"), (tr("nav.convert"), urls.convert_hub(lang)), (tr("nav.tools"), urls.tools_hub(lang)),
            (tr("nav.agents"), urls.agents(lang)), (tr("nav.developers"), urls.developers(lang))]


@functools.lru_cache(maxsize=4)
def site_footer(lang: str) -> str:
    """A közös lábléc HTML-je az /app oldalaihoz (szerveroldalon renderelve: a keresők is látják a linkeket)."""
    return env().get_template("_foot.html").render(t=lambda k, **kw: i18n.t(lang, k, **kw), lang=lang,
                                                   product=settings.PRODUCT_NAME)


def _indexing() -> bool:
    from app import seo
    return seo.indexable()

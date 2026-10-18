"""A nyilvános oldalak útvonalai — nyelvenként, a nyelvi fájl slugjaival, explicit útvonalakként (nincs
mindent elnyelő catch-all, így az /app, /api, /mcp útvonalakat nem zavarja)."""
from __future__ import annotations

import functools
from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.catalog import tools as T
from app.catalog.formats import CATEGORIES, FORMATS
from app.convert import graph
from app.site import content, i18n, render, urls


@functools.lru_cache(maxsize=8192)
def _html(lang: str, kind: str, args: tuple, indexing: bool) -> tuple[str, bool]:
    builder: Callable = getattr(content, kind)
    page = builder(lang, *args)
    return render.render(page), page.get("index", True)


def _resp(lang: str, kind: str, *args) -> HTMLResponse:
    from app import seo
    html, index = _html(lang, kind, tuple(args), seo.indexable())
    headers = {"Cache-Control": "public, max-age=600", "Content-Language": i18n.raw(lang, "_meta.html", lang)}
    if not seo.indexable() or not index:
        headers["X-Robots-Tag"] = "noindex, follow" if seo.indexable() else "noindex, nofollow"
    return HTMLResponse(html, headers=headers)


def build_router() -> APIRouter:
    r = APIRouter()
    for lang in i18n.langs():
        _add(r, lang)
    return r


def _add(r: APIRouter, lang: str) -> None:
    P = i18n.prefix(lang)
    S = lambda k: i18n.slug(lang, k)  # noqa: E731

    def get(path: str, fn: Callable) -> None:
        r.add_api_route(path, fn, methods=["GET"], include_in_schema=False, response_class=HTMLResponse)

    if P:                       # a nyelvi gyökér a nyelv konverziós hubjára visz (a kezdőlap az /app)
        async def lang_root():
            return RedirectResponse(urls.convert_hub(lang), status_code=301)
        get(P + "/", lang_root)
        get(P, lang_root)

    async def agents():
        return _resp(lang, "agents")
    get(f"{P}/{S('agents')}", agents)

    async def convert_hub():
        return _resp(lang, "convert_hub")
    get(f"{P}/{S('convert')}", convert_hub)

    async def convert_item(item: str):
        cat = urls.find_by_slug(lang, "cat", item)
        if cat in CATEGORIES:
            return _resp(lang, "category", cat)
        pair = urls.parse_pair(lang, item)
        if pair and graph.plan(*pair, static=True):
            return _resp(lang, "pair", *pair)
        raise HTTPException(404, "Not found")
    get(f"{P}/{S('convert')}/{{item}}", convert_item)

    async def formats_hub():
        return _resp(lang, "formats_hub")
    get(f"{P}/{S('formats')}", formats_hub)

    async def fmt_page(item: str):
        key = next((f.key for f in FORMATS.values() if f.slug == item), None)
        if not key:
            raise HTTPException(404, "Not found")
        return _resp(lang, "format_page", key)
    get(f"{P}/{S('formats')}/{{item}}", fmt_page)

    async def tools_hub():
        return _resp(lang, "tools_hub")
    get(f"{P}/{S('tools')}", tools_hub)

    async def tool_page(item: str):
        key = urls.find_by_slug(lang, "tool", item)
        if not key or key not in T.TOOLS or not T.available(key, static=True):
            raise HTTPException(404, "Not found")
        return _resp(lang, "tool", key)
    get(f"{P}/{S('tools')}/{{item}}", tool_page)

    async def compare_hub():
        return _resp(lang, "compare_hub")
    get(f"{P}/{S('compare')}", compare_hub)

    async def compare_page(item: str):
        key = urls.find_by_slug(lang, "cmp", item)
        if key not in content.COMPETITORS:
            raise HTTPException(404, "Not found")
        return _resp(lang, "compare", key)
    get(f"{P}/{S('compare')}/{{item}}", compare_page)

    async def developers():
        return _resp(lang, "developers")
    get(f"{P}/{S('developers')}", developers)

    async def docs():
        return _resp(lang, "docs")
    get(f"{P}/{S('developers')}/{S('docs')}", docs)

    async def job_builder():
        return _resp(lang, "job_builder")
    get(f"{P}/{S('developers')}/{S('job_builder')}", job_builder)

    async def api_pair(item: str):
        pair = urls.parse_pair(lang, item)
        if not pair or not graph.plan(*pair, static=True):
            raise HTTPException(404, "Not found")
        return _resp(lang, "api_pair", *pair)
    get(f"{P}/{S('developers')}/{S('convert')}/{{item}}", api_pair)

    async def connect_ai():
        return _resp(lang, "connect_ai")
    get(f"{P}/{S('connect_ai')}", connect_ai)

    async def pricing():
        return _resp(lang, "pricing")
    get(f"{P}/{S('pricing')}", pricing)

    async def units():
        return _resp(lang, "units_page")
    get(f"{P}/{S('units')}", units)

    async def unit_cat(item: str):
        from app.catalog.units import CATEGORIES as UC
        key = urls.find_by_slug(lang, "unitcat", item)
        if key not in UC:
            raise HTTPException(404, "Not found")
        return _resp(lang, "units_page", key)
    get(f"{P}/{S('units')}/{{item}}", unit_cat)

    async def timezones():
        return _resp(lang, "timezones_page")
    get(f"{P}/{S('timezones')}", timezones)


def all_paths(indexable_only: bool = True) -> dict[str, list[str]]:
    """Minden nyilvános oldal útvonala nyelvenként (sitemap, IndexNow, llms-full)."""
    out: dict[str, list[str]] = {}
    from app.catalog.units import CATEGORIES as UC
    for lang in i18n.langs():
        ps = [urls.agents(lang), urls.convert_hub(lang), urls.formats_hub(lang), urls.tools_hub(lang), urls.compare_hub(lang), urls.developers(lang),
              urls.docs(lang), urls.job_builder(lang), urls.connect_ai(lang), urls.pricing(lang), urls.units(lang), urls.timezones(lang)]
        ps += [urls.category(lang, c) for c in CATEGORIES]
        ps += [urls.fmt(lang, k) for k, f in FORMATS.items() if not indexable_only or f.tier <= 2]
        ps += [urls.pair(lang, s, d) for s, d in (content.indexable_pairs() if indexable_only else graph.all_pairs(static=True))]
        ps += [urls.tool(lang, x.key) for x in T.live(static=True) if not indexable_only or content.tool_indexable(x.key)]
        ps += [urls.compare(lang, c) for c in content.COMPETITORS]
        ps += [urls.unit_cat(lang, c) for c in UC]
        ps += [urls.api_pair(lang, s, d) for s, d in content.indexable_pairs()
               if FORMATS[s].tier + FORMATS[d].tier <= 3]
        out[lang] = ps
    return out

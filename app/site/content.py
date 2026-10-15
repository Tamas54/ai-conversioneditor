"""Oldalmodellek: minden nyilvános oldal tartalma a katalógus tényeiből és a nyelvi fájl mondataiból.

Nem sablonmásolat: egy konverziós oldal a két formátum saját tényeit (fejlesztő, év, típus, MIME), a párra
jellemző megjegyzést (mi marad meg), a tényleges motorláncot, a célformátum alapértelmezett minőségét, a
kapcsolódó párokat és eszközöket mutatja — ezek páronként mások. A „válasz-első” bekezdés konkrét számokat ad
(formátumszám, méretkorlát, megőrzési idő, alapminőség), hogy egy AI-kereső pontosan idézhesse.
"""
from __future__ import annotations

import functools
from typing import Any, Optional

from app.catalog import tools as T
from app.catalog.formats import CATEGORIES, FORMATS, by_category
from app.config import settings
from app.convert import graph
from app.convert.engines import ENGINES
from app.site import i18n, urls

t = i18n.t


# ---------- tények ----------

def product() -> str:
    return settings.PRODUCT_NAME


@functools.lru_cache(maxsize=1)
def counts() -> dict[str, int]:
    pairs = graph.all_pairs(static=True)
    return {"formats": len(FORMATS), "conversions": len(pairs), "tools": len(T.live(static=True)),
            "categories": len(CATEGORIES)}


def limits() -> dict[str, Any]:
    from app import billing
    from app.auth import GUEST_MAX_UPLOAD_MB
    return {"guest_mb": GUEST_MAX_UPLOAD_MB, "retention": settings.FILE_RETENTION_DAYS, "link_hours": settings.FILE_TTL_HOURS,
            "batch_free": billing.PLANS["free"]["batch"], "batch_pro": billing.PLANS["pro"]["batch"],
            "api_rate": api_rate()}


def api_rate() -> int:
    import os
    return int(os.getenv("API_RATE_PER_MIN", "60"))


# a célformátum alapértelmezett minősége — a válasz-első bekezdés és a GYIK konkrét számai
DEFAULTS: dict[str, tuple[str, dict[str, Any]]] = {
    "jpg": ("quality", {"q": 88}), "jfif": ("quality", {"q": 88}), "webp": ("quality", {"q": 85}),
    "avif": ("quality", {"q": 70}), "heic": ("quality", {"q": 80}), "heif": ("quality", {"q": 80}),
    "jxl": ("quality", {"q": 85}), "png": ("lossless", {}), "tiff": ("lzw", {}), "gif": ("palette", {}),
    "mp3": ("bitrate", {"br": "192 kbit/s"}), "aac": ("bitrate", {"br": "192 kbit/s"}), "m4a": ("bitrate", {"br": "192 kbit/s"}),
    "ogg": ("bitrate", {"br": "192 kbit/s"}), "opus": ("bitrate", {"br": "128 kbit/s"}), "wma": ("bitrate", {"br": "192 kbit/s"}),
    "flac": ("lossless_audio", {}), "wav": ("pcm", {"bits": 16}), "aiff": ("pcm", {"bits": 16}), "alac": ("lossless_audio", {}),
    "mp4": ("video", {"codec": "H.264", "crf": 23, "audio": "AAC"}), "mov": ("video", {"codec": "H.264", "crf": 23, "audio": "AAC"}),
    "mkv": ("video", {"codec": "H.264", "crf": 23, "audio": "AAC"}), "m4v": ("video", {"codec": "H.264", "crf": 23, "audio": "AAC"}),
    "webm": ("video", {"codec": "VP9", "crf": 32, "audio": "Opus"}), "avi": ("video_q", {"codec": "MPEG-4 Part 2", "audio": "MP3"}),
    "pdf": ("pdf", {}), "pdfa": ("pdfa", {}), "docx": ("docx", {}), "epub": ("epub", {}), "zip": ("zip", {"level": 6}),
    "7z": ("7z", {}), "woff2": ("woff2", {}),
}

RELATED_TOOLS: dict[str, tuple[str, ...]] = {
    "document": ("merge-pdf", "compress-pdf", "ocr-pdf", "sign-document"),
    "spreadsheet": ("merge-files", "fill-form", "bulk-fill-form"),
    "presentation": ("compress-pdf", "merge-pdf", "thumbnail"),
    "ebook": ("merge-files", "compress-pdf", "thumbnail"),
    "image": ("compress-image", "resize-image", "crop-image", "watermark-image", "remove-metadata"),
    "vector": ("resize-image", "thumbnail", "compress-image"),
    "video": ("compress-video", "trim-video", "video-to-gif", "watermark-video", "extract-audio"),
    "audio": ("trim-audio", "compress-mp3", "adjust-volume", "merge-files"),
    "archive": ("create-archive", "extract-archive", "merge-files"),
    "cad": ("thumbnail", "compress-pdf"),
    "font": ("create-archive", "read-metadata"),
}


def pair_indexable(s: str, d: str) -> bool:
    fs, fd = FORMATS[s], FORMATS[d]
    if fs.category == fd.category:
        return fs.tier + fd.tier <= (3 if fs.category == "archive" else 4)
    return fs.tier + fd.tier <= 3


@functools.lru_cache(maxsize=1)
def indexable_pairs() -> tuple[tuple[str, str], ...]:
    return tuple(p for p in graph.all_pairs(static=True) if pair_indexable(*p))


def tool_indexable(key: str) -> bool:
    return T.TOOLS[key].tier <= 2


# ---------- közös részek ----------

def alternates(builder, *args) -> dict[str, str]:
    return {L: builder(L, *args) for L in i18n.langs()}


def crumbs_home(lang: str) -> list[tuple[str, str]]:
    """A morzsamenü gyökere a meglévő honlap (/app) — a nyilvános oldalak annak a részei."""
    return [(product(), urls.home(lang))]


def org_ld() -> dict[str, Any]:
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    return {"@context": "https://schema.org", "@type": "Organization", "name": product(), "url": base + "/",
            "logo": base + "/static/apple-touch-icon.png"}


def breadcrumb_ld(crumbs: list[tuple[str, str]]) -> dict[str, Any]:
    return {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": i, "name": n, "item": urls.absolute(u)} for i, (n, u) in enumerate(crumbs, 1)]}


def faq_ld(faq: list[tuple[str, str]]) -> Optional[dict[str, Any]]:
    if not faq:
        return None
    return {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq]}


def app_ld(lang: str, name: str, desc: str, path: str, features: list[str], category: str = "UtilitiesApplication") -> dict:
    return {"@context": "https://schema.org", "@type": "WebApplication", "name": name, "description": desc,
            "url": urls.absolute(path), "applicationCategory": category, "operatingSystem": "Any (web browser)",
            "inLanguage": i18n.raw(lang, "_meta.html", lang), "isAccessibleForFree": True,
            "offers": {"@type": "Offer", "price": "0", "priceCurrency": "EUR"}, "featureList": features,
            "provider": {"@type": "Organization", "name": product()}}


def base_page(lang: str, kind: str, title: str, desc: str, path: str, alts: dict[str, str], crumbs, index: bool = True,
              **extra) -> dict[str, Any]:
    ld = [org_ld()]
    if crumbs and len(crumbs) > 1:
        ld.append(breadcrumb_ld(crumbs))
    return {"kind": kind, "lang": lang, "title": title, "desc": desc, "path": path, "alternates": alts, "crumbs": crumbs,
            "index": index, "jsonld": ld, "product": product(), **extra}


def fmt_name(lang: str, key: str) -> str:
    return FORMATS[key].label


def fmt_desc(lang: str, key: str) -> str:
    """A formátum egy mondatos leírása: a nyelvi fájlból, vagy a tényekből összerakva."""
    own = i18n.raw(lang, f"fmt.{key}")
    if own:
        return own
    f = FORMATS[key]
    kind = t(lang, f"catone.{f.category}")
    by = t(lang, "conv.fact_by", dev=f.developer) if f.developer else ""
    year = t(lang, "conv.fact_year", year=f.year) if f.year else ""
    return t(lang, "conv.fact_sentence", label=f.label, name=f.name, kind=kind, by=by, year=year).replace("  ", " ").replace(" .", ".")


def fmt_facts(lang: str, key: str) -> list[tuple[str, str]]:
    f = FORMATS[key]
    rows = [(t(lang, "facts.name"), f.name), (t(lang, "facts.ext"), ", ".join("." + e for e in (f.key, *f.aliases))),
            (t(lang, "facts.mime"), f.mime), (t(lang, "facts.category"), t(lang, f"cat.{f.category}.name"))]
    if f.developer:
        rows.append((t(lang, "facts.developer"), f.developer))
    if f.year:
        rows.append((t(lang, "facts.year"), str(f.year)))
    tags = [t(lang, f"tag.{x}") for x in sorted(f.tags) if i18n.raw(lang, f"tag.{x}")]
    if tags:
        rows.append((t(lang, "facts.type"), ", ".join(tags)))
    return rows


def from_form(lang: str, label: str) -> str:
    """A nyelv „-ból/-ből”-szabálya (_meta.suffix_from) egy betűszóra; ahol nincs szabály, maga a címke."""
    rule = i18n.raw(lang, "_meta.suffix_from")
    if not rule:
        return label
    last = label[-1:].upper()
    return label + (rule["back"] if last and last in rule.get("back_endings", "") else rule["front"])


def engine_names(engs: list[str]) -> str:
    out = []
    for e in engs:
        name = {"archive": "7-Zip / libarchive", "slideshow": "LibreOffice + FFmpeg", "python": "Python"}.get(e) or ENGINES.get(e, (e,))[0]
        if name not in out:
            out.append(name)
    return " → ".join(out)


def tool_name(lang: str, key: str) -> str:
    return i18n.raw(lang, f"tool.{key}.name", key.replace("-", " ").capitalize())


def tool_link(lang: str, key: str) -> Optional[dict[str, str]]:
    if key not in T.TOOLS or not T.available(key, static=True):
        return None
    return {"name": tool_name(lang, key), "url": urls.tool(lang, key)}


def pair_link(lang: str, s: str, d: str) -> dict[str, str]:
    return {"name": t(lang, "conv.link", src=FORMATS[s].label, dst=FORMATS[d].label), "url": urls.pair(lang, s, d)}


# ---------- oldalak ----------

POPULAR = [("docx", "pdf"), ("pdf", "docx"), ("pdf", "jpg"), ("jpg", "pdf"), ("heic", "jpg"), ("png", "jpg"), ("webp", "png"),
           ("mp4", "mp3"), ("mov", "mp4"), ("mp4", "gif"), ("wav", "mp3"), ("m4a", "mp3"), ("epub", "pdf"), ("mobi", "epub"),
           ("pptx", "pdf"), ("xlsx", "pdf"), ("csv", "xlsx"), ("svg", "png"), ("png", "svg"), ("rar", "zip"), ("zip", "7z"),
           ("ttf", "woff2"), ("dwg", "pdf"), ("dxf", "svg"), ("avif", "jpg"), ("pdf", "pdfa")]


def convert_hub(lang: str) -> dict[str, Any]:
    c = counts()
    crumbs = crumbs_home(lang) + [(t(lang, "nav.convert"), urls.convert_hub(lang))]
    cats = [{"key": k, "name": t(lang, f"cat.{k}.name"), "url": urls.category(lang, k), "desc": t(lang, f"cat.{k}.desc"),
             "formats": [{"label": f.label, "url": urls.fmt(lang, f.key)} for f in sorted(by_category(k), key=lambda f: (f.tier, f.key))][:12]}
            for k in CATEGORIES]
    p = base_page(lang, "convert_hub", t(lang, "chub.title", formats=c["formats"]), t(lang, "chub.desc", formats=c["formats"], conversions=c["conversions"]),
                  urls.convert_hub(lang), alternates(urls.convert_hub), crumbs, h1=t(lang, "chub.h1"),
                  lead=t(lang, "chub.lead", formats=c["formats"], conversions=c["conversions"]), cats=cats,
                  popular=[pair_link(lang, s, d) for s, d in POPULAR])
    return p


def category(lang: str, cat: str) -> dict[str, Any]:
    fl = sorted(by_category(cat), key=lambda f: (f.tier, f.key))
    pairs = [p for p in indexable_pairs() if FORMATS[p[0]].category == cat]
    groups = []
    for f in fl:
        mine = [d for s, d in pairs if s == f.key]
        if mine:
            groups.append({"label": f.label, "url": urls.fmt(lang, f.key),
                           "links": [{"name": FORMATS[d].label, "url": urls.pair(lang, f.key, d)} for d in mine]})
    crumbs = crumbs_home(lang) + [(t(lang, "nav.convert"), urls.convert_hub(lang)), (t(lang, f"cat.{cat}.name"), urls.category(lang, cat))]
    n_pairs = len([p for p in graph.all_pairs(static=True) if FORMATS[p[0]].category == cat])
    title = t(lang, f"cat.{cat}.title", n=len(fl))
    desc = t(lang, f"cat.{cat}.desc")
    p = base_page(lang, "category", title, desc, urls.category(lang, cat), alternates(urls.category, cat), crumbs,
                  h1=t(lang, f"cat.{cat}.h1"),
                  lead=t(lang, "cat.lead", n=len(fl), pairs=n_pairs, name=t(lang, f"cat.{cat}.name").lower()) + " " + t(lang, f"cat.{cat}.intro"),
                  formats=[{"label": f.label, "name": f.name, "url": urls.fmt(lang, f.key), "desc": fmt_desc(lang, f.key)} for f in fl],
                  groups=groups, tools=[x for x in (tool_link(lang, k) for k in RELATED_TOOLS.get(cat, ())) if x],
                  cat=cat, widget={"accept": ",".join("." + f.key for f in fl)})
    p["jsonld"].append(app_ld(lang, t(lang, f"cat.{cat}.h1"), desc, urls.category(lang, cat), [f.label for f in fl[:30]]))
    return p


def format_page(lang: str, key: str) -> dict[str, Any]:
    f = FORMATS[key]
    outs = graph.targets(key, static=True)
    ins = graph.sources(key, static=True)
    crumbs = crumbs_home(lang) + [(t(lang, "nav.convert"), urls.convert_hub(lang)),
                                  (t(lang, f"cat.{f.category}.name"), urls.category(lang, f.category)), (f.label, urls.fmt(lang, key))]
    title = t(lang, "fmtp.title", label=f.label, name=f.name, n=len(outs))
    desc = t(lang, "fmtp.desc", label=f.label, name=f.name, n=len(outs), m=len(ins))
    p = base_page(lang, "format", title, desc, urls.fmt(lang, key), alternates(urls.fmt, key), crumbs, index=f.tier <= 2,
                  h1=t(lang, "fmtp.h1", label=f.label, name=f.name), lead=fmt_desc(lang, key), facts=fmt_facts(lang, key),
                  outs=[{"name": FORMATS[d].label, "url": urls.pair(lang, key, d)} for d in outs],
                  ins=[{"name": FORMATS[s].label, "url": urls.pair(lang, s, key)} for s in ins],
                  tools=[x for x in (tool_link(lang, tt.key) for tt in T.live() if T.accepts(tt, key) and tt.tier <= 2) if x][:12],
                  widget={"accept": "." + key, "src": key, "targets": outs[:40]})
    return p


def pair(lang: str, s: str, d: str) -> dict[str, Any]:
    fs, fd = FORMATS[s], FORMATS[d]
    plan = graph.plan(s, d, static=True) or []
    engs = [e.engine for e in plan]
    c, L = counts(), limits()
    S_, D_ = fs.label, fd.label
    crumbs = crumbs_home(lang) + [(t(lang, "nav.convert"), urls.convert_hub(lang)),
                                  (t(lang, f"cat.{fs.category}.name"), urls.category(lang, fs.category)),
                                  (t(lang, "conv.crumb", src=S_, dst=D_), urls.pair(lang, s, d))]
    kind, kv = DEFAULTS.get(d, ("", {}))
    default_sentence = t(lang, f"default.{kind}", dst=D_, **kv) if kind else ""
    note_key = f"pairnote.{fs.category}>{fd.category}"
    note = i18n.raw(lang, f"pairnote.{s}>{d}") or i18n.raw(lang, note_key) or ""
    steps_n = len(plan)
    SF = from_form(lang, S_)
    answer = t(lang, "conv.answer", src=S_, dst=D_, src_from=SF, engines=engine_names(engs),
               steps=t(lang, "conv.steps_1") if steps_n == 1 else t(lang, "conv.steps_n", n=steps_n),
               guest_mb=L["guest_mb"], retention=L["retention"], product=product()) + (" " + default_sentence if default_sentence else "")
    from app.convert.options import engine_choices, for_pair
    opts = []
    for o in for_pair(s, d):
        if not i18n.raw(lang, f"opt.{o.key}.label"):
            continue
        sch = o.schema()
        if o.key == "engine":
            ch = engine_choices(s, d)
            if len(ch) <= 2:
                continue
            sch["enum"] = ch
        opts.append({"key": o.key, "label": t(lang, f"opt.{o.key}.label"), "help": t(lang, f"opt.{o.key}.help"), "schema": sch})
    rev = graph.plan(d, s, static=True) is not None
    others_out = [x for x in graph.targets(s, static=True) if x != d][:12]
    others_in = [x for x in graph.sources(d, static=True) if x != s and pair_indexable(x, d)][:12]
    faq = _faq(lang, "conv.faq", src=S_, dst=D_, guest_mb=L["guest_mb"], retention=L["retention"], product=product(),
               engines=engine_names(engs), batch_free=L["batch_free"], batch_pro=L["batch_pro"], api=urls.absolute(urls.api_pair(lang, s, d)),
               mcp=urls.absolute(urls.connect_ai(lang)), note=note or t(lang, "conv.note_generic", dst=D_))
    title = t(lang, "conv.title", src=S_, dst=D_, src_from=SF)
    desc = t(lang, "conv.desc", src=S_, dst=D_, srcname=fs.name, dstname=fd.name)
    p = base_page(lang, "pair", title, desc, urls.pair(lang, s, d), alternates(urls.pair, s, d), crumbs,
                  index=pair_indexable(s, d), h1=t(lang, "conv.h1", src=S_, dst=D_), lead=answer, note=note,
                  src={"key": s, "label": S_, "name": fs.name, "desc": fmt_desc(lang, s), "facts": fmt_facts(lang, s), "url": urls.fmt(lang, s)},
                  dst={"key": d, "label": D_, "name": fd.name, "desc": fmt_desc(lang, d), "facts": fmt_facts(lang, d), "url": urls.fmt(lang, d)},
                  steps=[t(lang, f"conv.step{i}", src=S_, dst=D_) for i in range(1, 5)], options=opts, src_from=SF,
                  chain=[{"engine": engine_names([e.engine]), "from": FORMATS[e.src].label, "to": FORMATS[e.dst].label} for e in plan],
                  reverse=pair_link(lang, d, s) if rev else None,
                  others_out=[pair_link(lang, s, x) for x in others_out], others_in=[pair_link(lang, x, d) for x in others_in],
                  tools=[x for x in (tool_link(lang, k) for k in RELATED_TOOLS.get(fd.category, ())) if x],
                  api_url=urls.api_pair(lang, s, d), faq=faq, widget={"accept": "." + s, "src": s, "target": d},
                  cat_url=urls.category(lang, fs.category), cat_name=t(lang, f"cat.{fs.category}.name"))
    p["jsonld"].append(app_ld(lang, t(lang, "conv.h1", src=S_, dst=D_), desc, urls.pair(lang, s, d),
                              [t(lang, f"conv.step{i}", src=S_, dst=D_) for i in range(1, 4)]))
    p["jsonld"].append({"@context": "https://schema.org", "@type": "HowTo", "name": t(lang, "conv.h1", src=S_, dst=D_),
                        "step": [{"@type": "HowToStep", "position": i, "text": x} for i, x in enumerate(p["steps"], 1)]})
    _add_faq_ld(p)
    return p


def tools_hub(lang: str) -> dict[str, Any]:
    crumbs = crumbs_home(lang) + [(t(lang, "nav.tools"), urls.tools_hub(lang))]
    groups = []
    for g, tl in T.by_group().items():
        groups.append({"key": g, "name": t(lang, f"tgroup.{g}.name"), "desc": t(lang, f"tgroup.{g}.desc"),
                       "tools": [{"name": tool_name(lang, x.key), "url": urls.tool(lang, x.key),
                                  "desc": i18n.raw(lang, f"tool.{x.key}.desc", "")} for x in tl]})
    ai = [{"name": t(lang, f"aitool.{k}.name"), "url": f"/app/make/{k}", "desc": t(lang, f"aitool.{k}.desc")} for k in AI_APP_TOOLS]
    c = counts()
    return base_page(lang, "tools_hub", t(lang, "thub.title", n=c["tools"]), t(lang, "thub.desc", n=c["tools"]), urls.tools_hub(lang),
                     alternates(urls.tools_hub), crumbs, h1=t(lang, "thub.h1"), lead=t(lang, "thub.lead", n=c["tools"]),
                     groups=groups, ai=ai)


AI_APP_TOOLS = ("document", "presentation", "scan-to-text", "photos/restore", "print-design", "infographic", "book")


def tool(lang: str, key: str) -> dict[str, Any]:
    tt = T.TOOLS[key]
    L = limits()
    name = tool_name(lang, key)
    crumbs = crumbs_home(lang) + [(t(lang, "nav.tools"), urls.tools_hub(lang)), (name, urls.tool(lang, key))]
    accepted = T.accepted_formats(tt)
    fmts = ", ".join(FORMATS[k].label for k in accepted[:24]) + (" …" if len(accepted) > 24 else "") if accepted else ""
    kw = dict(name=name, guest_mb=L["guest_mb"], retention=L["retention"], product=product(), formats=fmts,
              max_files=tt.max_files, batch_free=L["batch_free"], batch_pro=L["batch_pro"],
              engines=engine_names(list(tt.engines)) if tt.engines else "")
    title = t(lang, f"tool.{key}.title", **kw)
    desc = t(lang, f"tool.{key}.desc", **kw)
    intro = t(lang, f"tool.{key}.intro", **kw)
    points = [x.format(**kw) if isinstance(x, str) else x for x in (i18n.raw(lang, f"tool.{key}.points", []) or [])]
    faq = [(q.format(**kw), a.format(**kw)) for q, a in (i18n.raw(lang, f"tool.{key}.faq", []) or [])] + \
        _faq(lang, "toolfaq", **kw)
    from app.convert.options import OPTIONS, TOOL_OPTIONS
    opts = [{"key": k, "label": t(lang, f"opt.{k}.label"), "help": t(lang, f"opt.{k}.help"),
             "schema": (TOOL_OPTIONS.get(k) or OPTIONS[k]).schema()} for k in tt.options if i18n.raw(lang, f"opt.{k}.label")]
    p = base_page(lang, "tool", title, desc, urls.tool(lang, key), alternates(urls.tool, key), crumbs, index=tool_indexable(key),
                  h1=t(lang, f"tool.{key}.h1", **kw), lead=intro, points=points, faq=faq, tool=tt, options=opts,
                  steps=[t(lang, f"toolstep.{i}", name=name) for i in range(1, 4)],
                  related=[x for x in (tool_link(lang, r) for r in tt.related) if x],
                  formats=[{"name": FORMATS[k].label, "url": urls.fmt(lang, k)} for k in accepted[:60]],
                  widget={"tool": key, "accept": ",".join("." + k for k in accepted) if "*" not in tt.accepts else "",
                          "min": tt.min_files, "max": tt.max_files, "ai": tt.ai},
                  api_op=tt.operation, app_path=tt.app_path)
    p["jsonld"].append(app_ld(lang, name, desc, urls.tool(lang, key), points or [name]))
    _add_faq_ld(p)
    return p


COMPETITORS = ("cloudconvert", "freeconvert", "smallpdf", "ilovepdf")
# összevetés: (sor kulcsa, mi, cloudconvert, freeconvert, smallpdf, ilovepdf) — y = igen, n = nincs a nyilvános
# funkciólistájukon, p = részben; a versenytárs-adatok a nyilvános weboldalukról, ellenőrizve: CMP_CHECKED
CMP_ROWS = [
    ("formats", "y", "y", "y", "p", "p"), ("api", "y", "y", "y", "y", "y"), ("ai_edit", "y", "n", "n", "p", "p"),
    ("form_detect", "y", "n", "n", "n", "n"), ("agent_fill", "y", "n", "n", "n", "n"), ("esign", "y", "n", "n", "y", "y"),
    ("mcp", "y", "n", "n", "n", "n"), ("editors", "y", "n", "n", "p", "p"), ("hu", "y", "n", "n", "p", "y"),
    ("open_engines", "y", "p", "p", "n", "n"),
]
CMP_CHECKED = "2026-10"


def compare_hub(lang: str) -> dict[str, Any]:
    crumbs = crumbs_home(lang) + [(t(lang, "nav.compare"), urls.compare_hub(lang))]
    items = [{"name": t(lang, f"cmp.{k}.name"), "url": urls.compare(lang, k), "desc": t(lang, f"cmp.{k}.desc", product=product())} for k in COMPETITORS]
    return base_page(lang, "compare_hub", t(lang, "cmphub.title", product=product()), t(lang, "cmphub.desc", product=product()),
                     urls.compare_hub(lang), alternates(urls.compare_hub), crumbs, h1=t(lang, "cmphub.h1", product=product()),
                     lead=t(lang, "cmphub.lead", product=product()), entries=items, rows=_cmp_rows(lang), checked=CMP_CHECKED)


def _cmp_rows(lang: str, only: Optional[str] = None) -> list[dict[str, Any]]:
    c = counts()
    out = []
    for key, *vals in CMP_ROWS:
        cells = dict(zip(("us",) + COMPETITORS, vals))
        if only:
            cells = {"us": cells["us"], only: cells[only]}
        out.append({"label": t(lang, f"cmprow.{key}", formats=c["formats"]), "cells": cells})
    return out


def compare(lang: str, key: str) -> dict[str, Any]:
    name = t(lang, f"cmp.{key}.name")
    crumbs = crumbs_home(lang) + [(t(lang, "nav.compare"), urls.compare_hub(lang)), (t(lang, "cmp.crumb", product=product(), name=name), urls.compare(lang, key))]
    c = counts()
    kw = dict(product=product(), name=name, formats=c["formats"], conversions=c["conversions"])
    faq = [(q.format(**kw), a.format(**kw)) for q, a in (i18n.raw(lang, f"cmp.{key}.faq", []) or [])]
    p = base_page(lang, "compare", t(lang, "cmp.title", **kw), t(lang, f"cmp.{key}.desc", **kw), urls.compare(lang, key),
                  alternates(urls.compare, key), crumbs, h1=t(lang, "cmp.h1", **kw), lead=t(lang, f"cmp.{key}.intro", **kw),
                  rows=_cmp_rows(lang, key), them=key, them_name=name, checked=CMP_CHECKED, faq=faq,
                  advantages=[t(lang, f"cmpadv.{a}", **kw) for a in ("ai_edit", "form_detect", "esign", "agent_fill", "mcp")],
                  others=[{"name": t(lang, f"cmp.{k}.name"), "url": urls.compare(lang, k)} for k in COMPETITORS if k != key])
    _add_faq_ld(p)
    return p


def developers(lang: str) -> dict[str, Any]:
    crumbs = crumbs_home(lang) + [(t(lang, "nav.developers"), urls.developers(lang))]
    L, c = limits(), counts()
    top = [{"name": t(lang, "api.pairlink", src=FORMATS[s].label, dst=FORMATS[d].label), "url": urls.api_pair(lang, s, d)} for s, d in POPULAR]
    return base_page(lang, "developers", t(lang, "dev.title", product=product()), t(lang, "dev.desc", product=product(), formats=c["formats"]),
                     urls.developers(lang), alternates(urls.developers), crumbs, h1=t(lang, "dev.h1"),
                     lead=t(lang, "dev.lead", formats=c["formats"], conversions=c["conversions"], tools=c["tools"], rate=L["api_rate"]),
                     base=settings.PUBLIC_BASE_URL.rstrip("/"), top=top, docs_url=urls.docs(lang), jb_url=urls.job_builder(lang),
                     connect_url=urls.connect_ai(lang))


def docs(lang: str) -> dict[str, Any]:
    crumbs = crumbs_home(lang) + [(t(lang, "nav.developers"), urls.developers(lang)), (t(lang, "docs.crumb"), urls.docs(lang))]
    from app.api_v1 import catalog_ops
    return base_page(lang, "docs", t(lang, "docs.title", product=product()), t(lang, "docs.desc", product=product()), urls.docs(lang),
                     alternates(urls.docs), crumbs, h1=t(lang, "docs.h1"), lead=t(lang, "docs.lead", rate=limits()["api_rate"]),
                     base=settings.PUBLIC_BASE_URL.rstrip("/"), ops=catalog_ops(), rate=limits()["api_rate"])


def job_builder(lang: str) -> dict[str, Any]:
    crumbs = crumbs_home(lang) + [(t(lang, "nav.developers"), urls.developers(lang)), (t(lang, "jb.crumb"), urls.job_builder(lang))]
    return base_page(lang, "job_builder", t(lang, "jb.title", product=product()), t(lang, "jb.desc"), urls.job_builder(lang),
                     alternates(urls.job_builder), crumbs, h1=t(lang, "jb.h1"), lead=t(lang, "jb.lead"),
                     base=settings.PUBLIC_BASE_URL.rstrip("/"))


def api_pair(lang: str, s: str, d: str) -> dict[str, Any]:
    fs, fd = FORMATS[s], FORMATS[d]
    S_, D_ = fs.label, fd.label
    crumbs = crumbs_home(lang) + [(t(lang, "nav.developers"), urls.developers(lang)), (t(lang, "api.crumb", src=S_, dst=D_), urls.api_pair(lang, s, d))]
    plan = graph.plan(s, d, static=True) or []
    from app.convert.options import for_pair
    SF = from_form(lang, S_)
    p = base_page(lang, "api_pair", t(lang, "api.title", src=S_, dst=D_, src_from=SF), t(lang, "api.desc", src=S_, dst=D_),
                  urls.api_pair(lang, s, d), alternates(urls.api_pair, s, d), crumbs,
                  index=pair_indexable(s, d) and fs.tier + fd.tier <= 3, h1=t(lang, "api.h1", src=S_, dst=D_, src_from=SF),
                  lead=t(lang, "api.lead", src=S_, dst=D_, engines=engine_names([e.engine for e in plan]), rate=limits()["api_rate"]),
                  base=settings.PUBLIC_BASE_URL.rstrip("/"), s=s, d=d, src_label=S_, dst_label=D_,
                  options=[o.key for o in for_pair(s, d)], web_url=urls.pair(lang, s, d), docs_url=urls.docs(lang))
    return p


def connect_ai(lang: str) -> dict[str, Any]:
    crumbs = crumbs_home(lang) + [(t(lang, "nav.connect_ai"), urls.connect_ai(lang))]
    from app.mcp_server import mcp
    tools_ = sorted(((x.name, (x.description or "").strip().split("\n")[0][:220]) for x in mcp._tool_manager.list_tools()))
    p = base_page(lang, "connect_ai", t(lang, "cai.title", product=product()), t(lang, "cai.desc", product=product()),
                  urls.connect_ai(lang), alternates(urls.connect_ai), crumbs, h1=t(lang, "cai.h1", product=product()),
                  lead=t(lang, "cai.lead", product=product(), n=len(tools_)), mcp_url=settings.PUBLIC_BASE_URL.rstrip("/") + "/mcp",
                  mcp_tools=tools_, faq=_faq(lang, "cai.faq", product=product()))
    _add_faq_ld(p)
    return p


@functools.lru_cache(maxsize=1)
def agent_counts() -> dict[str, int]:
    """Az app ügynökének eszközei (a még nem kész, „PLACEHOLDER” jelűek nélkül) és az MCP-eszközök száma."""
    from app.llm import agent as A
    from app.mcp_server import mcp
    return {"agent": len([s for s in A.TOOL_SCHEMAS if "PLACEHOLDER" not in s["function"]["description"]]),
            "mcp": len(mcp._tool_manager.list_tools())}


def _agent_link(lang: str, label: str, ref: str) -> Optional[dict[str, str]]:
    if not ref.startswith("@"):
        return {"name": label, "url": ref}
    kind, _, arg = ref[1:].partition(":")
    if kind == "tool":
        return {"name": label, "url": urls.tool(lang, arg)} if arg in T.TOOLS else None
    fn = getattr(urls, kind, None)
    return {"name": label, "url": fn(lang, arg) if arg else fn(lang)} if fn else None


def agents(lang: str) -> dict[str, Any]:
    """Az app meglévő ügynöki funkciói egy oldalon: az ügynök, szerkesztés kéréssel, WordCraft, készítők, fotó,
    szken/űrlap/aláírás, MCP, API — mindegyik a meglévő termékoldalakra és eszközökre linkel."""
    c, L, n = counts(), limits(), agent_counts()
    kw = dict(product=product(), n_agent=n["agent"], n_mcp=n["mcp"], formats=c["formats"], tools=c["tools"],
              guest_mb=L["guest_mb"], retention=L["retention"])
    crumbs = crumbs_home(lang) + [(t(lang, "agents.crumb"), urls.agents(lang))]
    sections = []
    for sec in i18n.raw(lang, "agents.sections", []) or []:
        sections.append({"h": sec["h"].format_map(i18n._Safe(kw)), "p": sec["p"].format_map(i18n._Safe(kw)),
                         "examples": sec.get("examples", []),
                         "links": [x for x in (_agent_link(lang, a, b) for a, b in sec.get("links", [])) if x]})
    title, desc = t(lang, "agents.title", **kw), t(lang, "agents.desc", **kw)
    p = base_page(lang, "agents", title, desc, urls.agents(lang), alternates(urls.agents), crumbs,
                  h1=t(lang, "agents.h1", **kw), lead=t(lang, "agents.lead", **kw), sections=sections, n=n, counts=c,
                  faq=_faq(lang, "agents.faq", **kw))
    p["jsonld"].append(app_ld(lang, f"{product()} — {t(lang, 'agents.h1')}", desc, urls.agents(lang),
                              [s["h"] for s in sections], "BusinessApplication"))
    _add_faq_ld(p)
    return p


def units_page(lang: str, cat: Optional[str] = None) -> dict[str, Any]:
    from app.catalog import units as U
    crumbs = crumbs_home(lang) + [(t(lang, "nav.units"), urls.units(lang))]
    if cat:
        crumbs.append((t(lang, f"unitcat.{cat}.name"), urls.unit_cat(lang, cat)))
    cats = [{"key": k, "name": t(lang, f"unitcat.{k}.name"), "url": urls.unit_cat(lang, k)} for k in U.CATEGORIES]
    units_ = U.units_for(cat) if cat else []
    title = t(lang, "units.cat_title", name=t(lang, f"unitcat.{cat}.name")) if cat else t(lang, "units.title")
    desc = t(lang, "units.cat_desc", name=t(lang, f"unitcat.{cat}.name").lower(), n=len(units_)) if cat else t(lang, "units.desc", n=len(U.UNITS))
    path = urls.unit_cat(lang, cat) if cat else urls.units(lang)
    alts = alternates(urls.unit_cat, cat) if cat else alternates(urls.units)
    examples = U.examples(cat) if cat else []
    p = base_page(lang, "units", title, desc, path, alts, crumbs, h1=title,
                  lead=desc, cats=cats, cat=cat,
                  units=[{"key": u.key, "name": i18n.raw(lang, f"unit.{u.key}", u.symbol), "symbol": u.symbol} for u in units_],
                  examples=[{"text": t(lang, "units.example", v=_num(lang, v), a=i18n.raw(lang, f"unit.{a}", a), r=_num(lang, r), b=i18n.raw(lang, f"unit.{b}", b))}
                            for v, a, r, b in examples])
    p["jsonld"].append(app_ld(lang, title, desc, path, [c["name"] for c in cats]))
    return p


def timezones_page(lang: str) -> dict[str, Any]:
    from app.catalog import units as U
    crumbs = crumbs_home(lang) + [(t(lang, "nav.timezones"), urls.timezones(lang))]
    p = base_page(lang, "timezones", t(lang, "tz.title"), t(lang, "tz.desc"), urls.timezones(lang), alternates(urls.timezones), crumbs,
                  h1=t(lang, "tz.h1"), lead=t(lang, "tz.lead"), zones=U.common_zones())
    p["jsonld"].append(app_ld(lang, t(lang, "tz.h1"), t(lang, "tz.desc"), urls.timezones(lang), U.common_zones()[:10]))
    return p


def _num(lang: str, v: float) -> str:
    s = f"{v:,.6g}" if abs(v) < 1e15 else f"{v:.4e}"
    if lang == "hu":
        s = s.replace(",", " ").replace(".", ",")
    return s


def _faq(lang: str, key: str, **kw) -> list[tuple[str, str]]:
    items = i18n.raw(lang, key, []) or []
    out = []
    for q, a in items:
        out.append((q.format_map(i18n._Safe(kw)), a.format_map(i18n._Safe(kw))))
    return out


def _add_faq_ld(p: dict[str, Any]) -> None:
    ld = faq_ld(p.get("faq") or [])
    if ld:
        p["jsonld"].append(ld)

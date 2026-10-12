"""Keresők és AI-crawlerek: statikus eszköz-oldalak (cím, leírás, kanonikus URL, schema.org, látható ismertető),
robots.txt, llms.txt, sitemap.xml.

Az indexelés KAPCSOLÓVAL nyílik (INDEXING_OPEN=1) — amíg nincs végleges név és domain, a robots.txt mindent tilt és
minden HTML-válasz `X-Robots-Tag: noindex`. Nyitva is noindex marad, ami személyes: /app/file, /app/files, /app/connect.
Az llms.txt mindig elérhető (egy ügynök, akinek a linket megadják, ebből tudja meg, mit tud a szolgáltatás és hogyan
kapcsolódjon MCP-n).

A tartalom a keresési szándékra épül: a cím a kanonikus keresőkifejezéssel kezdődik („AI presentation maker”,
„Restore old photos online”, „Merge PDF”…), a leírás és a látható ismertető a kontextust adja, a GYIK a kérdés-alakú
kereséseket fedi; schema.org WebApplication + FAQPage + BreadcrumbList.
"""
from __future__ import annotations

import html
import json
from typing import Any, Optional

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, Response

from app.config import settings

router = APIRouter()

# eszköz-oldalak: útvonal (/app/make/…) → keresési szándék szerinti tartalom
PAGES: dict[str, dict[str, Any]] = {
    "document": {
        "title": "Online Word editor & AI document writer",
        "desc": "Write and edit Word documents in your browser — letters, reports, CVs — or let AI draft one from a short brief, then change it by asking.",
        "h1": "Write and edit Word documents online",
        "intro": "A Word-like editor in the browser, with templates for letters, reports and CVs. Describe what you need and the assistant writes a first draft from your facts only; then ask for changes in plain words — “indent every paragraph after the third”, “add a photo on page 22”.",
        "points": ["Edit .docx, .odt and .rtf files in the browser", "AI first drafts from a short brief", "Restyle a document like an example you upload", "Download as Word or PDF, print in one click"],
        "faq": [("Do I need Microsoft Word?", "No. The editor runs in your browser and saves real .docx files that open in Word."),
                ("Can it match my company's style?", "Yes. Upload one of your documents as the style example and the new one takes its fonts, headings and margins.")],
    },
    "presentation": {
        "title": "AI presentation maker — slides from a brief, a document or your template",
        "desc": "Make a designed PowerPoint deck from a short brief or from a Word document, on your own .pptx template, then edit it like in PowerPoint.",
        "h1": "Make a presentation — from a brief, a document or your template",
        "intro": "Describe the talk, or hand over a Word document: its headings become slides, its facts, tables and pictures are kept. Use your own PowerPoint template and the deck is built on its layouts, background and logo. Edit every slide by hand or by asking.",
        "points": ["Word to PowerPoint in one step", "Builds on your .pptx / .potx template", "Charts, tables and photos placed for you", "A Word handout back from any deck"],
        "faq": [("Can I turn a Word document into slides?", "Yes — choose “From a document”. Its structure, numbers and pictures are kept."),
                ("Will it use my company template?", "Yes. Add your .pptx or .potx as the style example: masters, layouts, backgrounds and logo stay.")],
    },
    "social-post": {
        "title": "Social media post maker — every size at once",
        "desc": "Headline and picture in, square, landscape and story sizes out — ready to post, or open it in the design editor to fine-tune.",
        "h1": "Make a social media post in every size",
        "intro": "Write a headline, pick a picture — your own, a free stock photo or an AI image — and get Instagram, LinkedIn and story sizes in one go. Cut the subject out onto a new background if you like, then fine-tune it in the design editor; the text stays editable.",
        "points": ["Square, landscape and story sizes", "Free stock photos or your own", "Background removal", "Editable in the design editor"],
        "faq": [("Which sizes do I get?", "1080×1080, 1200×630 and 1080×1920 — tick the ones you need.")],
    },
    "print-design": {
        "title": "Poster, flyer & certificate maker",
        "desc": "Posters, flyers, report covers and certificates — type the words, get a print-ready PDF with real typography, then fine-tune it in the design editor.",
        "h1": "Design posters, flyers and certificates",
        "intro": "Choose a format and fill in the words; layout and typography are done for you. It opens in an Illustrator-like design editor with every text still editable, and the print-ready PDF and PNG are in your files.",
        "points": ["Print-ready PDF", "Posters, flyers, report covers, certificates", "Your picture, a stock photo or an AI image", "Editable design file"],
        "faq": [("Is it print-ready?", "Yes — a vector PDF at the right paper size (A3, A4, A5, Letter).")],
    },
    "photos": {
        "title": "AI photo editor — edit photos with words, restore old photos",
        "desc": "Edit photos by describing the change, restore and colourise old family photos, remove backgrounds — every step as its own layer in a Photoshop file.",
        "h1": "Edit photos by describing the change",
        "intro": "“A bit warmer, dramatic sky, crop to 4:5, our logo bottom right” — done precisely, each step on its own layer in a PSD you can open and adjust. Photograph a whole album page and every old print is found, repaired and coloured.",
        "points": ["Precise edits from words, as layers", "Restore and colourise old photos", "Remove or replace the background", "Opens in a Photoshop-like editor"],
        "faq": [("Will it change my photo beyond recognition?", "No. Exact edits are done with real photo tools; AI retouching goes on a separate layer you can switch off."),
                ("Can it restore torn or faded photos?", "Yes — scratches, dust, stains, tears, fading and blur, and black-and-white photos can be colourised.")],
    },
    "photos/restore": {
        "title": "Restore old photos online — repair & colourise",
        "desc": "Repair scratched, torn, faded and blurry old photos and colourise black-and-white ones. Photograph a whole album page — every picture is found.",
        "h1": "Restore and colourise old photos",
        "intro": "Scratches, dust, stains, tears, fading and blur are repaired; black-and-white pictures can be coloured. The original stays as the bottom layer of a Photoshop file, so anything you don't like can be brought back.",
        "points": ["Whole album pages at once", "Colourise black and white", "Original kept as a layer"],
        "faq": [("How many photos at once?", "A whole album page — every print on it is found, straightened and restored separately.")],
    },
    "book": {
        "title": "Book typesetting — manuscript to book PDF",
        "desc": "Turn your Word manuscript into a typeset book: A5 or A4, book fonts, title page with cover. Check every page in the Word editor before you publish.",
        "h1": "Typeset your manuscript as a book",
        "intro": "Upload your .docx with chapter headings. It comes back typeset as a book — trim size, book typography, a title page with your cover — and opens in the Word editor so you can check every page. The PDF is made alongside.",
        "points": ["A5 or A4 trim size", "Title page with cover", "Check and fix in the Word editor", "Print-ready PDF"],
        "faq": [("Can I edit it after typesetting?", "Yes — the book is a Word document; fix anything and make the PDF again.")],
    },
    "scan-to-text": {
        "title": "Photo to text (OCR) — scan old books to Word",
        "desc": "Phone photos or scans of printed pages to an editable Word document, a clean new PDF or a searchable PDF — old spelling kept, pictures cut out.",
        "h1": "Turn photos of pages into editable text",
        "intro": "An old book, letters, a magazine: photograph the pages, and every page is read — headings, footnotes and pictures included — into Word, a new clean PDF or a searchable PDF that keeps the original look. Printed forms can be rebuilt as fillable PDFs.",
        "points": ["Word, PDF, searchable PDF or plain text", "Pictures and footnotes kept", "Old spelling stays as printed", "Printed forms → fillable PDF"],
        "faq": [("Does it work with phone photos?", "Yes — flat, well-lit pages, one page per photo. Scanned PDFs work too.")],
    },
    "pdf-tools": {
        "title": "PDF tools — merge, compress, PDF to Word, redact",
        "desc": "Merge PDFs, make them smaller, convert PDF to Word, add page numbers or a watermark, black out text for good, lock with a password, compare versions.",
        "h1": "PDF tools",
        "intro": "Pick a job, add your PDF and get a new file — the original stays as it was. Redaction removes the text from the file, not just covers it; scanned PDFs can be made searchable; printed forms can be made fillable.",
        "points": ["Merge and compress", "PDF to Word", "Redact for good", "Page numbers, watermark, password"],
        "faq": [("Is redaction permanent?", "Yes — the text is removed from the file, not just covered with a box.")],
    },
    "convert": {
        "title": "File converter — Word, PDF, PowerPoint, images",
        "desc": "Convert between Word, PDF, PowerPoint, Excel, images, Photoshop files and e-books with the best engine for each pair. The original stays as it was.",
        "h1": "Convert a file",
        "intro": "Pick a file and the format you need: DOCX, PDF, PPTX, XLSX, PNG, JPG, WebP, PSD, SVG, EPUB and more. Each pair uses the engine that does it best.",
        "points": ["Word ↔ PDF", "PowerPoint → PDF or images", "Images and Photoshop files", "E-books"],
        "faq": [("Which formats are supported?", "Word, OpenDocument, RTF, Markdown, HTML, PDF, PowerPoint, Excel/CSV, PNG, JPG, WebP, TIFF, PSD, SVG and EPUB.")],
    },
}
HOME = {
    "title": "{P} — documents, slides, photos and PDFs, changed by asking",
    "desc": "Documents, decks, old photos, scanned books, PDFs and forms — made for you, then fixed in plain words or by hand in built-in editors. Works from your own AI too.",
}
PRIVATE = ("file", "files", "connect")


def indexable() -> bool:
    return bool(settings.INDEXING_OPEN)


def _base() -> str:
    return settings.PUBLIC_BASE_URL.rstrip("/")


def page_for(rel: str) -> dict[str, Any]:
    """rel = az /app utáni útvonal (pl. „make/photos/restore”) → a fej és a látható ismertető adatai."""
    P = settings.PRODUCT_NAME
    parts = [p for p in rel.split("/") if p]
    base = _base()
    out: dict[str, Any] = {"title": HOME["title"].replace("{P}", P), "desc": HOME["desc"], "canonical": f"{base}/app",
                           "robots": "index, follow", "jsonld": [], "about": ""}
    if parts and parts[0] in PRIVATE:
        out["robots"] = "noindex, nofollow"
        out["canonical"] = f"{base}/app/{parts[0]}" if parts[0] != "file" else f"{base}/app/files"
        return out
    if parts and parts[0] == "plans":
        out.update(title=f"Plans & pricing — {P}", desc="Free to try; Plus and Pro plans with monthly credits, credit packs without a subscription.",
                   canonical=f"{base}/app/plans")
        return out
    if parts[:1] == ["make"] and len(parts) >= 2:
        key = "/".join(parts[1:3]) if "/".join(parts[1:3]) in PAGES else parts[1]
        pg = PAGES.get(key)
        if pg:
            url = f"{base}/app/make/{key}"
            out.update(title=f"{pg['title']} | {P}", desc=pg["desc"], canonical=url)
            out["jsonld"] = [
                {"@context": "https://schema.org", "@type": "WebApplication", "name": f"{P} — {pg['h1']}", "url": url,
                 "description": pg["desc"], "applicationCategory": "MultimediaApplication", "operatingSystem": "Any (web browser)",
                 "offers": {"@type": "Offer", "price": "0", "priceCurrency": "EUR"}, "featureList": pg["points"]},
                {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
                    {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in pg["faq"]]},
                {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": P, "item": f"{base}/app"},
                    {"@type": "ListItem", "position": 2, "name": pg["h1"], "item": url}]},
            ]
            e = html.escape
            points = "".join("<li>" + e(x) + "</li>" for x in pg["points"])
            faq = "".join("<h3>" + e(q) + "</h3><p>" + e(a) + "</p>" for q, a in pg["faq"])
            more = " · ".join('<a href="/app/make/' + k + '">' + e(PAGES[k]["h1"]) + "</a>" for k in PAGES if k != key and "/" not in k)
            out["about"] = ('<section class="tool-about" id="tool-about"><h1>' + e(pg["h1"]) + "</h1><p>" + e(pg["intro"]) + "</p>"
                            + "<ul>" + points + '</ul><div class="faq">' + faq + '</div><p class="more">More tools: ' + more + "</p></section>")
    return out


def head_html(meta: dict[str, Any]) -> str:
    e = html.escape
    tags = [f'<link rel="canonical" href="{e(meta["canonical"])}">',
            f'<meta name="robots" content="{e(meta["robots"] if indexable() else "noindex, nofollow")}">']
    for block in meta["jsonld"]:
        tags.append(f'<script type="application/ld+json">{json.dumps(block, ensure_ascii=False)}</script>')
    return "\n".join(tags)


def robots_header(meta: Optional[dict[str, Any]] = None) -> dict[str, str]:
    if not indexable() or (meta and meta["robots"].startswith("noindex")):
        return {"X-Robots-Tag": "noindex, nofollow"}
    return {}


@router.get("/robots.txt", include_in_schema=False)
async def robots() -> PlainTextResponse:
    if not indexable():
        return PlainTextResponse("# Not open for indexing yet.\nUser-agent: *\nDisallow: /\n")
    body = f"""User-agent: *
Allow: /app$
Allow: /app/make/
Allow: /app/plans
Allow: /llms.txt
Disallow: /app/file/
Disallow: /app/files
Disallow: /app/connect
Disallow: /api/
Disallow: /files/
Disallow: /preview/
Disallow: /editor/
Disallow: /mcp
Disallow: /dashboard
Disallow: /login
Disallow: /signup

Sitemap: {_base()}/sitemap.xml
"""
    return PlainTextResponse(body)


@router.get("/sitemap.xml", include_in_schema=False)
async def sitemap() -> Response:
    base = _base()
    urls = [f"{base}/app", f"{base}/app/plans", *(f"{base}/app/make/{k}" for k in PAGES)]
    body = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + \
        "".join(f"  <url><loc>{html.escape(u)}</loc></url>\n" for u in urls) + "</urlset>\n"
    return Response(body, media_type="application/xml", headers=robots_header())


@router.get("/llms.txt", include_in_schema=False)
async def llms() -> PlainTextResponse:
    """llmstxt.org: rövid, Markdown leírás AI-ügynököknek — mit tud a szolgáltatás, hol vannak az eszközök, hogyan
    kapcsolódjon MCP-n."""
    P, base = settings.PRODUCT_NAME, _base()
    tools = "\n".join(f"- [{v['h1']}]({base}/app/make/{k}): {v['desc']}" for k, v in PAGES.items())
    body = f"""# {P}

> Make and edit real files — Word documents, PowerPoint decks, PDFs, photos (PSD), print and social designs, scanned
> books and forms — from a short brief or an existing file, then change them in plain words or by hand in built-in
> editors. People use it in the browser; AI agents use the same tools over MCP.

## Tools

{tools}

## For AI agents (MCP)

- Endpoint: `{base}/mcp` (Streamable HTTP). Authenticate with a personal key the person creates under
  [Connect your AI]({base}/app/connect) — `Authorization: Bearer <key>` or `?key=<key>`.
- Every file in a tool result carries `edit_url`: give it to the person; it opens that file in the matching editor.
- Useful chains: write and revise a document (`wordcraft`) → slides from it (`pptx_build` with `from_document`) →
  a Word handout back (`convert` pptx → docx); a manuscript → a Word-typeset book (`design` template `book`);
  photos edited from words (`photo_edit`), old photos restored (`restore_photos`), scans read to text (`digitize`),
  PDF forms filled (`pdf_form`).

## Plans

- [Plans & pricing]({base}/app/plans): free to try; monthly credits on Plus and Pro; credit packs without a subscription.
"""
    return PlainTextResponse(body, media_type="text/plain; charset=utf-8")

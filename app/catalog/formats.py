"""Formátum-katalógus: minden fájlformátum, amit olvasunk vagy írunk — nyelvfüggetlen tényadatokkal.

Egy sor: kulcs (kiterjesztés, kisbetű; a többpontos is: "tar.gz"), kategória, teljes név, MIME, fejlesztő, év,
tulajdonság-címkék, népszerűség (1 = nagyon keresett … 3 = ritka). A nyelvi leírás az i18n-fájlokban van
("fmt.<kulcs>"); ahol hiányzik, a tényekből áll össze (app/site/content.py).

Címkék: lossy, lossless, raster, vector, text, binary, zip-xml, container, layered, animated, macro, template,
         compressed, metadata, fixed-layout, reflowable, ocr, legacy, camera-raw, single-file, open-standard.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

CATEGORIES = ("document", "spreadsheet", "presentation", "ebook", "image", "vector", "video", "audio", "archive",
              "cad", "font")


@dataclass(frozen=True)
class Format:
    key: str
    category: str
    name: str
    mime: str
    developer: str = ""
    year: Optional[int] = None
    tags: frozenset = field(default_factory=frozenset)
    tier: int = 2
    aliases: tuple = ()

    @property
    def slug(self) -> str:
        return self.key.replace(".", "-")

    @property
    def label(self) -> str:
        return self.key.upper()


def _f(key, cat, name, mime, dev="", year=None, tags="", tier=2, aliases=()) -> Format:
    return Format(key, cat, name, mime, dev, year, frozenset(t for t in tags.split() if t), tier, tuple(aliases))


_OOXML = "application/vnd.openxmlformats-officedocument"
_RAW = "image/x-raw"

_ALL: list[Format] = [
    # ---------- dokumentum ----------
    _f("pdf", "document", "Portable Document Format", "application/pdf", "Adobe", 1993, "fixed-layout binary open-standard", 1),
    _f("docx", "document", "Microsoft Word Open XML Document", f"{_OOXML}.wordprocessingml.document", "Microsoft", 2007, "zip-xml reflowable", 1),
    _f("doc", "document", "Microsoft Word 97–2003 Document", "application/msword", "Microsoft", 1997, "binary reflowable legacy", 1),
    _f("docm", "document", "Microsoft Word Macro-Enabled Document", "application/vnd.ms-word.document.macroEnabled.12", "Microsoft", 2007, "zip-xml macro", 3),
    _f("dot", "document", "Microsoft Word 97–2003 Template", "application/msword", "Microsoft", 1997, "binary template legacy", 3),
    _f("dotx", "document", "Microsoft Word Open XML Template", f"{_OOXML}.wordprocessingml.template", "Microsoft", 2007, "zip-xml template", 3),
    _f("odt", "document", "OpenDocument Text", "application/vnd.oasis.opendocument.text", "OASIS", 2005, "zip-xml open-standard", 2),
    _f("ott", "document", "OpenDocument Text Template", "application/vnd.oasis.opendocument.text-template", "OASIS", 2005, "zip-xml template open-standard", 3),
    _f("fodt", "document", "Flat OpenDocument Text (XML)", "application/vnd.oasis.opendocument.text-flat-xml", "OASIS", 2005, "text open-standard", 3),
    _f("rtf", "document", "Rich Text Format", "application/rtf", "Microsoft", 1987, "text reflowable", 2),
    _f("txt", "document", "Plain Text", "text/plain", "", None, "text", 1),
    _f("md", "document", "Markdown", "text/markdown", "John Gruber", 2004, "text", 1, ("markdown",)),
    _f("html", "document", "HyperText Markup Language", "text/html", "W3C / WHATWG", 1993, "text open-standard", 1, ("htm",)),
    _f("rst", "document", "reStructuredText", "text/x-rst", "Python community", 2002, "text", 3),
    _f("tex", "document", "LaTeX Source", "application/x-tex", "Leslie Lamport", 1984, "text", 2, ("latex",)),
    _f("pages", "document", "Apple Pages Document", "application/vnd.apple.pages", "Apple", 2005, "container", 2),
    _f("wpd", "document", "WordPerfect Document", "application/vnd.wordperfect", "Corel", 1980, "binary legacy", 3),
    _f("wps", "document", "Microsoft Works / WPS Writer Document", "application/vnd.ms-works", "Microsoft / Kingsoft", 1988, "binary legacy", 3),
    _f("lwp", "document", "Lotus Word Pro Document", "application/vnd.lotus-wordpro", "Lotus / IBM", 1996, "binary legacy", 3),
    _f("abw", "document", "AbiWord Document", "application/x-abiword", "AbiSource", 1998, "text", 3),
    _f("zabw", "document", "Compressed AbiWord Document", "application/x-abiword-compressed", "AbiSource", 1998, "compressed", 3),
    _f("sdw", "document", "StarOffice Writer Document", "application/vnd.stardivision.writer", "StarDivision", 1996, "binary legacy", 3),
    _f("hwp", "document", "Hangul Word Processor Document", "application/x-hwp", "Hancom", 1989, "binary", 3),
    _f("hwpx", "document", "Hangul Word Processor OWPML Document", "application/hwp+zip", "Hancom", 2011, "zip-xml", 3),
    _f("djvu", "document", "DjVu Scanned Document", "image/vnd.djvu", "AT&T Labs", 1998, "raster compressed", 2, ("djv",)),
    _f("xps", "document", "XML Paper Specification", "application/oxps", "Microsoft", 2006, "fixed-layout zip-xml", 3, ("oxps",)),
    _f("ps", "document", "PostScript", "application/postscript", "Adobe", 1984, "fixed-layout text", 3),
    _f("eml", "document", "E-mail Message (MIME)", "message/rfc822", "IETF", 1982, "text", 2),
    _f("xml", "document", "Extensible Markup Language", "application/xml", "W3C", 1998, "text open-standard", 2),
    _f("pub", "document", "Microsoft Publisher Document", "application/x-mspublisher", "Microsoft", 1991, "binary", 3),
    _f("vsd", "document", "Microsoft Visio 2003–2010 Drawing", "application/vnd.visio", "Microsoft", 1992, "binary vector", 3),
    _f("vsdx", "document", "Microsoft Visio Drawing (Open XML)", "application/vnd.ms-visio.drawing", "Microsoft", 2013, "zip-xml vector", 3),
    _f("json", "document", "JavaScript Object Notation", "application/json", "Douglas Crockford", 2001, "text open-standard", 2),
    _f("yaml", "document", "YAML Ain't Markup Language", "application/yaml", "YAML community", 2001, "text", 3, ("yml",)),
    _f("pdfa", "document", "PDF/A Archival Document", "application/pdf", "ISO", 2005, "fixed-layout open-standard", 2),
    _f("org", "document", "Emacs Org-mode Document", "text/x-org", "Carsten Dominik", 2003, "text", 3),
    _f("adoc", "document", "AsciiDoc Document", "text/asciidoc", "Stuart Rackham", 2002, "text", 3, ("asciidoc",)),
    _f("ipynb", "document", "Jupyter Notebook", "application/x-ipynb+json", "Project Jupyter", 2014, "text", 3),
    _f("textile", "document", "Textile Markup", "text/x-textile", "Dean Allen", 2002, "text", 3),
    _f("wiki", "document", "MediaWiki Markup", "text/x-wiki", "Wikimedia", 2002, "text", 3, ("mediawiki",)),
    # ---------- táblázat ----------
    _f("xlsx", "spreadsheet", "Microsoft Excel Open XML Spreadsheet", f"{_OOXML}.spreadsheetml.sheet", "Microsoft", 2007, "zip-xml", 1),
    _f("xls", "spreadsheet", "Microsoft Excel 97–2003 Workbook", "application/vnd.ms-excel", "Microsoft", 1997, "binary legacy", 1),
    _f("xlsm", "spreadsheet", "Microsoft Excel Macro-Enabled Workbook", "application/vnd.ms-excel.sheet.macroEnabled.12", "Microsoft", 2007, "zip-xml macro", 3),
    _f("xlsb", "spreadsheet", "Microsoft Excel Binary Workbook", "application/vnd.ms-excel.sheet.binary.macroEnabled.12", "Microsoft", 2007, "binary", 3),
    _f("xlt", "spreadsheet", "Microsoft Excel 97–2003 Template", "application/vnd.ms-excel", "Microsoft", 1997, "binary template legacy", 3),
    _f("xltx", "spreadsheet", "Microsoft Excel Open XML Template", f"{_OOXML}.spreadsheetml.template", "Microsoft", 2007, "zip-xml template", 3),
    _f("ods", "spreadsheet", "OpenDocument Spreadsheet", "application/vnd.oasis.opendocument.spreadsheet", "OASIS", 2005, "zip-xml open-standard", 2),
    _f("fods", "spreadsheet", "Flat OpenDocument Spreadsheet (XML)", "application/vnd.oasis.opendocument.spreadsheet-flat-xml", "OASIS", 2005, "text open-standard", 3),
    _f("csv", "spreadsheet", "Comma-Separated Values", "text/csv", "IETF (RFC 4180)", 2005, "text", 1),
    _f("tsv", "spreadsheet", "Tab-Separated Values", "text/tab-separated-values", "IANA", 1993, "text", 2),
    _f("numbers", "spreadsheet", "Apple Numbers Spreadsheet", "application/vnd.apple.numbers", "Apple", 2007, "container", 2),
    _f("dif", "spreadsheet", "Data Interchange Format", "text/x-dif", "Software Arts", 1980, "text legacy", 3),
    _f("slk", "spreadsheet", "Symbolic Link (SYLK)", "application/x-sylk", "Microsoft", 1980, "text legacy", 3, ("sylk",)),
    # ---------- prezentáció ----------
    _f("pptx", "presentation", "Microsoft PowerPoint Open XML Presentation", f"{_OOXML}.presentationml.presentation", "Microsoft", 2007, "zip-xml", 1),
    _f("ppt", "presentation", "Microsoft PowerPoint 97–2003 Presentation", "application/vnd.ms-powerpoint", "Microsoft", 1997, "binary legacy", 1),
    _f("pptm", "presentation", "Microsoft PowerPoint Macro-Enabled Presentation", "application/vnd.ms-powerpoint.presentation.macroEnabled.12", "Microsoft", 2007, "zip-xml macro", 3),
    _f("pps", "presentation", "Microsoft PowerPoint 97–2003 Slide Show", "application/vnd.ms-powerpoint", "Microsoft", 1997, "binary legacy", 3),
    _f("ppsx", "presentation", "Microsoft PowerPoint Open XML Slide Show", f"{_OOXML}.presentationml.slideshow", "Microsoft", 2007, "zip-xml", 2),
    _f("ppsm", "presentation", "Microsoft PowerPoint Macro-Enabled Slide Show", "application/vnd.ms-powerpoint.slideshow.macroEnabled.12", "Microsoft", 2007, "zip-xml macro", 3),
    _f("pot", "presentation", "Microsoft PowerPoint 97–2003 Template", "application/vnd.ms-powerpoint", "Microsoft", 1997, "binary template legacy", 3),
    _f("potx", "presentation", "Microsoft PowerPoint Open XML Template", f"{_OOXML}.presentationml.template", "Microsoft", 2007, "zip-xml template", 3),
    _f("potm", "presentation", "Microsoft PowerPoint Macro-Enabled Template", "application/vnd.ms-powerpoint.template.macroEnabled.12", "Microsoft", 2007, "zip-xml template macro", 3),
    _f("odp", "presentation", "OpenDocument Presentation", "application/vnd.oasis.opendocument.presentation", "OASIS", 2005, "zip-xml open-standard", 2),
    _f("fodp", "presentation", "Flat OpenDocument Presentation (XML)", "application/vnd.oasis.opendocument.presentation-flat-xml", "OASIS", 2005, "text open-standard", 3),
    _f("key", "presentation", "Apple Keynote Presentation", "application/vnd.apple.keynote", "Apple", 2003, "container", 2),
    # ---------- e-könyv ----------
    _f("epub", "ebook", "Electronic Publication", "application/epub+zip", "IDPF / W3C", 2007, "zip-xml reflowable open-standard", 1),
    _f("mobi", "ebook", "Mobipocket E-book", "application/x-mobipocket-ebook", "Mobipocket / Amazon", 2000, "binary reflowable", 1),
    _f("azw3", "ebook", "Kindle Format 8 (KF8)", "application/vnd.amazon.mobi8-ebook", "Amazon", 2011, "binary reflowable", 1),
    _f("azw", "ebook", "Amazon Kindle E-book", "application/vnd.amazon.ebook", "Amazon", 2007, "binary reflowable", 2),
    _f("azw4", "ebook", "Kindle Print Replica", "application/vnd.amazon.ebook", "Amazon", 2011, "binary fixed-layout", 3),
    _f("fb2", "ebook", "FictionBook 2", "application/x-fictionbook+xml", "FictionBook community", 2004, "text reflowable", 2),
    _f("fbz", "ebook", "Zipped FictionBook", "application/x-zip-compressed-fb2", "FictionBook community", 2004, "compressed reflowable", 3),
    _f("lit", "ebook", "Microsoft Reader E-book", "application/x-ms-reader", "Microsoft", 2000, "binary legacy", 3),
    _f("lrf", "ebook", "Sony BroadBand E-book", "application/x-sony-bbeb", "Sony", 2006, "binary legacy", 3),
    _f("pdb", "ebook", "Palm Database E-book (eReader)", "application/vnd.palm", "Palm", 1996, "binary legacy", 3),
    _f("pml", "ebook", "Palm Markup Language", "text/x-pml", "Palm", 2002, "text legacy", 3),
    _f("prc", "ebook", "Palm Resource / Mobipocket E-book", "application/x-mobipocket-ebook", "Palm / Mobipocket", 2000, "binary legacy", 3),
    _f("rb", "ebook", "Rocket eBook", "application/x-rocketbook", "NuvoMedia", 1998, "binary legacy", 3),
    _f("snb", "ebook", "Shanda Bambook E-book", "application/x-snb", "Shanda", 2010, "binary legacy", 3),
    _f("tcr", "ebook", "Psion TCR E-book", "application/x-tcr", "Psion", 1997, "compressed legacy", 3),
    _f("txtz", "ebook", "Zipped Text E-book", "application/x-txtz", "calibre", 2010, "compressed", 3),
    _f("htmlz", "ebook", "Zipped HTML E-book", "application/x-htmlz", "calibre", 2011, "compressed", 3),
    _f("oeb", "ebook", "Open eBook Publication", "application/oebps-package+xml", "Open eBook Forum", 1999, "zip-xml legacy", 3),
    _f("cbz", "ebook", "Comic Book Archive (ZIP)", "application/vnd.comicbook+zip", "", 2000, "container raster", 2),
    _f("cbr", "ebook", "Comic Book Archive (RAR)", "application/vnd.comicbook-rar", "", 2000, "container raster", 2),
    _f("cb7", "ebook", "Comic Book Archive (7z)", "application/x-cb7", "", 2005, "container raster", 3),
    _f("cbc", "ebook", "Comic Book Collection", "application/x-cbc", "calibre", 2010, "container raster", 3),
    _f("chm", "ebook", "Microsoft Compiled HTML Help", "application/vnd.ms-htmlhelp", "Microsoft", 1997, "compressed legacy", 3),
    # ---------- kép ----------
    _f("jpg", "image", "JPEG Image", "image/jpeg", "Joint Photographic Experts Group", 1992, "raster lossy", 1, ("jpeg", "jpe")),
    _f("png", "image", "Portable Network Graphics", "image/png", "W3C", 1996, "raster lossless open-standard", 1),
    _f("webp", "image", "WebP Image", "image/webp", "Google", 2010, "raster lossy lossless animated", 1),
    _f("heic", "image", "High Efficiency Image Coding", "image/heic", "MPEG / Apple", 2015, "raster lossy", 1),
    _f("heif", "image", "High Efficiency Image File Format", "image/heif", "MPEG", 2015, "raster lossy", 2),
    _f("avif", "image", "AV1 Image File Format", "image/avif", "Alliance for Open Media", 2019, "raster lossy lossless", 1),
    _f("gif", "image", "Graphics Interchange Format", "image/gif", "CompuServe", 1987, "raster lossless animated", 1),
    _f("bmp", "image", "Windows Bitmap", "image/bmp", "Microsoft", 1990, "raster lossless", 2, ("dib",)),
    _f("tiff", "image", "Tagged Image File Format", "image/tiff", "Aldus / Adobe", 1986, "raster lossless layered", 1, ("tif",)),
    _f("ico", "image", "Windows Icon", "image/vnd.microsoft.icon", "Microsoft", 1985, "raster", 1),
    _f("icns", "image", "Apple Icon Image", "image/icns", "Apple", 2001, "raster", 3),
    _f("cur", "image", "Windows Cursor", "image/x-win-bitmap", "Microsoft", 1985, "raster", 3),
    _f("psd", "image", "Adobe Photoshop Document", "image/vnd.adobe.photoshop", "Adobe", 1990, "raster layered", 1),
    _f("xcf", "image", "GIMP Image", "image/x-xcf", "GIMP", 1997, "raster layered", 3),
    _f("tga", "image", "Truevision TGA", "image/x-tga", "Truevision", 1984, "raster", 3),
    _f("jxl", "image", "JPEG XL Image", "image/jxl", "Joint Photographic Experts Group", 2021, "raster lossy lossless", 2),
    _f("jp2", "image", "JPEG 2000 Image", "image/jp2", "Joint Photographic Experts Group", 2000, "raster lossy lossless", 3, ("j2k", "jpf")),
    _f("jfif", "image", "JPEG File Interchange Format", "image/jpeg", "Eric Hamilton / C-Cube", 1992, "raster lossy", 2),
    _f("exr", "image", "OpenEXR High Dynamic Range Image", "image/x-exr", "Industrial Light & Magic", 2003, "raster", 3),
    _f("hdr", "image", "Radiance HDR Image", "image/vnd.radiance", "Greg Ward", 1985, "raster", 3),
    _f("dds", "image", "DirectDraw Surface", "image/vnd-ms.dds", "Microsoft", 1999, "raster", 3),
    _f("pcx", "image", "ZSoft PC Paintbrush Image", "image/x-pcx", "ZSoft", 1985, "raster legacy", 3),
    _f("ppm", "image", "Portable Pixmap", "image/x-portable-pixmap", "Jef Poskanzer", 1988, "raster", 3),
    _f("pgm", "image", "Portable Graymap", "image/x-portable-graymap", "Jef Poskanzer", 1988, "raster", 3),
    _f("pbm", "image", "Portable Bitmap", "image/x-portable-bitmap", "Jef Poskanzer", 1988, "raster", 3),
    _f("pnm", "image", "Portable Anymap", "image/x-portable-anymap", "Jef Poskanzer", 1988, "raster", 3),
    _f("sgi", "image", "Silicon Graphics Image", "image/sgi", "Silicon Graphics", 1990, "raster legacy", 3, ("rgb",)),
    _f("xbm", "image", "X BitMap", "image/x-xbitmap", "X Consortium", 1985, "raster text legacy", 3),
    _f("xpm", "image", "X PixMap", "image/x-xpixmap", "Groupe Bull", 1989, "raster text legacy", 3),
    _f("wbmp", "image", "Wireless Bitmap", "image/vnd.wap.wbmp", "WAP Forum", 1999, "raster legacy", 3),
    _f("fits", "image", "Flexible Image Transport System", "image/fits", "NASA / IAU", 1981, "raster", 3, ("fit",)),
    _f("apng", "image", "Animated PNG", "image/apng", "Mozilla", 2004, "raster lossless animated", 3),
    _f("dng", "image", "Adobe Digital Negative (RAW)", "image/x-adobe-dng", "Adobe", 2004, "camera-raw raster", 2),
    _f("cr2", "image", "Canon RAW 2", _RAW, "Canon", 2004, "camera-raw raster", 2),
    _f("cr3", "image", "Canon RAW 3", _RAW, "Canon", 2018, "camera-raw raster", 2),
    _f("crw", "image", "Canon RAW (CIFF)", _RAW, "Canon", 1998, "camera-raw raster legacy", 3),
    _f("nef", "image", "Nikon Electronic Format (RAW)", _RAW, "Nikon", 1999, "camera-raw raster", 2),
    _f("nrw", "image", "Nikon RAW (Coolpix)", _RAW, "Nikon", 2008, "camera-raw raster", 3),
    _f("arw", "image", "Sony Alpha RAW", _RAW, "Sony", 2006, "camera-raw raster", 2),
    _f("srf", "image", "Sony RAW (SRF)", _RAW, "Sony", 2004, "camera-raw raster legacy", 3),
    _f("sr2", "image", "Sony RAW 2", _RAW, "Sony", 2006, "camera-raw raster legacy", 3),
    _f("orf", "image", "Olympus RAW", _RAW, "Olympus / OM System", 2003, "camera-raw raster", 3),
    _f("rw2", "image", "Panasonic RAW 2", _RAW, "Panasonic", 2008, "camera-raw raster", 3),
    _f("raf", "image", "Fujifilm RAW", _RAW, "Fujifilm", 2000, "camera-raw raster", 3),
    _f("pef", "image", "Pentax Electronic File (RAW)", _RAW, "Pentax / Ricoh", 2003, "camera-raw raster", 3),
    _f("srw", "image", "Samsung RAW", _RAW, "Samsung", 2009, "camera-raw raster", 3),
    _f("x3f", "image", "Sigma X3F RAW", _RAW, "Sigma", 2002, "camera-raw raster", 3),
    _f("erf", "image", "Epson RAW", _RAW, "Epson", 2006, "camera-raw raster legacy", 3),
    _f("kdc", "image", "Kodak Digital Camera RAW", _RAW, "Kodak", 1998, "camera-raw raster legacy", 3),
    _f("dcr", "image", "Kodak RAW (DCR)", _RAW, "Kodak", 2002, "camera-raw raster legacy", 3),
    _f("mrw", "image", "Minolta RAW", _RAW, "Minolta", 2001, "camera-raw raster legacy", 3),
    _f("3fr", "image", "Hasselblad 3F RAW", _RAW, "Hasselblad", 2006, "camera-raw raster", 3),
    _f("mos", "image", "Leaf RAW", _RAW, "Leaf", 2005, "camera-raw raster", 3),
    _f("raw", "image", "Generic Camera RAW (Panasonic/Leica)", _RAW, "", 2003, "camera-raw raster", 3),
    # ---------- vektor ----------
    _f("svg", "vector", "Scalable Vector Graphics", "image/svg+xml", "W3C", 2001, "vector text open-standard", 1),
    _f("svgz", "vector", "Compressed SVG", "image/svg+xml", "W3C", 2001, "vector compressed", 3),
    _f("eps", "vector", "Encapsulated PostScript", "application/postscript", "Adobe", 1987, "vector", 1),
    _f("ai", "vector", "Adobe Illustrator Artwork", "application/illustrator", "Adobe", 1987, "vector layered", 1),
    _f("emf", "vector", "Windows Enhanced Metafile", "image/emf", "Microsoft", 1993, "vector binary", 2),
    _f("emz", "vector", "Compressed Enhanced Metafile", "image/x-emz", "Microsoft", 1993, "vector compressed", 3),
    _f("wmf", "vector", "Windows Metafile", "image/wmf", "Microsoft", 1990, "vector binary legacy", 2),
    _f("wmz", "vector", "Compressed Windows Metafile", "image/x-wmz", "Microsoft", 1990, "vector compressed legacy", 3),
    _f("cdr", "vector", "CorelDRAW Drawing", "application/vnd.corel-draw", "Corel", 1989, "vector binary", 2),
    _f("odg", "vector", "OpenDocument Drawing", "application/vnd.oasis.opendocument.graphics", "OASIS", 2005, "vector zip-xml open-standard", 3),
    _f("fodg", "vector", "Flat OpenDocument Drawing (XML)", "application/vnd.oasis.opendocument.graphics-flat-xml", "OASIS", 2005, "vector text open-standard", 3),
    _f("cgm", "vector", "Computer Graphics Metafile", "image/cgm", "ISO / ANSI", 1987, "vector legacy", 3),
    _f("plt", "vector", "HP-GL Plotter File", "application/vnd.hp-hpgl", "Hewlett-Packard", 1977, "vector text legacy", 3, ("hpgl",)),
    # ---------- videó ----------
    _f("mp4", "video", "MPEG-4 Part 14 Video", "video/mp4", "MPEG", 2001, "container lossy", 1),
    _f("mov", "video", "Apple QuickTime Movie", "video/quicktime", "Apple", 1991, "container", 1),
    _f("avi", "video", "Audio Video Interleave", "video/x-msvideo", "Microsoft", 1992, "container legacy", 1),
    _f("mkv", "video", "Matroska Video", "video/x-matroska", "Matroska", 2002, "container open-standard", 1),
    _f("webm", "video", "WebM Video", "video/webm", "Google", 2010, "container lossy open-standard", 1),
    _f("wmv", "video", "Windows Media Video", "video/x-ms-wmv", "Microsoft", 1999, "container lossy", 2),
    _f("flv", "video", "Flash Video", "video/x-flv", "Adobe", 2003, "container legacy", 2),
    _f("f4v", "video", "Flash MP4 Video", "video/x-f4v", "Adobe", 2007, "container legacy", 3),
    _f("m4v", "video", "iTunes Video (MPEG-4)", "video/x-m4v", "Apple", 2003, "container", 2),
    _f("mpeg", "video", "MPEG-1/2 Video", "video/mpeg", "MPEG", 1993, "container lossy", 2),
    _f("mpg", "video", "MPEG Video", "video/mpeg", "MPEG", 1993, "container lossy", 2),
    _f("m2ts", "video", "Blu-ray MPEG-2 Transport Stream", "video/mp2t", "Blu-ray Disc Association", 2006, "container", 3),
    _f("mts", "video", "AVCHD Video", "video/mp2t", "Sony / Panasonic", 2006, "container", 2),
    _f("ts", "video", "MPEG Transport Stream", "video/mp2t", "MPEG", 1995, "container", 3),
    _f("vob", "video", "DVD Video Object", "video/dvd", "DVD Forum", 1996, "container legacy", 3),
    _f("3gp", "video", "3GPP Multimedia", "video/3gpp", "3GPP", 2003, "container lossy legacy", 2),
    _f("3g2", "video", "3GPP2 Multimedia", "video/3gpp2", "3GPP2", 2004, "container lossy legacy", 3),
    _f("ogv", "video", "Ogg Video", "video/ogg", "Xiph.Org", 2008, "container open-standard", 3),
    _f("asf", "video", "Advanced Systems Format", "video/x-ms-asf", "Microsoft", 1996, "container legacy", 3),
    _f("mxf", "video", "Material Exchange Format", "application/mxf", "SMPTE", 2004, "container", 3),
    _f("dv", "video", "Digital Video", "video/x-dv", "IEC", 1995, "container legacy", 3),
    _f("divx", "video", "DivX Video", "video/divx", "DivX Inc.", 2001, "container lossy legacy", 3),
    _f("hevc", "video", "H.265 / HEVC Elementary Stream", "video/hevc", "ITU-T / MPEG", 2013, "lossy", 3, ("h265",)),
    _f("rm", "video", "RealMedia", "application/vnd.rn-realmedia", "RealNetworks", 1997, "container legacy", 3),
    _f("rmvb", "video", "RealMedia Variable Bitrate", "application/vnd.rn-realmedia-vbr", "RealNetworks", 2003, "container legacy", 3),
    _f("swf", "video", "Shockwave Flash", "application/x-shockwave-flash", "Adobe", 1996, "container legacy", 3),
    _f("wtv", "video", "Windows Recorded TV Show", "video/x-ms-wtv", "Microsoft", 2009, "container legacy", 3),
    _f("mjpeg", "video", "Motion JPEG", "video/x-motion-jpeg", "", 1992, "lossy", 3, ("mjpg",)),
    # ---------- hang ----------
    _f("mp3", "audio", "MPEG-1 Audio Layer III", "audio/mpeg", "Fraunhofer IIS / MPEG", 1993, "lossy", 1),
    _f("wav", "audio", "Waveform Audio", "audio/wav", "Microsoft / IBM", 1991, "lossless", 1),
    _f("aac", "audio", "Advanced Audio Coding", "audio/aac", "MPEG", 1997, "lossy", 1),
    _f("m4a", "audio", "MPEG-4 Audio", "audio/mp4", "Apple / MPEG", 2001, "container lossy", 1),
    _f("ogg", "audio", "Ogg Vorbis Audio", "audio/ogg", "Xiph.Org", 2000, "lossy open-standard", 1, ("oga",)),
    _f("flac", "audio", "Free Lossless Audio Codec", "audio/flac", "Xiph.Org", 2001, "lossless open-standard", 1),
    _f("opus", "audio", "Opus Audio", "audio/opus", "IETF / Xiph.Org", 2012, "lossy open-standard", 2),
    _f("wma", "audio", "Windows Media Audio", "audio/x-ms-wma", "Microsoft", 1999, "lossy legacy", 2),
    _f("aiff", "audio", "Audio Interchange File Format", "audio/aiff", "Apple", 1988, "lossless", 2, ("aif", "aifc")),
    _f("alac", "audio", "Apple Lossless Audio (in M4A)", "audio/mp4", "Apple", 2004, "lossless", 3),
    _f("m4b", "audio", "MPEG-4 Audiobook", "audio/mp4", "Apple", 2003, "container lossy", 3),
    _f("m4r", "audio", "iPhone Ringtone", "audio/mp4", "Apple", 2007, "container lossy", 3),
    _f("amr", "audio", "Adaptive Multi-Rate Audio", "audio/amr", "3GPP", 1999, "lossy", 2),
    _f("ac3", "audio", "Dolby Digital (AC-3)", "audio/ac3", "Dolby", 1991, "lossy", 3),
    _f("dts", "audio", "DTS Coherent Acoustics", "audio/vnd.dts", "DTS Inc.", 1993, "lossy", 3),
    _f("ape", "audio", "Monkey's Audio", "audio/ape", "Matthew Ashland", 2000, "lossless", 3),
    _f("wv", "audio", "WavPack Audio", "audio/wavpack", "David Bryant", 1998, "lossless lossy", 3),
    _f("mka", "audio", "Matroska Audio", "audio/x-matroska", "Matroska", 2002, "container", 3),
    _f("au", "audio", "Sun / NeXT Audio", "audio/basic", "Sun Microsystems", 1992, "lossless legacy", 3, ("snd",)),
    _f("caf", "audio", "Apple Core Audio Format", "audio/x-caf", "Apple", 2005, "container", 3),
    _f("mp2", "audio", "MPEG-1 Audio Layer II", "audio/mpeg", "MPEG", 1993, "lossy legacy", 3),
    _f("weba", "audio", "WebM Audio", "audio/webm", "Google", 2010, "lossy", 3),
    _f("gsm", "audio", "GSM 06.10 Audio", "audio/x-gsm", "ETSI", 1990, "lossy legacy", 3),
    _f("voc", "audio", "Creative Voice", "audio/x-voc", "Creative Labs", 1989, "lossless legacy", 3),
    _f("ra", "audio", "RealAudio", "audio/vnd.rn-realaudio", "RealNetworks", 1995, "lossy legacy", 3),
    # ---------- archívum ----------
    _f("zip", "archive", "ZIP Archive", "application/zip", "Phil Katz (PKWARE)", 1989, "compressed container", 1),
    _f("rar", "archive", "RAR Archive", "application/vnd.rar", "Eugene Roshal", 1993, "compressed container", 1),
    _f("7z", "archive", "7-Zip Archive", "application/x-7z-compressed", "Igor Pavlov", 1999, "compressed container open-standard", 1),
    _f("tar", "archive", "Tape Archive (tarball)", "application/x-tar", "Unix", 1979, "container", 1),
    _f("tar.gz", "archive", "Gzip-compressed Tarball", "application/gzip", "GNU", 1992, "compressed container", 1),
    _f("tgz", "archive", "Gzip-compressed Tarball (TGZ)", "application/gzip", "GNU", 1992, "compressed container", 2),
    _f("tar.bz2", "archive", "Bzip2-compressed Tarball", "application/x-bzip2", "Julian Seward", 1996, "compressed container", 2),
    _f("tbz2", "archive", "Bzip2-compressed Tarball (TBZ2)", "application/x-bzip2", "Julian Seward", 1996, "compressed container", 3),
    _f("tbz", "archive", "Bzip2-compressed Tarball (TBZ)", "application/x-bzip2", "Julian Seward", 1996, "compressed container", 3),
    _f("tar.bz", "archive", "Bzip-compressed Tarball", "application/x-bzip", "Julian Seward", 1996, "compressed container legacy", 3),
    _f("tar.xz", "archive", "XZ-compressed Tarball", "application/x-xz", "Lasse Collin", 2009, "compressed container", 2),
    _f("txz", "archive", "XZ-compressed Tarball (TXZ)", "application/x-xz", "Lasse Collin", 2009, "compressed container", 3),
    _f("tar.zst", "archive", "Zstandard-compressed Tarball", "application/zstd", "Meta (Facebook)", 2016, "compressed container", 3),
    _f("tar.7z", "archive", "7-Zip-compressed Tarball", "application/x-7z-compressed", "Igor Pavlov", 1999, "compressed container", 3),
    _f("tar.lzo", "archive", "LZO-compressed Tarball", "application/x-lzop", "Markus Oberhumer", 1996, "compressed container", 3),
    _f("tar.z", "archive", "Compress (.Z) Tarball", "application/x-compress", "Unix", 1984, "compressed container legacy", 3),
    _f("tz", "archive", "Compress (.Z) Tarball (TZ)", "application/x-compress", "Unix", 1984, "compressed container legacy", 3),
    _f("tzo", "archive", "LZO-compressed Tarball (TZO)", "application/x-lzop", "Markus Oberhumer", 1996, "compressed container", 3),
    _f("gz", "archive", "Gzip Compressed File", "application/gzip", "GNU", 1992, "compressed single-file", 1),
    _f("bz2", "archive", "Bzip2 Compressed File", "application/x-bzip2", "Julian Seward", 1996, "compressed single-file", 2),
    _f("bz", "archive", "Bzip Compressed File", "application/x-bzip", "Julian Seward", 1996, "compressed single-file legacy", 3),
    _f("xz", "archive", "XZ Compressed File", "application/x-xz", "Lasse Collin", 2009, "compressed single-file", 2),
    _f("zst", "archive", "Zstandard Compressed File", "application/zstd", "Meta (Facebook)", 2016, "compressed single-file", 2),
    _f("lz", "archive", "Lzip Compressed File", "application/x-lzip", "Antonio Diaz Diaz", 2008, "compressed single-file", 3),
    _f("lzma", "archive", "LZMA Compressed File", "application/x-lzma", "Igor Pavlov", 1998, "compressed single-file", 3),
    _f("lzo", "archive", "LZO Compressed File", "application/x-lzop", "Markus Oberhumer", 1996, "compressed single-file", 3),
    _f("z", "archive", "Unix Compress File", "application/x-compress", "Unix", 1984, "compressed single-file legacy", 3),
    _f("cab", "archive", "Microsoft Cabinet Archive", "application/vnd.ms-cab-compressed", "Microsoft", 1995, "compressed container", 3),
    _f("iso", "archive", "ISO 9660 Disc Image", "application/x-iso9660-image", "ISO", 1988, "container", 2),
    _f("img", "archive", "Raw Disk Image", "application/x-raw-disk-image", "", None, "container", 3),
    _f("dmg", "archive", "Apple Disk Image", "application/x-apple-diskimage", "Apple", 2001, "container", 2),
    _f("jar", "archive", "Java Archive", "application/java-archive", "Sun Microsystems", 1996, "compressed container", 3),
    _f("war", "archive", "Web Application Archive", "application/java-archive", "Sun Microsystems", 1999, "compressed container", 3),
    _f("apk", "archive", "Android Package", "application/vnd.android.package-archive", "Google", 2008, "compressed container", 3),
    _f("deb", "archive", "Debian Package", "application/vnd.debian.binary-package", "Debian", 1995, "container", 3),
    _f("rpm", "archive", "RPM Package", "application/x-rpm", "Red Hat", 1997, "container", 3),
    _f("cpio", "archive", "CPIO Archive", "application/x-cpio", "Unix", 1977, "container legacy", 3),
    _f("arj", "archive", "ARJ Archive", "application/x-arj", "Robert Jung", 1991, "compressed container legacy", 3),
    _f("lha", "archive", "LHA / LZH Archive", "application/x-lzh-compressed", "Haruyasu Yoshizaki", 1988, "compressed container legacy", 3, ("lzh",)),
    _f("ace", "archive", "ACE Archive", "application/x-ace-compressed", "Marcel Lemke", 1998, "compressed container legacy", 3),
    _f("alz", "archive", "ALZip Archive", "application/x-alz-compressed", "ESTsoft", 1999, "compressed container legacy", 3),
    _f("arc", "archive", "ARC Archive", "application/x-freearc", "System Enhancement Associates", 1985, "compressed container legacy", 3),
    _f("wim", "archive", "Windows Imaging Format", "application/x-ms-wim", "Microsoft", 2006, "compressed container", 3),
    # ---------- CAD ----------
    _f("dwg", "cad", "AutoCAD Drawing", "image/vnd.dwg", "Autodesk", 1982, "vector binary", 1),
    _f("dxf", "cad", "Drawing Exchange Format", "image/vnd.dxf", "Autodesk", 1982, "vector text", 1),
    # ---------- betű ----------
    _f("ttf", "font", "TrueType Font", "font/ttf", "Apple / Microsoft", 1991, "binary", 1),
    _f("otf", "font", "OpenType Font", "font/otf", "Microsoft / Adobe", 1996, "binary", 1),
    _f("woff", "font", "Web Open Font Format", "font/woff", "W3C", 2010, "compressed open-standard", 1),
    _f("woff2", "font", "Web Open Font Format 2", "font/woff2", "W3C / Google", 2014, "compressed open-standard", 1),
    _f("eot", "font", "Embedded OpenType", "application/vnd.ms-fontobject", "Microsoft", 1997, "binary legacy", 2),
    _f("ttc", "font", "TrueType Collection", "font/collection", "Apple / Microsoft", 1996, "binary", 3),
    _f("pfb", "font", "PostScript Type 1 Font (binary)", "application/x-font-type1", "Adobe", 1984, "binary legacy", 3),
    _f("pfa", "font", "PostScript Type 1 Font (ASCII)", "application/x-font-type1", "Adobe", 1984, "text legacy", 3),
    _f("sfd", "font", "FontForge Spline Font Database", "application/vnd.font-fontforge-sfd", "FontForge", 2000, "text", 3),
    _f("dfont", "font", "Mac OS Data-Fork Font", "application/x-dfont", "Apple", 2001, "binary legacy", 3),
    _f("bdf", "font", "Glyph Bitmap Distribution Format", "application/x-font-bdf", "Adobe", 1988, "text legacy", 3),
]

FORMATS: dict[str, Format] = {f.key: f for f in _ALL}
_ALIAS: dict[str, str] = {a: f.key for f in FORMATS.values() for a in f.aliases}
_SLUG: dict[str, str] = {f.slug: f.key for f in FORMATS.values()}


def get(key: str) -> Optional[Format]:
    return FORMATS.get(normalize(key))


def normalize(ext: str) -> str:
    """'.JPEG' → 'jpg', 'tar-gz' → 'tar.gz', 'archive.tar.gz' → 'tar.gz'."""
    e = (ext or "").strip().lower().lstrip(".")
    if e in FORMATS:
        return e
    if e in _ALIAS:
        return _ALIAS[e]
    if e in _SLUG:
        return _SLUG[e]
    return e


def from_filename(name: str) -> str:
    """Kiterjesztés egy fájlnévből — a többpontosak (tar.gz) előnyben."""
    low = (name or "").lower()
    for multi in sorted((k for k in FORMATS if "." in k), key=len, reverse=True):
        if low.endswith("." + multi):
            return multi
    return normalize(low.rsplit(".", 1)[-1]) if "." in low else ""


def media(fmt: str) -> bool:
    """Videó vagy hang: ffmpeg-kódolás (a nehéz munka kapuja ezt nézi, app/jobs.py heavy)."""
    f = get(fmt) if fmt else None
    return f is not None and f.category in ("video", "audio")


def by_category(cat: str) -> list[Format]:
    return [f for f in FORMATS.values() if f.category == cat]


def count_by_category() -> dict[str, int]:
    return {c: len(by_category(c)) for c in CATEGORIES}

"""Géppel olvasható AI-jelölés a kimeneteken (IPTC Digital Source Type, XMP) — újrakódolás nélkül.

A látható „AI生成” felirat eltávolítása helyett (amit a szolgáltató kötelezően tesz rá) a saját kimeneteinken
szerényen, szabványosan jelöljük, ha AI generálta vagy szerkesztette őket (EU AI Act 50. cikk szellemében):
    trainedAlgorithmicMedia               — teljesen AI-generált kép
    compositeWithTrainedAlgorithmicMedia  — valódi fotó, AI-val szerkesztve/helyreállítva
JPEG: APP1 XMP-szegmens a SOI után; PNG: iTXt „XML:com.adobe.xmp” darab az IEND előtt. A PSD-ben a rétegnevek
(„Restored (AI)”, „AI edit: …”) jelölnek.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path
from xml.sax.saxutils import escape

GENERATED = "trainedAlgorithmicMedia"
EDITED = "compositeWithTrainedAlgorithmicMedia"

_XMP = ('<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:xmp="http://ns.adobe.com/xap/1.0/" '
        'Iptc4xmpExt:DigitalSourceType="http://cv.iptc.org/newscodes/digitalsourcetype/{kind}" xmp:CreatorTool="{tool}">'
        '<dc:description><rdf:Alt><rdf:li xml:lang="x-default">{desc}</rdf:li></rdf:Alt></dc:description>'
        '</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>')


def _xmp(kind: str, desc: str) -> bytes:
    from app.config import settings
    return _XMP.format(kind=kind, tool=escape(settings.PRODUCT_NAME), desc=escape(desc[:300])).encode("utf-8")


def label(path: Path, kind: str = EDITED, desc: str = "Edited with AI") -> bool:
    """XMP beírása a fájlba (JPEG/PNG); más formátumnál nem csinál semmit. Vissza: sikerült-e."""
    try:
        data = path.read_bytes()
        xmp = _xmp(kind, desc)
        if data[:2] == b"\xff\xd8":
            payload = b"http://ns.adobe.com/xap/1.0/\x00" + xmp
            if len(payload) + 2 > 0xFFFF:
                return False
            seg = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
            path.write_bytes(data[:2] + seg + data[2:])
            return True
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            body = b"iTXt" + b"XML:com.adobe.xmp\x00\x00\x00\x00\x00" + xmp
            chunk = struct.pack(">I", len(body) - 4) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
            i = data.rfind(b"IEND") - 4
            path.write_bytes(data[:i] + chunk + data[i:])
            return True
    except OSError:
        pass
    return False

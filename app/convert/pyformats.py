"""Kis, saját formátum-kezelők, amelyekhez nincs megbízható fej nélküli motor:

    HPGL (.plt) → SVG          a plotter-parancsok (PU/PD/PA/PR/SP/IN) útvonalakká
    PDF → HPGL                 a vektoros rajzelemek (PyMuPDF get_drawings) tollmozgásokká
    Unix compress (.Z) írás    LZW, a compress(1)/ncompress bitformátumával (gzip -d, 7-Zip olvassa)
"""
from __future__ import annotations

import re
from pathlib import Path

PLU_PER_MM = 40.0                   # HPGL: 1 plotteregység = 0,025 mm
PT_TO_PLU = 25.4 / 72 * PLU_PER_MM   # 1 pt → plotteregység
_PENS = ["#000000", "#000000", "#d32f2f", "#388e3c", "#1976d2", "#fbc02d", "#7b1fa2", "#0097a7", "#5d4037"]


def hpgl_to_svg(src: Path, dst: Path) -> None:
    text = src.read_text("latin-1")
    cmds = re.findall(r"([A-Z]{2})([^A-Z;]*);?", text.upper())
    x = y = 0.0
    pen_down, relative, pen = False, False, 1
    paths: list[tuple[int, list[tuple[float, float]]]] = []
    cur: list[tuple[float, float]] = []

    def flush():
        nonlocal cur
        if len(cur) > 1:
            paths.append((pen, cur))
        cur = []
    for op, arg in cmds:
        nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", arg)]
        if op in ("IN", "DF"):
            flush()
            x = y = 0.0
            pen_down, relative = False, False
        elif op == "SP":
            flush()
            pen = int(nums[0]) if nums else 0
        elif op in ("PA", "PR", "PU", "PD"):
            if op == "PA":
                relative = False
            elif op == "PR":
                relative = True
            elif op == "PU":
                flush()
                pen_down = False
            else:
                pen_down = True
                if not cur:
                    cur = [(x, y)]
            for i in range(0, len(nums) - 1, 2):
                nx, ny = (x + nums[i], y + nums[i + 1]) if relative else (nums[i], nums[i + 1])
                x, y = nx, ny
                if pen_down:
                    cur.append((x, y))
                else:
                    cur = [(x, y)]
            if not pen_down:
                cur = [(x, y)]
        elif op == "CI" and nums:
            flush()
            r = nums[0]
            paths.append((pen, [("circle", x, y, r)]))  # type: ignore[list-item]
    flush()
    pts = [p for _, path in paths for p in path if len(p) == 2]
    if not pts:
        raise ValueError("no plot commands found in the HPGL file")
    minx, maxx = min(p[0] for p in pts), max(p[0] for p in pts)
    miny, maxy = min(p[1] for p in pts), max(p[1] for p in pts)
    w, h = max(maxx - minx, 1) / PLU_PER_MM, max(maxy - miny, 1) / PLU_PER_MM
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.2f}mm" height="{h:.2f}mm" '
           f'viewBox="{minx} {-maxy} {max(maxx - minx, 1)} {max(maxy - miny, 1)}">']
    for pen_no, path in paths:
        color = _PENS[pen_no % len(_PENS)]
        if path and len(path[0]) == 4:
            _, cx, cy, r = path[0]
            out.append(f'<circle cx="{cx}" cy="{-cy}" r="{r}" fill="none" stroke="{color}" stroke-width="{PLU_PER_MM * 0.25}"/>')
            continue
        d = "M" + " L".join(f"{px:g},{-py:g}" for px, py in path)
        out.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{PLU_PER_MM * 0.25}" stroke-linecap="round"/>')
    out.append("</svg>")
    dst.write_text("\n".join(out), "utf-8")


def pdf_to_hpgl(src: Path, dst: Path, page_no: int = 0, curve_steps: int = 12) -> None:
    import pymupdf
    lines = ["IN;", "SP1;"]
    with pymupdf.open(str(src)) as d:
        page = d[page_no]
        H = page.rect.height

        def P(pt) -> str:
            return f"{round(pt.x * PT_TO_PLU)},{round((H - pt.y) * PT_TO_PLU)}"
        for drw in page.get_drawings():
            for item in drw["items"]:
                kind = item[0]
                if kind == "l":
                    lines.append(f"PU{P(item[1])};PD{P(item[2])};")
                elif kind == "c":
                    p0, p1, p2, p3 = item[1:5]
                    seg = []
                    for i in range(1, curve_steps + 1):
                        t = i / curve_steps
                        mt = 1 - t
                        x = mt ** 3 * p0.x + 3 * mt * mt * t * p1.x + 3 * mt * t * t * p2.x + t ** 3 * p3.x
                        y = mt ** 3 * p0.y + 3 * mt * mt * t * p1.y + 3 * mt * t * t * p2.y + t ** 3 * p3.y
                        seg.append(P(pymupdf.Point(x, y)))
                    lines.append(f"PU{P(p0)};PD{','.join(seg)};")
                elif kind == "re":
                    r = item[1]
                    lines.append(f"PU{P(r.tl)};PD{P(r.tr)},{P(r.br)},{P(r.bl)},{P(r.tl)};")
                elif kind == "qu":
                    q = item[1]
                    lines.append(f"PU{P(q.ul)};PD{P(q.ur)},{P(q.lr)},{P(q.ll)},{P(q.ul)};")
    lines.append("PU;SP0;")
    if len(lines) <= 3:
        raise ValueError("the drawing has no vector lines to plot")
    dst.write_text("\n".join(lines) + "\n", "ascii")


def compress_z(data: bytes, maxbits: int = 16) -> bytes:
    """Unix compress (.Z): LZW, block mode, a compress(1) bit-csomagolásával (8 kódonkénti, n_bits bájtos csoportok)."""
    out = bytearray(b"\x1f\x9d" + bytes([0x80 | maxbits]))
    n_bits = 9
    maxcode = (1 << n_bits) - 1
    maxmaxcode = 1 << maxbits
    table: dict[tuple[int, int], int] = {}
    free_ent = 257
    buf = bytearray(maxbits)
    offset = 0                      # bitpozíció a csoportban

    def output(code: int) -> None:
        nonlocal offset, n_bits, maxcode
        # a kód LSB-től a bufferbe
        bit = offset
        c = code
        remaining = n_bits
        while remaining > 0:
            byte_i, bit_i = divmod(bit, 8)
            take = min(8 - bit_i, remaining)
            buf[byte_i] |= (c & ((1 << take) - 1)) << bit_i
            c >>= take
            bit += take
            remaining -= take
        offset += n_bits
        if offset == (n_bits << 3):
            out.extend(buf[:n_bits])
            buf[:] = bytes(maxbits)
            offset = 0
        if free_ent > maxcode:
            if offset > 0:
                out.extend(buf[:n_bits])
                buf[:] = bytes(maxbits)
                offset = 0
            n_bits += 1
            maxcode = maxmaxcode if n_bits == maxbits else (1 << n_bits) - 1

    if not data:
        return bytes(out)
    ent = data[0]
    for ch in data[1:]:
        key = (ent, ch)
        code = table.get(key)
        if code is not None:
            ent = code
            continue
        output(ent)
        if free_ent < maxmaxcode:
            table[key] = free_ent
            free_ent += 1
        ent = ch
    output(ent)
    if offset > 0:
        out.extend(buf[:(offset + 7) // 8])
    return bytes(out)

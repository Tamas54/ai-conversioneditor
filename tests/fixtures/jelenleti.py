"""Szintetikus havi jelenléti ív (a 14–15. pont tesztjeihez) — valódi sablon vagy személyes adat nincs a repóban.

Elrendezés: fejléc (Név, Munkakör, Hónap, Kód), a 7. sorban a táblázat fejléce, a d. nap a 7+d. sorban
(A: nap, B: érkezés, C: távozás, D: távollét-jelölő „+”, E: ledolgozott idő = C − B, ha mindkettő idő), a 39. sorban
az összesítők: B39 ledolgozott napok (COUNT), D39 távollétnapok (COUNTIF „+”), E39 összes óra (SUM, [h]:mm).
"""
from __future__ import annotations

from pathlib import Path

PROFILE = {
    "sheet": "Jelenléti ív",
    "header": {"name": "B3", "job_title": "B4", "code": "E4", "month": "E3"},
    "month_format": "{year}. {month}",
    "days": {"first_row": 8, "step": 1},
    "columns": {"arrive": "B", "leave": "C", "absence": "D"},
    "rules": {"workday": {"arrive": "{start}", "leave": "{end}"},
              "kinds": {"szabadság": {"leave": "SZABADSÁG", "absence": "+"},
                        "betegség": {"leave": "BETEGSÉG", "absence": "+"}}},
    "totals": {"hours": "E39", "absence_days": "D39", "worked_days": "B39"},
    "header_values": {"name": "Próba Ödön", "job_title": "Ügyintéző", "code": "T-001"},
    "defaults": {"start": "8:00", "end": "16:00"},
}


def make_template(path: Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, Side
    wb = Workbook()
    ws = wb.active
    ws.title = "Jelenléti ív"
    thin = Side(style="thin")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    ws["A1"] = "JELENLÉTI ÍV"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A1"].alignment = Alignment(horizontal="center")
    ws.merge_cells("A1:F1")
    for coord, text in (("A3", "Név:"), ("A4", "Munkakör:"), ("D3", "Hónap:"), ("D4", "Kód:")):
        ws[coord] = text
        ws[coord].font = Font(bold=True)
    ws.merge_cells("B3:C3")
    ws.merge_cells("B4:C4")
    ws.merge_cells("E3:F3")
    for c, h in zip("ABCDEF", ("Nap", "Érkezés", "Távozás", "Távollét", "Ledolgozott idő", "Aláírás")):
        cell = ws[f"{c}7"]
        cell.value = h
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
        cell.border = box
    for d in range(1, 32):
        r = 7 + d
        ws[f"A{r}"] = d
        for c in "BC":
            ws[f"{c}{r}"].number_format = "h:mm"
        ws[f"E{r}"] = f"=IF(AND(ISNUMBER(B{r}),ISNUMBER(C{r})),C{r}-B{r},0)"
        ws[f"E{r}"].number_format = "[h]:mm"
        for c in "ABCDEF":
            ws[f"{c}{r}"].border = box
            ws[f"{c}{r}"].alignment = Alignment(horizontal="center")
    ws["A39"] = "Összesen:"
    ws["A39"].font = Font(bold=True)
    ws["B39"] = "=COUNT(B8:B38)"
    ws["D39"] = '=COUNTIF(D8:D38,"+")'
    ws["E39"] = "=SUM(E8:E38)"
    ws["E39"].number_format = "[h]:mm"
    for c in "ABDE":
        ws[f"{c}39"].border = box
    for c, w in zip("ABCDEF", (8, 12, 14, 11, 16, 18)):
        ws.column_dimensions[c].width = w
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.print_area = "A1:F39"
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))
    return path

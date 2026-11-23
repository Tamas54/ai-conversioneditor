"""A robotos pakli (11-23, „Mire a robot megérkezik”, a Bridge pptx_build-je) hibái — gépi mércével, akárki hívja:

- a nagy szám SOHA nem törik két sorba („2031–” / „33”, „27,” / „1%”, „2400→” / „8920”): a mérés a betű
  legszélesebb megjelenésével számol (a Bridge Georgiát kért — a szerveren nincs, a régi becslés harmadával
  keskenyebbnek mérte), és ami 44 pt-on sem fér, annak a vége a címkébe kerül; a tartományt és a nyilat word joiner köti;
- a stockkép a keresőszavakhoz illik (fájlnév, kategóriák, címkék — determinisztikusan, AI nélkül); a „humanoid robot
  factory” keresésre jött berkeley-i konferenciafotó nem kerül a címlapra; ha semmi nem illik, nincs kép, és a check
  kimondja;
- a check a sok figyelmeztetés közül a kimaradókat is megnevezi (a 11–13. dia „csak szöveg” jelzése levágódott)."""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

NB, WJ = " ", "⁠"
ROOT = Path(__file__).resolve().parent.parent

# a robotos pakli stats-diái, ahogy a Bridge küldte (Georgia címbetűvel)
ROBOT_THEME = {"primary": "#0B2545", "accent": "#E09F3E", "text": "#13294B", "font_heading": "Georgia", "font_body": "Calibri"}
ROBOT_STATS = [
    {"layout": "stats", "title": "A gép már a bér közelében jár", "stats": [
        {"value": "4×", "label": "humanoid bérleti díja az összeszerelő bérköltségének (HNA-becslés)"},
        {"value": "<20 e $", "label": "gyártói célár = egy összeszerelő éves bérköltsége"},
        {"value": "2031–33", "label": "érdemi ipari bevezetés Magyarországon (HNA konzervatív becslés)"}]},
    {"layout": "stats", "title": "Kit érint: a munkahelyek ötöde", "stats": [
        {"value": "930 e", "label": "gépkezelő, összeszerelő, járművezető, egyszerű munka (KSH)"},
        {"value": "922 e", "label": "munkahely automatizációs kitettségben (PwC, 2019, modellszámítás)"},
        {"value": "12,2%", "label": "magas kockázat a foglalkoztatottak között (OECD)"},
        {"value": "27,1%", "label": "kitett a generatív MI-nek (OECD)"}]},
    {"layout": "stats", "title": "Amit a mérések mutatnak", "stats": [
        {"value": "−0,2 pp", "label": "foglalkoztatási ráta minden további ipari robot után, ezer dolgozóra"},
        {"value": "−0,42%", "label": "bérek ugyanígy"},
        {"value": "5% vs 25–33%", "label": "a gép és a munka effektív adóterhe (USA)"}]},
    {"layout": "stats", "title": "Termelékenység és bér évtizedekre elválhat", "stats": [
        {"value": "+46%", "label": "kibocsátás, Nagy-Britannia 1780–1840"},
        {"value": "+12%", "label": "reálbér ugyanebben az időben"},
        {"value": "240→99 penny", "label": "kézi takácsok heti bére, 1806–1820"}]},
    {"layout": "stats", "title": "Magyar példa: cséplőgép és Ózd", "stats": [
        {"value": "2400→8920", "label": "gőzcséplők száma, 1871 → 1895"},
        {"value": "11 000", "label": "Ózdi Kohászati Üzemek létszáma, 1985"},
        {"value": "3,5→19,3%", "label": "borsodi munkanélküliség, 1990–1996"},
        {"value": "30 e vs 7,2 e", "label": "megszűnt vs támogatott új munkahely"}]},
]


def _prs(fid: str):
    from pptx import Presentation
    from app.storage import output_path
    return Presentation(str(output_path(fid)))


def _boxes(slide) -> list:
    return [sh for sh in slide.shapes if sh.has_text_frame and sh.text_frame.text.strip()]


# ---------------------------------------------------------------- a szám egy sorban

def test_keep_units_binds_ranges_and_arrows_between_figures_with_a_word_joiner():
    from app.tools.pptx_deck import keep_units as k
    assert k("1780–1840") == f"1780{WJ}–{WJ}1840"
    assert k("2400→8920") == f"2400{WJ}→{WJ}8920"
    assert k("3,5→19,3%") == f"3,5{WJ}→{WJ}19,3%"
    assert k("10%–20%") == f"10%{WJ}–{WJ}20%"
    assert k(k("1780–1840")) == k("1780–1840")                                   # kétszer alkalmazva is ugyanaz
    assert k("1871 → 1895") == "1871 → 1895"                                     # szóköznél törni szabad
    assert k("Dél–Korea") == "Dél–Korea" and k("2019-2020") == "2019-2020"       # betű mellett / kötőjel: nem köt
    assert k("930 e munkahely") == f"930{NB}e munkahely"                         # „e” = ezer


def test_only_the_exact_family_counts_as_installed():
    from app.tools.pptx_deck import _named
    assert not _named("Georgia", "Noto Serif Georgian")                           # a régi részszó-egyezés hibája
    assert _named("DejaVu Serif", "DejaVu Serif,DejaVu Serif Condensed") and _named("caladea", "Caladea")


@pytest.mark.skipif(shutil.which("fc-match") is None, reason="fontconfig missing")
def test_a_font_the_server_lacks_is_measured_by_its_widest_stand_in():
    """Georgia: a szerveren nincs → a LibreOffice helyettesítője és a DejaVu Serif közül a szélesebb (a DeckCraft
    Merriweathere és a valódi Georgia sem szélesebb) — nem a régi, harmadával keskenyebb becslés."""
    from app.tools import pptx_deck as d
    files = d.measure_fonts("Georgia", bold=True)
    if any("eorgia" in Path(f).name for f in files):
        pytest.skip("Georgia is installed here")
    dv = d._fc("DejaVu Serif:style=Bold")
    if not dv or "dejavu" not in dv[0].lower():
        pytest.skip("DejaVu Serif missing")
    assert dv[1] in files
    from PIL import ImageFont
    real = ImageFont.truetype(dv[1], 100).getlength("27,1%") * 68 / 100
    assert d.text_width("27,1%", "Georgia", 68, bold=True) >= real
    assert d.text_width(f"1780{WJ}–{WJ}1840", "Georgia", 20) == d.text_width("1780–1840", "Georgia", 20)
    assert d.measure_fonts("Cambria", bold=True) == [d._fc("Caladea:style=Bold")[1]] or not d._fc("Caladea:style=Bold")


def test_shorten_stat_sends_the_end_into_the_label_step_by_step():
    from app.tools.pptx_deck import shorten_stat
    short = lambda n: (lambda v: len(v) <= n)  # noqa: E731 — „befér”, ha legfeljebb n karakter
    assert shorten_stat("5% vs 25–33%", short(6)) == ("5%", "vs 25–33%")
    assert shorten_stat("30 e vs 7,2 e", short(6)) == ("30 e", "vs 7,2 e")
    assert shorten_stat("4,3 millió", short(6)) == ("4,3", "millió")
    assert shorten_stat("240→99 penny", short(7)) == ("240→99", "penny")
    assert shorten_stat("240→99 penny", short(4)) == ("240", "→ 99 penny")
    assert shorten_stat("3,5→19,3%", short(6)) == ("3,5%", "→ 19,3%")          # a mértékegység a szám mellett is
    assert shorten_stat("2031–2033", short(6)) == ("2031", "–2033")
    assert shorten_stat("11 000", short(3)) == ("11 000", "")                    # egyetlen szám: nincs mit levágni


async def test_robot_deck_figures_fit_one_line_even_in_the_widest_font():
    from app.tools import pptx_deck
    res = await pptx_deck.build({"title": "Mire a robot megérkezik", "theme": ROBOT_THEME, "slides": ROBOT_STATS})
    prs = _prs(res["file_id"])
    for n, slide in enumerate(prs.slides):
        k = len(ROBOT_STATS[n]["stats"])
        boxes = _boxes(slide)
        figs = [sh for sh in boxes if sh.text_frame.paragraphs[0].runs[0].font.bold
                and sh.text_frame.paragraphs[0].runs[0].font.size.pt >= 20 and sh.top > 2 * 914400][:k]
        assert len(figs) == k, [sh.text_frame.text for sh in boxes]
        sizes = {sh.text_frame.paragraphs[0].runs[0].font.size.pt for sh in figs}
        assert len(sizes) == 1                                                    # egy dián egy méret
        for sh in figs:
            txt, size = sh.text_frame.text, sizes.copy().pop()
            assert pptx_deck.text_width(txt, "Georgia", size, bold=True) <= sh.width / 12700, (txt, size)
    texts = [sh.text_frame.text.replace(WJ, "").replace(NB, " ") for s in prs.slides for sh in _boxes(s)]
    assert "5%" in texts and any(t.startswith("vs 25–33% · a gép") for t in texts)
    assert "3,5%" in texts and any(t.startswith("→ 19,3% · borsodi") for t in texts)
    assert "2031–33" in texts and "12,2%" in texts and "27,1%" in texts            # ami befér, az egészben marad
    findings = " ".join(res["check"]["findings"])
    assert "“2400→8920”" in findings and "Fewer cards" in findings


@pytest.mark.engines
async def test_rendered_robot_stats_have_no_split_figure_and_no_broken_word(put_file):
    """A mérce: a validate_document split_number és broken_word jelzése 0 ugyanarra a bemenetre (a LibreOffice-
    tördelésben mindig, a szerkesztőében, ha a DeckCraft a gépen van)."""
    if shutil.which("soffice") is None:
        pytest.skip("LibreOffice not installed")
    from app.storage import output_path
    from app.tools import pptx_deck, validate
    res = await pptx_deck.build({"title": "Mire a robot megérkezik", "theme": ROBOT_THEME, "slides": ROBOT_STATS})
    src = output_path(res["file_id"])
    layouts, _ = await validate._layouts(src, "deck", want_file=True)
    try:
        assert layouts
        issues = validate.deck_issues(src, layouts)
    finally:
        for L in layouts:
            L.close()
    assert not [i["message"] for i in issues if i["kind"] in ("split_number", "broken_word")]


def test_line_breaks_see_through_the_word_joiner():
    from app.tools.validate import _line_breaks
    box = [0, 0, 200, 100]

    def words(*lines):
        return [(10 + 40 * i, 10 + 20 * n, 45 + 40 * i, 24 + 20 * n, w) for n, ln in enumerate(lines) for i, w in enumerate(ln)]
    bound = f"2400{WJ}→{WJ}8920"
    assert _line_breaks(words(["2400→"], ["8920"]), box, bound) == ("broken_word", "2400→", "8920")          # LibreOffice
    assert _line_breaks(words([f"2400{WJ}→{WJ}"], ["8920"]), box, bound) == ("broken_word", "2400→", "8920")  # DeckCraft
    assert _line_breaks(words(["kibocsátás,"], [f"1780{WJ}–{WJ}1840"]), box, f"kibocsátás, 1780{WJ}–{WJ}1840") is None


def test_find_replace_and_extract_see_through_the_word_joiner(tmp_path):
    from pptx import Presentation
    from pptx.util import Inches
    from app.tools.edits import _pptx_extract_text, _pptx_find_replace
    src = tmp_path / "a.pptx"
    prs = Presentation()
    tb = prs.slides.add_slide(prs.slide_layouts[6]).shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1))
    tb.text_frame.text = f"kibocsátás, 1780{WJ}–{WJ}1840, 90{NB}%"
    prs.save(str(src))
    assert "1780–1840" in _pptx_extract_text(src) and WJ not in _pptx_extract_text(src)
    dst = tmp_path / "b.pptx"
    _pptx_find_replace(src, dst, "1780–1840", "1770–1850", regex=False)
    assert "1770–1850" in _pptx_extract_text(dst)


# ---------------------------------------------------------------- a stockkép illik a kereséshez

BOLD_BETS = {"title": "Speakers at Bold Bets event at UC Berkeley.jpg", "url": "https://upload.wikimedia.org/x/Bold.jpg",
             "credit": "Photo: Intel Free Press (Wikimedia Commons), CC BY-SA 2.0", "width": 1600, "height": 1082,
             "categories": ["Ken Goldberg", "Saul Griffith", "Events at the University of California, Berkeley",
                            "Flickr images reviewed by FlickreviewR 2"],
             "description": "(L-R) Ken Goldberg and Saul Griffith both agree that humanoid robots are far from being "
                            "ready to take over roles of humans in factories."}
ASIMO = {"title": "ASIMO 4.28.11.jpg", "url": "https://upload.wikimedia.org/x/ASIMO.jpg", "credit": "c", "width": 2000,
         "height": 3000, "categories": ["Taken with Canon EOS 7D", "Self-published work", "ASIMO (2nd generation)"],
         "description": "Asimo at a Honda factory. This humanoid robot was created by Honda Motor Co."}
KUKA = {"title": "Factory Automation Robotics Palettizing Bread.jpg", "url": "https://upload.wikimedia.org/x/K.jpg",
        "credit": "c", "width": 3000, "height": 2000, "categories": ["KUKA robots"], "description": ""}
SKETCHFAB = {"title": "Cyberpunk: ACU-1 (Autonomous Cybernetic Unit)", "url": "https://x/acu.jpg", "credit": "c",
             "width": 1920, "height": 1080, "tags": ["android", "cyborg", "factory", "humanoid", "robot", "robotics"]}
CHIMNEY = {"title": "Old factory chimney in Ózd.jpg", "url": "https://x/c.jpg", "credit": "c", "width": 2000,
           "height": 1300, "categories": ["Ózd"], "description": "The old chimney of the Ózd steel works."}


def test_stock_relevance_judges_the_picture_by_its_own_subject():
    from app.tools.images import relevant
    q, ctx = "humanoid robot factory", "Mire a robot megérkezik"
    assert not relevant(q, ctx, BOLD_BETS)            # a leírás „humanoid robots … factories”, de a kép két ember
    assert relevant(q, ctx, ASIMO)                    # a leírás eleje a fájlnév tárgyáról (Asimo) szól
    assert relevant(q, ctx, KUKA) and relevant(q, ctx, SKETCHFAB)
    assert not relevant(q, ctx, CHIMNEY)              # csak a „factory” — a kép tárgya nem robot
    assert relevant("parliament", "", {"title": "Hungarian Parliament Building.jpg"})
    assert relevant("", "", BOLD_BETS)                # keresőszó nélkül nincs mit mérni


async def test_choose_stock_strict_leaves_out_what_does_not_fit_and_says_why(monkeypatch):
    from app.tools import images
    calls = []

    async def search(q, n=12):
        calls.append(q)
        return [dict(BOLD_BETS), dict(CHIMNEY)]

    async def rank(*a, **k):
        raise AssertionError("the vision model is not asked when nothing fits")
    monkeypatch.setattr(images, "search", search)
    monkeypatch.setattr(images, "rank", rank)
    misses: dict = {}
    got = await images.choose_stock([("s1", "humanoid robot factory", "Mire a robot megérkezik", 16 / 9)],
                                    medium="slide", strict=True, misses=misses)
    assert got == {} and "Speakers at Bold Bets" in misses["s1"] and len(calls) > 1   # a rövidített keresések is
    # a videó (nem szigorú) útja változatlan: a látómodell sorrendje (itt a keresőé), kapu nélkül

    async def search_order(q, ctx, cands, **k):
        return list(range(len(cands)))
    monkeypatch.setattr(images, "rank", search_order)
    got = await images.choose_stock([("v1", "humanoid robot factory", "x", 16 / 9)], medium="video")
    assert got["v1"]["url"] == BOLD_BETS["url"]


async def test_pptx_build_keeps_an_unrelated_stock_photo_off_the_slide(monkeypatch):
    from app.tools import images, pptx_deck

    async def search(q, n=12):
        return [dict(BOLD_BETS)]

    async def old_way(*a, **k):
        raise AssertionError("no unfiltered fallback search")
    monkeypatch.setattr(images, "search", search)
    monkeypatch.setattr(images, "_stock_progressive", old_way)
    res = await pptx_deck.build({"title": "Robot", "slides": [
        {"layout": "cover", "title": "Mire a robot megérkezik", "image": {"stock": "humanoid robot factory"}},
        {"layout": "image_right", "title": "A gyár", "bullets": ["Egy", "Kettő"], "image": {"stock": "humanoid robot factory"}}]})
    prs = _prs(res["file_id"])
    assert not [sh for s in prs.slides for sh in s.shapes if sh.shape_type == 13]       # nincs kép
    assert prs.slides[1]._element.cSld.get("name") == "bullets"                          # a fél dia nem marad üresen
    assert res["images"] == []
    found = [f for f in res["check"]["findings"] if f.startswith(("Slide 1: no picture", "Slide 2: no picture"))]
    assert len(found) == 2 and "humanoid robot factory" in found[0] and res["check"]["status"] == "needs_check"


# ---------------------------------------------------------------- a check nem vág le csendben

def test_deck_check_names_the_findings_beyond_the_first_twelve(tmp_path):
    from pptx import Presentation
    from app.tools import checks
    p = tmp_path / "d.pptx"
    prs = Presentation()
    prs.slides.add_slide(prs.slide_layouts[6])
    prs.save(str(p))
    rendered = [{"id": f"W{k}", "severity": "warning", "kind": "text_only", "slide": k, "message": f"Slide {k} text only"}
                for k in range(1, 15)] + [{"id": "E1", "severity": "error", "kind": "overflow", "slide": 9, "message": "over"}]
    ck, _ = checks.check_pptx(p, None, "pptx_build", {}, rendered)
    assert ck["status"] == "failed" and ck["findings"][0].startswith("E1 (error)")          # a hiba előre
    tail = next(f for f in ck["findings"] if f.startswith("…and"))
    assert "W12 text_only (slide 12)" in tail and "W14 text_only (slide 14)" in tail


# ---------------------------------------------------------------- az éles kép Python 3.10

def _py310() -> str | None:
    if sys.version_info[:2] == (3, 10):
        return sys.executable
    cands = [os.getenv("PY310"), shutil.which("python3.10"),
             *sorted(glob.glob(os.path.expanduser("~/.local/share/uv/python/cpython-3.10*/bin/python3.10")))]
    return next((c for c in cands if c and os.access(c, os.X_OK)), None)


def test_every_module_compiles_on_python_3_10():
    """Az éles kép Python 3.10 (Playwright jammy): 11-23-án egy f-string, amely 3.12-n lefordult, 3.10-en leállította a
    validate_document-et. Minden .py-t a 3.10 fordítójával fordítunk (fájl nem keletkezik)."""
    exe = _py310()
    if exe is None:
        pytest.skip("no Python 3.10 on this machine")
    skip = {".venv", ".claude", "node_modules", "__pycache__", ".git"}
    files = [str(p) for p in ROOT.rglob("*.py") if not skip & set(p.relative_to(ROOT).parts)]
    assert len(files) > 100
    code = ("import sys\nbad=[]\nfor p in sys.stdin.read().splitlines():\n"
            "    try: compile(open(p, encoding='utf-8').read(), p, 'exec', dont_inherit=True)\n"
            "    except SyntaxError as e: bad.append(f'{p}:{e.lineno}: {e.msg}')\nprint('\\n'.join(bad))")
    out = subprocess.run([exe, "-I", "-c", code], input="\n".join(files), capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and not out.stdout.strip(), out.stdout + out.stderr


async def test_template_path_gets_the_same_stock_gate_and_one_line_figures(monkeypatch, put_file, tmp_path):
    """template_file_id-vel is (sablon-út): a nem illő stockkép kimarad, a szám egy sorban marad, a check szól."""
    from pptx import Presentation
    from app.tools import images, pptx_deck

    async def search(q, n=12):
        return [dict(BOLD_BETS)]

    async def old_way(*a, **k):
        raise AssertionError("no unfiltered fallback search")
    monkeypatch.setattr(images, "search", search)
    monkeypatch.setattr(images, "_stock_progressive", old_way)
    tpl = tmp_path / "minta.pptx"
    Presentation().save(str(tpl))
    res = await pptx_deck.build({"title": "Robot", "template_file_id": put_file(tpl), "slides": [
        {"layout": "bullets", "title": "A gyár", "bullets": ["Egy", "Kettő"], "image": {"stock": "humanoid robot factory"}},
        ROBOT_STATS[4]]})
    prs = _prs(res["file_id"])
    assert not [sh for s in prs.slides for sh in s.shapes if sh.shape_type == 13]
    figs = [sh for sh in prs.slides[1].shapes if sh.has_text_frame and sh.text_frame.paragraphs[0].runs
            and sh.text_frame.paragraphs[0].runs[0].font.bold]
    assert len(figs) == 4
    for sh in figs:
        r = sh.text_frame.paragraphs[0].runs[0]
        assert pptx_deck.text_width(r.text, pptx_deck.theme_font(prs.slides[1], major=False), r.font.size.pt,
                                    bold=True) <= sh.width / 12700 - 14.4, r.text
    findings = " ".join(res["check"]["findings"])
    assert "Slide 1: no picture" in findings and "“2400→8920”" in findings

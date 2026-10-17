"""A dia időzítése (p:timing): videó, ami magától indul, és lépésenkénti felépülés (magyarázó ábra).

Videó a dián: a PowerPoint saját időzítése, hogy a Keynote (iPhone, Mac) és a PowerPoint le is játssza.

A python-pptx csak egy „várakozó” médiacsomópontot ír (`delay="indefinite"`, kattintás-sorozat nélkül): a dián
ott a videó, de semmi nem indítja el — iPhone-on a Keynote a borítóképét mutatja, lejátszó nélkül. Itt azt írjuk,
amit a PowerPoint ír „Indítás: automatikusan” + „kattintásra szünet/folytatás” beállításnál:

  mainSeq        a dia megjelenésekor (onBegin) mediacall playFrom(0.0) — automatikus indulás
  p:video        a médiacsomópont (hangerő, a borítókép látszik, amíg nem megy)
  interactiveSeq a videóra koppintva/kattintva togglePause — szünet és folytatás
"""
from __future__ import annotations

from lxml import etree

_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_NS = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'

_TIMING = """<p:timing {ns}><p:tnLst><p:par><p:cTn id="1" dur="indefinite" restart="never" nodeType="tmRoot"><p:childTnLst>
<p:seq concurrent="1" nextAc="seek"><p:cTn id="2" dur="indefinite" nodeType="mainSeq"><p:childTnLst>
<p:par><p:cTn id="3" fill="hold"><p:stCondLst><p:cond delay="indefinite"/><p:cond evt="onBegin" delay="0"><p:tn val="2"/></p:cond></p:stCondLst><p:childTnLst>
<p:par><p:cTn id="4" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>
<p:par><p:cTn id="5" presetID="1" presetClass="mediacall" presetSubtype="0" fill="hold" nodeType="afterEffect"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>
<p:cmd type="call" cmd="playFrom(0.0)"><p:cBhvr><p:cTn id="6" dur="{dur}" fill="hold"/><p:tgtEl><p:spTgt spid="{spid}"/></p:tgtEl></p:cBhvr></p:cmd>
</p:childTnLst></p:cTn></p:par></p:childTnLst></p:cTn></p:par></p:childTnLst></p:cTn></p:par>
</p:childTnLst></p:cTn>
<p:prevCondLst><p:cond evt="onPrev" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:prevCondLst>
<p:nextCondLst><p:cond evt="onNext" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:nextCondLst></p:seq>
<p:video><p:cMediaNode vol="{vol}"><p:cTn id="7" fill="hold" display="0"><p:stCondLst><p:cond delay="indefinite"/></p:stCondLst></p:cTn><p:tgtEl><p:spTgt spid="{spid}"/></p:tgtEl></p:cMediaNode></p:video>
<p:seq concurrent="1" nextAc="seek"><p:cTn id="8" restart="whenNotActive" fill="hold" evtFilter="cancelBubble" nodeType="interactiveSeq">
<p:stCondLst><p:cond evt="onClick" delay="0"><p:tgtEl><p:spTgt spid="{spid}"/></p:tgtEl></p:cond></p:stCondLst>
<p:endSync evt="end" delay="0"><p:rtn val="all"/></p:endSync><p:childTnLst>
<p:par><p:cTn id="9" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>
<p:par><p:cTn id="10" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>
<p:par><p:cTn id="11" presetID="2" presetClass="mediacall" presetSubtype="0" fill="hold" nodeType="clickEffect"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>
<p:cmd type="call" cmd="togglePause"><p:cBhvr><p:cTn id="12" dur="1" fill="hold"/><p:tgtEl><p:spTgt spid="{spid}"/></p:tgtEl></p:cBhvr></p:cmd>
</p:childTnLst></p:cTn></p:par></p:childTnLst></p:cTn></p:par></p:childTnLst></p:cTn></p:par>
</p:childTnLst></p:cTn>
<p:nextCondLst><p:cond evt="onClick" delay="0"><p:tgtEl><p:spTgt spid="{spid}"/></p:tgtEl></p:cond></p:nextCondLst></p:seq>
</p:childTnLst></p:cTn></p:par></p:tnLst></p:timing>"""

# CT_Slide: cSld, clrMapOvr, transition, timing, extLst
_AFTER = ("cSld", "clrMapOvr", "transition")


def timing_xml(spid: int, dur_ms: int, vol: int = 80000) -> etree._Element:
    xml = _TIMING.format(ns=_NS, spid=int(spid), dur=max(1, int(dur_ms)), vol=int(vol)).replace("\n", "")
    return etree.fromstring(xml)


def autoplay(sld, spid: int, dur_ms: int, vol: int = 80000) -> None:
    """A dia (p:sld elem) időzítése: a megadott videó magától indul, koppintásra megáll/folytatódik."""
    _put(sld, timing_xml(spid, dur_ms, vol))


# Lépésenkénti felépülés: a PowerPoint „Megjelenés: áttűnés” (fade) belépő effektusa kattintásonként egy lépésre — az
# első lépés a diával együtt látszik, a többi kattintásra jön (mint a videóban a kibontás). A PDF és a képek a kész
# állapotot mutatják (a rejtett alakzat is rajta van), a szerkesztőben minden alakzat szerkeszthető marad.
_EFFECT = ('<p:par><p:cTn id="{id}" presetID="10" presetClass="entr" presetSubtype="0" fill="hold"{grp} nodeType="{node}">'
           '<p:stCondLst><p:cond delay="{delay}"/></p:stCondLst><p:childTnLst>'
           '<p:set><p:cBhvr><p:cTn id="{id1}" dur="1" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst></p:cTn>'
           '<p:tgtEl><p:spTgt spid="{spid}"/></p:tgtEl><p:attrNameLst><p:attrName>style.visibility</p:attrName></p:attrNameLst>'
           '</p:cBhvr><p:to><p:strVal val="visible"/></p:to></p:set>'
           '<p:animEffect transition="in" filter="fade"><p:cBhvr><p:cTn id="{id2}" dur="{dur}"/><p:tgtEl><p:spTgt spid="{spid}"/>'
           '</p:tgtEl></p:cBhvr></p:animEffect></p:childTnLst></p:cTn></p:par>')


def reveal_xml(steps: list[list[tuple[int, bool]]], dur_ms: int = 450) -> etree._Element:
    """steps: kattintásonként az alakzatok [(spid, van-e szövege)] — a lépésen belül együtt (withEffect) tűnnek fel."""
    ids = iter(range(3, 100000))
    clicks, bld = [], []
    for step in steps:
        effects = []
        for k, (spid, has_text) in enumerate(step):
            effects.append(_EFFECT.format(id=next(ids), id1=next(ids), id2=next(ids), spid=int(spid), dur=int(dur_ms),
                                          node="clickEffect" if k == 0 else "withEffect", delay=0,
                                          grp=' grpId="0"' if has_text else ""))
            if has_text:
                bld.append(f'<p:bldP spid="{int(spid)}" grpId="0" animBg="1"/>')
        outer, inner = next(ids), next(ids)
        clicks.append(f'<p:par><p:cTn id="{outer}" fill="hold"><p:stCondLst><p:cond delay="indefinite"/></p:stCondLst><p:childTnLst>'
                      f'<p:par><p:cTn id="{inner}" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>'
                      + "".join(effects) + '</p:childTnLst></p:cTn></p:par></p:childTnLst></p:cTn></p:par>')
    xml = (f'<p:timing {_NS}><p:tnLst><p:par><p:cTn id="1" dur="indefinite" restart="never" nodeType="tmRoot"><p:childTnLst>'
           '<p:seq concurrent="1" nextAc="seek"><p:cTn id="2" dur="indefinite" nodeType="mainSeq"><p:childTnLst>'
           + "".join(clicks) + '</p:childTnLst></p:cTn>'
           '<p:prevCondLst><p:cond evt="onPrev" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:prevCondLst>'
           '<p:nextCondLst><p:cond evt="onNext" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:nextCondLst>'
           '</p:seq></p:childTnLst></p:cTn></p:par></p:tnLst>'
           + (f'<p:bldLst>{"".join(dict.fromkeys(bld))}</p:bldLst>' if bld else "") + '</p:timing>')
    return etree.fromstring(xml)


def reveal(sld, steps: list[list[tuple[int, bool]]]) -> None:
    """A dia alakzatai kattintásonként, lépésenként tűnnek fel (üres lépés kimarad)."""
    steps = [st for st in steps if st]
    if steps:
        _put(sld, reveal_xml(steps))


def _put(sld, new) -> None:
    old = sld.find(f"{{{_P}}}timing")
    if old is not None:
        old.addprevious(new)
        sld.remove(old)
        return
    prev = [c for c in sld if isinstance(c.tag, str) and etree.QName(c).localname in _AFTER]
    if prev:
        prev[-1].addnext(new)
    else:
        sld.insert(0, new)

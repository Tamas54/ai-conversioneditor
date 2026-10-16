"""Videó a dián: a PowerPoint saját időzítése, hogy a Keynote (iPhone, Mac) és a PowerPoint le is játssza.

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
    old = sld.find(f"{{{_P}}}timing")
    new = timing_xml(spid, dur_ms, vol)
    if old is not None:
        old.addprevious(new)
        sld.remove(old)
        return
    prev = [c for c in sld if isinstance(c.tag, str) and etree.QName(c).localname in _AFTER]
    if prev:
        prev[-1].addnext(new)
    else:
        sld.insert(0, new)

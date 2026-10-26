"""Az effekt-család regisztere (a videó effekt-rétegei: {"type": "effect", "effect": <név>, …}).

Effektek: lightning (villám a kézből — a Kommandant hangolta), fireball (tűzgolyó a tenyérből), flames (lángok — a
„tüzes grafikon”: on: egy grafikai réteg, a lángok az alfája felső éléből). Tervben: flame_jet (lángcsóva), energy_orb
(hideg energiagömb).

HOGYAN KÉSZÜL EGY ÚJ EFFEKT (egy fájl)
--------------------------------------
1. app/tools/fx/<név>.py: egy common.Effect alosztály —
     NAME = "<név>"; VERSION = 1 (a modulban; a rajz változásakor emelni — a gyorsítótár kulcsában van);
     PARAMS = {mező: (fajta, alap[, min, max])} — fajta: "int" | "float" | "target" | "choice:a|b|c";
       (a közös mezők — at / after_said / at_speech_end_after, dur, on, seed — nem ide valók, azokat a réteg adja);
     DEFAULT_DUR (s); a színek (GLOW, …), a jelenet színezése: TINT (BGR szorzó a fényességre), GRADE = (erősség,
       sötétítés, lecsengés / kocka); a fény: G1 / G2 = (homály-szigma a fél képmagasság arányában, erősség), LIT =
       (a fénypont Gauss-szigmája, erőssége);
     draw(layer, core, casters, t, active, lights) → erő (0–1): a saját rajz a fél felbontású izzás-rétegre (BGR 0–255
       float, a közös compose két homállyal izzást csinál belőle, screen keveréssel teszi a képre) és a teljes
       felbontású magra (hozzáadva); a fénypontok (fél felbontású px) a lights-ba. Csak self.rng-t használj (a rajz
       determinisztikus: ugyanarra a bemenetre bájtra ugyanaz — a teszt a hash-t nézi).
2. Ide, a REGISTRY-be: "<név>": ("app.tools.fx.<név>", "<Osztály>").
3. Teszt: tests/test_effect_layers.py mintájára (séma, determinisztikus hash, „szól-e”).
Minden más közös, és nem kell hozzányúlni: a követés (kéz + póz, szakaszonként, tartalom szerint Postgresben), a kiváltó
(at / after_said / at_speech_end_after, 10 ms-os mondatvég), a horgony és az irány (kéz / alkar / test / pont /
téglalap; cél: arm | right | left | up | down | [x, y]), a fény (Grade + compose: screen, sötétítés, színezés), a klip
(előre komponálva a verzió felvételéből, a tartalom szerint gyorsítótárazva), a builder, a végső render és az élő
előnézet (app/tools/effect_layers.py).

A HORGONY (on)
--------------
  · "hand" (alap): a felemelt kéz, az alkar irányával (követés); "body": a törzs (póz);
  · [x, y]: egy pont; [x, y, w, h]: egy téglalap (közép + méret, 0–1) — követés nélkül;
  · "L17": egy másik grafikai réteg (a „tüzes grafikon”): a réteg saját alfája a sugárzó maszk (common.mask_edge: a
    felső éle oszloponként) — lent, hogyan.

A KÉPRE TÉTEL (blend)
---------------------
  · over (alap): a klip a verzió felvételéből, az effekttel ELŐRE KOMPONÁLVA (a Grade a felvétel fényességéből
    sötétít és színez) — átlátszatlan felvétel-réteg a felvétel fölött, a grafika alatt, normál keveréssel; minden út
    változtatás nélkül viszi (gyors végső render, élő előnézet, szakasz-előnézet, szerkesztő);
  · screen: csak a fény (fekete alapon, Grade nélkül) — a builderben blendingMode = SCREEN; a gyors render a szakaszát
    a motorral rajzolja a felvétellel együtt (fast_render.classify: „átlátszatlan szakasz”); az élő előnézetben a
    réteg-klip a szólóban NORMAL-lal készül, a bejegyzés blend: "screen", a lejátszó gl.blendFunc(ONE,
    ONE_MINUS_SRC_COLOR)-ral, előszorzott rgb·a-val keveri: out = 1 − (1 − base)(1 − rgb·a) (add: base + rgb·a).

FELFEDÉS (reveal: "L5") — „a villám kirajzolja a grafikont”
----------------------------------------------------------
{"type": "effect", "effect": "lightning", "reveal": "L5", lead?: 0.5, build?: 1.2} — az effekt a kézből a réteg
dobozába csap, és a becsapódásra a réteg kirajzolódik (a saját beúszásával), szikrákkal a kirajzolódó élén.
Az óra a FELFEDETT RÉTEG: az at-ja a becsapódás kockája; az effekt at-ja = a réteg at-ja − lead, a hossza lead + build
(effect_layers.sync_reveals — minden építéskor újraszámolva: a réteget mozgatva vagy átidőzítve a villám követi, a
beszédhez a réteg at-ját kell kötni, pl. {"say": …}). Így a grafikon a saját animációjával, a saját rétegeként marad
(szerkeszthető, az élő előnézet a saját klipjét rajzolja), az effekt csak a fényt teszi rá — nincs második óra, ami
elcsúszhatna. A menet: fx/reveal.py (bármelyik effekt × bármelyik grafikai réteg; az effekt hookjai: Effect.strike — a
dobozba csap —, Effect.crackle — szikrák a kirajzolódó élen; a villám felülírja mindkettőt). Két klip: a jelenet
színezése a felvétel fölött (normál) + a fény a felfedett réteg fölött (screen).

A „TÜZES GRAFIKON” (on: "L17") — így működik
--------------------------------------------
  1. effect_layers.prepare_on_layers: a projekt első építése után a motor a cél-réteget EGYEDÜL, átlátszóan rendereli
     a hatás szakaszára (fél méretben; ugyanaz, mint live_layers._solo + alpha), az alfája kockánként a rajzolóhoz megy
     (a követés helyett) — a kirajzolódó diagrammal együtt nő;
  2. common.Anchors: on = réteg → common.mask_edge: a maszk felső éle oszloponként → egy Caster (a lángok felfelé);
  3. blend = screen kötelező (a fény a réteg FÖLÖTT): a builder a klipet a cél-réteg fölé teszi (moveBefore), SCREEN
     keveréssel; az élő előnézet z-sorrendje a motor sorrendjéből jön (live_layers), így magától fölé kerül;
  4. a projekt újraépül a klippel. A gyorsítótár kulcsa a cél-réteg builder-leírása (szöveg, értékek, elemek) +
     az effekt mezői + a szakasz.
  Bővíthető: a vonal kirajzolódó vége (az alfa új pixelei az előző kockához képest) mint horgony — egy „mentén ég”
  effekthez.
"""
from __future__ import annotations

import importlib
import re
from typing import Any

REGISTRY: dict[str, tuple[str, str]] = {
    "lightning": ("app.tools.fx.lightning", "Lightning"),
    "fireball": ("app.tools.fx.fireball", "Fireball"),
    "flames": ("app.tools.fx.flames", "Flames"),
}
PLANNED = ("flame_jet", "energy_orb")
COMMON_FIELDS = ("effect", "on", "blend", "after_said", "at_speech_end_after", "seed", "trigger", "reveal", "lead", "build")
REVEAL_LEAD, REVEAL_BUILD = 0.5, 1.2     # s: a becsapás a réteg előtt; a felépülés (szikrák a kirajzolódó élén)
BLENDS = ("over", "screen")       # over: előre komponálva a felvétellel (a jelenet színezésével); screen: csak a fény


def names() -> tuple[str, ...]:
    return tuple(REGISTRY)


def get(name: str):
    """Az effekt osztálya (common.Effect alosztály)."""
    mod, cls = REGISTRY[name]
    return getattr(importlib.import_module(mod), cls)


def version(name: str) -> int:
    from app.tools.fx import common
    return int(getattr(importlib.import_module(REGISTRY[name][0]), "VERSION", 1)) * 1000 + common.TRACK_VERSION


def fields(name: str) -> tuple[str, ...]:
    return COMMON_FIELDS + tuple(get(name).PARAMS)


def params(name: str, r: dict[str, Any], what: str) -> dict[str, Any]:
    """Az effekt saját mezői a sémája szerint (PARAMS), alapértékkel, a határok közé szorítva. ValueError: rossz alak."""
    from app.tools.fx import common
    out: dict[str, Any] = {}
    for key, spec in get(name).PARAMS.items():
        kind, default = spec[0], spec[1]
        v = r.get(key)
        if v in (None, ""):
            out[key] = default
            continue
        if kind == "target":
            if isinstance(v, (list, tuple)):
                if len(v) != 2 or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
                    raise ValueError(f"{what} ({name}): target [x, y] = the point to aim at, 0–1 of the frame (or "
                                     f"{' | '.join(common.TARGETS)}).")
                out[key] = [round(max(-0.5, min(1.5, float(x))), 4) for x in v]
            else:
                tg = str(v).strip().lower().replace("towards the ", "").replace(" edge", "")
                if tg not in common.TARGETS:
                    raise ValueError(f"{what} ({name}): target {v!r} — arm (the forearm's direction) | right | left | up | "
                                     "down (towards that edge) | [x, y] (a point, 0–1 of the frame).")
                out[key] = tg
        elif kind.startswith("choice:"):
            opts = kind.split(":", 1)[1].split("|")
            if str(v).lower() not in opts:
                raise ValueError(f"{what} ({name}): {key} {v!r} — {' | '.join(opts)}.")
            out[key] = str(v).lower()
        else:
            try:
                x = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{what} ({name}): {key} = a number ({spec[2]:g}–{spec[3]:g}).") from None
            x = max(float(spec[2]), min(float(spec[3]), x))
            out[key] = int(round(x)) if kind == "int" else round(x, 3)
    return out


def is_layer(on: Any) -> bool:
    """A horgony egy másik grafikai réteg ("L17")."""
    return isinstance(on, str) and on not in ("hand", "body")


def anchor(v: Any, what: str) -> Any:
    """A horgony (on) tisztítva: hand | body | [x, y] | [x, y, w, h] | egy grafikai réteg id-ja ("L17")."""
    if v in (None, "", "hand", "hands", "arm"):
        return "hand"
    if isinstance(v, str) and v.strip().lower() in ("body", "torso", "chest"):
        return "body"
    if isinstance(v, (list, tuple)) and len(v) in (2, 4) and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
        xs = [round(max(0.0, min(1.0, float(x))), 4) for x in v]
        if len(xs) == 4:
            xs[2], xs[3] = max(0.01, xs[2]), max(0.01, xs[3])
        return xs
    if isinstance(v, str) and re.fullmatch(r"[A-Za-z]\w{0,19}", v.strip()):
        return v.strip()                                  # egy grafikai réteg id-ja: az alfája a sugárzó (a „tüzes grafikon”)
    raise ValueError(f"{what} (effect): on = \"hand\" | \"body\" | [x, y] | [x, y, w, h] (0–1 of the frame).")

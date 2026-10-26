"""Felfedés: egy effekt egy másik grafikai réteget fed fel („a villám kirajzolja a grafikont”). Bármelyik effekt
mehet vele (a hookjai: Effect.strike / Effect.crackle, alapértékkel — a villám felülírja), bármelyik grafikai réteggel.

Az óra a FELFEDETT RÉTEG: a saját at-ja a becsapódás kockája; az effekt ebből számol (effect_layers.sync_reveals):
  · becsapás (lead kocka a réteg előtt): az effekt a horgonytól (a kéz) a réteg dobozába csap — Effect.strike;
  · becsapódás (a réteg első kockája): villanás + izzó folt a doboz közepén; a réteg innen a SAJÁT beúszásával rajzolódik ki;
  · felépülés (build kocka): szikrák / kisülések a réteg kirajzolódó élén — az alfájának az előző kockához képest új
    pixelei (az oszlop, ahogy nő; a vonal, ahogy halad), ha ez kevés, a felső éle — Effect.crackle; a kéz betáplálása
    a felépülés első felében lecseng;
  · utána a fény lecseng, a réteg a rendes alakjában marad.
Kimenet kockánként kettő (Effect.split): a felvétel a jelenet színezésével (a felvétel fölött, a grafika alatt) és csak a
fény (screen, a felfedett réteg FÖLÖTT).
"""
from __future__ import annotations

from typing import Any, Optional

import cv2
import numpy as np

from app.tools.fx import common

VERSION = 1
FRONT_MIN = 20              # ennél kevesebb új pixel: a felső él is szikrázik
FRONT_MAX = 600             # legfeljebb ennyi pont kockánként (egyenletesen ritkítva — determinisztikus)


def front(prev: Optional[np.ndarray], mask: Optional[np.ndarray], W: int, H: int) -> np.ndarray:
    """A réteg kirajzolódó éle (px, W × H-ban): az alfájának új (átlátszatlan) pixelei az előző kockához képest; ha ez
    kevés, a felső éle is."""
    if mask is None:
        return np.zeros((0, 2), np.float32)
    mh, mw = mask.shape[:2]
    a = mask > common.OPAQUE
    new = a & ~(prev > common.OPAQUE) if prev is not None and prev.shape == mask.shape else a
    ys, xs = np.nonzero(new)
    pts = np.stack([xs * (W / mw), ys * (H / mh)], 1).astype(np.float32) if len(xs) else np.zeros((0, 2), np.float32)
    if len(pts) < FRONT_MIN:
        pts = np.concatenate([pts, common.mask_edge(mask, W, H)])
    if len(pts) > FRONT_MAX:
        pts = pts[np.linspace(0, len(pts) - 1, FRONT_MAX).round().astype(int)]
    return pts


class Reveal:
    """Egy effekt (common.Effect) felfedő menete. box: a felfedett réteg doboza (x0, y0, x1, y1 px); lead / build:
    kockák. frame(kép, követés, előző maszk, maszk, i) → (színezett felvétel, fény)."""

    def __init__(self, fx: common.Effect, box: tuple[float, float, float, float], lead: int, build: int):
        self.fx, self.box, self.lead, self.build = fx, box, int(lead), max(1, int(build))
        fx.reveal_lead = self.lead / fx.fps                         # (a hookok tudják, mikor a becsapódás: t = lead)
        self.center = np.array([(box[0] + box[2]) / 2, (box[1] + box[3]) / 2], np.float32)

    def frame(self, img: np.ndarray, tr: dict[str, Any], prev: Optional[np.ndarray], mask: Optional[np.ndarray],
              i: int) -> tuple[np.ndarray, np.ndarray]:
        fx = self.fx
        t = i / fx.fps
        casters = fx.anchors(tr, t, i < self.lead + self.build)
        layer = np.zeros((fx.h2, fx.w2, 3), np.float32)
        core = np.zeros((fx.H, fx.W, 3), np.float32)
        lights: list[np.ndarray] = []
        power = 0.0
        k = i - self.lead
        if k < 0:                                                   # becsapás
            power = fx.strike(layer, core, casters, t, lights, self.box, 1.0)
        elif k < self.build:
            u = k / self.build
            if k < 4:                                               # becsapódás: villanás + izzó folt
                fx.flash = max(fx.flash, 1.0 - 0.2 * k)
                r = int((min(self.box[2] - self.box[0], self.box[3] - self.box[1]) * (0.15 + 0.1 * k)) / 2)
                cv2.circle(layer, tuple(int(x) // 2 for x in self.center), max(2, r), (fx.GLOW * (1 - 0.2 * k)).tolist(), -1,
                           cv2.LINE_AA)
                lights.append(self.center / 2)
                power = 1.0
            feed = max(0.0, 1.0 - 2.0 * u)                          # a kéz betáplálása a felépülés első felében
            if feed > 0:
                power = max(power, fx.strike(layer, core, casters, t, lights, self.box, feed))
            power = max(power, fx.crackle(layer, core, front(prev, mask, fx.W, fx.H), t, lights, 1.0 - 0.5 * u))
        fx.step_light(power)
        return fx.split(img, layer, core, lights, power)

"""Villám a kézből (Palpatine, VI. rész) — a 10-25-i próba (.claude/qa/lightning/lightning.py), ahogy a Kommandant
hangolta: mélykék / ibolya izzás vékony fehér-cián maggal; sok szaggatott szál IRÁNYÍTOTT kötegben (nem legyező) a cél
felé, egy-egy rövid elágazással; a jelenet elsötétül és kékül; lágy Gauss-fény a kézen; screen keverés; 3 kockás
felfutás; az irány az alkar, vagy a megadott cél."""
from __future__ import annotations

import math
from typing import Any

import cv2
import numpy as np

from app.tools.fx import common

VERSION = 2                 # a rajz változásakor emelni (a gyorsítótár kulcsában van)


class Lightning(common.Effect):
    NAME = "lightning"
    PARAMS = {"target": ("target", "arm"), "strands": ("int", 6, 1, 16), "intensity": ("float", 1.0, 0.3, 2.0)}
    DEFAULT_DUR = 4.0
    CORE = np.array([255, 245, 215], np.float32)        # BGR: fehér-cián mag
    GLOW = np.array([255, 70, 40], np.float32)          # BGR: mélykék, lilás izzás
    TINT = np.array([1.25, 0.55, 0.45], np.float32)     # BGR: a kékülő jelenet
    GRADE = (0.75, 0.5, 0.85)
    G1, G2, LIT = (1 / 90, 2.0), (1 / 25, 1.0), (0.16, 0.45)

    def __init__(self, W: int, H: int, fps: float, params: dict[str, Any], seed: int = 7):
        super().__init__(W, H, fps, params, seed)
        self.strands = int(params.get("strands") or 6)
        self.glow_w, self.core_w = max(4, int(self.h2 * 0.022)), max(1, int(self.H * 0.0028))

    def draw(self, layer, core, casters, t, active, lights) -> float:
        rng, power = self.rng, 0.0
        for c in casters:
            power = max(power, c.power)
            lights.append(c.center / 2)
            target, d = common.aim(self.target, c.center, c.d, self.W, self.H)
            ramp = min(1.0, t * self.fps / common.RAMP_FRAMES + 1 / common.RAMP_FRAMES)       # 3 kockás felfutás
            side = np.array([-d[1], d[0]], np.float32)
            for _ in range(max(1, round(self.strands * ramp))):     # irányított köteg: vékony, szaggatott szálak a cél felé
                st = c.pts[int(rng.integers(len(c.pts)))]
                end = target + side * rng.normal(0, self.diag * 0.08)
                main_ = common.bolt(st, end, 0.17, 7, rng)
                strands = [(main_, 1.0)]
                for _b in range(int(rng.integers(0, 2))):           # rövid elágazás
                    i = int(rng.integers(len(main_) // 6, len(main_) * 3 // 4))
                    a2 = math.atan2(d[1], d[0]) + rng.choice([-1, 1]) * rng.uniform(0.3, 0.9)
                    e2 = main_[i] + np.array([math.cos(a2), math.sin(a2)], np.float32) * self.diag * rng.uniform(0.05, 0.15)
                    strands.append((common.bolt(main_[i], e2, 0.22, 5, rng), 0.5))
                for b, wmul in strands:
                    q = np.round(b).astype(np.int32)
                    cv2.polylines(layer, [q // 2], False, self.GLOW.tolist(), max(2, int(self.glow_w * wmul)), cv2.LINE_AA)
                    cv2.polylines(core, [q], False, self.CORE.tolist(), max(1, int(self.core_w * wmul)), cv2.LINE_AA)
            cc = tuple(int(x) // 2 for x in c.center)                # izzás a tenyér körül
            cv2.circle(layer, cc, int(self.h2 * 0.05), self.GLOW.tolist(), -1, cv2.LINE_AA)
        return power

"""Lángok: a sugárzó pontokból felfelé (vagy a cél felé) lobogó lángnyelvek, narancs izzással, sárgás maggal — a
„tüzes grafikon” effektje (on: egy grafikai réteg: a lángok az oszlopok tetejéből, a vonal éléből csapnak fel), de kézből /
pontból / téglalapból is. Ugyanaz a screen keverés, mint a villámé (app/tools/fx/common.py)."""
from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from app.tools.fx import common

VERSION = 1


class Flames(common.Effect):
    NAME = "flames"
    PARAMS = {"target": ("target", "up"), "height": ("float", 1.0, 0.3, 3.0), "density": ("float", 1.0, 0.3, 2.0),
              "intensity": ("float", 1.0, 0.3, 2.0)}
    DEFAULT_DUR = 4.0
    GLOW = np.array([20, 100, 255], np.float32)         # BGR: narancs
    DEEP = np.array([10, 40, 210], np.float32)          # BGR: vörös
    CORE = np.array([150, 230, 255], np.float32)        # BGR: sárgás mag
    TINT = np.array([0.45, 0.75, 1.3], np.float32)
    GRADE = (0.4, 0.3, 0.85)
    G1, G2, LIT = (1 / 110, 1.5), (1 / 30, 0.8), (0.12, 0.35)
    CORE_GAIN = 0.8
    MAX_TONGUES = 40

    def __init__(self, W: int, H: int, fps: float, params: dict[str, Any], seed: int = 7):
        super().__init__(W, H, fps, params, seed)
        self.h = self.H * 0.06 * float(params.get("height") or 1.0)
        self.density = float(params.get("density") or 1.0)

    def draw(self, layer, core, casters, t, active, lights) -> float:
        rng, power = self.rng, 0.0
        ramp = min(1.0, t * self.fps / 6 + 1 / 6)                   # a láng 6 kocka alatt kap erőre
        for c in casters:
            power = max(power, c.power)
            _, d = common.aim(self.target, c.center, c.d, self.W, self.H)
            side = np.array([-d[1], d[0]], np.float32)
            pts = c.pts
            n = min(len(pts), max(1, int(round(self.MAX_TONGUES * self.density))))
            idx = np.linspace(0, len(pts) - 1, n).round().astype(int) if len(pts) > 1 else np.zeros(1, int)
            for p in pts[idx]:
                for _ in range(2):                                  # két nyelv pontonként: egy magas, egy alacsony
                    h = self.h * ramp * rng.uniform(0.45, 1.2)
                    tip = p + d * h + side * rng.normal(0, h * 0.25)
                    tongue = common.bolt(p, tip, 0.22, 4, rng)
                    w = max(2, int(self.h * 0.16))
                    col = self.GLOW if rng.uniform() < 0.7 else self.DEEP
                    cv2.polylines(layer, [np.round(tongue / 2).astype(np.int32)], False, col.tolist(), max(2, w // 2), cv2.LINE_AA)
                    cv2.polylines(core, [np.round(tongue[: len(tongue) * 2 // 3]).astype(np.int32)], False,
                                  (self.CORE * 0.8).tolist(), max(1, w // 4), cv2.LINE_AA)
            lights.append(c.center / 2)
        return power * ramp

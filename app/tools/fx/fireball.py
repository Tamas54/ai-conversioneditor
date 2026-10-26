"""Tűzgolyó: a tenyérben formálódik (form másodperc alatt nő), aztán az alkar irányában (vagy a cél felé) elrepül,
narancs izzással, lágy csóvával és hátrafelé lobogó lángnyelvekkel; a jelenet melegen sötétedik. Ugyanaz a screen
keverés, mint a villámé (app/tools/fx/common.py). Ha a cél egy képen belüli pont, ott szétcsap (kis robbanás)."""
from __future__ import annotations

import math
from typing import Any, Optional

import cv2
import numpy as np

from app.tools.fx import common

VERSION = 1


def _smooth(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


class Fireball(common.Effect):
    NAME = "fireball"
    PARAMS = {"target": ("target", "arm"), "size": ("float", 1.0, 0.4, 2.5), "form": ("float", 0.6, 0.2, 2.0),
              "speed": ("float", 1.4, 0.4, 4.0), "intensity": ("float", 1.0, 0.3, 2.0)}
    DEFAULT_DUR = 2.0
    GLOW = np.array([30, 120, 255], np.float32)         # BGR: narancs
    DEEP = np.array([10, 45, 220], np.float32)          # BGR: mélyvörös a széleken
    CORE = np.array([190, 235, 255], np.float32)        # BGR: halványsárga-fehér mag
    TINT = np.array([0.45, 0.75, 1.3], np.float32)      # BGR: a melegedő jelenet
    GRADE = (0.5, 0.35, 0.85)
    G1, G2, LIT = (1 / 70, 1.6), (1 / 18, 1.0), (0.2, 0.55)
    CORE_GAIN = 1.0
    TRAIL = 10                                          # a csóva ennyi kocka helyéből

    def __init__(self, W: int, H: int, fps: float, params: dict[str, Any], seed: int = 7):
        super().__init__(W, H, fps, params, seed)
        self.R = self.H * 0.035 * float(params.get("size") or 1.0)
        self.form = float(params.get("form") or 0.6)
        self.v = float(params.get("speed") or 1.4) * self.diag / self.fps          # px / kocka
        self.phase, self.pos, self.dir = "form", None, None
        self.start: Optional[np.ndarray] = None
        self.goal: Optional[np.ndarray] = None
        self.trail: list[np.ndarray] = []
        self.burst = -1

    def _ball(self, layer, core, p: np.ndarray, r: float, d: np.ndarray, lights) -> None:
        rng, h2 = self.rng, self.h2
        side = np.array([-d[1], d[0]], np.float32)
        for k, q in enumerate(self.trail):                          # a csóva: halványuló, szűkülő foltok
            w = (k + 1) / (len(self.trail) + 1)
            cv2.circle(layer, tuple(int(x) for x in q / 2), max(1, int(r * (0.35 + 0.5 * w) / 2)),
                       (self.DEEP * (1 - w) + self.GLOW * w).tolist(), -1, cv2.LINE_AA)
        for _ in range(4):                                          # lángnyelvek hátrafelé
            tip = p - d * r * rng.uniform(1.4, 3.0) + side * rng.normal(0, r * 0.6)
            lick = common.bolt(p, tip, 0.3, 4, rng)
            cv2.polylines(layer, [np.round(lick / 2).astype(np.int32)], False, self.GLOW.tolist(),
                          max(2, int(r * 0.22)), cv2.LINE_AA)
        for _ in range(6):                                          # a golyó: lobogó foltok
            o = p + rng.normal(0, r * 0.22, 2).astype(np.float32)
            col = self.DEEP * 0.3 + self.GLOW * 0.7 if rng.uniform() < 0.6 else self.GLOW
            cv2.circle(layer, tuple(int(x) for x in o / 2), max(1, int(r * rng.uniform(0.55, 0.9) / 2)), col.tolist(), -1, cv2.LINE_AA)
        # a mag: lágy, nem éles korong (koncentrikus körök + kis homály a környékén)
        x0, y0 = int(max(0, p[0] - r * 1.2)), int(max(0, p[1] - r * 1.2))
        x1, y1 = int(min(self.W, p[0] + r * 1.2)), int(min(self.H, p[1] + r * 1.2))
        if x1 - x0 > 2 and y1 - y0 > 2:
            roi = core[y0:y1, x0:x1]
            c = (int(p[0]) - x0, int(p[1]) - y0)
            for f, g in ((0.55, 0.35), (0.38, 0.35), (0.22, 0.3)):
                cv2.circle(roi, c, max(1, int(r * f)), (self.CORE * g).tolist(), -1, cv2.LINE_AA)
            core[y0:y1, x0:x1] = cv2.GaussianBlur(roi, (0, 0), max(0.8, r * 0.12))
        lights.append(p / 2)

    def draw(self, layer, core, casters, t, active, lights) -> float:
        if self.phase == "done" or (not active and self.phase == "form"):
            return 0.0
        if self.phase == "form":
            if not casters:
                return 0.0
            c = max(casters, key=lambda x: x.power)
            k = _smooth(t / self.form)
            r = self.R * (0.25 + 0.75 * k)
            self._ball(layer, core, c.center, r, c.d, lights)
            if t + 1e-9 >= self.form:                               # elrugaszkodás: a tenyérből a cél felé
                self.goal, self.dir = common.aim(self.target, c.center, c.d, self.W, self.H)
                self.start, self.pos, self.phase = c.center.copy(), c.center.copy(), "fly"
            return 0.4 + 0.6 * k
        if self.phase == "fly":
            self.trail = (self.trail + [self.pos.copy()])[-self.TRAIL:]
            self.pos = self.pos + self.dir * self.v
            far = np.linalg.norm(self.pos - self.start) >= np.linalg.norm(self.goal - self.start)
            off = not (-3 * self.R < self.pos[0] < self.W + 3 * self.R and -3 * self.R < self.pos[1] < self.H + 3 * self.R)
            inside = 0 <= self.goal[0] <= self.W and 0 <= self.goal[1] <= self.H
            if far and inside:
                self.phase, self.burst = "burst", 0
            elif far or off or not active:
                self.phase = "done"
                return 0.0
            else:
                self._ball(layer, core, self.pos, self.R, self.dir, lights)
                return 1.0
        if self.phase == "burst":                                   # szétcsapás a célpontban (8 kocka)
            n = 8
            k = self.burst / n
            g = self.goal
            cv2.circle(layer, tuple(int(x) for x in g / 2), int(self.R * (1 + 3 * k) / 2), (self.GLOW * (1 - k)).tolist(),
                       max(2, int(self.R * 0.5 * (1 - k))), cv2.LINE_AA)
            cv2.circle(layer, tuple(int(x) for x in g / 2), int(self.R * (1 + 1.5 * k) / 2), (self.GLOW * (1 - k)).tolist(), -1,
                       cv2.LINE_AA)
            lights.append(g / 2)
            self.burst += 1
            if self.burst > n:
                self.phase = "done"
            return 1.0 - k
        return 0.0

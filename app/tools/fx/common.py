"""Az effekt-család közös darabjai (app/tools/fx/__init__.py: a regiszter és a leírás).

  · követés: Tracker — kéz (legfeljebb 2) + póz kockánként (MediaPipe Hand + Pose Landmarker, Apache-2.0, CPU), a kép
    arányában (0–1); a hívó (effect_layers) a tartalom szerint Postgresben tárolja, és újra nem futtatja;
  · horgony: anchors() — honnan sugároz az effekt: a kéz / az alkar (követés), a test (póz), egy pont, egy téglalap →
    Caster-ek (sugárzó pontok, irány, erő, középpont);
  · irány: aim() — az alkar iránya, vagy a cél (a kép egy széle, vagy egy pont);
  · fény: Grade (a jelenet sötétítése és színezése a felvétel fényességéből: kép·(1−a) + fényesség·TINT·a, ·m) +
    compose() (két homály az izzásra, Gauss-fény a sugárzó pontokon, screen keverés — a meleg kép nem ég ki —, + a mag);
  · Effect: az alaposztály — kockánként: horgony → draw() (az effekt saját rajza a fél felbontású izzás-rétegre és a
    teljes felbontású magra) → a villanás / színezés állapota → compose(). Egy új effekt egy fájl: PARAMS + draw().

A méretek mind a kép MAGASSÁGÁHOZ mérve (a villám próbája 720×1280-on készült): bármekkora felbontáson ugyanaz a kép.
A rajz tiszta függvény: (kockák, követés, paraméterek, mag) → kockák, bájtra ugyanaz (a tesztek a hash-t nézik).
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np

TRACK_VERSION = 1
TRACK_SIDE = 1280           # a felismerő ekkora (hosszabbik oldal) kivilágosított másolatot kap — a próbáé is ennyi volt
TIPS, MCPS = (8, 12, 16, 20), (5, 9, 13, 17)
FLASH_K = 0.6               # a villanás lecsengése kockánként
RAMP_FRAMES = 3             # felfutás az indításnál
TARGETS = ("arm", "right", "left", "up", "down")
HAND_MODEL, POSE_MODEL = "hand_landmarker.task", "pose_landmarker_full.task"


def tail_frames(decay: float, floor: float = 0.02) -> int:
    """Ennyi kocka az effekt vége után, amíg a színezés (decay-es lecsengés) floor alá csökken — a klip ezt is tartalmazza."""
    return int(math.ceil(math.log(floor) / math.log(decay)))


def bolt(p0: np.ndarray, p1: np.ndarray, rough: float, depth: int, rng: np.random.Generator) -> np.ndarray:
    """Középpont-eltolásos tört vonal p0 → p1: minden szinten a felezőpont merőlegesen elmozdul (a hossz arányában)."""
    pts = np.array([p0, p1], np.float32)
    for _ in range(depth):
        a, b = pts[:-1], pts[1:]
        d = b - a
        n = np.stack([-d[:, 1], d[:, 0]], 1)
        L = np.linalg.norm(d, axis=1, keepdims=True) + 1e-6
        mid = (a + b) / 2 + n / L * (rng.normal(0, 1, (len(a), 1)) * L * rough)
        out = np.empty((len(pts) + len(mid), 2), np.float32)
        out[0::2], out[1::2] = pts, mid
        pts = out
    return pts


_CLAHE = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))


def brighten(frame: np.ndarray) -> np.ndarray:
    """Sötét felvétel a felismerőnek: gamma + helyi kontraszt (CLAHE) a fényességen."""
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    L = lab[..., 0]
    if L.mean() < 110:
        L = (255 * (L / 255.0) ** 0.55).astype(np.uint8)
    lab[..., 0] = _CLAHE.apply(L)
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


class Smooth:
    """Pontok simítása (EMA), kulcsonként (bal / jobb kéz, kar)."""

    def __init__(self, k: float = 0.55):
        self.k, self.v, self.miss = k, {}, {}

    def __call__(self, label: str, pts: Optional[np.ndarray]) -> Optional[np.ndarray]:
        if pts is None:
            self.miss[label] = self.miss.get(label, 0) + 1
            if self.miss[label] > 4:
                self.v.pop(label, None)
            return self.v.get(label)
        self.miss[label] = 0
        prev = self.v.get(label)
        if prev is not None and prev.shape != pts.shape:      # kézkövető ↔ pózpontok váltásnál újrakezd
            prev = None
        self.v[label] = pts if prev is None else prev * (1 - self.k) + pts * self.k
        return self.v[label]


def open_hand(p: np.ndarray) -> float:
    """0–1: mennyire nyitott a kéz (az ujjhegyek a csuklótól messzebb vannak, mint a tövük)."""
    w = p[0]
    r = [np.linalg.norm(p[t] - w) / (np.linalg.norm(p[m] - w) + 1e-6) for t, m in zip(TIPS, MCPS)]
    return float(np.clip((np.mean(r) - 1.3) / 0.5, 0, 1))


# ---------- a követés (MediaPipe) ----------

def models_dir() -> Path:
    return Path(os.getenv("MEDIAPIPE_MODELS", "/opt/models"))


def available() -> Optional[str]:
    """None, ha a követő fut ezen a szerveren; különben a hiányzó rész neve."""
    try:
        import mediapipe  # noqa: F401
        from mediapipe.tasks.python import vision  # noqa: F401
    except Exception:  # noqa: BLE001
        return "MediaPipe (hand tracking) isn't installed on this server"
    for m in (HAND_MODEL, POSE_MODEL):
        if not (models_dir() / m).exists():
            return f"the hand-tracking model {m} isn't installed on this server"
    return None


def tracker_id() -> str:
    try:
        import mediapipe
        ver = mediapipe.__version__
    except Exception:  # noqa: BLE001
        ver = "none"
    return f"mediapipe-{ver}:hand+pose-full:t{TRACK_VERSION}:side{TRACK_SIDE}"


class Tracker:
    """Kéz (legfeljebb 2) + póz kockánként, VIDEO módban (a háló az előző kockából követ). A bemenet BGR uint8."""

    def __init__(self):
        from mediapipe.tasks import python as mpt
        from mediapipe.tasks.python import vision
        d = models_dir()
        self.hand = vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
            base_options=mpt.BaseOptions(model_asset_path=str(d / HAND_MODEL)), running_mode=vision.RunningMode.VIDEO,
            num_hands=2, min_hand_detection_confidence=0.4, min_tracking_confidence=0.4))
        self.pose = vision.PoseLandmarker.create_from_options(vision.PoseLandmarkerOptions(
            base_options=mpt.BaseOptions(model_asset_path=str(d / POSE_MODEL)), running_mode=vision.RunningMode.VIDEO))

    def __call__(self, frame: np.ndarray, ms: int) -> dict[str, Any]:
        """→ {"h": [[[x, y] × 21] …], "p": [[x, y, láthatóság] × 33] | None} — a kép arányában (0–1)."""
        import mediapipe as mp
        h, w = frame.shape[:2]
        k = min(1.0, TRACK_SIDE / max(h, w))
        small = cv2.resize(frame, (max(2, round(w * k)), max(2, round(h * k))), interpolation=cv2.INTER_AREA) if k < 1 else frame
        img = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(brighten(small), cv2.COLOR_BGR2RGB))
        res = self.hand.detect_for_video(img, ms)
        pres = self.pose.detect_for_video(img, ms)
        r5 = lambda v: round(float(v), 5)                                                   # noqa: E731
        hands = [[[r5(q.x), r5(q.y)] for q in hl] for hl in (res.hand_landmarks or [])]
        pose = [[r5(q.x), r5(q.y), r5(q.visibility or 0.0)] for q in pres.pose_landmarks[0]] if pres.pose_landmarks else None
        return {"h": hands, "p": pose}

    def close(self) -> None:
        for x in (self.hand, self.pose):
            try:
                x.close()
            except Exception:  # noqa: BLE001
                pass


# ---------- a horgony és az irány ----------

@dataclass
class Caster:
    """Ahonnan az effekt sugároz: pts (sugárzó pontok, px), d (egységirány), power (0–1), center (a fény helye, px)."""
    pts: np.ndarray
    d: np.ndarray
    power: float
    center: np.ndarray


def needs_tracking(on: Any) -> bool:
    return on in (None, "hand", "body")


OPAQUE = 240                # a maszkban ennél átlátszatlanabb pont sugároz: az oszlop, a vonal, a szöveg — a félig
                            # átlátszó sötét panel (86 % = 219) nem (különben a panel teteje égne a grafikon helyett)


def mask_edge(mask: np.ndarray, W: int, H: int, step: Optional[int] = None) -> np.ndarray:
    """Egy réteg alfájának (H × W, 0–255) felső éle oszloponként (minden step. oszlopban az első átlátszatlan pont) →
    [(x, y)] px — innen lobognak a lángok (egy grafikon oszlopainak teteje, a kirajzolt vonal)."""
    if mask.shape[:2] != (H, W):
        mask = cv2.resize(mask, (W, H), interpolation=cv2.INTER_AREA)
    step = step or max(4, W // 48)
    cols = mask[:, ::step] > OPAQUE
    has = cols.any(0)
    if not has.any():
        return np.zeros((0, 2), np.float32)
    ys = cols.argmax(0)
    xs = np.arange(cols.shape[1]) * step
    return np.stack([xs[has], ys[has]], 1).astype(np.float32)


class Anchors:
    """A horgony kockánként → [Caster]. on: hand (alap: a felemelt kéz, az alkar irányával — a villám próbájának
    szabályai) | body (a törzs közepe, a vállak közt) | [x, y] (egy pont) | [x, y, w, h] (egy téglalap: közép + méret,
    0–1). A simítás állapota itt él (kezenként)."""

    def __init__(self, W: int, H: int, on: Any = "hand"):
        self.W, self.H, self.on = W, H, on if on is not None else "hand"
        self.sm, self.arms = Smooth(), Smooth(0.4)

    def __call__(self, tr: dict[str, Any], t: float, active: bool) -> list[Caster]:
        W, H, on = self.W, self.H, self.on
        if isinstance(on, str) and on not in ("hand", "body"):    # egy grafikai réteg: az alfájának felső éle sugároz
            m = tr.get("mask")
            pts = mask_edge(m, W, H) if (active and m is not None) else np.zeros((0, 2), np.float32)
            if not len(pts):
                return []
            return [Caster(pts, np.array([0.0, -1.0], np.float32), 1.0, pts.mean(0))]
        if isinstance(on, (list, tuple)):                          # rögzített pont / téglalap (követés nélkül)
            if not active:
                return []
            c = np.array([on[0] * W, on[1] * H], np.float32)
            if len(on) == 4:
                hw, hh = on[2] * W / 2, on[3] * H / 2
                pts = np.array([c, c + [-hw, -hh], c + [hw, -hh], c + [-hw, hh], c + [hw, hh], c + [0, -hh]], np.float32)
            else:
                pts = c[None]
            return [Caster(pts, np.array([1.0, 0.0], np.float32), 1.0, c)]
        hand_pts = [np.array([[x * W, y * H] for x, y in hl], np.float32) for hl in tr.get("h") or []]
        pl = tr.get("p")
        out: list[Caster] = []
        if pl and on == "body":
            P = lambda i: np.array([pl[i][0] * W, pl[i][1] * H], np.float32)               # noqa: E731
            if active and min(pl[11][2], pl[12][2]) > 0.25:
                c = self.sm("body", (P(11) + P(12)) / 2 * 0.7 + (P(23) + P(24)) / 2 * 0.3)
                out.append(Caster(np.array([P(11), P(12), c], np.float32), np.array([1.0, 0.0], np.float32), 1.0, c))
            return out
        if pl:
            P = lambda i: np.array([pl[i][0] * W, pl[i][1] * H], np.float32)               # noqa: E731
            vis = lambda i: float(pl[i][2])                                                 # noqa: E731
            hip_y = (P(23)[1] + P(24)[1]) / 2 if min(vis(23), vis(24)) > 0.2 else H * 0.75
            for name, (sh, el, wr, pk, ix, th) in {"L": (11, 13, 15, 17, 19, 21), "R": (12, 14, 16, 18, 20, 22)}.items():
                if vis(wr) < 0.25:
                    self.sm(name, None)
                    continue
                w_ = P(wr)
                near = [hp for hp in hand_pts if np.linalg.norm(hp[0] - w_) < H * 0.12]
                hp = self.sm(name, near[0] if near else np.array([P(wr), P(th), P(ix), P(pk)], np.float32))
                if hp is None or len(hp) != (21 if near else 4):
                    hp = near[0] if near else np.array([P(wr), P(th), P(ix), P(pk)], np.float32)
                tips = hp[[4, 8, 12, 16, 20]] if len(hp) == 21 else hp[1:]
                d = self.arms(name, w_ - P(el))
                if d is None or np.linalg.norm(d) < H * 0.05:                               # a kamerára mutat: a kéz iránya
                    d = (hp[9] - hp[0]) if len(hp) == 21 else (P(ix) - w_)
                raised = w_[1] < hip_y - H * 0.03
                o = max(open_hand(hp), 0.6) if len(hp) == 21 else 0.8
                if active and (raised or t < 1.5):            # az első másfél mp-ben akkor is, ha a kéz még emelkedik
                    out.append(Caster(tips, d / (np.linalg.norm(d) + 1e-6), o, hp[9] if len(hp) == 21 else w_))
        else:                                                 # póz nélkül: csak a kézkövető
            for hp in hand_pts:
                o = open_hand(hp)
                if active and o >= 0.35:
                    d = hp[9] - hp[0]
                    out.append(Caster(hp[[4, 8, 12, 16, 20]], d / (np.linalg.norm(d) + 1e-6), o, hp[9]))
        return out


def aim(target: Any, center: np.ndarray, d: np.ndarray, W: int, H: int) -> tuple[np.ndarray, np.ndarray]:
    """A cél és az irány: arm = az alkar irányában, a képen túl; right | left | up | down = a kép széle felé a horgony
    magasságában / oszlopában; [x, y] = egy pont (0–1). → (célpont px, egységirány)"""
    diag = math.hypot(W, H)
    if target in (None, "arm"):
        return center + d * diag * 0.85, d
    if isinstance(target, (list, tuple)):
        tgt = np.array([float(target[0]) * W, float(target[1]) * H], np.float32)
    else:
        tgt = np.array({"right": [W * 1.1, center[1]], "left": [-W * 0.1, center[1]], "up": [center[0], -H * 0.1],
                        "down": [center[0], H * 1.1]}[target], np.float32)
    return tgt, (tgt - center) / (np.linalg.norm(tgt - center) + 1e-6)


# ---------- a fény ----------

class Grade:
    """A jelenet sötétítése és színezése (a felvétel fényességéből), lecsengéssel: a = strength·gk, m = 1 − darken·gk +
    0.25·flash; kép·(1−a) + fényesség·TINT·a, ·m — egy 3×3-as mátrix kockánként (cv2.transform)."""

    def __init__(self, tint: np.ndarray, strength: float, darken: float, decay: float):
        self.tint, self.strength, self.darken, self.decay = np.asarray(tint, np.float32), strength, darken, decay
        self.k = 0.0

    def step(self, on: bool) -> float:
        self.k = self.decay * self.k + (1 - self.decay) * (1.0 if on else 0.0)
        return self.k

    def matrix(self, flash: float) -> np.ndarray:
        gk = min(1.0, self.k)
        a, m = self.strength * gk, 1 - self.darken * gk + 0.25 * flash
        return (m * ((1 - a) * np.eye(3, dtype=np.float32) + a * np.outer(self.tint, np.full(3, 1 / 3, np.float32)))).astype(np.float32)


def _blur(x: np.ndarray, sigma: float) -> np.ndarray:
    """Gauss-homály; a széles (≥ 16 px) fél felbontáson számolva és visszanagyítva — mérve (10-26, 1080×1920, σ = 38):
    7× gyorsabb (30 ms a 210 helyett), az eltérés legfeljebb 2,4 / 255 (átlag 0,17) — nem látszik."""
    if sigma < 16:
        return cv2.GaussianBlur(x, (0, 0), sigma)
    h, w = x.shape[:2]
    small = cv2.resize(x, (max(1, w // 2), max(1, h // 2)), interpolation=cv2.INTER_AREA)
    return cv2.resize(cv2.GaussianBlur(small, (0, 0), sigma / 2), (w, h), interpolation=cv2.INTER_LINEAR)


def glow_of(layer: np.ndarray, lights: list[np.ndarray], flash: float, W: int, H: int, *, glow_color: np.ndarray,
            g1: tuple[float, float], g2: tuple[float, float], lit: tuple[float, float],
            grid: Optional[tuple[np.ndarray, np.ndarray]] = None) -> np.ndarray:
    """Az izzás (0–1, teljes méret): a fél felbontású réteg két homállyal + Gauss-fény a fénypontokon."""
    h2, w2 = layer.shape[:2]
    a = _blur(layer, h2 * g1[0])
    b = _blur(layer, h2 * g2[0])
    g = np.clip(a * g1[1] + b * (g2[1] * (0.7 + 0.3 * flash)), 0, 255)
    if lights:
        yy, xx = grid if grid is not None else [x.astype(np.float32) for x in np.mgrid[0:h2, 0:w2]]
        spot = np.zeros((h2, w2), np.float32)
        s2 = 2 * (h2 * lit[0]) ** 2
        for cx, cy in lights:
            spot += np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / s2)
        g += spot[..., None] * (np.asarray(glow_color, np.float32) * (lit[1] * (0.6 + 0.4 * flash)))
    return cv2.resize(np.clip(g, 0, 255), (W, H)) * (1 / 255.0)


def compose(img: np.ndarray, layer: np.ndarray, core: Optional[np.ndarray], lights: list[np.ndarray], grade: Grade,
            flash: float, *, glow_color: np.ndarray, g1: tuple[float, float], g2: tuple[float, float], lit: tuple[float, float],
            core_gain: float = 0.9, grid: Optional[tuple[np.ndarray, np.ndarray]] = None) -> np.ndarray:
    """A kész kocka: izzás (layer, fél felbontás) két homállyal — g1 / g2 = (szigma a fél magasság arányában, erősség) —,
    Gauss-fény a fénypontokon (lit = (szigma arány, erősség), glow_color színben), a jelenet a Grade-del, az izzás screen
    keveréssel (a meleg kép nem ég ki fehérre), végül a mag hozzáadva."""
    H, W = img.shape[:2]
    h2, w2 = layer.shape[:2]
    a = _blur(layer, h2 * g1[0])
    b = _blur(layer, h2 * g2[0])
    g = np.clip(a * g1[1] + b * (g2[1] * (0.7 + 0.3 * flash)), 0, 255)
    if lights:
        yy, xx = grid if grid is not None else [x.astype(np.float32) for x in np.mgrid[0:h2, 0:w2]]
        spot = np.zeros((h2, w2), np.float32)
        s2 = 2 * (h2 * lit[0]) ** 2
        for cx, cy in lights:
            spot += np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / s2)
        g += spot[..., None] * (np.asarray(glow_color, np.float32) * (lit[1] * (0.6 + 0.4 * flash)))
    glow = cv2.resize(np.clip(g, 0, 255), (W, H)) * (1 / 255.0)
    out = cv2.transform(img.astype(np.float32), grade.matrix(flash))
    out = 255 - (255 - out) * (1 - glow)                       # screen
    if core is not None:
        out += core * core_gain
    return np.clip(out, 0, 255).astype(np.uint8)


class Effect:
    """Az effektek alaposztálya. Az alosztály adja: NAME, PARAMS (a mezők sémája: {név: (fajta, alap, min, max)} —
    fajta: int | float | target | choice:<a|b|c>), DEFAULT_DUR, GLOW (BGR), a Grade-je (TINT, GRADE = (strength,
    darken, decay)), G1 / G2 / LIT (compose), és draw(). Kockánként: a horgony → draw() a sugárzókra → a villanás és a
    színezés → compose()."""
    NAME = "effect"
    PARAMS: dict[str, tuple] = {}
    DEFAULT_DUR = 4.0
    GLOW = np.array([255, 70, 40], np.float32)
    TINT = np.array([1.25, 0.55, 0.45], np.float32)
    GRADE = (0.75, 0.5, 0.85)
    G1, G2, LIT = (1 / 90, 2.0), (1 / 25, 1.0), (0.16, 0.45)
    CORE_GAIN = 0.9

    def __init__(self, W: int, H: int, fps: float, params: dict[str, Any], seed: int = 7):
        self.W, self.H, self.fps = int(W), int(H), float(fps)
        self.p = params
        self.target = params.get("target") or "arm"
        self.intensity = float(params.get("intensity") or 1.0)
        self.rng = np.random.default_rng(int(seed))
        # blend "screen": csak a fény (fekete alapon, a jelenet színezése nélkül) — a képre screen keveréssel kerül, egy
        # grafikai réteg fölé is (lásd fx/__init__.py, A HORGONY); alap: előre komponálva a felvétellel
        self.light_only = params.get("blend") == "screen"
        self.anchors = Anchors(self.W, self.H, params.get("on") or "hand")
        self.grade = Grade(self.TINT, *self.GRADE)
        self.flash = 0.0
        self.h2, self.w2 = self.H // 2, self.W // 2                    # az izzás fél felbontáson (a homály a drága rész)
        self.diag = math.hypot(self.W, self.H)
        yy, xx = np.mgrid[0:self.h2, 0:self.w2]
        self._grid = (yy.astype(np.float32), xx.astype(np.float32))
        self.powers: list[float] = []

    @classmethod
    def tail(cls) -> int:
        return tail_frames(cls.GRADE[2])

    def draw(self, layer: np.ndarray, core: np.ndarray, casters: list[Caster], t: float, active: bool,
             lights: list[np.ndarray]) -> float:
        """Az effekt saját rajza: layer (fél felbontás, BGR 0–255, float) és core (teljes felbontás) — lights-ba a
        fénypontok (fél felbontású px). → az erő (0 = nem szól ezen a kockán)."""
        raise NotImplementedError

    def frame(self, img: np.ndarray, tr: dict[str, Any], t: float, active: bool) -> np.ndarray:
        casters = self.anchors(tr, t, active)
        layer = np.zeros((self.h2, self.w2, 3), np.float32)
        core = np.zeros((self.H, self.W, 3), np.float32)
        lights: list[np.ndarray] = []
        power = self.draw(layer, core, casters, t, active, lights)
        self.flash = FLASH_K * self.flash + (1 - FLASH_K) * (power * self.rng.uniform(0.55, 1.0))
        self.grade.step(power > 0)
        self.powers.append(round(power, 2))
        if self.light_only:
            img, grade = np.zeros_like(img), Grade(self.TINT, 0.0, 0.0, self.GRADE[2])
            if not (power > 0 or self.flash > 0.02):
                return img
        elif not (power > 0 or self.flash > 0.02 or self.grade.k > 0.02):
            return img
        else:
            grade = self.grade
        gi = self.intensity
        return compose(img, layer, core if power > 0 else None, lights, grade, self.flash, glow_color=self.GLOW,
                       g1=(self.G1[0], self.G1[1] * gi), g2=(self.G2[0], self.G2[1] * gi), lit=(self.LIT[0], self.LIT[1] * gi),
                       core_gain=self.CORE_GAIN, grid=self._grid)

    # ---------- két kimenet: a jelenet színezése (a felvétel fölött) + csak a fény (screen, egy réteg fölött) ----------

    def step_light(self, power: float) -> None:
        """A villanás és a színezés állapota egy kockával tovább (a frame() is így lép)."""
        self.flash = FLASH_K * self.flash + (1 - FLASH_K) * (power * self.rng.uniform(0.55, 1.0))
        self.grade.step(power > 0)
        self.powers.append(round(power, 2))

    def split(self, img: np.ndarray, layer: np.ndarray, core: np.ndarray, lights: list[np.ndarray],
              power: float) -> tuple[np.ndarray, np.ndarray]:
        """(a felvétel a jelenet színezésével, csak a fény fekete alapon) — screen(színezett, fény) = a frame() képe
        (a mag itt screen-nel kerül rá, ott hozzáadva: a fehér-cián mag mindkét úton ~fehér)."""
        gi = self.intensity
        graded = img if not (self.flash > 0.02 or self.grade.k > 0.02) else \
            np.clip(cv2.transform(img.astype(np.float32), self.grade.matrix(self.flash)), 0, 255).astype(np.uint8)
        if not (power > 0 or self.flash > 0.02):
            return graded, np.zeros_like(img)
        glow = glow_of(layer, lights, self.flash, self.W, self.H, glow_color=self.GLOW, g1=(self.G1[0], self.G1[1] * gi),
                       g2=(self.G2[0], self.G2[1] * gi), lit=(self.LIT[0], self.LIT[1] * gi), grid=self._grid)
        light = glow * 255.0
        if power > 0:
            light += core * self.CORE_GAIN
        return graded, np.clip(light, 0, 255).astype(np.uint8)

    def strike(self, layer: np.ndarray, core: np.ndarray, casters: list[Caster], t: float, lights: list[np.ndarray],
               box: tuple[float, float, float, float], amount: float) -> float:
        """Felfedés, 1. szakasz: az effekt a horgonytól a doboz (x0, y0, x1, y1 px) felé — alapból a saját rajza a doboz
        közepére céloz. amount: 0–1 (a betáplálás erőssége; a felépülés alatt lecseng)."""
        keep = self.target
        self.target = [((box[0] + box[2]) / 2) / self.W, ((box[1] + box[3]) / 2) / self.H]
        try:
            return self.draw(layer, core, casters, t, True, lights) * amount
        finally:
            self.target = keep

    def crackle(self, layer: np.ndarray, core: np.ndarray, pts: np.ndarray, t: float, lights: list[np.ndarray],
                amount: float) -> float:
        """Felfedés, 2. szakasz: szikrák a felfedett réteg kirajzolódó élén (pts px) — alapból izzó pontok; az effekt
        felülírhatja (a villám: rövid, szaggatott kisülések)."""
        if not len(pts):
            return 0.0
        rng = self.rng
        for p in pts[rng.choice(len(pts), size=min(len(pts), 60), replace=False)]:
            cv2.circle(layer, (int(p[0]) // 2, int(p[1]) // 2), max(1, int(self.h2 * 0.006)), self.GLOW.tolist(), -1, cv2.LINE_AA)
        lights.append(pts.mean(0) / 2)
        return amount

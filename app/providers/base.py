"""A szolgáltatói felület: create(model, input) → feladat, status(feladat), fetch_result(feladat) → saját tárhelyen lévő
fájlok, cancel(feladat); plusz egyenleg és bemenet-feltöltés. Egy új szolgáltató (fal, Replicate…) ezt valósítja meg."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# egységes állapotok (a szolgáltatóé a raw-ban marad)
PENDING, RUNNING, SUCCESS, FAIL = "pending", "running", "success", "fail"
TERMINAL = (SUCCESS, FAIL)


class ProviderError(Exception):
    """code: a szolgáltató hibakódja (402 = nincs kredit nála, 422 = rossz bemenet, 429 = túl sok kérés…)."""

    def __init__(self, msg: str, code: Optional[int] = None):
        super().__init__(msg)
        self.code = code


@dataclass
class TaskState:
    task_id: str
    state: str                                   # PENDING | RUNNING | SUCCESS | FAIL
    urls: list[str] = field(default_factory=list)   # az eredmény (a szolgáltatónál, korlátozott ideig)
    cost: Optional[float] = None                 # a TÉNYLEGES költség a szolgáltató egységében (kie: creditsConsumed)
    error: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)   # pl. lastFrameUrl
    raw: dict[str, Any] = field(default_factory=dict)


class Provider:
    name = "provider"

    def same_key(self) -> bool:                   # a kulcs változott-e (env) — akkor új példány kell
        return True

    async def create(self, model: str, input: dict[str, Any], *, callback_url: Optional[str] = None) -> str:
        raise NotImplementedError

    async def status(self, task_id: str) -> TaskState:
        raise NotImplementedError

    async def fetch_result(self, state: TaskState) -> list[str]:
        """Az eredményfájlok AZONNAL a saját tárhelyre (a szolgáltató csak napokig tartja) → file_id-k."""
        raise NotImplementedError

    async def cancel(self, task_id: str) -> bool:
        raise NotImplementedError

    async def balance(self) -> float:
        raise NotImplementedError

    async def upload(self, path: Any) -> str:
        """Egy helyi bemenet (kép, videó) → a szolgáltatónak elérhető URL."""
        raise NotImplementedError

"""A szolgáltatói felület: create(model, input) → feladat, status(feladat), fetch_result(feladat) → saját tárhelyen lévő
fájlok, cancel(feladat); plusz egyenleg és bemenet-feltöltés. Egy új szolgáltató (fal, Replicate…) ezt valósítja meg."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

# egységes állapotok (a szolgáltatóé a raw-ban marad)
PENDING, RUNNING, SUCCESS, FAIL = "pending", "running", "success", "fail"
RESULT_MAX_BYTES = 2 * 1024 ** 3
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
    has_balance = True                            # van-e egyenleg-lekérdezése (a napi egyeztetés csak ezeket nézi)

    def same_key(self) -> bool:                   # a kulcs változott-e (env) — akkor új példány kell
        return True

    async def create(self, model: str, input: dict[str, Any], *, callback_url: Optional[str] = None) -> str:
        raise NotImplementedError

    async def status(self, task_id: str) -> TaskState:
        raise NotImplementedError

    async def fetch_result(self, state: TaskState) -> list[str]:
        """Az eredményfájlok AZONNAL a saját tárhelyre (a szolgáltató csak napokig tartja) → file_id-k."""
        from app import net
        from app.storage import new_file_id, output_path
        out: list[str] = []
        for url in state.urls:
            ext = Path(urlparse(url).path).suffix.lstrip(".").lower() or "mp4"
            fid = new_file_id(ext if ext.isalnum() and len(ext) <= 5 else "mp4")
            try:
                await net.download(url, output_path(fid), RESULT_MAX_BYTES, timeout=600)
            except Exception:
                output_path(fid).unlink(missing_ok=True)
                raise
            out.append(fid)
        return out

    async def cancel(self, task_id: str) -> bool:
        raise NotImplementedError

    async def balance(self) -> float:
        raise NotImplementedError

    async def upload(self, path: Any) -> str:
        """Egy helyi bemenet (kép, videó) → a szolgáltatónak elérhető URL."""
        raise NotImplementedError

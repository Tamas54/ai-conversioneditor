"""Configuration — env vars + defaults."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()


class Settings(BaseModel):
    # --- Storage ---
    DATA_DIR: Path = Path(os.getenv("DATA_DIR", "/data"))
    FILE_TTL_HOURS: int = int(os.getenv("FILE_TTL_HOURS", "24"))   # aláírt letöltési link élettartama
    # Fájlok megőrzése a köteten (szerkesztő + verziótörténet: nem 24 óra, mint a linkeké)
    FILE_RETENTION_DAYS: int = int(os.getenv("FILE_RETENTION_DAYS", "30"))
    CLEANUP_INTERVAL_MIN: int = int(os.getenv("CLEANUP_INTERVAL_MIN", "60"))

    # --- Worker pool (pre-warm light: 2 LibreOffice + 1 Chromium) ---
    LO_WARM: int = int(os.getenv("LO_WARM", "2"))
    LO_MAX: int = int(os.getenv("LO_MAX", "8"))
    CHROMIUM_WARM: int = int(os.getenv("CHROMIUM_WARM", "1"))
    CHROMIUM_MAX: int = int(os.getenv("CHROMIUM_MAX", "4"))

    # --- LLM (SiliconFlow → Kimi K2.6) ---
    SILICONFLOW_API_KEY: str = os.getenv("SILICONFLOW_API_KEY", "")
    SILICONFLOW_BASE_URL: str = os.getenv(
        "SILICONFLOW_BASE_URL", "https://api.siliconflow.com/v1"
    )
    LLM_MODEL_PRIMARY: str = os.getenv("LLM_MODEL_PRIMARY", "moonshotai/Kimi-K2.6")
    LLM_MODEL_FALLBACK: str = os.getenv(
        "LLM_MODEL_FALLBACK", "deepseek-ai/DeepSeek-V4-Pro"
    )
    LLM_TIMEOUT_S: int = int(os.getenv("LLM_TIMEOUT_S", "120"))

    # --- Auth ---
    # Token-based file access (HMAC-signed, 24h TTL). Plus a static
    # MCP-side token so only authorized clients can hit the MCP endpoint.
    FILE_SIGNING_KEY: str = os.getenv("FILE_SIGNING_KEY", "change-me-in-railway")
    MCP_AUTH_TOKEN: str = os.getenv("MCP_AUTH_TOKEN", "")
    # Postgres: verziótörténet + lektorálási kérések (Railway: ${{Postgres.DATABASE_URL}}); üres = nélküle fut
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")
    # Dashboard-belépés (üres = nyitott dashboard, csak helyi fejlesztéshez); a süti aláíró kulcsa.
    DASHBOARD_PASSWORD: str = os.getenv("DASHBOARD_PASSWORD", "")
    SESSION_SECRET: str = os.getenv("SESSION_SECRET", os.getenv("FILE_SIGNING_KEY", "change-me-in-railway"))

    # --- WordCraft (Rust Word-motor, upstream release-ből; Dockerfile tölti le) ---
    # CLI: headless parancsok, gyors konverzió. WEB_DIR: a WASM-szerkesztő statikus fájljai.
    WORDCRAFT_CLI: str = os.getenv("WORDCRAFT_CLI", "/opt/wordcraft/bin/wordcraft-cli")
    WORDCRAFT_WEB_DIR: Path = Path(os.getenv("WORDCRAFT_WEB_DIR", "/opt/wordcraft-web"))
    WORDCRAFT_TIMEOUT_S: int = int(os.getenv("WORDCRAFT_TIMEOUT_S", "120"))
    # PhotoCraft (képek) és VectorCraft (dizájn) — ugyanígy kész release-ből (Dockerfile).
    PHOTOCRAFT_CLI: str = os.getenv("PHOTOCRAFT_CLI", "/opt/photocraft/bin/photocraft-cli")
    PHOTOCRAFT_WEB_DIR: Path = Path(os.getenv("PHOTOCRAFT_WEB_DIR", "/opt/photocraft-web"))
    VECTORCRAFT_CLI: str = os.getenv("VECTORCRAFT_CLI", "/opt/vectorcraft/bin/vectorcraft-cli")
    VECTORCRAFT_WEB_DIR: Path = Path(os.getenv("VECTORCRAFT_WEB_DIR", "/opt/vectorcraft-web"))
    # Háttéreltávolítás (rembg): ISNet — egy nehéz, ellenfényes fotón messze a legjobb (10-11 mérés).
    CUTOUT_MODEL_PATH: Path = Path(os.getenv("CUTOUT_MODEL_PATH", "/opt/models/isnet-general-use.onnx"))
    EDITOR_MAX_SAVE_MB: int = int(os.getenv("EDITOR_MAX_SAVE_MB", "100"))

    # --- Public base URL (Railway sets this) ---
    PUBLIC_BASE_URL: str = os.getenv(
        "PUBLIC_BASE_URL", os.getenv("RAILWAY_PUBLIC_DOMAIN", "http://localhost:8000")
    )


def _with_scheme(url: str) -> str:
    """RAILWAY_PUBLIC_DOMAIN csak hosztnév (séma nélkül) — enélkül a fájl-URL-ek
    relatívként oldódnak fel a böngészőben és az MCP-klienseknél."""
    if not url:
        return "http://localhost:8000"
    if "://" in url:
        return url
    local = url.startswith(("localhost", "127.0.0.1", "0.0.0.0"))
    return ("http://" if local else "https://") + url


settings = Settings()
settings.PUBLIC_BASE_URL = _with_scheme(settings.PUBLIC_BASE_URL)

# Ensure dirs exist
for sub in ("inputs", "outputs", "temp"):
    (settings.DATA_DIR / sub).mkdir(parents=True, exist_ok=True)

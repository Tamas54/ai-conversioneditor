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
    FILE_TTL_HOURS: int = int(os.getenv("FILE_TTL_HOURS", "24"))
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

    # --- Public base URL (Railway sets this) ---
    PUBLIC_BASE_URL: str = os.getenv(
        "PUBLIC_BASE_URL", os.getenv("RAILWAY_PUBLIC_DOMAIN", "http://localhost:8000")
    )


settings = Settings()

# Ensure dirs exist
for sub in ("inputs", "outputs", "temp"):
    (settings.DATA_DIR / sub).mkdir(parents=True, exist_ok=True)

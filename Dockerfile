# ai-conversioneditor MCP — Dockerfile
# Base: Playwright Python image (Chromium + Python + system deps already in)
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    PORT=8000

# --- System deps: LibreOffice, Pandoc, fonts, WeasyPrint deps ---
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice \
    libreoffice-writer \
    libreoffice-calc \
    libreoffice-impress \
    pandoc \
    fonts-liberation \
    fonts-dejavu \
    fonts-noto \
    fonts-noto-cjk \
    # WeasyPrint runtime deps:
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz0b \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    # Misc:
    poppler-utils \
    ghostscript \
    curl \
    tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- Python deps ---
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# --- App code ---
COPY app /app/app

# --- Data dir for Railway Volume mount ---
RUN mkdir -p /data/inputs /data/outputs /data/temp

EXPOSE 8000

# tini for proper PID 1 / signal handling (LibreOffice subprocess cleanup)
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.main"]

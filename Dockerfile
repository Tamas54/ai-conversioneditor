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
    # Cambria/Calibri metrikai megfelelői: nélkülük a LO DejaVu Serifet, a WordCraft Liberation
    # Serifet tesz a Word-dokumentumokba (325 vs 390 oldal ugyanarra; velük 342 vs 344)
    fonts-crosextra-caladea \
    fonts-crosextra-carlito \
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
    unzip \
    tini \
    && rm -rf /var/lib/apt/lists/*

# --- WordCraft (storytold/wordcraft, MIT OR Apache-2.0): Rust Word-motor, kész release ---
# CLI → /opt/wordcraft/bin (headless parancsok, gyors konverzió);
# WASM-szerkesztő → /opt/wordcraft-web (a dashboard ✏️ gombja). Verzióváltásnál a két
# SHA256-ot a release SHA256SUMS.txt-jéből kell átírni.
ARG WORDCRAFT_VERSION=0.5.0
ARG WORDCRAFT_CLI_SHA256=994bb376479ef6d76735cf4029e5ef3c2d52a85a4c9acde2b37c33b19c570203
ARG WORDCRAFT_WEB_SHA256=196d0070fec6572238298e211ac4a3ea0510b7a0ac8d6096a5ee59f2ff69f85d
RUN set -eux; \
    base="https://github.com/storytold/wordcraft/releases/download/v${WORDCRAFT_VERSION}"; \
    cd /tmp; \
    curl -fsSL -o wc.tar.gz "$base/wordcraft-${WORDCRAFT_VERSION}-linux-x86_64.tar.gz"; \
    echo "${WORDCRAFT_CLI_SHA256}  wc.tar.gz" | sha256sum -c -; \
    mkdir -p /opt/wordcraft; \
    tar xzf wc.tar.gz --strip-components=1 -C /opt/wordcraft \
        "wordcraft-${WORDCRAFT_VERSION}-linux-x86_64/bin/wordcraft-cli" \
        "wordcraft-${WORDCRAFT_VERSION}-linux-x86_64/share/doc/wordcraft"; \
    curl -fsSL -o web.zip "$base/wordcraft-web-${WORDCRAFT_VERSION}.zip"; \
    echo "${WORDCRAFT_WEB_SHA256}  web.zip" | sha256sum -c -; \
    unzip -q web.zip; \
    mv "wordcraft-web-${WORDCRAFT_VERSION}" /opt/wordcraft-web; \
    gzip -9 -k /opt/wordcraft-web/*.wasm /opt/wordcraft-web/*.js; \
    rm -f wc.tar.gz web.zip; \
    WORDCRAFT_NO_PREFS=1 /opt/wordcraft/bin/wordcraft-cli --version
ENV WORDCRAFT_CLI=/opt/wordcraft/bin/wordcraft-cli \
    WORDCRAFT_WEB_DIR=/opt/wordcraft-web

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

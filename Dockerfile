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
# A webes szerkesztő a SAJÁT buildünk (build/wordcraft-web: Caladea/Carlito befordítva, Discord-gomb
# nélkül) — release: github.com/Tamas54/ai-conversioneditor/releases/tag/wordcraft-web-0.5.0-aice1
ARG WORDCRAFT_WEB_TAG=wordcraft-web-0.5.0-aice1
ARG WORDCRAFT_WEB_SHA256=181c06e415e292b56e07e1ecdd241f31bfac979e56c1eabe7ff6c67151b1b005
RUN set -eux; \
    base="https://github.com/storytold/wordcraft/releases/download/v${WORDCRAFT_VERSION}"; \
    cd /tmp; \
    curl -fsSL -o wc.tar.gz "$base/wordcraft-${WORDCRAFT_VERSION}-linux-x86_64.tar.gz"; \
    echo "${WORDCRAFT_CLI_SHA256}  wc.tar.gz" | sha256sum -c -; \
    mkdir -p /opt/wordcraft; \
    tar xzf wc.tar.gz --strip-components=1 -C /opt/wordcraft \
        "wordcraft-${WORDCRAFT_VERSION}-linux-x86_64/bin/wordcraft-cli" \
        "wordcraft-${WORDCRAFT_VERSION}-linux-x86_64/share/doc/wordcraft"; \
    curl -fsSL -o web.zip "https://github.com/Tamas54/ai-conversioneditor/releases/download/${WORDCRAFT_WEB_TAG}/${WORDCRAFT_WEB_TAG}.zip"; \
    echo "${WORDCRAFT_WEB_SHA256}  web.zip" | sha256sum -c -; \
    unzip -q web.zip; \
    mv "${WORDCRAFT_WEB_TAG}" /opt/wordcraft-web; \
    gzip -9 -k /opt/wordcraft-web/*.wasm /opt/wordcraft-web/*.js; \
    rm -f wc.tar.gz web.zip; \
    WORDCRAFT_NO_PREFS=1 /opt/wordcraft/bin/wordcraft-cli --version
ENV WORDCRAFT_CLI=/opt/wordcraft/bin/wordcraft-cli \
    WORDCRAFT_WEB_DIR=/opt/wordcraft-web

# --- PhotoCraft (képek) + VectorCraft (dizájn/layout): ugyanaz a minta, kész release, SHA256 ---
# + ISNet háttéreltávolító modell (rembg release, onnxruntime-mal fut: app/tools/cutout.py)
ARG PHOTOCRAFT_VERSION=0.7.0
ARG PHOTOCRAFT_CLI_SHA256=641f831252fd31b43ee1a12e572d7442e5652c2e5bf623de085741c370f0c91d
ARG PHOTOCRAFT_WEB_SHA256=dfcbaac00f81f5d05ccffba36a14b320c1b1c7b23a4135fe54877f527e550eec
ARG VECTORCRAFT_VERSION=0.9.0
ARG VECTORCRAFT_CLI_SHA256=afbc4a0904740dfd1208e717664eb294965055e74c227c2ac3dc53af502e46a4
ARG VECTORCRAFT_WEB_SHA256=4ebe8829a22fa17975c96765dfe9812bd2834e3e721381b4b14abb802c4d7dc1
# Office-motorok (ugyanaz a release-minta): DeckCraft · PdfCraft · DesignCraft · GridCraft
ARG DECKCRAFT_VERSION=0.5.0
ARG DECKCRAFT_CLI_SHA256=c2648c6071f631ecc6639ce6b7d60f68eed83ddeaba6b1559b07c5046863117c
ARG DECKCRAFT_WEB_SHA256=49d6aa4871dbfe4e2dd4ebc367db8fbfe6df6db6ba49872e10058e13175c5f51
ARG PDFCRAFT_VERSION=0.6.0
ARG PDFCRAFT_CLI_SHA256=9a136e71e37b459ed5746dbe19a364ce84cf5d995c6f6c98af6147d818390a53
ARG PDFCRAFT_WEB_SHA256=a957a75e35390fa8747ae97464b7a9f178cce61edda22b3b98764462060d15a5
ARG DESIGNCRAFT_VERSION=0.6.0
ARG DESIGNCRAFT_CLI_SHA256=abb661cd2fc53677d040fe0be3f241bbb8c0fad9619192a83109d53bd514bfb9
ARG DESIGNCRAFT_WEB_SHA256=b43ebefc5b7900c8223953d632951308a347935adc6fad3ff00e4a7a1d050d63
ARG GRIDCRAFT_VERSION=0.5.0
ARG GRIDCRAFT_CLI_SHA256=e6505c2f929f9854c350a0df26189c4e56c8adf87cc61625f80d6a8eec2b628d
ARG GRIDCRAFT_WEB_SHA256=fbd9dbc48694676812c7bbca7f5f2e776cbc52c37d8f425a2dc033b5d23ce606
ARG ISNET_SHA256=60920e99c45464f2ba57bee2ad08c919a52bbf852739e96947fbb4358c0d964a
RUN set -eux; cd /tmp; \
    for spec in "photocraft ${PHOTOCRAFT_VERSION} ${PHOTOCRAFT_CLI_SHA256} ${PHOTOCRAFT_WEB_SHA256}" \
                "vectorcraft ${VECTORCRAFT_VERSION} ${VECTORCRAFT_CLI_SHA256} ${VECTORCRAFT_WEB_SHA256}" \
                "deckcraft ${DECKCRAFT_VERSION} ${DECKCRAFT_CLI_SHA256} ${DECKCRAFT_WEB_SHA256}" \
                "pdfcraft ${PDFCRAFT_VERSION} ${PDFCRAFT_CLI_SHA256} ${PDFCRAFT_WEB_SHA256}" \
                "designcraft ${DESIGNCRAFT_VERSION} ${DESIGNCRAFT_CLI_SHA256} ${DESIGNCRAFT_WEB_SHA256}" \
                "gridcraft ${GRIDCRAFT_VERSION} ${GRIDCRAFT_CLI_SHA256} ${GRIDCRAFT_WEB_SHA256}"; do \
        set -- $spec; app=$1; ver=$2; \
        base="https://github.com/storytold/${app}/releases/download/v${ver}"; \
        curl -fsSL -o cli.tar.gz "$base/${app}-${ver}-linux-x86_64.tar.gz"; \
        echo "$3  cli.tar.gz" | sha256sum -c -; \
        mkdir -p /opt/${app}; \
        tar xzf cli.tar.gz --strip-components=1 -C /opt/${app} \
            "${app}-${ver}-linux-x86_64/bin/${app}-cli" "${app}-${ver}-linux-x86_64/share/doc/${app}"; \
        curl -fsSL -o web.zip "$base/${app}-web-${ver}.zip"; \
        echo "$4  web.zip" | sha256sum -c -; \
        unzip -q web.zip; mv "${app}-web-${ver}" /opt/${app}-web; \
        gzip -9 -k /opt/${app}-web/*.wasm /opt/${app}-web/*.js; \
        rm -f cli.tar.gz web.zip; \
        /opt/${app}/bin/${app}-cli --version >/dev/null; \
    done; \
    mkdir -p /opt/models; \
    curl -fsSL -o /opt/models/isnet-general-use.onnx \
        https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx; \
    echo "${ISNET_SHA256}  /opt/models/isnet-general-use.onnx" | sha256sum -c -
ENV PHOTOCRAFT_CLI=/opt/photocraft/bin/photocraft-cli \
    PHOTOCRAFT_WEB_DIR=/opt/photocraft-web \
    VECTORCRAFT_CLI=/opt/vectorcraft/bin/vectorcraft-cli \
    VECTORCRAFT_WEB_DIR=/opt/vectorcraft-web \
    DECKCRAFT_CLI=/opt/deckcraft/bin/deckcraft-cli DECKCRAFT_WEB_DIR=/opt/deckcraft-web \
    PDFCRAFT_CLI=/opt/pdfcraft/bin/pdfcraft-cli PDFCRAFT_WEB_DIR=/opt/pdfcraft-web \
    DESIGNCRAFT_CLI=/opt/designcraft/bin/designcraft-cli DESIGNCRAFT_WEB_DIR=/opt/designcraft-web \
    GRIDCRAFT_CLI=/opt/gridcraft/bin/gridcraft-cli GRIDCRAFT_WEB_DIR=/opt/gridcraft-web \
    CUTOUT_MODEL_PATH=/opt/models/isnet-general-use.onnx

WORKDIR /app

# --- Python deps ---
COPY requirements.txt constraints.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt -c /app/constraints.txt

# --- App code ---
COPY app /app/app

# --- Data dir for Railway Volume mount ---
RUN mkdir -p /data/inputs /data/outputs /data/temp

EXPOSE 8000

# tini for proper PID 1 / signal handling (LibreOffice subprocess cleanup)
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.main"]

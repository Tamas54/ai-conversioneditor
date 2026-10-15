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
    # az automata tervező címbetűi (design_auto.FONT_PAIRS: condensed / heavy / playful) — OFL
    fonts-bebas-neue \
    fonts-lato \
    fonts-lobster \
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

# --- Konverziós motorok (bővítés 10-15, mind nyílt forrású; app/convert/engines.py futásidőben ellenőrzi őket) ---
# videó/hang: ffmpeg · képek: ImageMagick, libvips, libheif, LibRaw (dcraw_emu) · vektor: Inkscape, librsvg, Potrace
# e-könyv: Calibre · archívum: 7-Zip, The Unarchiver, libarchive, zstd, lzip, lzop · PDF/DjVu/XPS: MuPDF, QPDF, DjVuLibre
# OCR-réteg: Tesseract (+ nyelvek; az ocrmypdf a pip-ből) · metaadat: ExifTool · betűk: FontForge
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    imagemagick \
    libvips-tools \
    libheif-examples \
    libraw-bin \
    inkscape \
    librsvg2-bin \
    potrace \
    calibre \
    p7zip-full \
    unar \
    libarchive-tools \
    zstd \
    lzip \
    lzop \
    djvulibre-bin \
    mupdf-tools \
    qpdf \
    tesseract-ocr \
    tesseract-ocr-eng tesseract-ocr-hun tesseract-ocr-deu tesseract-ocr-fra tesseract-ocr-spa tesseract-ocr-ita \
    tesseract-ocr-pol tesseract-ocr-ces tesseract-ocr-slk tesseract-ocr-ron tesseract-ocr-nld tesseract-ocr-por \
    unpaper \
    pngquant \
    libimage-exiftool-perl \
    fontforge \
    libreoffice-draw \
    && rm -rf /var/lib/apt/lists/*

# LibreDWG (GPLv3, GNU): DWG ⇄ DXF (dwg2dxf, dxf2dwg) — a jammy-ben nincs csomag, ezért forrásból, SHA256-tal
ARG LIBREDWG_VERSION=0.13.3
ARG LIBREDWG_SHA256=83f1f6e78a744777a481ff4520e4cef3f8ac4b2c1c25671077ca12fe81e8816e
RUN set -eux; cd /tmp; \
    apt-get update; apt-get install -y --no-install-recommends build-essential; \
    curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 -o libredwg.tar.xz \
        "https://ftp.gnu.org/gnu/libredwg/libredwg-${LIBREDWG_VERSION}.tar.xz"; \
    echo "${LIBREDWG_SHA256}  libredwg.tar.xz" | sha256sum -c -; \
    tar xJf libredwg.tar.xz; cd "libredwg-${LIBREDWG_VERSION}"; \
    ./configure --disable-bindings --disable-python --disable-docs --disable-dependency-tracking --prefix=/usr/local; \
    make -j"$(nproc)"; make install; ldconfig; \
    cd /tmp; rm -rf libredwg*; \
    apt-get purge -y --auto-remove build-essential; rm -rf /var/lib/apt/lists/*; \
    dwg2dxf --version >/dev/null

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
    curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 -o wc.tar.gz "$base/wordcraft-${WORDCRAFT_VERSION}-linux-x86_64.tar.gz"; \
    echo "${WORDCRAFT_CLI_SHA256}  wc.tar.gz" | sha256sum -c -; \
    mkdir -p /opt/wordcraft; \
    tar xzf wc.tar.gz --strip-components=1 -C /opt/wordcraft \
        "wordcraft-${WORDCRAFT_VERSION}-linux-x86_64/bin/wordcraft-cli" \
        "wordcraft-${WORDCRAFT_VERSION}-linux-x86_64/share/doc/wordcraft"; \
    curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 -o web.zip "https://github.com/Tamas54/ai-conversioneditor/releases/download/${WORDCRAFT_WEB_TAG}/${WORDCRAFT_WEB_TAG}.zip"; \
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
# A CLI-k az upstream release-ek; a webes szerkesztők a SAJÁT buildjeink (build/craft-web: közösségi/
# idegen linkek nélkül, DeckCraft/GridCraft Calibri/Cambria-kompatibilis fontokkal) — release-ek:
# github.com/Tamas54/ai-conversioneditor/releases/tag/<app>-web-<verzió>-aiceN
ARG PHOTOCRAFT_VERSION=0.7.0
ARG PHOTOCRAFT_CLI_SHA256=641f831252fd31b43ee1a12e572d7442e5652c2e5bf623de085741c370f0c91d
ARG PHOTOCRAFT_WEB_TAG=photocraft-web-0.7.0-aice1
ARG PHOTOCRAFT_WEB_SHA256=30c6e897ca59d4a6a4e1fbc3a5d25bcf32b5c24a7e04c2b1d6210d974d1633c3
ARG VECTORCRAFT_VERSION=0.9.0
ARG VECTORCRAFT_CLI_SHA256=afbc4a0904740dfd1208e717664eb294965055e74c227c2ac3dc53af502e46a4
ARG VECTORCRAFT_WEB_TAG=vectorcraft-web-0.9.0-aice1
ARG VECTORCRAFT_WEB_SHA256=bb002f83cdf5db6d09c8cc292c1eb9904457dbe1de45d6bf5f1b27ff968fdf1d
# Office-motorok (ugyanaz a release-minta): DeckCraft · PdfCraft · DesignCraft · GridCraft
ARG DECKCRAFT_VERSION=0.5.0
ARG DECKCRAFT_CLI_SHA256=c2648c6071f631ecc6639ce6b7d60f68eed83ddeaba6b1559b07c5046863117c
ARG DECKCRAFT_WEB_TAG=deckcraft-web-0.5.0-aice1
ARG DECKCRAFT_WEB_SHA256=c12037eda3d6f18e61776b9c6cf04f31e23563ffa59197a0a10df85a092cc911
ARG PDFCRAFT_VERSION=0.6.0
ARG PDFCRAFT_CLI_SHA256=9a136e71e37b459ed5746dbe19a364ce84cf5d995c6f6c98af6147d818390a53
ARG PDFCRAFT_WEB_TAG=pdfcraft-web-0.6.0-aice1
ARG PDFCRAFT_WEB_SHA256=bd51065418b5bd3218f1435b9155b2b4da13f0cbad227a17ba412bb1a7953022
ARG DESIGNCRAFT_VERSION=0.6.0
ARG DESIGNCRAFT_CLI_SHA256=abb661cd2fc53677d040fe0be3f241bbb8c0fad9619192a83109d53bd514bfb9
ARG DESIGNCRAFT_WEB_TAG=designcraft-web-0.6.0-aice1
ARG DESIGNCRAFT_WEB_SHA256=40e57c9a5dba6ccb9beefe420448e31fea47cff768b6695994d3fbcc00209d6f
ARG GRIDCRAFT_VERSION=0.5.0
ARG GRIDCRAFT_CLI_SHA256=e6505c2f929f9854c350a0df26189c4e56c8adf87cc61625f80d6a8eec2b628d
ARG GRIDCRAFT_WEB_TAG=gridcraft-web-0.5.0-aice1
ARG GRIDCRAFT_WEB_SHA256=7e73f8e8d39021fcb48a086c24c3b077c6a83dbf980275d5b5a44290c1e68f76
ARG ISNET_SHA256=60920e99c45464f2ba57bee2ad08c919a52bbf852739e96947fbb4358c0d964a
# emberszegmentálás a kivágáshoz: MINDEN ember maradjon, egészben (az ISNet csak a fő alanyt tartja — 10-12)
ARG HUMANSEG_SHA256=01eb6a29a5c4d8edb30b56adad9bb3a2a0535338e480724a213e0acfd2d1c73c
# az alany kivágása: BiRefNet-lite (MIT) — az ISNet a háttér világos tárgyait ködként a fej mellé tette (10-12)
ARG BIREFNET_SHA256=5600024376f572a557870a5eb0afb1e5961636bef4e1e22132025467d0f03333
RUN set -eux; cd /tmp; \
    for spec in "photocraft ${PHOTOCRAFT_VERSION} ${PHOTOCRAFT_CLI_SHA256} ${PHOTOCRAFT_WEB_TAG} ${PHOTOCRAFT_WEB_SHA256}" \
                "vectorcraft ${VECTORCRAFT_VERSION} ${VECTORCRAFT_CLI_SHA256} ${VECTORCRAFT_WEB_TAG} ${VECTORCRAFT_WEB_SHA256}" \
                "deckcraft ${DECKCRAFT_VERSION} ${DECKCRAFT_CLI_SHA256} ${DECKCRAFT_WEB_TAG} ${DECKCRAFT_WEB_SHA256}" \
                "pdfcraft ${PDFCRAFT_VERSION} ${PDFCRAFT_CLI_SHA256} ${PDFCRAFT_WEB_TAG} ${PDFCRAFT_WEB_SHA256}" \
                "designcraft ${DESIGNCRAFT_VERSION} ${DESIGNCRAFT_CLI_SHA256} ${DESIGNCRAFT_WEB_TAG} ${DESIGNCRAFT_WEB_SHA256}" \
                "gridcraft ${GRIDCRAFT_VERSION} ${GRIDCRAFT_CLI_SHA256} ${GRIDCRAFT_WEB_TAG} ${GRIDCRAFT_WEB_SHA256}"; do \
        set -- $spec; app=$1; ver=$2; tag=$4; \
        base="https://github.com/storytold/${app}/releases/download/v${ver}"; \
        curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 -o cli.tar.gz "$base/${app}-${ver}-linux-x86_64.tar.gz"; \
        echo "$3  cli.tar.gz" | sha256sum -c -; \
        mkdir -p /opt/${app}; \
        tar xzf cli.tar.gz --strip-components=1 -C /opt/${app} \
            "${app}-${ver}-linux-x86_64/bin/${app}-cli" "${app}-${ver}-linux-x86_64/share/doc/${app}"; \
        curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 -o web.zip "https://github.com/Tamas54/ai-conversioneditor/releases/download/${tag}/${tag}.zip"; \
        echo "$5  web.zip" | sha256sum -c -; \
        unzip -q web.zip; mv "${tag}" /opt/${app}-web; \
        gzip -9 -k /opt/${app}-web/*.wasm /opt/${app}-web/*.js; \
        rm -f cli.tar.gz web.zip; \
        /opt/${app}/bin/${app}-cli --version >/dev/null; \
    done; \
    mkdir -p /opt/models; \
    curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 -o /opt/models/isnet-general-use.onnx \
        https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx; \
    echo "${ISNET_SHA256}  /opt/models/isnet-general-use.onnx" | sha256sum -c -; \
    curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 -o /opt/models/u2net_human_seg.onnx \
        https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net_human_seg.onnx; \
    echo "${HUMANSEG_SHA256}  /opt/models/u2net_human_seg.onnx" | sha256sum -c -; \
    curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 -o /opt/models/birefnet-general-lite.onnx \
        https://github.com/danielgatis/rembg/releases/download/v0.0.0/BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx; \
    echo "${BIREFNET_SHA256}  /opt/models/birefnet-general-lite.onnx" | sha256sum -c -
# Videó (storytold, Apache-2.0): EffectCraft (After Effects-szerű: .ecproj, MP4-render — az automata videó motorja)
# + FilmCraft (Premiere-szerű vágó a kész filmekhez). A CLI a release linux tarjából, SHA256-tal.
# A web zip a SAJÁT, közösségi linkek nélküli build (build/craft-web, workflow: craft-web, app=effectcraft|filmcraft,
#   suffix=aice1 — 10-12 kiadva). Verzióváltás: új tag (-aice2), és a három ARG: *_WEB_TAG, *_WEB_URL, *_WEB_SHA256.
ARG EFFECTCRAFT_VERSION=0.8.0
ARG EFFECTCRAFT_CLI_SHA256=725c88cf379122d70f941dd3edacb54479d63ff1e2a00fae318eaee02ad762c4
ARG EFFECTCRAFT_WEB_TAG=effectcraft-web-0.8.0-aice1
ARG EFFECTCRAFT_WEB_URL=https://github.com/Tamas54/ai-conversioneditor/releases/download/effectcraft-web-0.8.0-aice1/effectcraft-web-0.8.0-aice1.zip
ARG EFFECTCRAFT_WEB_SHA256=ad3ef721eb8c8b6f33302806369436b688e326dcc2f575372e897c0d31a3ca85
ARG FILMCRAFT_VERSION=0.6.0
ARG FILMCRAFT_CLI_SHA256=b3d706d599699ead76b46a29d5697287b9f75a0bc9eb40f7733db7657dae8204
ARG FILMCRAFT_WEB_TAG=filmcraft-web-0.6.0-aice1
ARG FILMCRAFT_WEB_URL=https://github.com/Tamas54/ai-conversioneditor/releases/download/filmcraft-web-0.6.0-aice1/filmcraft-web-0.6.0-aice1.zip
ARG FILMCRAFT_WEB_SHA256=621c8d7da0d8c2c7050228540d97469aa8a01dc4fb1bb03675df86c5a3c658ac
RUN set -eux; cd /tmp; \
    for spec in "effectcraft ${EFFECTCRAFT_VERSION} ${EFFECTCRAFT_CLI_SHA256} ${EFFECTCRAFT_WEB_TAG} ${EFFECTCRAFT_WEB_URL} ${EFFECTCRAFT_WEB_SHA256}" \
                "filmcraft ${FILMCRAFT_VERSION} ${FILMCRAFT_CLI_SHA256} ${FILMCRAFT_WEB_TAG} ${FILMCRAFT_WEB_URL} ${FILMCRAFT_WEB_SHA256}"; do \
        set -- $spec; app=$1; ver=$2; tag=$4; \
        curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 -o cli.tar.gz "https://github.com/storytold/${app}/releases/download/v${ver}/${app}-${ver}-linux-x86_64.tar.gz"; \
        echo "$3  cli.tar.gz" | sha256sum -c -; \
        mkdir -p /opt/${app}; \
        tar xzf cli.tar.gz --strip-components=1 -C /opt/${app} \
            "${app}-${ver}-linux-x86_64/bin/${app}-cli" "${app}-${ver}-linux-x86_64/share/doc/${app}"; \
        curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 -o web.zip "$5"; \
        echo "$6  web.zip" | sha256sum -c -; \
        unzip -q web.zip; mv "${tag}" /opt/${app}-web; \
        gzip -9 -k /opt/${app}-web/*.wasm /opt/${app}-web/*.js; \
        rm -f cli.tar.gz web.zip; \
        /opt/${app}/bin/${app}-cli --version >/dev/null; \
    done
ENV EFFECTCRAFT_CLI=/opt/effectcraft/bin/effectcraft-cli EFFECTCRAFT_WEB_DIR=/opt/effectcraft-web \
    FILMCRAFT_CLI=/opt/filmcraft/bin/filmcraft-cli FILMCRAFT_WEB_DIR=/opt/filmcraft-web
ENV PHOTOCRAFT_CLI=/opt/photocraft/bin/photocraft-cli \
    PHOTOCRAFT_WEB_DIR=/opt/photocraft-web \
    VECTORCRAFT_CLI=/opt/vectorcraft/bin/vectorcraft-cli \
    VECTORCRAFT_WEB_DIR=/opt/vectorcraft-web \
    DECKCRAFT_CLI=/opt/deckcraft/bin/deckcraft-cli DECKCRAFT_WEB_DIR=/opt/deckcraft-web \
    PDFCRAFT_CLI=/opt/pdfcraft/bin/pdfcraft-cli PDFCRAFT_WEB_DIR=/opt/pdfcraft-web \
    DESIGNCRAFT_CLI=/opt/designcraft/bin/designcraft-cli DESIGNCRAFT_WEB_DIR=/opt/designcraft-web \
    GRIDCRAFT_CLI=/opt/gridcraft/bin/gridcraft-cli GRIDCRAFT_WEB_DIR=/opt/gridcraft-web \
    CUTOUT_MODEL_PATH=/opt/models/isnet-general-use.onnx \
    HUMAN_SEG_MODEL_PATH=/opt/models/u2net_human_seg.onnx \
    BIREFNET_MODEL_PATH=/opt/models/birefnet-general-lite.onnx

WORKDIR /app

# --- Python deps ---
COPY requirements.txt constraints.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt -c /app/constraints.txt
# A pip a rendszer lxml-jét (python3-lxml) letörli, miközben a dpkg telepítettnek hiszi; a Calibre html5-parsere
# viszont a rendszer libxml2-jéhez kötött → újratelepítjük. Az app a /usr/local pip-lxml-jét használja (az van elöl a
# sys.path-ban), a Calibre a rendszerét (engines._env_for: PYTHONPATH=/usr/lib/python3/dist-packages).
RUN apt-get update && apt-get install -y --reinstall --no-install-recommends python3-lxml \
    && rm -rf /var/lib/apt/lists/* \
    && PYTHONPATH=/usr/lib/python3/dist-packages python3 -c "import html5_parser, lxml; assert lxml.__file__.startswith('/usr/lib')"

# --- App code ---
COPY app /app/app

# --- Data dir for Railway Volume mount ---
RUN mkdir -p /data/inputs /data/outputs /data/temp

EXPOSE 8000

# tini for proper PID 1 / signal handling (LibreOffice subprocess cleanup)
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.main"]

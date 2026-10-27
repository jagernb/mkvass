FROM golang:1.23-bookworm AS mkvtool-builder

ARG MKVAUTOSUBSET_REPO=https://github.com/SAGIRIxr/MkvAutoSubset.git
ARG MKVAUTOSUBSET_REF=next

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        pkg-config \
        libass-dev \
        libexpat1-dev \
        libfontconfig1-dev \
        libfreetype-dev \
        libfribidi-dev \
        libharfbuzz-dev \
        libpng-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/* \
    && git clone --depth 1 --branch "${MKVAUTOSUBSET_REF}" "${MKVAUTOSUBSET_REPO}" /src/MkvAutoSubset

WORKDIR /src/MkvAutoSubset/mkvtool

RUN CGO_ENABLED=1 \
    CGO_CFLAGS="-O3 -DHB_EXPERIMENTAL_API" \
    CGO_LDFLAGS="-lass -lfreetype -lm -lz -lfontconfig -lpng -lfribidi -lharfbuzz -lharfbuzz-subset -lexpat" \
    go build -mod=mod -ldflags "-s -w" -o /usr/local/bin/mkvtool

FROM python:3.12-slim

ARG APP_VERSION=1.1.7
ARG BUILD_DATE=2026-04-25T00:00:00Z
ARG MKVAUTOSUBSET_REPO=https://github.com/SAGIRIxr/MkvAutoSubset.git
ARG MKVAUTOSUBSET_REF=next

LABEL org.opencontainers.image.title="mkvass" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.description="mkvass with MkvAutoSubset ASS-to-PGS renderer"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        mkvtoolnix \
        fonts-dejavu-core \
        libass9 \
        libexpat1 \
        libfontconfig1 \
        libfreetype6 \
        libfribidi0 \
        libharfbuzz0b \
        libharfbuzz-subset0 \
        libpng16-16 \
        zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY --from=mkvtool-builder /usr/local/bin/mkvtool /usr/local/bin/mkvtool

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV APP_VERSION=${APP_VERSION} \
    BUILD_DATE=${BUILD_DATE} \
    MEDIA_DIR=/media \
    HOST=0.0.0.0 \
    PORT=8080 \
    DEFAULT_OUTPUT_DIR= \
    PGS_CONVERTER_CMD=mkvtool \
    MKVMERGE_CMD=mkvmerge \
    PGS_FONT_DIR=/usr/share/fonts/truetype/dejavu \
    PGS_FRAMERATE=23.976 \
    PGS_RESOLUTION=1920*1080 \
    MKVAUTOSUBSET_REPO=${MKVAUTOSUBSET_REPO} \
    MKVAUTOSUBSET_REF=${MKVAUTOSUBSET_REF}

EXPOSE 8080

CMD ["python", "-m", "app.server"]

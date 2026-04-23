FROM python:3.12-slim

ARG MKVTOOL_VERSION=v5.6.4
ARG TARGETARCH

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ffmpeg mkvtoolnix fonts-dejavu-core \
    && case "$TARGETARCH" in \
        amd64) MKVTOOL_ASSET="mkvtool-linux-amd64" ;; \
        arm64) MKVTOOL_ASSET="mkvtool-linux-arm64" ;; \
        *) echo "Unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /usr/local/bin/mkvtool "https://github.com/MkvAutoSubset/MkvAutoSubset/releases/download/${MKVTOOL_VERSION}/${MKVTOOL_ASSET}" \
    && chmod +x /usr/local/bin/mkvtool \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV MEDIA_DIR=/media \
    HOST=0.0.0.0 \
    PORT=8080 \
    DEFAULT_OUTPUT_DIR= \
    PGS_CONVERTER_CMD=mkvtool \
    PGS_FONT_DIR=/usr/share/fonts/truetype/dejavu \
    PGS_FRAMERATE=23.976 \
    PGS_RESOLUTION=1920*1080

EXPOSE 8080

CMD ["python", "-m", "app.server"]

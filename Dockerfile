FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV MEDIA_DIR=/media \
    HOST=0.0.0.0 \
    PORT=8080

EXPOSE 8080

CMD ["python", "-m", "app.server"]

FROM rclone/rclone:1.68.2 AS rclone
FROM mwader/static-ffmpeg:7.1.1 AS ffmpeg

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Minimal certs & tzdata; copy static rclone and ffmpeg/ffprobe binaries (used with yt-dlp for HLS remuxing)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg /ffprobe /usr/local/bin/ffprobe

WORKDIR /app

# Cache third-party Python dependencies in their own Docker layer
COPY pyproject.toml /app/pyproject.toml
RUN mkdir -p /app/app && touch /app/app/__init__.py \
    && pip install --upgrade pip \
    && pip install . \
    && rm -rf /app/app

COPY app /app/app
RUN pip install --no-deps .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,::1,fc00::/7"]

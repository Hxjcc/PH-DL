FROM rclone/rclone:1.76.0 AS rclone
FROM mwader/static-ffmpeg:9.1 AS ffmpeg

FROM python:3.12-slim

# Keep MALLOC_ARENA_MAX=2 to cap glibc arena RSS across worker threads, but do NOT
# set MALLOC_TRIM_THRESHOLD_ (which disables glibc's dynamic mmap_threshold and pins it
# at 128 KiB, causing massive mmap/munmap page-fault churn on 256-512 KiB TLS/HTTP buffers).
# Idle heap memory is reclaimed after jobs via malloc_trim(0) in app/engine/memory.py.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MALLOC_ARENA_MAX=2

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

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-access-log"]

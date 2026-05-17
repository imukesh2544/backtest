# ─── Stage 1: Build dependencies ─────────────────────────────
FROM python:3.11-slim AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libssl-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# ─── Stage 2: Runtime image ───────────────────────────────────
FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    libssl3 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /root/.local /root/.local
COPY app.py .
COPY stocks.json .
COPY templates/ templates/
RUN mkdir -p /data/cache

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/root/.local/bin:$PATH" \
    PORT=5000 \
    CACHE_DIR=/data/cache \
    STOCKS_FILE=/app/stocks.json \
    CACHE_TTL_HOURS=6 \
    LOG_LEVEL=INFO \
    WORKERS=4 \
    THREADS=4

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/health')" || exit 1

# gthread worker: each worker has THREADS threads, supports streaming SSE
CMD gunicorn \
    --bind 0.0.0.0:$PORT \
    --workers $WORKERS \
    --worker-class gthread \
    --threads $THREADS \
    --timeout 600 \
    --keep-alive 5 \
    --log-level info \
    --access-logfile - \
    --error-logfile - \
    app:app

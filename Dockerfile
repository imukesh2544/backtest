# ─── Stage 1: Build dependencies ─────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# ─── Stage 2: Runtime image ───────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /root/.local /root/.local

# Copy application files
COPY app.py .
COPY stocks.json .

# Create cache directory
RUN mkdir -p /data/cache

# Runtime env vars (overridable via K8s ConfigMap / env)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/root/.local/bin:$PATH" \
    PORT=5000 \
    CACHE_DIR=/data/cache \
    STOCKS_FILE=/app/stocks.json \
    CACHE_TTL_HOURS=6 \
    LOG_LEVEL=INFO \
    WORKERS=2

EXPOSE 5000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/health')" || exit 1

# Run with gunicorn (production WSGI server)
CMD gunicorn \
    --bind 0.0.0.0:$PORT \
    --workers $WORKERS \
    --worker-class sync \
    --timeout 120 \
    --keep-alive 5 \
    --log-level info \
    --access-logfile - \
    --error-logfile - \
    app:app

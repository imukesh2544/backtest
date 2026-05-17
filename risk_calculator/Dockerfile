# ── Stage 1: Build ───────────────────────────────────────────
FROM python:3.11-slim AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# ── Stage 2: Runtime ─────────────────────────────────────────
FROM python:3.11-slim
WORKDIR /app
COPY --from=builder /root/.local /root/.local
COPY app/        ./app/
COPY templates/  ./templates/
COPY static/     ./static/

RUN mkdir -p /app/exports

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/root/.local/bin:$PATH" \
    PORT=6000 \
    WORKERS=2 \
    THREADS=4 \
    LOG_LEVEL=INFO \
    APP_PREFIX=/risk-calculator \
    EXPORT_DIR=/app/exports

EXPOSE 6000

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:6000/health')" || exit 1

CMD gunicorn \
    --bind 0.0.0.0:$PORT \
    --workers $WORKERS \
    --worker-class gthread \
    --threads $THREADS \
    --timeout 120 \
    --keep-alive 5 \
    --log-level info \
    --access-logfile - \
    --error-logfile - \
    app.main:app

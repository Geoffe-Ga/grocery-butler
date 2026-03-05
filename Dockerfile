# --- Builder stage ---
FROM python:3.13-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# --- Runtime stage ---
FROM python:3.13-slim

# Non-root user for security
RUN groupadd --system butler && useradd --system --gid butler butler

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
WORKDIR /app
COPY grocery_butler/ grocery_butler/
COPY Procfile .

# Own the workdir
RUN chown -R butler:butler /app

USER butler

ENV PORT=8000
EXPOSE $PORT

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/health')"

CMD ["gunicorn", "grocery_butler.app:create_app()", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "120"]

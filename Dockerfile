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
COPY --chmod=0755 docker-entrypoint.sh /app/docker-entrypoint.sh

# Own the workdir
RUN chown -R butler:butler /app

USER butler

# Issue #64: arms create_app()'s production fail-fast startup checks
# (missing FLASK_SECRET_KEY / RUBOTPAUL_SHARED_SECRET refuse to boot).
ENV APP_ENV=production
ENV PORT=8000
EXPOSE $PORT

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/health')"

# The entrypoint runs pending DB migrations, then execs the CMD below
# (issue #58) -- Railway has no Heroku-style `release` phase, so the
# migration step must happen inside the container's own boot sequence.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD gunicorn 'grocery_butler.app:create_app()' --bind "0.0.0.0:${PORT}" --workers ${WEB_CONCURRENCY:-2} --timeout 120

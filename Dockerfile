# ---- Conjunction Tracker: API + scheduler image ----
# Single image runs the whole service (scheduled fetcher + REST API) in one
# container, as required by the assignment.

FROM python:3.11-slim AS base

# Faster, quieter, reproducible Python in containers.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so this layer is cached across code changes.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Application code + default mission config.
COPY app ./app
COPY config ./config

# Persisted SQLite database lives here (mounted as a volume in compose).
RUN mkdir -p /data

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app /data
USER appuser

# Operational defaults (override via -e / env_file / compose).
ENV DATABASE_PATH=/data/conjunctions.db \
    SATELLITE_CONFIG_PATH=/app/config/satellites.yaml \
    API_HOST=0.0.0.0 \
    API_PORT=8000

EXPOSE 8000

# Container-native healthcheck hits the service's own /health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else sys.exit(1)"

CMD ["python", "-m", "app.main"]

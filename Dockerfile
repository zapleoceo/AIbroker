FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install deps first so they cache in their own layer independent of src edits.
# This list MIRRORS [project.dependencies] in pyproject.toml — keep them in
# sync when adding/removing a dep. (Kept explicit, rather than `pip install .`,
# purely for that layer-cache: a src-only change must not trigger a full dep
# reinstall on every deploy.)
COPY pyproject.toml ./
RUN pip install --no-cache-dir hatchling \
    && pip install --no-cache-dir \
        "fastapi>=0.115" "uvicorn[standard]>=0.32" \
        "pydantic>=2.9" "pydantic-settings>=2.5" \
        "sqlalchemy[asyncio]>=2.0" "asyncpg>=0.30" \
        "cryptography>=43" "httpx>=0.27" \
        "litellm>=1.50" "structlog>=24.4" \
        "python-multipart>=0.0.9" "jinja2>=3.1" "click>=8.1"

# Code
COPY src/ ./src/
RUN pip install --no-cache-dir -e .

# Drop root: the API decrypts provider tokens, so an RCE must not land as root
# in the container. Non-root can still bind :8000 (>1024) and read the code.
RUN useradd --create-home --uid 10001 app \
    && chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "aibroker.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]

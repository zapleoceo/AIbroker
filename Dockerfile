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
# requirements.lock is exported from uv.lock (the versions CI tests against):
#   uv export --frozen --no-dev --no-hashes --no-emit-project -o requirements.lock
# Regenerate it whenever pyproject.toml or uv.lock changes. The layer is keyed on
# that file alone, so a src-only change does not reinstall dependencies, and the
# code layer below installs the package with --no-deps so nothing here is
# resolved a second time (the old hand-copied list pinned a different litellm
# than pyproject and the `pip install -e .` below then installed a second copy).
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

# Code
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps -e .

# Drop root: the API decrypts provider tokens, so an RCE must not land as root
# in the container. Non-root can still bind :8000 (>1024) and read the code.
# /var/lib/aibroker holds the notifier's alert-dedup state files. Created here
# owned by `app` because non-root can't mkdir under /var/lib at runtime — the
# monitor hit PermissionError there once we dropped root (2026-07-10).
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /var/lib/aibroker /run/aibroker-agent-rpc/claude /run/aibroker-agent-rpc/codex \
    && chown -R app:app /app /var/lib/aibroker /run/aibroker-agent-rpc \
    && chmod 0700 /run/aibroker-agent-rpc/claude /run/aibroker-agent-rpc/codex
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "aibroker.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--timeout-graceful-shutdown", "10"]

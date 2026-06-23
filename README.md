# AIbroker

Centralized key broker for AI/LLM provider API keys. Self-hosted, async,
production-ready from day one.

## What it does

- Holds a pool of API keys for many providers (LLM and arbitrary HTTP APIs)
- Authenticates client projects (Vera, Stepan, future) via per-project keys
- Selects the best available key for each request (LRU + cost/cooldown aware)
- Health-monitors keys (auto cooldown on 429, mark dead on 401/403)
- Two operating modes:
  - **Proxy mode** — broker calls the provider with its key, returns response.
    Used for LLM (chat/embeddings) via [LiteLLM](https://litellm.ai) SDK.
  - **Vending mode** — broker hands out a key with a short lease, client
    calls provider directly, reports usage back. Used for arbitrary APIs.
- Per-project daily/monthly cost caps + global cap
- Full audit log (who, what, when, how much)
- Telegram alerts on key death, cap breach, monitor failures

## Architecture

```
┌─── client projects ────┐
│ Vera, Stepan, …        │  HTTPS, X-Project-Key
└──────────┬─────────────┘
           ▼
┌──────────────────────────────────────────────────────┐
│ aibroker-api (FastAPI)                               │
│   POST /v1/proxy/{provider}/* …  → LiteLLM(.acompletion) │
│   POST /v1/key, /v1/usage, /v1/release  (vending)    │
│   GET  /v1/health, /admin, /dashboard                │
└──────────┬───────────────────────────────────────────┘
           │
           ▼
┌─── aibroker-postgres ─────────────────────┐
│ projects, api_keys, leases, usage_log,    │
│ audit_log                                 │
└───────────────────────────────────────────┘
           ▲
           │
┌──── aibroker-monitor (cron) ─────────────┐
│ pings each key every 10 min → marks dead │
│ pushes Telegram alerts                   │
└───────────────────────────────────────────┘
```

## Quick start (dev)

```bash
cp .env.example .env
docker compose up --build
# API on http://localhost:8004
# Dashboard on http://localhost:8004/dashboard
```

Bootstrap an admin project:

```bash
docker exec -it aibroker-api python -m aibroker.scripts.bootstrap \
  --admin-key "$(grep ADMIN_KEY .env | cut -d= -f2)"
```

## Production deploy

`git push origin master` → GH Actions → rsync to Hetzner → `docker compose up -d`.

Domain: `https://aib.zapleo.com` (Cloudflare → nginx → broker on :8004).

## Layout

```
AIbroker/
├── README.md
├── docker-compose.yml
├── .env.example
├── pyproject.toml
├── alembic.ini
├── infra/
│   ├── nginx-aib.conf          # /etc/nginx/sites-enabled/aib
│   └── sql/init.sql            # schema bootstrap (idempotent)
├── src/aibroker/
│   ├── main.py                 # FastAPI app + lifespan
│   ├── config.py               # settings (pydantic-settings)
│   ├── auth.py                 # X-Project-Key, X-Admin-Key
│   ├── db/
│   │   ├── engine.py           # async engine + sessionmaker
│   │   └── models.py           # SQLAlchemy ORM
│   ├── crypto.py               # Fernet at-rest encryption
│   ├── routing/
│   │   ├── selector.py         # LRU + cap-aware token picker
│   │   ├── chains.py           # capability → provider order
│   │   └── cost_guard.py       # daily/monthly cap enforcement
│   ├── routes/
│   │   ├── proxy.py            # /v1/proxy/{provider}/*
│   │   ├── vending.py          # /v1/key, /v1/usage, /v1/release
│   │   ├── admin.py            # /admin/projects, /admin/keys
│   │   ├── health.py           # /healthz, /v1/health
│   │   └── dashboard.py        # /dashboard
│   ├── providers/
│   │   ├── base.py             # Provider ABC
│   │   ├── litellm_adapter.py  # LLM chat/embed via litellm SDK
│   │   └── health_probes.py    # cheapest call per provider
│   ├── telemetry/
│   │   ├── notifier.py         # Telegram alerts
│   │   └── audit.py            # audit_log writer
│   └── scripts/
│       ├── bootstrap.py        # create admin project
│       └── migrate_from_vera.py  # one-shot copy from vera.tokens
├── migrations/                 # alembic
│   ├── env.py
│   └── versions/
├── tests/
│   ├── conftest.py
│   ├── test_auth.py
│   ├── test_selector.py
│   ├── test_cost_guard.py
│   └── test_proxy_e2e.py
├── monitor/                    # separate container — cron health checker
│   ├── Dockerfile
│   └── monitor.py
└── .github/workflows/
    ├── test.yml
    └── deploy.yml
```

## License

Proprietary, owner: zapleoceo.

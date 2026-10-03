# AIbroker

Centralized key broker for AI/LLM provider API keys. Self-hosted, async,
production-ready from day one.

## What it does

- Holds a pool of API keys for many providers (LLM and arbitrary HTTP APIs)
- Authenticates client projects (Vera, Stepan, future) via per-project keys
- Selects the best available key for each request (LRU + cost/cooldown aware)
- Health-monitors keys (auto cooldown on 429, mark dead on 401/403)
- **Proxy mode only** — the broker calls the provider with its own key and
  returns the response. LLM calls (chat, embeddings, transcription, vision)
  go through the [LiteLLM](https://litellm.ai) SDK. (Key vending, where the
  client got a leased key, was removed 2026-07-12.)
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
│   POST /v1/jobs (+ GET /v1/jobs/{id}), /v1/deep,     │
│        /v1/embed, /v1/transcribe(/jobs), /v1/decisions│
│        → routing chain → LiteLLM                     │
│   GET  /v1/health, /healthz, /admin, /dashboard      │
└──────────┬───────────────────────────────────────────┘
           │
           ▼
┌─── aibroker-postgres ─────────────────────┐
│ projects, api_keys, leases, usage_log,    │
│ audit_log, deep_jobs, …                   │
└───────────────────────────────────────────┘
           ▲
           │
┌──── aibroker-monitor (src/aibroker/monitor.py) ──┐
│ pings each key periodically → marks dead         │
│ pushes Telegram alerts                           │
└───────────────────────────────────────────────────┘
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

`git push origin master` → GitHub Actions (`docs`, `test`, `integration`,
`quality` gates) → SSH forced-command `aibroker-deploy`
(`infra/aibroker-deploy.sh`): `git fetch` + `reset --hard`, `docker compose
build` / `up -d`, a drift gate (exit 12) and an all-services health gate of up
to 180 s (exit 11). No rsync. Details in [docs/deploy-ops.md](docs/deploy-ops.md).

Client endpoints: `/v1/jobs` (+ `GET /v1/jobs/{id}`), `/v1/deep`, `/v1/embed`,
`/v1/transcribe`, `/v1/transcribe/jobs`, `/v1/decisions`. Sync `/v1/chat`
returns 410.

Domain: `https://aib.zapleo.com` (Cloudflare → nginx → broker on :8004).

## Layout

```
AIbroker/
├── README.md
├── docker-compose.yml
├── .env.example
├── pyproject.toml
├── infra/
│   ├── nginx-aib.conf          # /etc/nginx/sites-enabled/aib
│   ├── aibroker-deploy.sh      # forced-command deploy wrapper
│   └── sql/
│       ├── init.sql            # first-boot bootstrap (mirrors every migration)
│       └── migrations/         # hand-written NNN_*.sql, applied with psql
├── src/aibroker/
│   ├── main.py                 # FastAPI app + lifespan
│   ├── config.py               # settings (pydantic-settings)
│   ├── auth.py                 # X-Project-Key, X-Admin-Key
│   ├── db/
│   │   ├── engine.py           # async engine + sessionmaker
│   │   └── models.py           # SQLAlchemy ORM
│   ├── crypto.py               # Fernet at-rest encryption
│   ├── monitor.py              # aibroker-monitor: key health checks + alerts
│   ├── routing/
│   │   ├── selector.py         # LRU + cap-aware token picker
│   │   ├── chains.py           # capability → provider order
│   │   └── cost_guard.py       # daily/monthly cap enforcement
│   ├── routes/
│   │   ├── proxy.py            # /v1/jobs, /v1/embed, /v1/transcribe, /v1/deep, /v1/decisions, /v1/chat (410)
│   │   ├── admin.py            # /admin/projects, /admin/keys
│   │   ├── health.py           # /healthz, /v1/health
│   │   ├── dashboard.py        # /dashboard (routes)
│   │   ├── dashboard_assets.py # dashboard CSS/JS assets
│   │   ├── dashboard_data.py   # dashboard data queries
│   │   ├── dashboard_render.py # dashboard HTML render
│   │   └── dashboard_scopes.py # dashboard scope helpers
│   ├── providers/
│   │   ├── adapters.py         # ProviderAdapter registry (adapter_for)
│   │   ├── litellm_adapter.py  # LLM chat/embed via litellm SDK
│   │   └── health_probes.py    # cheapest call per provider
│   ├── telemetry/
│   │   ├── notifier.py         # Telegram alerts
│   │   └── audit.py            # audit_log writer
│   ├── services/               # llm_service, job_queue, deep_jobs, …
│   └── scripts/
│       └── bootstrap.py        # create admin project
├── migrations/README.md        # how schema changes are applied
├── tests/
│   ├── conftest.py
│   ├── test_auth.py
│   ├── test_selector.py
│   ├── test_cost_guard.py
│   └── test_init_sql_mirrors_migrations.py
└── .github/workflows/
    ├── ci.yml
    ├── deploy.yml
    └── docs-check.yml
```

## License

Proprietary, owner: zapleoceo.

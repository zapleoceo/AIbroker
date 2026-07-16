# Deploy & ops

## Auto-deploy

Push to `master` → `.github/workflows/deploy.yml` runs **four jobs**:

1. **`docs` job** — any file under `src/`, `infra/`, `services/`, or
   `monitor/` changed must be matched by a `docs/` change. Opt-out per
   commit: literal `docs-not-needed`.
2. **`test` job** — `pytest`, must pass. Repo-wide coverage gate **70%**
   on `aibroker` package (stair-step; never drops).
3. **`quality` job** — strict static analysis on the diff:
   - **Ruff** with `E,F,W,I,B,UP,SIM,C4,RET` (simplify, comprehensions,
     unreachable-after-return). E501/E402 ignored (no formatter; tests
     set env before import). Diff-only: legacy code is grandfathered,
     strict rules apply to whatever this push touched.
   - **Vulture** `--min-confidence 80` on changed files — surfaces dead
     funcs/classes ruff's F401/F841 doesn't see.
   - **Diff-cover** — every changed line ≥75% covered by tests in this
     push (separate from the 70% repo gate). Catches "new function
     without a test".
   - **Docs name-sync** — extract every public def/class from the diff
     (skip `_private`, `test_*`). **Added** symbols must appear in
     `docs/*.md`; **removed** symbols must NOT remain there. Opt-out:
     `docs-not-needed`.
4. **`deploy` job** — `needs: [docs, test, quality]`. SSH to
   `aib.zapleo.com`; key on the server is wired to
   `command="/usr/local/bin/aibroker-deploy"` in `authorized_keys`.
   Wrapper does `git pull → docker compose build → up -d → poll
   healthz for up to 60s`.

### What this guarantees

Anything that reaches production has: passing tests, ≥75% coverage on
the actual diff, no dead code in the touched files, no syntax/import
issues, every public name documented, no orphan refs to removed code.
If any gate fails, deploy is blocked.

If any step fails, Telegram alert goes to `OWNER_TELEGRAM_ID` from
`@aibzapleo_bot`. A separate `docs-check.yml` workflow runs the docs gate
on every push (including feature branches) so PRs see the verdict early.

## Restricted SSH key

Generated once on a dev box:
```
ssh-keygen -t ed25519 -f aibroker_gh_deploy -N "" -C "github-actions-aibroker-deploy"
```

Public part appended to `/root/.ssh/authorized_keys` on the server:
```
command="/usr/local/bin/aibroker-deploy",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 AAAA…
```

If this key leaks, the worst an attacker can do is re-run our deploy
script. No shell, no scp, no port-forward.

## One-shot: import legacy usage from Vera + Stepan

Before the broker existed both projects logged token usage in their own
`usage_log` tables. To consolidate that history into the broker
dashboard's per-project drill-down:

```bash
ssh hetzner-root
cd /var/www/aibroker
./infra/migrate_legacy_usage.sh dry-run   # show source counts + sums
./infra/migrate_legacy_usage.sh apply     # COPY into broker.usage_log
```

What it does:
- Resolves `projects.id` in the broker by name (`vera`, `stepan`).
- `DELETE` any prior `legacy:%`-tagged rows for that project — reruns
  don't double-count.
- COPYs from `<source>-postgres` directly into `aibroker-postgres` via
  a stdout pipe (no intermediate file).
- Imported rows are tagged `workflow = 'legacy:vera[:original_workflow]'`
  so they're distinguishable from live broker traffic in the drill-down's
  capability/workflow breakdown.
- `api_key_id` is NULL (legacy ids don't map to broker `api_keys`; FK is
  `ON DELETE SET NULL`).
- `lease_id` and `http_status` are NULL (legacy schemas lack them).
- `success` (bool) becomes `status` (`'ok'` / `'error'`).
- `error_kind` is copied when the source has the column (Vera does;
  Stepan does not).

Rerun whenever you want a fresh snapshot — old `legacy:%` rows are
wiped and re-imported, live broker rows are never touched.

## Schema migrations

Applied via `psql` directly against the running container — no Alembic in
production. Every file in `infra/sql/migrations/` is idempotent
(`IF NOT EXISTS`), so re-running is safe:

```
ssh hetzner-root
docker exec -i aibroker-postgres psql -U aibroker aibroker \
  < /var/www/aibroker/infra/sql/migrations/010_deep_jobs_payload_hash.sql
```

Apply a migration BEFORE merging the code that depends on it (the deploy
pipeline ships code only). `infra/sql/init.sql` mirrors every migration for
fresh-DB bootstrap. Migration 010 (2026-07-16) adds `deep_jobs.payload_hash`
plus the `ix_deep_jobs_dedup` index for in-flight job dedup — the code
degrades to plain inserts (with a logged warning) if it lands first, but
dedup stays off until the migration is applied.

## Redis container (2026-07-16)

`docker-compose.yml` now includes `aibroker-redis` (`redis:7-alpine`) —
shared selector state (cache-affinity + saturation verdicts) across the two
uvicorn workers / future nodes. **No ops action needed**: the next
`docker compose up -d` creates it, and `REDIS_URL` is wired into `api` and
`monitor` by compose itself (nothing to add to `.env`).

Cache semantics on purpose: `--save ""` (no persistence, nothing to back
up), 64 MB `allkeys-lru` cap, no published ports (compose-network only).
If the container is down the app fails open to its old in-process behaviour
— worst case a slightly colder provider prompt-cache, never an outage.

## Connection scaling

> **Status 2026-07-16: PgBouncer is INSTALLED** (`aibroker-pgbouncer`,
> edoburu/pgbouncer, transaction pooling, DEFAULT_POOL_SIZE=15,
> MAX_CLIENT_CONN=200, MAX_PREPARED_STATEMENTS=500 so asyncpg's prepared
> statements survive pooling). `DATABASE_URL` on api/monitor points at
> `pgbouncer:6432`; `DIRECT_DATABASE_URL` keeps the deep-jobs LISTEN
> connection on `postgres:5432` — NOTIFY subscriptions need a pinned backend
> and silently die under transaction pooling. Rollback = point DATABASE_URL
> back at `postgres:5432` and redeploy; the app has no other coupling to the
> pooler. AUTH_TYPE=plain is confined to the compose-internal network (no
> published ports). (2026-07-16)

Decision documented, nothing installed. Current math:

- `db/engine.py`: `pool_size=10` + `max_overflow=20` = **30 connections max
  per process**; the `api` container runs **2 uvicorn workers** → 60 max from
  the API alone, plus the `monitor` container's own engine (another 30 worst
  case) — vs Postgres's default `max_connections = 100`.
- In practice the pools sit far below their ceilings (sessions are
  short-lived; the 2026-07-16 session-diet work cut per-attempt session churn
  further), so today a pooler would add a hop and a component for no measured
  win.

**Threshold — add PgBouncer (transaction pooling) when a second broker node
appears or the worker count doubles.** Either step puts the theoretical max
(≥120 from API workers alone) past what default Postgres can take, and
per-process SQLAlchemy pools stop being a global cap at all once processes
multiply across nodes.

How it slots in when the time comes (sketch, not applied): add a
`pgbouncer` service to `docker-compose.yml` (e.g. `edoburu/pgbouncer`,
`pool_mode=transaction`, `default_pool_size≈20`) between the app and
`postgres`, point `DATABASE_URL` at it, and drop the app-side pool to
`pool_size≈5, max_overflow≈5` per worker. Caveats to check then: no
session-level state across transactions (no advisory locks / LISTEN on
pooled connections — the deep-jobs NOTIFY listener needs a DIRECT
connection to Postgres, bypassing the pooler), and `pool_pre_ping` stays on.

## Manual deploy fallback

```
ssh hetzner-root
cd /var/www/aibroker
git pull
docker compose build
docker compose up -d
```

## Secrets

### Server `.env` (at `/var/www/aibroker/.env`, mode 600)

| Var | Purpose |
|---|---|
| `POSTGRES_PASSWORD` | broker postgres root |
| `TOKEN_SECRET` | Fernet key for `api_keys.token_encrypted` |
| `ADMIN_KEY` | X-Admin-Key for `/admin/*` and dashboard fallback |
| `INTERNAL_SECRET` | reserved for monitor↔api auth (HMAC) |
| `SESSION_SECRET` | HMAC for browser session cookies |
| `TELEGRAM_BOT_TOKEN` | `@aibzapleo_bot` — sends alerts + signs login widget |
| `TELEGRAM_BOT_USERNAME` | for embedding the widget on `/login` |
| `OWNER_TELEGRAM_ID` | only this Telegram user can log into dashboard |
| `GLOBAL_DAILY_CAP_USD` | global daily spend cap |
| `PUBLIC_HOST` | for absolute URLs on login page |
| `LOG_LEVEL` | INFO / DEBUG |

### GitHub Actions secrets (`zapleoceo/AIbroker`)

| Secret | Notes |
|---|---|
| `HETZNER_HOST` | `195.201.31.49` |
| `HETZNER_PORT` | `9617` |
| `HETZNER_SSH_KEY` | the restricted private key (`aibroker_gh_deploy`) |
| `TELEGRAM_BOT_TOKEN_VERA` | optional, for failure alerts |
| `OWNER_TELEGRAM_ID` | optional, for failure alerts |

## Rotating keys

- `ADMIN_KEY`: edit `.env`, `docker compose up -d --force-recreate api`.
- `TOKEN_SECRET`: don't rotate without a re-encryption migration — every
  row in `api_keys.token_encrypted` will become unreadable.
- `SESSION_SECRET`: rotate freely; users will be logged out once.
- `TELEGRAM_BOT_TOKEN`: rotate in BotFather, paste new token in `.env`,
  `up -d --force-recreate api monitor`.

## Health snapshot

- `https://aib.zapleo.com/healthz` — liveness
- `https://aib.zapleo.com/v1/health` — per-provider alive/cooldown/dead
- Logs: `docker compose logs -f api` on the server.

## Disaster recovery

The Postgres volume (`aibroker_pgdata`) is the only persistent state.
Take a daily snapshot:
```
ssh hetzner-root "docker exec aibroker-postgres pg_dump -U aibroker aibroker | gzip > /var/backups/aibroker-$(date +%F).sql.gz"
```

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

## Local ASR (2026-07-18, moved in-repo same day; model-bump attempted same day, reverted)

`services/asr-local/` — self-hosted `faster-whisper` (`small`, int8, 1 CPU
thread, `beam_size=5`, 1.5GB cap) — is its own `docker-compose.yml` service
(`aibroker-asr-local`), built and run alongside `api` on this repo's own
compose network. `api` reaches it at `ASR_LOCAL_URL` (default
`http://aibroker-asr-local:8000`); unreachable/unset degrades safely to
groq/gemini/openai (see `docs/api.md`'s `local` transcription section).

**Model size ceiling on this host (2026-07-18).** Tried bumping `small` ->
`large-v3-turbo` (bigger encoder, better multilingual accuracy — worth it
since volume is low, ~10 req/day, no backfill, so the model's RAM footprint
is the only real cost, not throughput). First attempt used a
non-existent repo id (`Systran/faster-whisper-large-v3-turbo` 401s — Systran
never published that conversion; the real public one is
`deepdml/faster-whisper-large-v3-turbo-ct2`) and failed CI's docker build
fast (~45s) before ever reaching the server. Fixed the repo id, then tested
loading it **directly on the server** in an isolated, unconstrained
container (not the real deploy) before trying again — **OOM-killed (exit
137)**. Tried `medium` as a fallback the same way — also OOM-killed. Swap was
100% full both times (`free -h`), so there was no headroom for the transient
peak during download+int8 quantization (meaningfully above the model's final
resident size). Reverted all three files (Dockerfile, docker-compose.yml,
app.py default) back to `small`; kept `beam_size=5` (up from greedy) as the
accuracy lever that costs CPU/latency, not RAM. The failed GitHub Actions
deploy (`docker compose build` failing) never reached `up -d`, so production
was unaffected throughout both attempts.

Revisit if this host gets more RAM, or a dedicated host is stood up for
asr-local — `WHISPER_MODEL` env var is the only thing that needs to change.
Before trying again: check `free -h` for swap headroom, and load-test the
candidate model directly on the server in a throwaway container first
(`docker run --rm -v ...:/test.py python:3.12-slim ...`) rather than finding
out via a failed deploy.

This briefly lived in vera3's own compose stack instead, reached over a
cross-project network join (`api` joining `vera3_default` as an external
network) — reverted same day: a vera3-side refactor deleted that service
entirely (its own voice pipeline moved to calling this broker uniformly,
which made its local copy look redundant), not realizing the broker's
`local` provider was only ever a thin proxy to that exact container, not a
model of its own. Deleting the one real model host silently took the
broker-wide feature down with it. Owning the service directly means the
one thing the broker's own routing depends on can't become collateral
damage in an unrelated project's cleanup again — no other project's compose
file needs to keep a network name stable for this to keep working.

Same 2-cores-shared-with-Stepan2/Vera constraint applies regardless of which
compose file the container lives in — nothing about resource math changed by
moving it, only the ownership boundary.

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

**How it runs today.** The `pgbouncer` service in `docker-compose.yml` sits
between the app containers and `postgres`:

- `POOL_MODE=transaction` — a server backend is held only for the duration
  of a transaction, so `DEFAULT_POOL_SIZE=15` real Postgres connections
  serve up to `MAX_CLIENT_CONN=200` client ones, `LISTEN_PORT=6432`.
- `MAX_PREPARED_STATEMENTS=500` (pgbouncer ≥ 1.21) lets asyncpg's
  protocol-level prepared statements survive transaction pooling — without
  it every SQLAlchemy statement re-prepares or errors under the pooler.
- `api` and `monitor` both get
  `DATABASE_URL=…@pgbouncer:6432/aibroker` (all pooled traffic) and
  `DIRECT_DATABASE_URL=…@postgres:5432/aibroker` — the one bypass, used
  only by the deep-jobs dispatcher's asyncpg LISTEN connection
  (`services/job_queue.py`): NOTIFY subscriptions need a pinned backend
  and would silently die under transaction pooling.
- **Rollback**: flip `DATABASE_URL` back to `postgres:5432` and redeploy.
  Nothing else in the app knows the pooler exists.

**Why (threshold history, kept as background).** `db/engine.py` runs
`pool_size=10 + max_overflow=20` = 30 connections max per process; 2
uvicorn workers in `api` → 60 max from the API alone, plus the `monitor`
container's engine (another 30 worst case) — uncomfortably close to
Postgres's default `max_connections = 100` when those were direct backend
connections. The documented threshold was "add PgBouncer when a second
broker node appears or the worker count doubles"; the 2026-07-16 scale
work (Redis shared state, NOTIFY dispatcher — the prep for a second node)
crossed that line, so the pooler went in with it. The app-side SQLAlchemy
pool is unchanged — its 30 per-process connections now terminate at
pgbouncer, which multiplexes them onto ~15 real backends. Session-level
caveats that shaped the layout: no advisory locks or LISTEN on pooled
connections (hence `DIRECT_DATABASE_URL`), `pool_pre_ping` stays on.

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
| `HETZNER_HOST` | origin server IP — value lives ONLY in the GitHub secret, never in this repo (see note below) |
| `HETZNER_PORT` | non-standard SSH port — same, secret-only |
| `HETZNER_SSH_KEY` | the restricted private key (`aibroker_gh_deploy`) |

> **Why the host/port are not written here (2026-07-24):** this repository is
> PUBLIC. The origin sits behind Cloudflare, so publishing the origin IP undoes
> that protection entirely — anyone can then reach nginx directly
> (`curl -H 'Host: aib.zapleo.com' http://<origin-ip>/…`) and bypass
> Cloudflare's WAF, DDoS protection and rate limiting, plus probe SSH on the
> non-standard port. The values were committed here previously, so they must be
> treated as permanently public (git history keeps them) — the durable fix is
> the firewall: allow :80 only from Cloudflare's published ranges, so a direct
> origin request is dropped even by someone who knows the IP.
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

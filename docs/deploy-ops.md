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

## Job retention (2026-07-26)

`deep_jobs` had no retention and had grown to **1.6 GB / 103k rows** — every row
stores the FULL request payload (Stepan's system prompt alone is ~79k chars, and
async transcription parks base64 audio there too), so the nightly dump was
~886 MB of finished work nobody would ever read again. Only 51 rows were
actually live (`pending`).

`job_queue.purge_finished_jobs()` deletes terminal jobs past
`JOB_RETENTION_DAYS` (default **7**), batched at `JOB_RETENTION_BATCH`
(default 5000) so a big first sweep can't hold a long lock — it simply takes
several passes. The dispatcher calls it every `JOB_RETENTION_EVERY_TICKS`
(default 300 ≈ 25 min at the idle poll), Postgres only. Both workers running it
is harmless: the DELETE is idempotent.

Only `done`/`error` rows with a `completed_at` are eligible — a `pending` or
`running` job is untouchable regardless of age, so nothing can be deleted out
from under the dispatcher or while a caller might still poll it. Verified
against real Postgres before shipping (old done/error deleted; fresh done,
pending and running all kept).

No client change is needed: nothing but the queue itself reads `deep_jobs`, and
callers poll their result within seconds.

Tune per environment with `JOB_RETENTION_DAYS` / `JOB_RETENTION_BATCH` /
`JOB_RETENTION_EVERY_TICKS`.

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

## Local vision (2026-08-31)

`vision-local` runs **upstream `llama-server`** (`ghcr.io/ggml-org/llama.cpp:server`)
serving **Qwen3-VL-4B-Instruct Q4_K_M** on CPU. Container
`aibroker-vision-local`; `api` reaches it at `VISION_LOCAL_URL` (default
`http://aibroker-vision-local:8080`). Unset or unreachable degrades safely to
`gemini -> openrouter -> openai`.

**Why it exists.** Vision was running an 8% success rate: over 14 days, 1762 ok
against ~20000 errors (12102 `CapBlock`, 3828 gemini `RateLimitError`, 4053
openrouter `RateLimitError`), essentially all of it one client's traffic at
240-300 distinct images/day.

**Unlike `asr-local`, we write no service code.** `llama-server` already
provides everything the asr-local wrapper had to hand-roll: an OpenAI-shaped
`/v1/chat/completions` that accepts `image_url`, `--sleep-idle-seconds` for
idle unload, `/health` that is exempt from the idle timer (so the compose
healthcheck cannot keep the model awake), and `response_format: json_schema`
for grammar-constrained output. All broker-side logic lives in
`providers/litellm_adapter._describe_via_local_vision`.

**Model is MOUNTED, not baked into an image.** Deploys build on the production
host inside a 10-minute CI step (`/usr/local/bin/aibroker-deploy`); pulling
3.3GB of weights into a layer on every Dockerfile touch would put that budget
at risk for nothing. One-time host setup:

    mkdir -p /var/lib/aibroker-vision/model && cd /var/lib/aibroker-vision/model
    curl -fLO https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF/resolve/main/Qwen3VL-4B-Instruct-Q4_K_M.gguf
    curl -fLO https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF/resolve/main/mmproj-Qwen3VL-4B-Instruct-F16.gguf
    # then rename to qwen3-vl-4b-Q4_K_M.gguf / qwen3-vl-4b-mmproj-F16.gguf,
    # or point VISION_MODEL_DIR elsewhere.

### Measured on this host, 2026-08-31

| | |
|---|---|
| model load | 22s |
| chat screenshot, model resident | **69s** |
| dense document | up to 192s |
| one-shot CLI (reloads model per call) | 81-192s, median 163s |
| **peak RSS** | **5105MB** -> `mem_limit: 5600m` |

Peak RSS climbs 4781 -> 5105MB across consecutive runs and then plateaus:
most of it is page cache for the mmap'd weights, which the kernel reclaims
under pressure. Composition: 2.5GB weights + 0.84GB mmproj (F16) + 1.13GB f16
KV cache at `-c 8192` + compute buffers.

**Images MUST be downscaled to 1024px on the long edge** — done broker-side in
`_downscale` (Pillow), governed by `VISION_LOCAL_MAX_PX`. This is load-bearing,
not an optimization: at native resolution the vision encoder does not fit in
memory here.

### Two failed configurations, recorded so they aren't retried

**Native-resolution input + `--memory=3800m` + quantized KV cache (`--cache-type-k/v q8_0`):**
a single image ran past **600s without ever completing**, at 179-190% CPU — it
was not hung, it was thrashing. At 3.51GB against a 3.8GB cap with swap
disabled, the page cache for the 2.5GB mmap'd GGUF was being evicted and
re-read from disk continuously. The same image at 1024px with a 5600m limit
took 69s. **A too-small memory cap on an mmap-backed model does not OOM — it
silently runs ~10x slower.**

**`response_format: {"type":"json_schema","schema":{...}}`** — the flat form —
is accepted by llama-server and **silently ignored**: it answers with
free-form JSON whose keys have nothing to do with the schema. Caught in
production on the first live image, which came back with a `vision_type`
that is not in the enum at all. Only the OpenAI-style nesting,
`{"type":"json_schema","json_schema":{"schema":{...}}}`, actually constrains
decoding — verified by probing a single-value enum against the running
server: constrained under the nested form, invented keys under the flat one.
There is no error and no warning, so this can only be caught by asserting on
output, never by watching for a failure.

**`--parallel 2`** was not adopted: a second slot buys a second KV cache
(~1.1GB) this host does not have the RAM for.

### 2026-09-02: seven host-wide OOM kills in 24h, and what fixed what

llama-server was killed 7 times in 24h at 4.4-5.5GB anon-rss. Crucially these
were **global** OOM kills (`constraint=CONSTRAINT_NONE`, `global_oom`), NOT the
container's cgroup limit — it died below its own 5.47GiB cap because the HOST
ran out. `docker inspect` reports `OOMKilled: false` for exactly this reason.
The kernel picked llama-server every time on merit: its `oom_score` is 922
against 666-680 for everything else on the box, so tuning `oom_score_adj` would
change nothing.

**Where the memory actually is.** Measured under load: `RssAnon 3446MB` vs
`RssFile 595MB`. The main GGUF *is* mmap'd, but only ~455-591MB of its 2.36GB
stays resident and it thrashes (`workingset_refault_file` 979688 against 10847
for anon). The mmproj has **no mapping at all** — llama.cpp's multimodal loader
reads it into the heap, so all 836MB is unconditionally anonymous. On top of
that `--repack` (default: enabled) converts Q4_K tensors into AVX-friendly
layouts at load time, which means copying them into fresh anonymous buffers.
Hence `file-rss: 0kB` in the OOM records: by kill time there was no reclaimable
page cache left anywhere on the host.

**Adopted, both measured:**
- `-c 8192` -> `6144`. Returns ~288MB of KV cache. NOT 4096: across 183 real
  calls `tokens_in` was p50=851 / p90=979 / p99=1235 / **max=4451**, and
  `tokens_out` peaked at 341 — 4096 would truncate the largest document we have
  actually served.
- `--sleep-idle-seconds 900` -> `120`. Real gaps between bursts are 28-37 min,
  so the model already slept before nearly every burst; 900 bought almost no
  avoided reloads (16 sleeps against 8 reloads in one window; a wake costs
  ~16s) while holding ~5GB through idle windows on a host with ~1.4GB free.

**Rejected, and why — do not retry without new evidence:**
- `--no-repack`. It is the most direct fix for the mechanism above: the weights
  would stay as reclaimable file-backed mmap instead of ~3.3GB of anonymous
  private-dirty copies. But on the same image it ran **310s against 203s,
  +53%**. Against the 300s call timeout that aborts nearly every request.
  Repacking is bought with memory and sold as speed, and at a 130s average we
  have no speed to sell.
- mmproj F16 -> Q8_0 would save 364MiB straight out of anonymous memory, which
  is the right kind of memory to attack. But **no Q8_0 mmproj exists on the
  host** — it has to be produced first, and its accuracy cost on receipts and
  bank screens is unmeasured. Open, not rejected.
- Raising swap / tuning `vm.swappiness` (currently 10): no. The 4-5GB is
  anonymous memory actively read during inference; forcing it to swap trades an
  OOM for multi-second per-page stalls.
- Trimming other containers: nothing to take. All ~34 others total 1.8-2.2GB,
  postgres configs are stock, no restart loops, no duplicate services.

**Open question.** One real image (1920x2560 portrait document, 768x1024 after
downscale, ~768 vision tokens) failed to complete in 900s AND again in 700s,
while neighbours at 589824 pixels finished in 94-203s. A 4x+ time difference
for 33% more pixels is not explained by size. Unresolved; it is part of why
`local` books ~35 TimeoutErrors, which correctly fall through to gemini.

### While it runs

Available RAM drops to ~1.5GB and load average to ~3.5 on 4 cores. At
240-300 images/day that is 11-14h/day in that state. `mem_limit` is mandatory:
without one the kernel may pick postgres as the OOM victim rather than the
model.

### Peak-hour overflow is expected and correct

Distinct images per hour: median 10, p90 21, **peak 162** (00:00 UTC, every
day). One serialized worker clears ~50/hour, so in the peak hour the cloud tail
of the chain takes the overflow. That is the design, not a failure — `local`
returning nothing simply walks the chain to `gemini`.

## Local ASR (2026-07-18, moved in-repo same day; model-bump attempted same day, reverted)

`services/asr-local/` — self-hosted `faster-whisper` (`small`, int8, 1 CPU
thread, `beam_size=5`, 1.5GB cap) — is its own `docker-compose.yml` service
(`aibroker-asr-local`), built and run alongside `api` on this repo's own
compose network. `api` reaches it at `ASR_LOCAL_URL` (default
`http://aibroker-asr-local:8000`); unreachable/unset degrades safely to
groq/gemini/openai (see `docs/api.md`'s `local` transcription section).

**Idle unload (2026-08-28).** The model is no longer preloaded at startup and
is dropped after `WHISPER_IDLE_UNLOAD_S` idle seconds (default 600; 0
disables). `get_model()` was already lazy, so this is only a reaper task plus
a last-used stamp; the reaper takes the same `_transcribe_lock` the decode
path uses, so it can never unload mid-request.

Measured on this host that day, in-container:

```
before import        9 MB
after import        62 MB   <- libraries alone
model loaded       519 MB   (4.4s)
after del + gc     252 MB
```

The running container sat at ~195MB resident. Against ~19 transcriptions a
DAY the model is idle ~99% of the time, and the host is 2 cores / 3.7GB with
1.3GB already in swap — so a permanently resident idle model gets paged out
anyway and paged back in on the next call. Paying the 4.4s load explicitly on
a cold request is cheaper and more predictable than that, on calls whose
measured latency is 15-180s regardless.

`gc.collect()` alone is NOT enough, and measuring only the fresh-start number
hides it. After a load/unload cycle the process had freed the model but glibc
kept the arenas, so the host still saw 230MB — a ~50MB saving, not ~210MB.
`_return_arenas_to_os()` calls `malloc_trim(0)`; measured in-container:

```
model loaded        508 MB
after del + gc      241 MB   <- what the host still saw
after malloc_trim    72 MB   <- what it sees now
```

It is guarded, so a non-glibc base image just skips it.

**Not changed, and why.** `cpus: 1.0` and `WHISPER_CPU_THREADS=1` stay: the
host has 2 cores and vera3-postgres was measured pinning ~99% of one, so
giving ASR a second core would contend with production rather than speed
anything up. `beam_size=5` stays too — a synthetic-audio benchmark could not
separate it from fixed overhead (no real speech means the decoder barely
runs), so there is no measurement supporting a drop, and it was chosen
deliberately for non-English accuracy.
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

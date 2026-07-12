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

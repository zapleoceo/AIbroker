# Deploy & ops

## Auto-deploy

Push to `master` → `.github/workflows/deploy.yml` runs **three jobs** in
this order:

1. **`docs` job** — fails if any change under `src/`, `infra/`, `services/`
   or `monitor/` lands without a matching change under `docs/`. Opt-out
   per commit: literal `docs-not-needed` in the commit message.
2. **`test` job** — `pytest`, must pass. Coverage gate currently **58%**
   on `aibroker` package (stair-step; never drops).
3. **`deploy` job** — `needs: [docs, test]`. SSH to `aib.zapleo.com`; the
   key on the server is wired to `command="/usr/local/bin/aibroker-deploy"`
   in `authorized_keys`. Wrapper does `git pull → docker compose build
   → up -d → poll healthz for up to 60s`.

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
| `DEFAULT_LEASE_SECONDS` | vending mode default lease |
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

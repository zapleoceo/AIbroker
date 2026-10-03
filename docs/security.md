# Security

## Threat model

| Asset | Threat | Mitigation |
|---|---|---|
| Provider API keys at rest | DB dump leaked | Fernet (AES-128 CBC + HMAC-SHA256) with key from `.env`. Encrypted column `api_keys.token_encrypted`. |
| Provider API keys in transit | Logs, wire capture | Only ever sent to the actual provider over TLS by LiteLLM. Never logged. |
| Provider API keys echoed in provider ERROR bodies | `api_keys.last_error`, dashboard, DB backups | Some providers quote the offending key in a 401/403 body. Since 2026-09-07 `_penalize` scrubs key-shaped substrings (`sk-…`, `AIza…`, `gsk_…`, `csk-…`, `Bearer …`, `key=…`) before the reason is persisted or rendered. Before that, this row of the table was not quite true. |
| `X-Admin-Key` | Brute force, replay | High-entropy random (48 bytes). HMAC compare. Not stored anywhere except server `.env`. |
| `X-Project-Key` | DB compromise → revealing keys | Stored as `sha256(plain).hexdigest()` only. Plaintext prefix (12 chars) for ops display. |
| Dashboard sessions | Cookie theft | HMAC-SHA256 signed (`<uid>.<exp>.<sig>`), `httponly`, `secure`, `samesite=lax`, 30d TTL. Single owner. |
| Deploy SSH key | Server takeover | Restricted in `authorized_keys`: `command="..." + no-pty + no-*-forwarding`. Worst case attacker re-runs our deploy. |
| Telegram login | Spoofed user_id | Verify HMAC-SHA256 over sorted params with secret = `sha256(bot_token)`. Reject if user_id ≠ `OWNER_TELEGRAM_ID`. Reject if `auth_date > 24h old`. |
| Cost cap (`daily_cost_cap_usd`) | Concurrent requests race past the per-key cap (TOCTOU) | `reserve_cost`/`release_cost` use a single atomic `UPDATE ... WHERE ... RETURNING` — Postgres row-locking serializes concurrent writers so the cap can never be overshot. See **Cost guard** in [`routing.md`](routing.md). |

## Audit log

Every admin op writes a row to `audit_log`:

```
actor       — 'admin' | 'project:<name>' | 'tg:<user_id>' | 'dashboard'
action      — 'project.create' | 'key.create' | 'key.disable' | 'cap_block' | 'login.success' | ...
target      — what was acted on (e.g. 'cerebras/eatmeat', 'id=12')
metadata    — JSONB, arbitrary
ip          — best-effort client IP
created_at  — server time
```

(The `vend` action disappeared with vending mode, removed 2026-07-12 —
old `vend` rows remain in the table as history.)

`audit_log` rows are never updated. They are deleted only by retention:
`purge_old_logs` (`services/job_queue.py`) drops `cap_block` rows after 14 days
(`AUDIT_CAPBLOCK_RETENTION_DAYS`) and every other row after 365 days
(`AUDIT_RETENTION_DAYS`); `usage_log` rows go after 120 days
(`USAGE_RETENTION_DAYS`).

## Key leak response runbook

If a provider API key leaks (e.g. accidentally pasted in chat, committed to a public repo):

1. Open `/dashboard`, find the key by `provider/label`, click **disable**.
   Sets `is_active=false` immediately. Selector skips it from now on.
2. Rotate the key with the provider (vendor console).
3. Click **delete** in the dashboard. Audit log records the deletion.
4. Create a fresh key with the new token via the **Add API key** form.
5. The key-create flow probes the new key immediately (quota discovery);
   the background monitor re-confirms on its adaptive cadence
   (dead/cooldown keys every 600s sweep, alive keys ≈ hourly — see
   [providers.md](providers.md#health-probes)).

## Project key leak response

If `X-Project-Key` leaks:

1. `POST /admin/projects` to create a replacement project with the same scopes.
2. Update the client app's `BROKER_PROJECT_KEY` env, redeploy.
3. Revoke the old key: dashboard → the old project → **Delete** (the key stops
   authenticating at once; usage history keeps its `project_id`), or, to keep
   the project row and its history attached, `UPDATE projects SET
   is_active=false WHERE name='…'` in psql.
4. Review `usage_log` rows for the leaked project (`project_id` +
   `created_at`) for suspicious activity — every proxied call is logged
   there.

## What's NOT covered

- **Rate limiting is at nginx, per project key** (2026-09-07; `infra/nginx-aib.conf`): `limit_req_zone $http_x_project_key … rate=600r/m`, burst 200, 429 on excess. Sized so job polling (~5 req/s per busy project) never trips it; only a genuine flood does. Requests without a project key (dashboard sessions, `/healthz`) are not limited — nginx skips an empty zone key — and rely on owner-only sessions and the Cloudflare IP restriction instead. There is still no rate limiting INSIDE the app, so a request that bypasses nginx (none can, from outside: `api` binds 127.0.0.1 only) is unlimited.
- **Per-IP rate limit too** (2026-10-03): `infra/nginx-aib.conf` adds `limit_req_zone $binary_remote_addr zone=aib_ip ... rate=1800r/m` (burst 400) next to the per-project zone, because requests with an empty or forged `X-Project-Key` skipped the project zone entirely. The file is updated; apply it on the server by hand (`nginx -t && nginx -s reload`).
- **No mTLS** between projects and broker. We rely on `X-Project-Key` over TLS to CF, then HTTP from CF to origin.
- **No KMS** — `TOKEN_SECRET` is on disk. If someone roots the box, all keys can be decrypted.
- **No PII redaction** in audit_log. Today we don't log message bodies — but if that changes, redact first.

## Site review hardening (2026-10-02)

- **Security headers** on every response (`main._security_headers`, middleware; a header a route already set is kept): `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'`, `Strict-Transport-Security: max-age=31536000`. The CSP is deliberately `frame-ancestors` only — the pages use inline `<script>`/`<style>` and the Telegram login widget, so no `script-src`/`style-src`. HSTS carries no `includeSubDomains`/`preload`.
- **`GET /v1/health`** is still public with the same top-level `providers` key, but an anonymous caller gets ONE aggregate row (`provider: "all"`, summed alive/cooldown/dead/total) plus `detail: false`; per-provider rows need a valid `X-Admin-Key` or owner session (`health._is_privileged`, `health._aggregate_health`). The per-provider split was a map of which free pools to exhaust.
- **`/openapi.json`** stays public but no longer lists `/dashboard/*`, `/admin/*`, `/login`, `/logout`, `/api/tg_login` (`include_in_schema=False` on the dashboard and admin routers).
- **Provider validation:** `POST /admin/keys` answers 400 and `POST /dashboard/keys/create` flashes an error for a provider outside `DEFAULT_MODEL`; the dashboard also refuses to add a key for a provider that is in no routing chain (mistral today). The scope-checkbox tooltip now HTML-escapes the provider.
- **Dashboard forms:** cost caps parse through `dashboard._parse_cost_cap` (blank = none; junk, negative, `nan`, `inf` -> flash instead of a 500 or a cap that never trips); flash redirects are percent-encoded (`dashboard._flash_url`); key labels and project names are capped at 100 characters (`_MAX_NAME_LEN`). `GET /logout` (`dashboard.logout_get`) no longer logs out — it redirects to `/dashboard`; the nav button POSTs `/logout`.
- **Editing a key whose provider is in no chain** keeps its stored scopes (all of its scope boxes are disabled, so the form submits none).
- **Privacy wording** on the landing page: request bodies are not logged, but async job payloads (`/v1/jobs`, `/v1/deep`, `/v1/transcribe/jobs`) are stored in `deep_jobs` and purged after `JOB_RETENTION_DAYS` (default 7).

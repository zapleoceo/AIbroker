# Security

## Threat model

| Asset | Threat | Mitigation |
|---|---|---|
| Provider API keys at rest | DB dump leaked | Fernet (AES-128 CBC + HMAC-SHA256) with key from `.env`. Encrypted column `api_keys.token_encrypted`. |
| Provider API keys in transit | Logs, wire capture | Only ever sent to the actual provider over TLS by LiteLLM. Never logged. |
| `X-Admin-Key` | Brute force, replay | High-entropy random (48 bytes). HMAC compare. Not stored anywhere except server `.env`. |
| `X-Project-Key` | DB compromise → revealing keys | Stored as `sha256(plain).hexdigest()` only. Plaintext prefix (12 chars) for ops display. |
| Dashboard sessions | Cookie theft | HMAC-SHA256 signed (`<uid>.<exp>.<sig>`), `httponly`, `secure`, `samesite=lax`, 30d TTL. Single owner. |
| Deploy SSH key | Server takeover | Restricted in `authorized_keys`: `command="..." + no-pty + no-*-forwarding`. Worst case attacker re-runs our deploy. |
| Telegram login | Spoofed user_id | Verify HMAC-SHA256 over sorted params with secret = `sha256(bot_token)`. Reject if user_id ≠ `OWNER_TELEGRAM_ID`. Reject if `auth_date > 24h old`. |
| Cost cap (`daily_cost_cap_usd`) | Concurrent requests race past the per-key cap (TOCTOU) | `reserve_cost`/`release_cost` use a single atomic `UPDATE ... WHERE ... RETURNING` — Postgres row-locking serializes concurrent writers so the cap can never be overshot. See **Cost guard** in [`routing.md`](routing.md). |

## Audit log

Every admin op and every key checkout writes a row to `audit_log`:

```
actor       — 'admin' | 'project:<name>' | 'tg:<user_id>' | 'dashboard'
action      — 'project.create' | 'key.create' | 'key.disable' | 'vend' | 'login.success' | ...
target      — what was acted on (e.g. 'cerebras/eatmeat', 'id=12')
metadata    — JSONB, arbitrary
ip          — best-effort client IP
created_at  — server time
```

`audit_log` is append-only. There is no UPDATE or DELETE in code.

## Key leak response runbook

If a provider API key leaks (e.g. accidentally pasted in chat, committed to a public repo):

1. Open `/dashboard`, find the key by `provider/label`, click **disable**.
   Sets `is_active=false` immediately. Selector skips it from now on.
2. Rotate the key with the provider (vendor console).
3. Click **delete** in the dashboard. Audit log records the deletion.
4. Create a fresh key with the new token via the **Add API key** form.
5. Health monitor will probe the new key within 10 min and mark it alive.

## Project key leak response

If `X-Project-Key` leaks:

1. `POST /admin/projects` to create a replacement project with the same scopes.
2. Update the client app's `BROKER_PROJECT_KEY` env, redeploy.
3. The old project's `is_active` set to `false` (no API endpoint yet — use psql).
4. Audit log will show all `vend` ops by the leaked project — review for suspicious activity.

## What's NOT covered

- **No rate limiting** on `/v1/jobs`/`/v1/embed` (proxy mode) per project beyond cost caps. A buggy client can hammer the broker; you'll see it in `calls_1h` on the dashboard.
- **No mTLS** between projects and broker. We rely on `X-Project-Key` over TLS to CF, then HTTP from CF to origin.
- **No KMS** — `TOKEN_SECRET` is on disk. If someone roots the box, all keys can be decrypted.
- **No PII redaction** in audit_log. Today we don't log message bodies — but if that changes, redact first.

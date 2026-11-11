# Subscription logins: health, one-shot alerts, dashboard re-login

Added 2026-11-11. Covers the three subscription worker slots in `ACCOUNT_CHOICES`
(`agent/account_choices.py`): `claude-sub/owner`, `claude-sub/claude-2` (Claude Code) and
`codex-sub/owner` (Codex). Before this, a dropped login showed up only as failing runs
(e.g. `claude-2`: `billing_mode_refused: CLI did not positively report subscription mode`)
and the only fix was SSH plus an interactive `/login` inside the container.

## What the owner sees

- **Dashboard → Subscriptions** (`/dashboard/subscriptions`): one card per slot with its
  name, app, mailbox, plan, worker slot, when it was last checked and a status chip
  (Working / Not signed in / Login broken / Worker silent / No data yet), plus a
  **Sign in again** button.
- **Telegram**: one message when a login drops, one when it works again. Nothing repeats
  while the state stays the same.

### Names

`display_name()` (`agent/login_identity.py`) builds `CLD_XXXX` for Claude Code and
`GPT_XXXX` for Codex, where `XXXX` is the first four letters/digits of the mailbox login,
upper-cased: `zaporozec_d@itstep.org` → `CLD_ZAPO` / `GPT_ZAPO`. The name follows the
mailbox the CLI itself reports, so signing a slot in with another account renames it on the
next check. A slot that never reported a mailbox shows `CLD_<worker id>` (e.g.
`CLD_claude-2`). This is a display name only: the stored account id (`owner`, `claude-2`)
is unchanged, because runs, resumable sessions, project policies (`claude-sub/owner`) and
container env (`AGENT_ACCOUNT_ID`) all key on it.

## How health is measured

Each subscription worker runs `login_loop()` (`agent/login_worker.py`) next to its job loop.
`worker_login_identity()` resolves the worker's own slot: the provider from its single
capability, the account from `AGENT_ACCOUNT_ID` (the Codex worker has none and serves
`owner`). Every `AGENT_LOGIN_PROBE_S` (300 s) `login_step()` asks the CLI:

- Claude: `probe_claude()` runs `claude auth status`; `parse_claude_status()` reads
  `loggedIn`, `authMethod`, `email`, `subscriptionType`, `orgName`. Only
  `authMethod == "claude.ai"` counts as a subscription login (an API-key login does not).
- Codex: `probe_codex()` asks `codex app-server` the official `account/read`;
  `parse_codex_account()` accepts `{"account": {"type": "chatgpt", "email", "planType"}}`.
  The mailbox comes from the CLI, never from `auth.json`.

Both use the same minimal environment as a real run (`cli_env()`, `codex_env()`; no DB URL,
no broker secret) and the run's file credential store (`codex_overrides()`).
`probe()` dispatches by provider; a probe that hangs past 40 s reports "timed out".
Every parser returns a `LoginReport` (`logged_in`, `email`, `plan`, `detail`); CLI output is
passed through `strip_ansi()` first, because Codex colours its output. A probe that cannot
ask the CLI at all (timeout, no answer) returns `failed=True`; `record_status()` then writes
nothing, so the row ages into `stale` and never sends a false "dropped" alert.
`kill_group()` ends a probe or a sign-in by killing the CLI's whole process group:
`codex app-server` starts helpers that inherit our pipes, and killing only the main pid
left `wait()` hanging, so every Codex probe hit the 40 s timeout although the answer came
in under a second (prod 2026-11-11).

`record_status()` (`agent/login_status.py`) upserts `agent_login_status`
(`AgentLoginStatusRow`, migration 027; primary key `(provider, account_id)`).
The last known mailbox survives a logout (a logged-out CLI reports none) so the alert can
still name it; `changed_at` moves only when `logged_in` flips. `known_email()` reads it back
to pre-fill the next sign-in page.

### Verdict

`verdict()` turns a row plus run history into one state, and both the page and the alert use
`account_logins()`, which applies it to every slot, so they can never disagree:

| State | Rule |
|---|---|
| `unknown` | the worker has never reported |
| `stale` | no report for 20 min (`STALE_AFTER`, four missed probes) |
| `logged_out` | the CLI says it is not signed in |
| `broken` | signed in, but the newest run that failed with an account-down code (`ACCOUNT_DOWN_CODES`: `auth_refresh_failed`, `subscription_disabled`) is newer than both the last completed run and the last login change |
| `ok` | otherwise |

A later successful run or a fresh sign-in clears `broken`. Ordinary failures (bad JSON,
quota waits) never count. `AccountLogin` is the per-slot result.

## One-shot Telegram alerts

`check_subscription_logins()` (`agent/login_alerts.py`) runs on every monitor tick. Per slot
the key is `sub_login:<provider>/<account>`:

- `logged_out` / `broken` → `alert(..., throttle_min=10 years)`: sent once, then suppressed
  for as long as the notifier state file exists.
- `ok` → `recover(...)`: sent only if that state file exists, and deletes it, so the next drop
  alerts again.
- `stale` / `unknown` → nothing either way (a restarting worker must not cause a flap).

`down_message()` / `up_message()` name the slot, the app, the mailbox and link the
Subscriptions page:

```
⚠️ aibroker
🔌 Подписка отвалилась: CLD_DEMO
Приложение: Claude Code (claude-sub/claude-2)
Ящик: demoniwwwe@gmail.com
Админка: https://aib.zapleo.com/dashboard/subscriptions
Причина: сессия не залогинена
```

## Re-login from the dashboard

The api never runs a CLI or touches a credential: the dashboard opens a row in
`agent_login_sessions` (`AgentLoginSessionRow`, migration 027) and the slot's own worker carries it out.
Every transition is a compare-and-swap on `state` (`login_sessions.py`), so a cancel can never
race a code submit:

`requested` → `starting` → `awaiting_user` → (`code_submitted` → `verifying`) →
`done` | `failed` | `expired` | `cancelled`

| Step | Who | Function |
|---|---|---|
| **Sign in again** | dashboard | `subscription_login_start()` → `start_session()` (cancels any unfinished one of the slot) |
| claim | worker | `claim_requested()` |
| run the CLI | worker | `run_login()` (`agent/login_flow.py`) |
| show link / code | dashboard | `subscriptions_page()`, `subscription_login_panel()` with `panel_view()` |
| paste the Claude code | dashboard | `subscription_login_code()` → `submit_code()` |
| hand the code over | worker | `take_code()` |
| cancel | dashboard | `subscription_login_cancel()` → `cancel_session()` |
| result | worker | `finish()`, then a fresh probe and `record_status()` |

Other helpers: `latest_session()`, `get_login()`, `advance()`, and `expire_orphans()`, which at
worker start-up expires sessions a previous process was running.

**Claude** — `claude auth login --claudeai [--email <last known mailbox>]` prints a sign-in
link (`parse_login_url()` accepts only `claude.com`, `claude.ai` and `auth.openai.com` https
links) and waits for the code on stdin. The owner opens the link, signs in to the account the
slot should use, and pastes the code into the dashboard. The panel stops polling while the
code box is open, so a refresh never wipes a half-pasted code.

**Codex** — `codex login` (browser flow, NOT `--device-auth`, which needs device-code sign-in
enabled in the ChatGPT security settings first) prints a sign-in link and listens on
`127.0.0.1:1455` inside the worker. The owner signs in; the browser is then sent to
`http://127.0.0.1:1455/auth/callback?code=...` and cannot open it - expected. The owner
pastes that full address; `codex_callback()` accepts only http on `127.0.0.1`/`localhost`
port 1455 with the callback path and a `code`, and rebuilds the address from its query, so a
pasted line can never point the worker at another host. The worker replays it locally and
the CLI finishes.

The Codex sign-in runs in a STAGING home (`$CODEX_HOME/.login-<session>`), and the new
`auth.json` replaces the live one (atomic rename, mode 600) only after the CLI succeeded.
Running `codex login` straight in the live home deleted the working login as soon as the
sign-in started (prod 2026-11-11 16:48: a cancelled re-login left Codex logged out). A
cancelled, expired or failed sign-in now leaves the working login untouched; the staging
home is always removed.

Time limits: the link must appear within 45 s, the Claude code within
`AGENT_LOGIN_CODE_WAIT_S` (600 s) for both apps, the CLI must exit
within 120 s of receiving the code. Each outcome ends in a terminal state with a message;
the CLI output tail is shown on failure with the code replaced by `***`.

### Why the pasted line is sealed, not Fernet-encrypted

Workers deliberately run with a placeholder `TOKEN_SECRET` (docker-compose.yml): a compromised
worker must not be able to decrypt the API keys. So the code is sealed per login instead
(`agent/login_seal.py`): the worker makes a fresh RSA-2048 key with `new_key()` and publishes
only `public_pem()` on the session row; `submit_code()` encrypts the pasted code with `seal()`
(a one-time AES-256-GCM key encrypts the text, RSA-OAEP-SHA256 wraps that key, because a
Codex callback address is longer than plain RSA can carry; up to 4096 bytes); the worker
opens it with `unseal()`. The private key exists only in that worker
process's memory, so neither the database nor its readers can recover the code. `take_code()`
clears `code_sealed` in the same update that hands it over, and `finish()` clears the public
key and any sealed code. The code is never logged or audited; the audit log
records only `subscription.relogin`, `subscription.relogin.code` (with `ok=`) and
`subscription.relogin.cancel`.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_LOGIN_ENABLED` | `true` | `false` stops the worker's probe and re-login loop |
| `AGENT_LOGIN_PROBE_S` | `300` | how often each worker checks its login |
| `AGENT_LOGIN_CODE_WAIT_S` | `600` | how long a Claude sign-in waits for the pasted code |

## Deploy

Apply migration 027 **before** the new images start (the workers write the new tables):

```sh
docker exec -i aibroker-postgres psql -v ON_ERROR_STOP=1 -U aibroker aibroker \
  < /var/www/aibroker/infra/sql/migrations/027_agent_logins.sql
```

Then rebuild/restart api, monitor and the three `agent-worker*` services (compose profile
`agent`). Within one probe interval every slot shows a state; a slot that is already logged
out sends its one "dropped" message at the first monitor tick after that.

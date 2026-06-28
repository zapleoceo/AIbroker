# API reference

Base URL (production): `https://aib.zapleo.com`

OpenAPI live: [`GET /docs`](https://aib.zapleo.com/docs)

## Public (no auth)

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Public bilingual (EN/RU) landing page — product overview, OG/Twitter/Schema.org metadata |
| `GET` | `/robots.txt` | Crawler policy — index everything except `/admin/`, `/dashboard`, `/api/` |
| `GET` | `/sitemap.xml` | XML sitemap with hreflang EN/RU alternates |
| `GET` | `/llms.txt` | LLM-friendly site descriptor (Jeremy Howard proposal) — markdown summary for Perplexity / ChatGPT browse / Claude search |
| `GET` | `/healthz` | `{ok: true, service, ts}` — liveness probe |
| `GET` | `/v1/health` | Per-provider alive/cooldown/dead/total counts |
| `GET` | `/login` | Telegram Login Widget for `/dashboard` |
| `GET` | `/api/tg_login` | TG widget callback — sets HMAC cookie, redirects to `/dashboard` |
| `GET` | `/logout` | Clears session cookie |

## Client (X-Project-Key required)

| Method | Path | Body | Returns |
|---|---|---|---|
| `POST` | `/v1/chat?capability=<cap>` | `ChatRequest` | `ChatResponse` |
| `POST` | `/v1/embed?provider=<p>` | `EmbedRequest` | `EmbedResponse` |
| `POST` | `/v1/key` | `KeyRequest` | `KeyResponse` (lease + plaintext key) |
| `POST` | `/v1/usage` | `UsageReport` | `{recorded: true}` |
| `POST` | `/v1/release` | `{lease_id}` | `{released: bool}` |

### Capabilities for `/v1/chat`

`chat:fast`, `chat:smart`, `chat:code`, `prefilter`, `structured`, `vision`.

See [routing.md](./routing.md) for the chain per capability.

### Scopes a project must hold

| Endpoint | Required scope |
|---|---|
| `/v1/chat` | `llm:chat` |
| `/v1/embed` | `llm:embed` |
| `/v1/key` | the scope passed in the body |

## Admin (X-Admin-Key required)

| Method | Path | Description |
|---|---|---|
| `POST` | `/admin/projects` | Create project — returns one-time `project_key` |
| `GET` | `/admin/projects` | List all projects |
| `POST` | `/admin/keys` | Create OR upsert an API key (encrypted at rest) |
| `GET` | `/admin/keys?provider=…` | List keys, optional provider filter |
| `POST` | `/admin/keys/{id}/disable` | Soft-disable |
| `DELETE` | `/admin/keys/{id}` | Hard delete |

## Dashboard (cookie OR X-Admin-Key)

| Method | Path | Description |
|---|---|---|
| `GET` | `/dashboard?from=&to=` | Inventory + range-driven KPIs (spend/calls/tokens for the chosen date range), sortable tables with TOTAL footers, inline edit. `from`/`to` default to today. |
| `POST` | `/dashboard/keys/create` | HTML form: add or upsert key |
| `POST` | `/dashboard/keys/{id}/edit` | HTML form: rename, change tier/scope/cap, rotate token |
| `POST` | `/dashboard/keys/{id}/disable` | Toggle active |
| `POST` | `/dashboard/keys/{id}/delete` | Hard delete (confirm prompt) |
| `POST` | `/dashboard/projects/create` | HTML form handler |
| `POST` | `/dashboard/projects/{id}/edit` | HTML form: rename, change scopes/cap/email |
| `GET` | `/dashboard/projects/{id}?range=24h\|7d\|30d` | Drill-down — per-project KPI cards, breakdown by provider/capability/model/status, last 50 calls. Range pill swaps the window. |
| `POST` | `/dashboard/keys/{id}/delete` | Confirmed delete |
| `POST` | `/dashboard/projects/create` | Form — shows one-time key in flash |

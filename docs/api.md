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
| `GET` | `/favicon.svg` | Brand favicon (hub-and-spokes, brand colours). Cache 24h. |
| `GET` | `/favicon.ico` | Same SVG served at the legacy default path — keeps dev consoles 404-free. |
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
| `POST` | `/v1/transcribe` | multipart `file` | `TranscribeResponse` |
| `POST` | `/v1/key` | `KeyRequest` | `KeyResponse` (lease + plaintext key). `429` if the project exceeds `VENDING_RATE_LIMIT_PER_MINUTE` (default 30/min) — see **Threat model** in [security.md](security.md). |
| `POST` | `/v1/usage` | `UsageReport` | `{recorded: true, request_id}` |
| `POST` | `/v1/release` | `{lease_id}` | `{released: bool}` |

### Capabilities for `/v1/chat`

`chat:fast`, `chat:smart`, `chat:code`, `chat:edit`, `prefilter`,
`structured`, `translate`, `vision`.

`translate` routes to small fast non-reasoning models first
(mistral-small → gemini-flash → cohere-r7b → groq), tuned for the "translate,
don't answer" task under a tight client timeout. Identical translate requests
are served from an in-process exact-match cache (24h TTL) — repeated phrases
skip the LLM entirely (`provider="cache"` in the response).

**For structured/JSON output, send a full `json_schema`, not a bare
`json_object`.** With `response_format={"type":"json_schema","json_schema":
{"name":…, "strict":true, "schema":{…}}}` the schema-capable providers (gemini,
openai, groq) grammar-constrain generation, so the model **cannot** return
invalid JSON — this is the root-cause fix for the `InvalidJSON` failures, far
better than the broker's post-hoc JSON validation. The broker forwards the
schema unchanged; providers that don't support it (cerebras/cohere) are
automatically deprioritized for JSON requests.

`vision` accepts OpenAI-style multimodal `content`: a `ChatMessage.content`
may be a plain string **or** a list of blocks, e.g.
`[{"type":"text","text":"что на фото?"}, {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,…"}}]`.
LiteLLM forwards both shapes to vision-capable models (gemini → openai). Pass
images as base64 data URLs — anthropic was removed from the vision chain because
it 400s on fetch-gated image URLs.

`ChatResponse` carries `cache_read_tokens` / `cache_write_tokens` (0 unless
the call routed through anthropic and hit its prompt cache — see
[providers.md](providers.md#prompt-caching-2026-07-01-wired-end-to-end-2026-07-02))
and `request_id` (the `usage_log` row id — match your own logs against the
broker's).

### `/v1/embed?provider=<p>` (default `voyage`)

The broker retries **up to 5 keys of the same provider** on failure
(2026-07-02) before returning `502`. It does **not** fall back to a different
provider — `voyage-3` and `cohere embed-english-v3` are different vector
spaces, and silently switching mid-batch would poison a vector index with
incomparable embeddings. `provider` is your explicit choice; the broker only
rotates keys within it. If you need a specific fallback provider, call
`/v1/embed?provider=cohere` yourself and re-embed the affected batch — don't
mix vectors from two providers in one index.

### `/v1/transcribe` (audio → text)

Multipart upload, field name `file` (≤25 MB — Whisper's limit). Optional
`?workflow=` query tag. Chain: `groq` whisper-large-v3-turbo (free) →
`openai` whisper-1. Returns
`{text, provider, model, cost_usd, latency_ms, key_label, request_id}`.

### `request_id` — correlating a call across both sides

`ChatResponse`/`EmbedResponse`/`TranscribeResponse` and `/v1/usage`'s reply
all carry `request_id` — the `usage_log.id` for that exact call. Log it on
your side (Stepan/Vera); if a call misbehaves, quote it back to us and we can
look the row up directly (`/dashboard/projects/{id}` — the "Recent 50 calls"
table's leading `req id` column, sortable, also usable as a search target)
instead of grepping timestamps against provider/model/workflow.

### Scopes a project must hold

| Endpoint | Required scope |
|---|---|
| `/v1/chat` (chat:*) | `llm:chat` |
| `/v1/chat?capability=vision` | `llm:vision` |
| `/v1/embed` | `llm:embed` |
| `/v1/transcribe` | `llm:audio` |
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

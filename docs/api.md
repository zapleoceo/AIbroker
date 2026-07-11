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
| `GET` | `/v1/health` | Per-provider alive/cooldown/dead/total counts — content-negotiated (see below) |
| `GET` | `/login` | Telegram Login Widget for `/dashboard` |
| `GET` | `/api/tg_login` | TG widget callback — sets HMAC cookie, redirects to `/dashboard` |
| `GET` | `/logout` | Clears session cookie |

### `/v1/health` — content negotiation (2026-07-11)

Same public endpoint, two representations, chosen by `Accept`:

- No `Accept` header, `Accept: */*`, or any non-HTML accept (curl, scripts,
  uptime monitors — matches the TestClient default) → the original
  `{"providers": [{"provider", "alive", "cooldown", "dead", "total"}, …]}`
  JSON, unchanged. This is the documented, stable machine-readable contract —
  anything already polling it keeps working with zero code change.
- `Accept: text/html` (a browser, e.g. clicking the dashboard nav link) →
  a small bilingual EN/RU status page: a stacked green/alive · yellow/cooldown
  · red/dead bar per provider, plus top-line totals. No auth, no spend/usage
  data (this endpoint never carried that) — safe to stay public.

`routes/health.py`: `_fetch_provider_health()` is the single data fetch both
representations render from; `_render_health_html()` / `_health_provider_card()`
build the page (reuses `landing.py`'s dark-theme CSS variables + lang-toggle
JS for visual consistency with the rest of the public site). Both paths send
`Cache-Control: no-store` — this reflects live key state (monitor ticks,
adaptive cooldowns), so a CDN/browser must never cache a snapshot.

## Client (X-Project-Key required)

| Method | Path | Body | Returns |
|---|---|---|---|
| `POST` | `/v1/chat?capability=<cap>` | — | **`410 Gone`** — sync chat removed 2026-07-10; use `/v1/jobs` |
| `POST` | `/v1/jobs?capability=<cap>` | `ChatRequest` | `JobSubmitResponse` (async — `202` + `job_id`). **The way to do chat.** |
| `GET` | `/v1/jobs/{job_id}` | — | `JobResponse` (poll: `pending`\|`done`\|`error`) |
| `POST` | `/v1/deep` | `DeepRequest` | `DeepSubmitResponse` — **alias** for `/v1/jobs?capability=chat:deep` (backward-compat) |
| `GET` | `/v1/deep/{job_id}` | — | `JobResponse` — alias for `/v1/jobs/{job_id}` |
| `POST` | `/v1/embed?provider=<p>` | `EmbedRequest` | `EmbedResponse` (**sync — stays sync**, see below) |
| `POST` | `/v1/transcribe` | multipart `file` | `TranscribeResponse` (**sync — stays sync**) |
| `POST` | `/v1/key` | `KeyRequest` | `KeyResponse` (lease + plaintext key). `429` if the project exceeds `VENDING_RATE_LIMIT_PER_MINUTE` (default 30/min) — see **Threat model** in [security.md](security.md). |
| `POST` | `/v1/usage` | `UsageReport` | `{recorded: true, request_id}` |
| `POST` | `/v1/release` | `{lease_id}` | `{released: bool}` |

### Chat is async-only (2026-07-10)

**Sync `POST /v1/chat` was removed — it returns `410 Gone`.** Do all chat via
the async job API (`POST /v1/jobs?capability=X` → poll `GET /v1/jobs/{id}`, see
below). A synchronous chat call could 504 through the proxy read-timeout before
the fallback chain finished; the job queue has no such ceiling and exhaustively
rotates keys. `embed`/`transcribe` **stay synchronous** — they're fast (~1s),
never hit that timeout, and routing them through submit/poll would only add
latency for no benefit.

### Capabilities (for `/v1/jobs`)

`chat:fast`, `chat:smart`, `chat:code`, `chat:edit`, `chat:deep`, `prefilter`,
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

A completed chat `JobResponse` carries `cache_read_tokens` / `cache_write_tokens` (0 unless
the call routed through anthropic and hit its prompt cache — see
[providers.md](providers.md#prompt-caching-2026-07-01-wired-end-to-end-2026-07-02))
and `request_id` (the `usage_log` row id — match your own logs against the
broker's).

### Async jobs — `/v1/jobs` (submit + poll)

Same request body as `/v1/chat` (incl. `response_format`), but the broker
**never holds the connection**: it returns `202` with a `job_id` immediately,
runs the call in the background, and you **poll** `GET /v1/jobs/{job_id}` until
`status` is `done` or `error`. Available for every chat capability
(`chat:fast`/`smart`/`code`/`edit`/`deep`, `structured`, `prefilter`,
`translate`, `vision`) — `embedding`/`transcription` stay sync-only (fast, no
held-connection problem to solve).

**Why migrate off sync `/v1/chat` onto this:** a synchronous call is bounded by
your client read timeout and the broker's own nginx/Cloudflare read timeout
(~60–120s). A slow/oversubscribed provider can 504 you *before* the broker has
finished walking its fallback chain. The async job has no such ceiling — the
broker can exhaustively rotate every available key and you still get the answer
when you next poll. **Sync `/v1/chat` is gone (`410 Gone`)** — the job API is
the only way to do chat.

```
POST /v1/jobs?capability=chat:smart
  → 202 {"job_id": 123, "status": "pending",
         "poll_url": "/v1/jobs/123", "poll_after_s": 2}

GET /v1/jobs/123
  → 200 {"job_id":123,"status":"pending","poll_after_s":2}      # keep polling
  → 200 {"job_id":123,"status":"done","text":"…","provider":…,  # done
         "tokens_in":…,"tokens_out":…,"cost_usd":…,"request_id":…}
  → 200 {"job_id":123,"status":"error","error":"…"}             # failed
```

`poll_after_s` is the broker's suggested wait before the next poll (widens for
long jobs). A job belongs to exactly one project — polling someone else's
`job_id` is a `404`. Poll is a pure read; the dispatcher owns the lifecycle —
a job whose worker died mid-run sits in `running` past the stale window and is
re-queued by the next tick (so a deploy delays answers, never drops them), and
a job with no capacity is re-queued with backoff until it succeeds or gives up
after the retry cap. `chat:deep` is **async-only** (nemotron runs minutes);
`POST /v1/chat` returns `410 Gone` for every capability. `POST /v1/deep` +
`GET /v1/deep/{job_id}` remain as backward-compatible aliases of the generic
endpoints.

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

A completed chat `JobResponse`, `EmbedResponse`/`TranscribeResponse`, and `/v1/usage`'s reply
all carry `request_id` — the `usage_log.id` for that exact call. Log it on
your side (Stepan/Vera); if a call misbehaves, quote it back to us and we can
look the row up directly (`/dashboard/projects/{id}` — the "Recent 50 calls"
table's leading `req id` column, sortable, also usable as a search target)
instead of grepping timestamps against provider/model/workflow.

### Scopes a project must hold

| Endpoint | Required scope |
|---|---|
| `/v1/jobs?capability=chat:*` | `llm:chat` |
| `/v1/jobs?capability=vision` | `llm:vision` |
| `/v1/jobs?capability=<cap>` | scope per capability (`chat:*`→`llm:chat`, `vision`→`llm:vision`, `chat:deep`→`llm:deep`) |
| `/v1/deep` | `llm:deep` |
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
| `GET` | `/dashboard/projects/{id}?range=1h\|4h\|12h\|24h\|7d\|30d` | Drill-down — per-project KPI cards, breakdown by provider/capability/model/status, last 50 calls. Range pill swaps the window. |
| `POST` | `/dashboard/keys/{id}/delete` | Confirmed delete |
| `POST` | `/dashboard/projects/create` | Form — shows one-time key in flash |

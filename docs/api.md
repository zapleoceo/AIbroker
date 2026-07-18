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
don't answer" task under a tight client timeout. Identical `translate` and
`prefilter` requests are served from an in-process exact-match response
cache (`services/response_cache.py`) — repeated inputs skip the LLM
entirely (`provider="cache"` in the response). TTL is per-capability:
24h for `translate` (a phrase's translation is stable), 10 min for
`prefilter` (kept short so a prompt/threshold change rolls through
quickly). Chat capabilities are never cached.

### Request bounds (2026-07-16)

`max_tokens` and `temperature` are validated at submit — out-of-range
values return `422`:

- chat (`ChatRequest`, every `/v1/jobs` capability): `max_tokens`
  1..16384 (default 1024), `temperature` 0..2 (default 0.7).
- deep (`DeepRequest`, the `/v1/deep` alias): `max_tokens` 1..32768
  (default 4096) — the deep lane legitimately generates long answers.

Rationale: an oversized `max_tokens` inflates the cost-guard's worst-case
reservation estimate and silently knocks every capped paid key out of the
chain — the paid tail vanishes and the request 503s with keys sitting
idle.

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

**Budget-cap error is honest + terminal (2026-07-16).** When a job fails
because the project's (or the global) daily cost cap is spent, the `error`
field reads exactly `daily budget cap reached — retry after 00:00 UTC` — NOT
the generic `no provider available`. This is a **terminal** `error` (the broker
burns no further retries — more retries can't create budget), so the client
contract is: on that message, **stop resubmitting and retry after the next UTC
midnight**, when daily caps reset. The owner also gets a 24h-throttled alert, so
a silently-capped project is visible rather than invisibly stalled.

**In-flight dedup (2026-07-16, migration 010).** An identical
`POST /v1/jobs` payload (same project, capability, and canonical request
body) submitted within **30 minutes** while the prior job is still
`pending` or `running` returns the SAME `job_id` — the client's resubmit
storm collapses onto one job it just keeps polling. A `done`/`error` job
never dedups: after a failure a resubmit legitimately means "retry".
Best-effort, not a uniqueness constraint (two truly simultaneous identical
submits can still both insert). Client contract: resubmitting is harmless —
you'll get back the in-flight `job_id`; just poll it.

`poll_after_s` is the broker's suggested wait before the next poll (widens for
long jobs). A job belongs to exactly one project — polling someone else's
`job_id` is a `404`. Poll is a pure read; the dispatcher owns the lifecycle —
a job whose worker died mid-run sits in `running` past the stale window and is
re-queued by the next tick (so a deploy delays answers, never drops them), and
a job with no capacity is re-queued with backoff until it succeeds or gives up
after the retry cap. The job's **final retry escalates to paid-tier keys only**
(`paid_only`, 2026-07-16) — the last attempt may be billed even when free
capacity would eventually recover, so a job never dies while a paid key has
budget. `chat:deep` is **async-only** (nemotron runs minutes);
`POST /v1/chat` returns `410 Gone` for every capability. `POST /v1/deep` +
`GET /v1/deep/{job_id}` remain as backward-compatible aliases of the generic
endpoints.

### `/v1/embed?provider=<p>` (default `voyage`)

The broker retries **up to 5 keys of the same provider** on failure
(2026-07-02) before returning `502`. It does **not** fall back to a different
provider — `voyage-4` and `cohere embed-english-v3` are different vector
spaces, and silently switching mid-batch would poison a vector index with
incomparable embeddings. `provider` is your explicit choice; the broker only
rotates keys within it. If you need a specific fallback provider, call
`/v1/embed?provider=cohere` yourself and re-embed the affected batch — don't
mix vectors from two providers in one index.

### `/v1/transcribe` (audio → text)

Multipart upload, field name `file` (≤25 MB — Whisper's limit). Optional
`?workflow=` query tag. Chain: `local` (self-hosted faster-whisper, see
below) → `groq` whisper-large-v3-turbo (free) → `gemini` (chat-based audio,
separate quota) → `openai` whisper-1. Returns
`{text, provider, model, cost_usd, latency_ms, key_label, request_id}`.

#### `local` — self-hosted faster-whisper (2026-07-18, moved in-repo)

Chain-first, always tried before any external provider — free, private, no
external rate limit. Backed by this repo's own `services/asr-local`
(`faster-whisper large-v3-turbo`, int8, CPU, `beam_size=5`) — its own
`docker-compose.yml` service (`aibroker-asr-local`), on the same compose
network as `api`, no cross-project dependency. (2026-07-18 history: this originally lived in
vera3's own compose stack, reached over a cross-project network join —
a same-day vera3 refactor deleted that service entirely, since from vera3's
side "voice/audio now goes through the broker" made its own copy look
redundant. It wasn't: the broker's `local` provider was only ever a proxy to
that same container, not its own model — deleting the one real model host
took the feature down broker-wide too. Moved in-repo so the service the
broker's routing depends on can't be an casualty of an unrelated project's
cleanup again.)

Reached over plain HTTP via `_transcribe_via_local_asr` / `_post_local_asr`
in `providers/litellm_adapter.py` — not through LiteLLM, since it isn't an
LLM SDK-compatible endpoint. Always requests `language=auto`: broker callers
are multi-tenant (e.g. Stepan2's mostly-Bahasa leads), so a single fixed
default language would be wrong for most of them.

Configured via `ASR_LOCAL_URL` (empty = disabled, every request falls
straight through to groq/gemini/openai) and `ASR_LOCAL_TIMEOUT_S` (default
180s — asr-local serializes every call behind a single lock on 1 CPU thread,
so a request can queue behind another one already in flight). A downed or
slow-past-timeout local service raises `TimeoutError`, which
`classify_provider_error` cools down like any other rate limit — so it
degrades to the external chain instead of being retried every call with no
backoff.

**Model (2026-07-18: `small` -> `large-v3-turbo`).** Real volume is low
(~10 req/day, no backfill), so the model's fixed RAM cost — not decode
throughput — was the only real constraint; 1 CPU thread stays the ceiling
either way. turbo keeps large-v3's encoder (the part that drives multilingual
accuracy — Stepan2's leads are mostly Bahasa) with its decoder pruned
32->4 layers, and fits comfortably under the container's 1.5GB `mem_limit` at
int8. `beam_size` bumped 1->5 alongside it — slower per call, which a
low-volume queue can afford, meaningfully better accuracy than greedy
decoding. Roll back to `small` via the `WHISPER_MODEL` env var if the host
ever gets memory-tight.

**Correction pass (2026-07-18).** Even with the larger model, every
successful `local` transcript is still proofread by one `chat:fast` call
(`services/llm_service._correct_local_transcript`) before it's returned —
fixes misheard words/punctuation, never translates or changes meaning. Cheap
insurance on top of the model upgrade, not a substitute for it. Best-effort:
if the correction call has no available provider, hits the project/global
budget cap, or raises, the raw local transcript is returned unchanged rather
than losing a working answer. Tagged `workflow=<caller's workflow>+asr-correct`
(or bare `asr-correct` when the caller sent none) in `usage_log`, so it's
visible as its own line in the dashboard's per-project workflow breakdown,
not folded into the caller's own tag. Only applied to the `local` provider —
groq/gemini/openai's transcripts already come from full-size hosted models.

### `request_id` — correlating a call across both sides

A completed chat `JobResponse` and `EmbedResponse`/`TranscribeResponse`
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

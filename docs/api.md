# API reference

Native agent tool calls: see [native tools contract](native-tools.md) for optional
`tools`/`tool_choice`, tool-result history, validation and rollout limitations.

Base URL (production): `https://aib.zapleo.com`

OpenAPI live: [`GET /docs`](https://aib.zapleo.com/docs). Dashboard, admin and
login routes are excluded from the public `/openapi.json` (2026-10-02).

## Quick start: self-signup

Any service can get itself a working key with one unauthenticated call, no approval:

```bash
curl -X POST https://aib.zapleo.com/v1/signup \
  -H "Content-Type: application/json" \
  -d '{"name": "my-service", "contact": "me@example.com", "purpose": "connect + test"}'
```

```json
{
  "name": "my-service",
  "project_key": "aib_prj_...",
  "limits": {"daily_cost_cap_usd": 0.0, "total_request_cap": 100,
             "scopes": ["llm:chat", "llm:embed"]},
  "docs_url": "https://aib.zapleo.com/docs",
  "upgrade": "Need more? Contact the owner with your project name to raise the limits."
}
```

Send `project_key` as `X-Project-Key` on every other call. **It is shown once** - store it.

- `name` (required) is normalised to a lowercase slug (`My Bot!` -> `my-bot`, 2-60 chars). It must
  be unique: on a clash a `-<4 hex>` suffix is appended, so always read the returned `name`.
  `contact` (email or handle) and `purpose` are optional and only help the owner decide on upgrades.
- **Limits.** `daily_cost_cap_usd = 0` means *free providers only* (a paid call is refused; `0` is
  not "unlimited" - `NULL` is). `total_request_cap = 100` is a lifetime count of **client requests**:
  one per `POST` to `/v1/jobs`, `/v1/deep`, `/v1/embed`, `/v1/decisions`, `/v1/transcribe` and
  `/v1/transcribe/jobs`, however many providers the broker tries behind it. Polling
  (`GET /v1/jobs/{id}`), `/v1/models` and requests rejected for scope or body errors are free.
  A request counts once admitted, even if the provider then fails. Default scopes are `llm:chat` and
  `llm:embed` (`SIGNUP_DEFAULT_SCOPES`).
- **Errors.**

| Status | `error` | Meaning |
|---|---|---|
| `422` | `invalid_name` | the name has fewer than 2 usable characters (`a-z`, `0-9`) |
| `403` | `signup_disabled` | the owner turned self-signup off (`SIGNUP_ENABLED=false`) |
| `429` | `signup_rate_limited` | too many signups: `scope` is `ip` (default 3/day per address) or `global` (default 50/day); `Retry-After` is set |
| `429` | `request_cap_exhausted` | on any later call: the project spent its lifetime allowance, body `{"error": "request_cap_exhausted", "limit": 100, "used": 100, "message": "..."}` |

- **More limits.** Message the owner with the project `name`; they raise `total_request_cap` and the
  daily cost cap in the dashboard (project -> Settings; a blank request cap = unlimited). Projects
  created before this feature have no request cap.

## Public (no auth)

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/signup` | Self-signup: returns a restricted project key once ($0/day, 100 requests total) - see [Quick start](#quick-start-self-signup) |
| `GET` | `/` | Public bilingual (EN/RU) landing page — product overview, OG/Twitter/Schema.org metadata |
| `GET` | `/robots.txt` | Crawler policy — index everything except `/admin/`, `/dashboard`, `/api/` |
| `GET` | `/sitemap.xml` | XML sitemap with hreflang EN/RU alternates |
| `GET` | `/llms.txt` | LLM-friendly site descriptor (Jeremy Howard proposal) — markdown summary for Perplexity / ChatGPT browse / Claude search |
| `GET` | `/favicon.svg` | Brand favicon (hub-and-spokes, brand colours). Cache 24h. |
| `GET` | `/favicon.ico` | Same SVG served at the legacy default path — keeps dev consoles 404-free. |
| `GET` | `/healthz` | `{ok: true, service, ts}` — liveness probe |
| `GET` | `/v1/health` | alive/cooldown/dead/total counts — one aggregate row for anonymous callers, per-provider rows with `X-Admin-Key` or the owner session; content-negotiated (see below) |
| `GET` | `/login` | Telegram Login Widget for `/dashboard` |
| `GET` | `/api/tg_login` | TG widget callback — sets HMAC cookie, redirects to `/dashboard` |
| `POST` | `/logout` | Clears session cookie, redirects to `/login` |
| `GET` | `/logout` | Does NOT log out (2026-10-02, so a stray `<img src=/logout>` cannot sign the owner out) — redirects to `/dashboard` |
| `GET` | `/dashboard/assets.css`, `/dashboard/assets.js` | Dashboard CSS/JS, public, versioned by content hash (`?v=`), cached for a year |

### `/v1/health` — content negotiation (2026-07-11)

Same public endpoint, two representations, chosen by `Accept`:

- No `Accept` header, `Accept: */*`, or any non-HTML accept (curl, scripts,
  uptime monitors — matches the TestClient default) → the original
  `{"providers": [{"provider", "alive", "cooldown", "dead", "total"}, …],
  "detail": bool}` JSON. Same row shape as ever; `detail` was added 2026-10-02.
- `Accept: text/html` (a browser, e.g. clicking the dashboard nav link) →
  a small bilingual EN/RU status page: a stacked green/alive · yellow/cooldown
  · red/dead bar per provider, plus top-line totals. No spend/usage
  data (this endpoint never carried that); anonymous callers see the aggregate
  row only.

**Anonymous vs privileged (2026-10-02).** An anonymous caller gets ONE
aggregate row (`"provider": "all"`) and `"detail": false` — the public
endpoint no longer lists which providers the broker uses or how many keys each
holds. Per-provider rows (`"detail": true`) need `X-Admin-Key` or the owner
dashboard session. Both the JSON and the HTML representation follow this rule.

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
| `POST` | `/v1/transcribe/jobs` | multipart `file` | `JobSubmitResponse` (async — `202` + `job_id`, poll `GET /v1/jobs/{id}`) |
| `GET` | `/v1/deep/{job_id}` | — | `JobResponse` — alias for `/v1/jobs/{job_id}` |
| `POST` | `/v1/embed?provider=<p>` | `EmbedRequest` | `EmbedResponse` (**sync — stays sync**, see below) |
| `POST` | `/v1/transcribe` | multipart `file` | `TranscribeResponse` (**sync — stays sync**) |
| `POST` | `/v1/decisions` | `DecisionRequest` | `DecisionResponse` (**sync**, typed choice — see below) |
| `GET` | `/v1/models` | — | `{object:"list", data:[model…]}` — every model a caller can pin (see "Pinning a model") |

### Chat is async-only (2026-07-10)

**Sync `POST /v1/chat` was removed — it returns `410 Gone`.** Do all chat via
the async job API (`POST /v1/jobs?capability=X` → poll `GET /v1/jobs/{id}`, see
below). A synchronous chat call could 504 through the proxy read-timeout before
the fallback chain finished; the job queue has no such ceiling and exhaustively
rotates keys. `embed`/`transcribe` **stay synchronous** — they're fast (~1s),
never hit that timeout, and routing them through submit/poll would only add
latency for no benefit.

### Transcription: sync or async (2026-07-26)

`POST /v1/transcribe` (multipart `file`) still answers synchronously and is the
right call when a fast provider serves — gemini-3.5-transcribe returns in 1-3.4 s.

`POST /v1/transcribe/jobs` (`transcribe_submit` in `routes/proxy.py`) takes
the same multipart upload, returns `202` with a
`job_id` immediately, and is polled with the ordinary `GET /v1/jobs/{id}`.
Use it whenever a lost transcript is worse than a delayed one: a long clip,
or a chain that has to walk to its last provider, can outlast a client read
timeout, and the synchronous call then loses the transcript. Queued, the slow
path finishes and the caller collects it, plus it inherits the queue's
retries, backpressure and restart-survival.

The audio is base64'd into the job payload (the queue stores JSONB and cannot
hold raw bytes) and is **cleared the moment the job reaches a terminal state**,
so voice notes never accumulate in the database or the nightly backup. The 25 MB
Whisper ceiling is enforced before queueing; transport limits are under
"Request body limits" below.

### Capabilities (for `/v1/jobs`)

`chat:fast`, `chat:smart`, `chat:sales`, `chat:code`, `chat:edit`,
`chat:deep`, `prefilter`, `structured`, `translate`, `vision`. (`transcription`
goes through `/v1/transcribe/jobs`, `embedding` through `/v1/embed`, `decision`
through `/v1/decisions`.)

`chat:sales` (2026-07-23) is the "smart LLM, no rigid script" sales lane:
Claude Sonnet leads the chain on its own daily cap (chain: anthropic → gemini →
deepseek → sambanova). Uses the ordinary `llm:chat` scope. Like every other lane it FORCES
JSON when you send `response_format` (Claude has no native `json_object` mode,
so the broker upgrades it to a permissive `json_schema` served via tool-use,
and unwraps LiteLLM's tool envelope for you). A brief 2026-07-26 experiment
exempted this lane to keep Sonnet's reasoning — forced tool-use and reasoning
are mutually exclusive on this model — but it produced 44% unusable replies in
production and was reverted. If you want the reasoning instead of the JSON
guarantee, simply omit `response_format` on this lane.

`translate` routes to small fast non-reasoning models first
(gemini-flash → groq; cerebras, mistral and cohere were removed from this
chain), tuned for the "translate,
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
{"name":…, "strict":true, "schema":{…}}}` only gemini and openai
grammar-constrain generation, so with them the model **should not** return
invalid JSON — this is the root-cause fix for the `InvalidJSON` failures, far
better than the broker's post-hoc JSON validation. The broker forwards the
schema unchanged; providers that don't enforce it (groq, cerebras, cohere,
openrouter — `JSON_UNRELIABLE_PROVIDERS`) are deprioritized for JSON requests
(still tried, but after the reliable ones).

`vision` accepts OpenAI-style multimodal `content`: a `ChatMessage.content`
may be a plain string **or** a list of blocks, e.g.
`[{"type":"text","text":"что на фото?"}, {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,…"}}]`.
LiteLLM forwards both shapes to vision-capable models (see the vision chain
below). Pass
images as base64 data URLs — anthropic was removed from the vision chain because
it 400s on fetch-gated image URLs.

A completed chat `JobResponse` carries `cache_read_tokens` from the saved job result
(`null` for older jobs without the field; 0 when the provider reported no cache read — see
[providers.md](providers.md#prompt-caching-2026-07-01-wired-end-to-end-2026-07-02))
and `request_id` (the `usage_log` row id — match your own logs against the
broker's).

### Async jobs — `/v1/jobs` (submit + poll)

Same request body as `/v1/chat` (incl. `response_format`), but the broker
**never holds the connection**: it returns `202` with a `job_id` immediately,
runs the call in the background, and you **poll** `GET /v1/jobs/{job_id}` until
`status` is `done` or `error`. Available for every chat capability
(`chat:fast`/`smart`/`sales`/`code`/`edit`/`deep`, `structured`, `prefilter`,
`translate`, `vision`) — `embedding` stays sync-only (fast, no held-connection
problem to solve); transcription has its own `POST /v1/transcribe/jobs`.

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
  → 200 {"job_id":123,"status":"pending","poll_after_s":5}      # keep polling
  → 200 {"job_id":123,"status":"done","text":"…","provider":…,  # done
         "tokens_in":…,"tokens_out":…,"cache_read_tokens":…,
         "cost_usd":…,"request_id":…}
  → 200 {"job_id":123,"status":"error","error":"…"}             # failed
```

**Caps apply to `/v1/embed`, `/v1/transcribe` and `/v1/decisions` too (2026-09-07).** Until
then the cost guard had exactly one caller — the chat path — so a project could
spend past its daily cap through embeddings or transcription, and a PAID
whisper key was always booked at $0.00 (its own cap was decorative). Both
endpoints now reserve before the call and release after, like chat; a spent
project/global cap ends the walk with the same `daily budget cap reached —
retry after 00:00 UTC` message (HTTP 503 on these two sync endpoints), a
per-key cap just moves to the provider's next key. Whisper calls are priced
per audio-minute (`whisper_cost`; the pre-call reservation uses
`estimate_transcription_cost`) (`openai/whisper-1` $0.006/min; groq whisper at list) from
the provider-reported duration, or a bitrate estimate when it is absent. Free
keys are unchanged: $0, no reservation.

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

`poll_after_s` is the broker's suggested wait before the next poll: 2 only in
the submit response, then 5 s for a job under 30 s old, 10 s under 2 min, 20 s
after that (`next_poll_after_s`). A job belongs to exactly one project — polling someone else's
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

### `/v1/decisions` — typed decisions (2026-09-23)

A **decision model** does not write text. It takes `state` (the text to judge)
and named, typed `questions`, and returns one typed answer per question with
calibrated probabilities. Served by TypeSafe **Jev** (`typesafe/jev-1.13`) on
OpenRouter's dedicated `/api/alpha/decisions` endpoint — the model refuses
`/chat/completions` outright, which is why this is not a `/v1/jobs` capability.

```json
POST /v1/decisions
{"state": "Help! My payouts have been failing for 3 days.",
 "workflow": "triage",
 "questions": {
   "urgent":  {"type": "noul",   "instructions": "Is this urgent?",
               "criteria": {"true": "time-sensitive", "false": "not urgent"}},
   "project": {"type": "choice", "instructions": "Which project?",
               "criteria": {"itstep": "the academy", "veranda": "the bar"}},
   "weight":  {"type": "score",  "instructions": "How important?",
               "criteria": ["noise", "low", "high"]}}}
```

```json
{"answers": {
   "urgent":  {"type": "noul", "noul": 0.95},
   "project": {"type": "choice", "choice": "itstep",
               "probabilities": {"itstep": 0.81, "veranda": 0.19}, "confidence": 0.7},
   "weight":  {"type": "score", "score": 1.8, "probabilities": {"0": 0.05, "1": 0.1, "2": 0.85},
               "confidence": 0.8}},
 "provider": "openrouter", "model": "openrouter/typesafe/jev-1.13",
 "model_served": "typesafe/jev-1.13-20260917",
 "tokens_in": 307, "tokens_out": 23, "cost_usd": 0.0000129,
 "latency_ms": 350, "key_label": "gemma4", "request_id": 512700}
```

Question shapes are checked **before** a key is used and a bad one returns
`422`: `noul` criteria must be exactly `true`/`false`, `choice` takes 1–255
options, `score` 2–10 ordered levels. Up to 64 questions per call — send every
decision about one `state` in a single call, it is billed once for the input.

- **Paid keys only.** The lane rotates OpenRouter keys with `tier='paid'` and
  scope `llm:decision`. A `$0` free-tier key is *not* refused by this model
  (measured: 200 OK, cost booked) — paid-only is a routing choice, so the
  spend lands on the account holding the prepaid credit and its spend limit.
- **Caps apply.** The reservation is priced at jev's own $0.042/M input rate
  (LiteLLM has no price for it and would reserve $0, letting a project run
  past its daily cap).
- **Price.** $0.042 per million input tokens, $0 output. Measured on 120 real
  Vera triage events: median 0.36 s, p90 0.46 s, ~$0.000055 per event with
  four questions.
- **Free fallback (2026-10-03).** Jev is always the primary. If the caller did
  not pin `model` and Jev fails on a key (provider error, or the project/global/key
  cap blocks the reservation), `run_decision` immediately retries on the SAME
  key with Inception **Mercury Decide** (`openrouter/inception/mercury-decide:free`,
  $0, same endpoint and response shape). The fallback is never reserved against a
  cap (it costs $0, so a spent cap such as a `$0/day` project cap must not refuse
  it); whatever `usage.cost` it reports is still booked. `model` in the response
  names the model that actually answered. Why only a fallback: measured
  2026-10-03 in series on real Vera events against her own triage (42 events x3
  runs + 42 fresh events, mean of 4 runs), Mercury vs Jev: project 69% vs 66%,
  needs_action 76% vs 80%, importance exact 30% vs 53% (both 95-98% within one
  level). Mercury is deterministic; Jev varies between identical calls. A request
  that pins `model` gets no fallback. If the fallback fails too, the key is
  skipped and the next one is tried; all failing is `502`.
- `503` — no paid key carries `llm:decision`; `502` — every key failed; an
  HTTP `402` from the provider cools the key as out-of-money.

**Code path.** Route `decisions_endpoint` (`DecisionRequest` → `DecisionResponse`)
→ `validate_questions` (a malformed question raises `DecisionRequestInvalid` → 422,
before any key is touched) → `run_decision` in `llm_service` (key pick, cap
reservation sized by `estimate_tokens`, rotation; all keys failing raises
`DecisionFailed` → 502) → adapter `decide` in `providers/decisions.py`, which
raises `DecisionHTTPError` carrying the provider body on non-2xx. Success
returns a `DecisionOutcome` (answers, cost, latency, key label).

### Vision (`?capability=vision`)

Submitted through the generic async job endpoints
(`POST /v1/jobs?capability=vision`, poll `GET /v1/jobs/{id}`). Payload is one
message whose `content` is a block list —
`[{"type":"text",...},{"type":"image_url","image_url":{"url":...}}]`, i.e. an
image plus a prompt.

Chain: `local` (self-hosted Qwen3-VL, see below) → `gemini` → `sambanova` →
`openrouter` → `deepseek` → `openai`. The regular walk is **free-only**
(`free_first_walk`, `FREE_WALK_CAPABILITIES`): the paid providers (`deepseek`,
`openai`) are tried only on the job queue's final retry (`paid_only`).

#### `local` — self-hosted Qwen3-VL-4B (2026-08-31)

Chain-FIRST. Free, private, unmetered — added because the cloud vision tier was
answering only ~8% of calls (see `docs/deploy-ops.md` "Local vision" for the
error breakdown and the measured latency/RSS numbers). Backed by upstream
`llama-server` in the `vision-local` compose service; reached over plain HTTP by
`_describe_via_local_vision` / `_post_local_vision`, never through LiteLLM —
`local/` is a routing label, not a LiteLLM provider.

Two conditions hand the request straight on to the cloud tail instead:

- **the image is a remote URL, not inline base64** — the cloud providers can
  fetch it; llama-server would have to egress from our host to do the same, and
  deliberately does not.
- **an empty body** — a 4B model on CPU that produced nothing is not a real
  answer. Booked as `EmptyBody`/502 and escalated to the next provider (not the
  next key: `local` is one process, so re-asking it is deterministic).

**Response shape is unchanged.** `text` carries PROSE for every provider on
this chain, `local` included — the local model answers under a JSON grammar
internally, and the adapter unwraps it before returning. This matters because
`local` leads the chain and any cloud provider can serve the very next call: a
provider-dependent shape would break callers precisely on fallback.

Two OPTIONAL fields ride alongside, populated only when `local` answered and
`null` otherwise:

| field | meaning |
|---|---|
| `vision_type` | detected kind — `чек`, `накладная`, `банковский экран`, `переписка`, `постер`, `документ`, `таблица`, `фото`, `другое` |
| `vision_format` | shape of `text` — `text`, `markdown`, `json` |

Both are classified on the *same single pass* that answers the caller's prompt
(a second pass would double the CPU cost of an already ~69s call). A client
reading only `text` is unaffected.

Timeout is `VISION_LOCAL_TIMEOUT_S` (300 s per HTTP call), not the 60s every
other provider gets; the whole attempt, including the wait for the single local
slot (`VISION_LOCAL_QUEUE_WAIT_S`), is bounded at 570 s: one image measured 69s and a dense document 192s on this hardware, so the
flat ceiling would abort every call, cool the key, and fall through to the
rate-limited cloud providers — burning CPU for nothing.

### `/v1/transcribe` (audio → text)

Multipart upload, field name `file` (≤25 MB — Whisper's limit). Optional
`?workflow=` query tag. Chain (2026-10-04): `gemini` (`gemini-3.5-transcribe`)
→ `groq` whisper-large-v3-turbo (free fallback) → `openai` whisper-1. Returns
`{text, provider, model, cost_usd, latency_ms, key_label, request_id}`.

**Why gemini leads (2026-10-04 bake-off, 15 real voice notes).** gemini
1-3.4 s, 15/15 ok, the most faithful and verbatim transcript; groq 0.2-0.7 s
but it normalizes surzhyk / Ukrainian speech into literary Ukrainian and made
meaning errors; the self-hosted whisper that used to sit in the chain was
16-84 s with the worst quality and one 180 s timeout, and was retired
entirely (no `local` transcription provider, no self-hosted ASR service, no
proofreading pass). **Privacy:** on the gemini free tier Google may use the
audio to improve its products; the paid tier ($0.005/min) does not.

### `X-Request-Id` — grouping every attempt of one request (2026-10-03)

Every response carries `X-Request-Id`. Each provider attempt of the request (every key
tried, every fallback hop, every dispatcher retry of a queued job) is written to
`usage_log` with that id in `usage_log.request_id` (migration 015), so a fallback trail
(`groq 429 → gemini ok`) groups with one `WHERE request_id = …`. Sync endpoints get a fresh
32-hex id (a sane client-supplied `X-Request-Id` of 8-64 `[A-Za-z0-9._-]` characters is
honoured instead). Queued jobs use `job-<job_id>` — returned on submit AND on every poll —
and it is shared by all retries of the job. The body field `request_id` below is a
different thing: the `usage_log.id` of the winning attempt's row (unchanged).

### `request_id` (body) — correlating a call across both sides

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
| `/v1/decisions` | `llm:decision` |

## Admin (X-Admin-Key required)

| Method | Path | Description |
|---|---|---|
| `POST` | `/admin/projects` | Create project — returns one-time `project_key` |
| `GET` | `/admin/projects` | List all projects |
| `POST` | `/admin/keys` | Create OR upsert an API key (encrypted at rest) |
| `GET` | `/admin/keys?provider=…` | List keys, optional provider filter |
| `POST` | `/admin/keys/{id}/disable` | Soft-disable |
| `DELETE` | `/admin/keys/{id}` | Hard delete |

## `model` vs `model_served` (2026-09-13)

Every response that names a model carries two fields:

| field | what it is |
|---|---|
| `model` | the **routing** name the broker asked for — `deepseek/deepseek-flash`, `local/qwen3vl`. Prices the call, keys the dashboard's by-model aggregates, stable. |
| `model_served` | the **exact** model that answered, when that says more: `DeepSeek-V4.1-Flash` behind the DeepSeek family alias, the loaded gguf behind `local/qwen3vl`. `null` when the routing name is already the exact model id (most cloud models), so a client reading only `model` sees no change. |

Present on `GET /v1/jobs/{id}` / `GET /v1/deep/{id}` (done jobs), `POST
/v1/embed` and `POST /v1/transcribe`. Derived by
`providers/model_identity.py:served_model` and stored per call in
`usage_log.model_served` (migration 011) — see `docs/providers.md` for why an
alias is not always the model that ran. The dashboard's "Recent 50 calls"
shows `model_served` when present, with the routing name in the cell tooltip.

## Pinning a model (exact, 2026-10-03)

`model` in the body of `POST /v1/jobs` (`/v1/deep`, `/v1/embed`, `/v1/decisions`)
pins **exactly that model**. Valid values are the ids and names listed by
`GET /v1/models` (below): a routing id (`"gemini/gemini-2.5-flash"`) or a bare
canonical name (`"gpt-oss-120b"`, which several providers serve).

| `model` | effect |
|---|---|
| absent | normal walk: the chain's providers in policy order, each with its default model and rotation |
| `gemini/gemini-2.5-flash` | exactly that model on its provider; never another provider, never rotated |
| `gpt-oss-120b` (served by cerebras, groq, cloudflare) | exactly that model on each provider that serves it, in a **fixed order**: free-tier keys of every provider first (providers by `ProviderSpec.rank`), then paid keys. No shuffling — the same request walks the same way every time |
| not in the catalog | **`400`** with close-match suggestions, before anything is queued |
| in the catalog but not a model for that capability, or its provider is not in the capability's chain | **`400`** (`does not serve …` / `not served by any provider of the … chain`) |

Chat lanes are interchangeable for pinning (a model wired for `prefilter` may be pinned
on `chat:fast`); an embedding model cannot be pinned on chat. The project still needs the
capability's scope and a key of that provider — pinning grants nothing. The final-retry
paid escalation keeps the pin and walks the paid pass only. A job queued before a registry
change whose pin is no longer in the catalog ends `status=error` with the suggestions (no
retries).

Before this (2026-09-26) a qualified `provider/model` merely restricted the walk to that
provider and an unqualified name was applied to whatever provider the chain reached;
unknown names were forwarded and failed at the provider. History: `docs/routing.md`.

The model actually used comes back in `model` (what was asked for) and, when it says more,
in `model_served`.

### `GET /v1/models` — what can be pinned

Any authenticated project key (no scope needed). Built from the provider registry, so a
new registry entry shows up here with no other change.

```json
{"object": "list", "data": [
  {"id": "cerebras/gpt-oss-120b", "object": "model", "owned_by": "cerebras",
   "name": "gpt-oss-120b",
   "capabilities": ["chat:code", "chat:fast", "chat:smart", "structured"],
   "also_served_by": ["groq/openai/gpt-oss-120b", "cloudflare/@cf/openai/gpt-oss-120b"],
   "price": {"kind": "litellm", "input_usd_per_mtok": 0.35, "output_usd_per_mtok": 0.75}}
]}
```

`price.kind`: `litellm` (litellm's map), `override` (our list price), `per_minute`
(`usd_per_minute`), `free`, `local`, or `unknown` (not priced anywhere — a bug the registry
test catches). Entries are ordered the way a bare name resolves (free providers by rank,
paid last). Free-tier keys bill $0 whatever the nominal price shows.

### Cache affinity (2026-10-03)

For **every** capability a request family — `(project, workflow, capability, pinned model)` —
is pinned to the `(provider, model, key)` that last served it, for `AFFINITY_TTL_S`
(default 2 h; Redis-shared, fail-open). The next request of the family tries that exact key
first, so the provider-side prompt cache stays warm, and moves on only when it is cooling,
capped or errored; the walk then re-pins to whatever succeeded. A paid pinned provider is
never promoted ahead of a free chain head (free-first stays policy), and the affine leg
never spends a paid key where the walk heads with a free provider. Callers need to do
nothing; send a stable `workflow` to get a stable family. A stable `prompt_cache_key`
derived from the family is sent to providers that document one (OpenAI, Mistral, Cerebras:
`prompt_cache_key`; OpenRouter: `session_id`).

## Request body limits (2026-10-02)

nginx (`infra/nginx-aib.conf`) allows 4 MB by default, 26 MB on
`/v1/transcribe*` and 28 MB on `/v1/jobs`. Application limits behind it: 25 MB
audio (`_MAX_AUDIO_BYTES`) and 20 MB per decoded image (`_MAX_IMAGE_BYTES`).
Over the nginx limit the caller gets `413` from nginx before the app sees the
request.

## Vision jobs: what is rejected at submit (2026-09-12)

`POST /v1/jobs?capability=vision` validates every inline `data:` image before
queueing (`services/vision_payload.py:inline_image_problem`): if the bytes
cannot be decoded as an image the submit is refused with **400** and a reason
such as `inline image #1 is an MP4/MOV video container — the declared
image/jpeg cannot be decoded by any vision provider`. Before this, such a
payload walked the whole provider chain, was re-queued eight times and
resubmitted by the client — the same MP4 was processed 8 times in one day.
Clients should treat the 400 as permanent for that file. Remote `http(s)`
image URLs are not checked (only cloud providers fetch them).

Vision results are also served from the in-process response cache for 24h:
an identical payload (same bytes, same prompt, same params) returns the
earlier description with `provider: "cache"` and no provider call.

## Dashboard (cookie OR X-Admin-Key)

| Method | Path | Description |
|---|---|---|
| `GET` | `/dashboard?from=&to=` | Inventory + range-driven KPIs (spend/calls/tokens for the chosen date range), sortable tables with TOTAL footers, inline edit. `from`/`to` default to today. |
| `POST` | `/dashboard/keys/create` | HTML form: add or upsert key |
| `POST` | `/dashboard/keys/{id}/edit` | HTML form: rename, change tier/scope/cap, rotate token |
| `POST` | `/dashboard/keys/{id}/test` | probe one key, returns a status chip |
| `POST` | `/dashboard/projects/{id}/rotate-token` | new project key (shown once); see dashboard.md |
| `POST` | `/dashboard/keys/{id}/disable` | Toggle active |
| `POST` | `/dashboard/keys/{id}/delete` | Hard delete (confirm prompt) |
| `POST` | `/dashboard/projects/create` | HTML form handler — shows the one-time key in the flash |
| `POST` | `/dashboard/projects/{id}/edit` | HTML form: rename, change scopes/cap/email |
| `POST` | `/dashboard/projects/{id}/delete` | Hard delete a client project (confirm prompt; `dash_delete_project`, 2026-09-12). The key stops authenticating at once; usage history keeps its project_id. |
| — | project page breakdown cards | Long workflow and model names are truncated with an ellipsis; hover shows the full name (2026-09-26 — `sinhrm.candidate_screening` pushed the workflow sparklines past the tile edge). Fixed table layout in `.brk-card-split` / `.brk-card-models`. |
| `GET` | `/dashboard/projects/{id}?range=1h\|4h\|12h\|24h\|7d\|30d` | Drill-down — per-project KPI cards, breakdown by provider/capability/model/status, last 50 calls. Range pill swaps the window. |


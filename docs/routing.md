# Routing, scopes & cost guard

> **2026-07-10 (anthropic JSON via tool-use)**: Claude ignores OpenAI's
> `response_format={"type":"json_object"}` (litellm drops the unsupported param),
> so it had no JSON enforcement and sometimes replied in PLAIN TEXT on follow-ups
> → InvalidJSON (~30% on chat:smart). A new `_AnthropicAdapter` upgrades a
> json_object request to a PERMISSIVE `json_schema`
> (`{"type":"object","additionalProperties":true}`), which litellm routes through
> Claude's native tool-use → guaranteed JSON object, with the caller's own fields
> preserved (verified 8/8 valid, all 17 fields). A caller-supplied real
> json_schema is left untouched.

> **2026-07-10 (cooldown — stop exhausted keys churning + reach reserve keys)**:
> `cooldown_until` honoured a provider's `retryDelay` literally. A free Gemini key
> whose DAILY quota is used up still returns a short `retryDelay` (~24s), so the
> broker re-picked the dead key ~100×/hr — burning the per-provider attempt
> budget, inflating the error count, and starving reserve (`is_reserve`) keys:
> because the shared pool never stayed exhausted, `pick_and_reserve` never fell
> through to the reserve key (it sorts last). Fix: floor a retry hint at the
> escalating adaptive backoff — `max(retry_after, adaptive_cooldown)`. A one-off
> blip still waits ~the hint; a key that keeps 429-ing gets parked up to
> `MAX_COOLDOWN_S` (30 min) and drops out of rotation, so working keys (incl.
> reserve) are selected and the request stops wasting attempts. `is_reserve` is a
> per-key flag, NOT tied to paid tier — a reserve key IS used, just after the
> shared pool is genuinely exhausted. (Also made `adaptive_cooldown`'s window
> query portable so the SQLite gate exercises the retry-after path.)

> **2026-07-10 (token-cost optimization)**: DEFAULT_MODEL + chain changes after a
> live usage review of Stepan (project 4, hitting its $4/day cap with a ~40%
> error rate). Also: cerebras `gemma-4-31b` (new free non-reasoning model) wired
> for `translate`/`prefilter` + added to the translate chain (fast, unlike
> cerebras gpt-oss's ~16s think); `zai-glm-4.7` skipped (reasoning, no gain).
> **anthropic REMOVED** from chat:fast/smart/code/edit/structured — its one key is
> out of credit ("credit balance is too low") so it only flapped errors;
> DEFAULT_MODEL entries kept, re-add to the chains once the balance is topped up.
> **cloudflare added to chat:smart + chat:code** — same free @cf/openai/gpt-oss-120b
> it already serves on chat:fast (quality-neutral, same model family as
> cerebras/groq smart), extra free burst before the paid tail.
> **github REMOVED entirely** (provider + key + chains/DEFAULT_MODEL/quotas/probe) —
> free tier is ~150 req/day on 1 key with a non-UTC reset window, so the key sat
> exhausted (155 attempts / 0 success / all 429 on its last full day). Dead weight.
> **Empty-body retry (JSON gate)**: a blank/whitespace response is now a TRANSIENT
> throttle, not a model defect — DeepSeek's json_object intermittently returns an
> empty string on very large prompts (~24% on Stepan's 52k-char follow-up prompt,
> random per key/call). run_chat retries the SAME provider's next key (usually
> valid) instead of skipping the provider; recorded as `EmptyBody` (vs the
> `InvalidJSON` used for non-empty malformed bodies, which still skip the model).
> CAPPED at `_MAX_EMPTY_RETRIES=1`: some prompts make json_object empty
> DETERMINISTICALLY (a full ~30k-char system prompt is empty on every key/call —
> verified 30k→empty 4/4 vs 16k→OK 4/4), so retrying every key just burns the
> provider. After the cap it's a normal miss → next provider. Root trigger is the
> caller's oversized system prompt, not the broker.
> - **deepseek stays on `deepseek-chat`** (chat:fast/smart/edit/code). A brief
>   move to the cheaper `deepseek-v4-flash` was REVERTED same day: v4-flash is a
>   REASONING model whose hidden reasoning eats the max_tokens budget, so our
>   short-max_tokens JSON replies got truncated → InvalidJSON (~49% on chat:fast,
>   12% on chat:smart) + Timeouts; `reasoning_effort=disable` doesn't stop it.
>   deepseek-chat is non-reasoning and returns clean JSON at any max_tokens
>   (verified mt=120: chat valid, v4-flash empty). Caveat: DeepSeek's /models
>   lists only v4-* and flags chat for ~07-24 deprecation — needs a non-reasoning
>   JSON-reliable replacement before then (NOT a blind swap to a reasoning model).
> - **gemini `chat:smart` `2.5-pro` → `2.5-flash`**: 2.5-pro's free tier
>   (~50-100 RPD/5 RPM) 429'd ~100% under smart volume (4096 err / 0 ok in 3d);
>   2.5-flash (~250 RPD/10 RPM ≈ 2000/day across our keys) serves it for free.
> - **cohere `chat:smart`/`chat:code` `command-a-03-2025` → `command-r7b-12-2024`**:
>   flagship command-a billed ~$2.4/day mostly on FAILED calls; r7b is the cheap
>   fallback tier.
>
> **2026-06-26**: Cohere retired `command-r` / `command-r-plus` on 2025-09-15.
> Cohere chain now routes through `command-r7b-12-2024` (small/fast) for
> `chat:fast` / `prefilter` / `structured` / `chat:smart` / `chat:code`.
> Embed model `embed-english-v3.0` unchanged.
>
> **2026-07-01**: `chat:edit` narrowed to JSON-reliable providers only —
> `[gemini, deepseek, anthropic]`. mistral / cohere were dropped (and their
> `chat:edit` DEFAULT_MODEL entries removed): when gemini was on cooldown they
> returned Bahasa-drifted and torn JSON that broke Coach. The `llm:edit` scope
> on existing mistral/cohere keys is now inert — harmless, no cleanup needed.
> Supersedes the 2026-06-26 free-breadth extension.
>
> **2026-07-01**: cerebras quota is TOKENS-only (1M/day). The `req/day` axis
> was dropped from `PROVIDER_QUOTAS` and auto-discover no longer ingests
> cerebras' `x-ratelimit-limit-requests-day` header — a single key logged
> 4,866 req against its 2,400 header without a 429, so it was never a hard cap.
>
> **2026-07-01**: cost tracking restored (`cost_per_token`; `completion_cost`
> had silently zeroed all costs since 2026-06-27) and DeepSeek peak/valley
> pricing added (2x in peak UTC hours from mid-July). See **Cost guard**.
>
> **2026-07-02**: mistral quota has no daily axes. Confirmed live: mistral
> publishes only PER-MINUTE headers (`x-ratelimit-limit-req-minute=50`,
> `-tokens-minute=50000`) — no daily cap. The old `req_per_day=86_400` /
> `tok_per_day=500_000` seed was invented, never backed by evidence; real keys
> sustained 1.3-1.7M tok/day at ~260% of the fake cap while 99.96% `ok`,
> showing fully red on the dashboard despite being alive and healthy.

## Capability → provider chain + required scope

Source of truth: `src/aibroker/routing/chains.py` (`CAPABILITY_CHAINS`,
`CAPABILITY_SCOPE`). Routes and the selector import from here — never duplicate
these tables. A drift test asserts every capability has a scope and every
provider in a chain has a `DEFAULT_MODEL` entry.

| Capability | Chain (left→right) | Scope | Notes |
|---|---|---|---|
| `chat:fast` | cerebras → groq → gemini → mistral → cohere → openrouter → sambanova → zai → cloudflare → deepseek → openai | `llm:chat` | Strict free-first (2026-07-05) — paid is always last. cloudflare = gpt-oss-120b. nvidia REMOVED (kimi-k2.6 → 404), anthropic REMOVED (out of credit), github REMOVED (exhausted, 0 success) — all 2026-07-10. |
| `chat:smart` | cerebras → groq → gemini → mistral → cohere → openrouter → sambanova → cloudflare → openai → deepseek | `llm:chat` | Strict free-first; deepseek-chat last (non-reasoning, JSON-reliable). gemini = 2.5-flash (2026-07-10). cloudflare added for free burst; nvidia/anthropic/github removed 2026-07-10. |
| `chat:code` | cerebras → groq → openrouter → gemini → mistral → sambanova → cloudflare → deepseek → openai | `llm:chat` | Strict free-first; Codestral via mistral when other free chains are dry. anthropic/github removed 2026-07-10. |
| `chat:edit` | **gemini → deepseek** | `llm:edit` | Coach editor (Stepan). JSON-reliable only: gemini (free, thinking disabled) → deepseek-chat. anthropic dropped 2026-07-10 (out of credit). mistral/cohere/cerebras/groq/openrouter excluded — malformed JSON breaks Coach. |
| `chat:deep` | **nvidia** (nemotron-3-ultra-550b-a55b) | `llm:deep` | Long-context/reasoning lane, 1M-token context. No latency guarantee — single-provider, no fallback. **Async-only** — `POST /v1/chat` returns **410 Gone** (all capabilities); use `POST /v1/jobs?capability=chat:deep` (or `/v1/deep`) + `GET /v1/jobs/{id}`. |
| `prefilter` | cerebras → groq → gemini → mistral → cohere → openrouter → sambanova → zai → cloudflare | `llm:chat` | No paid; cheap pre-filter. cerebras = gemma-4-31b (fast non-reasoning, 2026-07-10). github removed. |
| `translate` | cerebras → mistral → gemini → cohere → groq | `llm:chat` | Trivial task: SMALL FAST non-reasoning models first. cerebras = gemma-4-31b (2026-07-10, fast non-reasoning — added first); mistral-small / gemini-flash / cohere-r7b follow (~0.3-2s). cerebras/groq gpt-oss "thinks" ~16s so it's NOT used here (gemma is). Reuses `llm:chat` keys but hits models the chat chains reach last. |
| `structured` | groq → gemini → mistral → cohere → openrouter → anthropic → openai | `llm:chat` | cerebras dropped 2026-07-01: HTTP-200 malformed JSON (~4.6k/wk). groq (same base model) stays. |
| `vision` | gemini → openai | `llm:vision` | anthropic dropped 2026-07-01: 400 "Unable to download the file" on Vera's image URLs (~1.4k/wk). Re-add once images are passed as base64. openai is the paid fallback when gemini is RPM-exhausted. cloudflare tried and pulled same day 2026-07-04, see below. |
| `transcription` | groq → openai | `llm:audio` | Whisper: groq whisper-large-v3-turbo (free) → openai whisper-1. `/v1/transcribe` route |
| `embedding` | voyage → cohere | `llm:embed` | voyage primary; cohere fallback (embed-english-v3) |

`chain_for(cap)` raises `ValueError` on an unknown capability; the proxy rejects
unknown capabilities with HTTP 400 via `is_known_capability`. `scope_for(cap)`
returns the scope the **project** must hold and the **key** must carry.

> **Removed providers.** `sambanova`, `nvidia`, `mistral` were in the chains but
> had no `DEFAULT_MODEL`, so `model_for` returned `None` and they were silently
> skipped — the chains lied about their breadth. They're now out. Re-add only
> with (a) a verified `DEFAULT_MODEL`, (b) a health probe, (c) a prod key test.
>
> **sambanova re-added (2026-07-04).** All three criteria met: a real key
> (`api.sambanova.ai/v1/chat/completions`) returned 200 with `sambanova/Meta-
> Llama-3.3-70B-Instruct`, confirmed `x-ratelimit-limit-requests-day: 20` with
> a genuine ~24h reset (not a one-time trial grant — it renews daily for as
> long as the free program exists). 20 req/day/key is too thin to be a
> workhorse, so it sits at the tail of `chat:fast`/`chat:smart`/`chat:code`/
> `prefilter` as pure extra breadth; adding more sambanova keys adds up
> linearly (10 keys ≈ 200 req/day pool).
>
> **github re-added (2026-07-04).** Also confirmed live (real PAT with
> `models:read` scope, 200 OK on `github/gpt-4o-mini` via LiteLLM's `github/`
> prefix → `models.inference.ai.azure.com`). Its response headers
> (`x-ratelimit-limit-requests: 20000`, `renewalperiod: 60s`) are the backend
> Azure deployment's raw capacity, NOT GitHub's actual per-account cap — they
> are deliberately excluded from `extract_quota_headers`' auto-discovery
> (would silently over-report by ~100x). `quotas.py` instead uses GitHub's own
> documented per-account daily cap for the Free Copilot tier / "low" tier
> models: 150 req/day. `chat:smart` defaults to `gpt-4o-mini` too, not
> `gpt-4o` — GitHub's "high" tier models have a much stricter free-account cap
> and haven't been verified.
>
> **`chat:deep` added (2026-07-04).** A dedicated long-context/reasoning
> capability for NVIDIA's Nemotron 3 Ultra (550B MoE, 55B active,
> `nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b`) — 1M-token context (95%
> RULER@1M), strong agentic benchmarks (91% PinchBench), but **slow**: a live
> test measured ~27s for 5 output tokens on the free, oversubscribed pool.
> Unfit for any latency-sensitive chain, so it gets its own scope
> (`llm:deep`) and its own single-provider chain — no fallback, a miss is a
> 503 by design, not a silent slide into a fast/cheap model.
>
> NVIDIA's free tier is fundamentally different from sambanova/github: no
> rate-limit headers at all (only `nvcf-status: fulfilled`), and it's **1,000
> ONE-TIME inference credits** (not a renewing daily/monthly quota). LiteLLM
> also has no pricing entry for these models, so `cost_usd` is always `0` for
> nvidia calls — the usual `daily_cost_cap_usd` safety net is blind here. The
> only real guard is each key's `daily_limit` (request count).
>
> **kimi-k2.6/deepseek-v4-pro wired into chat:fast/chat:smart (2026-07-05).**
> The "silently convert to real pay-as-you-go billing" risk noted above
> assumed a payment method on file — **this account has none**, so once the
> 1,000 one-time credits are spent the key simply stops working (a real
> `402`/"add a payment method" error, same shape as any other exhausted free
> key going `mark_dead`), not an actual charge with nothing to charge
> against. Both models confirmed live with real, valid JSON output on a
> `response_format=json_object` test — `kimi-k2.6` (~1.4s) → `chat:fast`;
> `deepseek-v4-pro` (~7.4s, slower but chat:smart's latency budget is
> looser) → `chat:smart`. `nemotron-3-ultra` stays `chat:deep`-only — it's
> the one genuinely too slow (~27s+ seen live) for any synchronous
> capability.
>
> **`chat:deep` made async-only (2026-07-05).** Real production latency
> (Stepan2) was observed up to ~8 minutes — far past Cloudflare's edge
> timeout (~100s) and this broker's own `infra/nginx-aib.conf`
> (`proxy_read_timeout 120s`). The caller got a 504 while the broker was
> still waiting on nemotron and would eventually log a perfectly good `ok`
> that nobody was left to receive — `usage_log` had zero `http_status=504`
> rows despite the caller-visible failures, because the timeout happens at
> the proxy layer, before the broker's own response.
>
> `POST /v1/chat` now returns `410 Gone` for **every** capability (sync chat
> was removed 2026-07-10). Use the job API instead:
> - `POST /v1/deep` — same body shape as `/v1/chat` minus `response_format`
>   (nemotron isn't JSON-reliable, don't ask it for structured output).
>   Returns `202` immediately with `{job_id, poll_url, poll_after_s}`.
> - `GET /v1/deep/{job_id}` — scoped to the caller's own project (a job from
>   another project 404s, same as a wrong id). Returns `status`
>   (`pending`/`done`/`error`) and, once `done`, the same fields `/v1/chat`
>   returns (`text`, `provider`, `tokens_in/out`, `cost_usd`, `latency_ms`,
>   `key_label`, `request_id`).
>
> A drained queue backs this — `submit_job` only INSERTs a `pending` row and
> returns the `job_id` immediately; it does **not** run the call in-process.
> A background `dispatcher_loop`/`drain_once` per uvicorn worker claims eligible
> rows atomically (`UPDATE … WHERE id IN (SELECT … FOR UPDATE SKIP LOCKED)`, so
> workers never double-claim) and runs each through `run_chat`. Poll requests
> read straight from Postgres, so they work regardless of which worker answers.
> Because the work is a durable row rather than an in-process task, a job
> survives the worker that submitted it restarting — a `running` row whose
> worker died is re-queued by a later tick. See `docs/architecture.md` for the
> full drained-queue model.
> **cloudflare tried in `vision`, then pulled the SAME DAY (2026-07-04).**
> Confirmed live (real token + account ID, 200 OK) against a garbage-bytes
> probe — but that only proved auth+connectivity. A follow-up test with a
> REAL base64 data-URL image (the exact format gemini/openai receive through
> this code path) 400'd: `"Unsupported image data"` (`code: 3010`). Workers
> AI's llava wants raw byte-array image input, not an OpenAI-style
> `image_url` — LiteLLM doesn't convert between the two for cloudflare. Left
> in the chain, it would be dead weight that always fails, exactly the
> "chains that lie about their breadth" problem the sambanova/nvidia removal
> note above already warns against. Pulled back out of `CAPABILITY_CHAINS`;
> `DEFAULT_MODEL`/`quotas.py`/`cooldown.py`/health-probe entries stay (same
> "known but not chained" treatment `github` got before its own prod key
> test). Lesson: a probe with placeholder/garbage payload only proves
> auth — always follow up with a real-shaped payload before trusting a chain
> addition.
>
> Not wired for `transcription` either — LiteLLM's cloudflare provider only
> implements chat completions (no audio submodule), and Workers AI's whisper
> endpoint has a different request shape LiteLLM doesn't speak; using it
> would need a raw HTTP call outside LiteLLM.
>
> Genuinely safer than nvidia in one respect: no card on file at all (so no
> silent-billing path), and the free tier ("10,000 neurons/day") actually
> renews daily — but it's a compute-unit budget, not a request count, so no
> honest `req_per_day` axis exists (see quotas.py).
>
> **Cloudflare needs an account ID, not just a token.** Its API URL is
> `https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}`
> — the account ID isn't a secret but must ride along with the key. Added a
> new nullable `api_keys.account_id` column (see `db/models.py`,
> `infra/sql/init.sql`) and `litellm_adapter.extra_for_provider()` builds the
> full `api_base` override from it at call time. **The trailing slash on
> `api_base` matters** — LiteLLM's cloudflare transformation does
> `api_base + encoded_model` with no separator; omit it and every call 404s
> with "No route for that URI".
>
> **Known gap:** the health monitor's `probe_all()` only carries
> `(api_key_id, provider, plain_token)` per key — no account_id — so
> cloudflare keys are NOT probed by the background monitor yet. Liveness is
> only inferred from real traffic (`_penalize` on a failed call), same as
> before any provider had a dedicated probe.
>
> **zai (Z.ai/Zhipu) added (2026-07-05).** Confirmed live, but only
> `glm-4.5-flash` — the bigger `glm-4.5`/`glm-4.5-air` both 429'd with
> "Insufficient balance or no resource package" on this account, so
> `chat:smart`/`chat:code` stay off this provider; only `chat:fast` and
> `prefilter` use it, tail position. LiteLLM DOES have a real (zero) price
> for `glm-4.5-flash` — `cost_usd` isn't blind here like nvidia/cloudflare.
> No rate-limit headers exposed and no documented per-account daily cap
> found, so `quotas.py` carries no invented axis (same reasoning as
> mistral above).
>
> **cloudflare added to `chat:fast`/`prefilter` (2026-07-07), previously
> vision-only.** Part of a live audit of every provider's model lineup
> (`docs/routing.md` research request: find cheaper/newer models for
> existing paid keys, and idle capacity on free ones). Confirmed live with
> the REAL strict Vera triage `json_schema` (not just `json_object`): valid
> JSON, ~1.6s, correct classification, `litellm.get_supported_openai_params`
> confirms `response_format` is genuinely supported (unlike zai). Uses
> `@cf/openai/gpt-oss-120b` — same model family already proven reliable on
> cerebras/groq. This was previously-idle free capacity (10k neurons/day, no
> card on file) — only `vision` used this key before.
>
> One other candidate from the same audit was tested live and REJECTED:
> - **`groq/llama-3.1-8b-instant`** (candidate to replace `gpt-oss-120b` for
>   more throughput per token-day budget) — failed the real triage
>   `json_schema` outright ("Failed to call a function. Please adjust your
>   prompt.") The 8B model can't reliably do this structured-JSON task;
>   `gpt-oss-120b` stays.
>
> Also surfaced by this audit: the paid **gemini `demoniwwwe`** key's
> prepayment credits are depleted (live `429 RESOURCE_EXHAUSTED` on both
> flash and pro) — blocks the key entirely regardless of model choice,
> needs a top-up at ai.studio (real billing action, not a code fix). The
> earlier flash → flash-lite chat:fast cost-swap proposal is deferred until
> the key is actually callable again to verify quality live. The paid
> **anthropic `default`** key remains dead from 2026-07-05's credit
> exhaustion (see below) — same category, same blocker (user top-up).
> **openai** has zero keys configured in prod at all right now — harmless
> (last in every chain, an empty pick just falls through instantly) but a
> dead link if no key is ever added.
>
> **Paid moved to the tail of every `chat:*` chain (2026-07-05).** Was:
> `deepseek` sat ahead of `openrouter`/`github`/`sambanova`/`zai` "for
> backfill speed" (a documented exception from when deepseek was the only
> tail addition). As free tail providers accumulated, this stopped making
> sense — a paid call could fire the moment the first ~5 free providers
> were saturated, while 3+ more free providers (all confirmed live)
> further down the chain went untried. Explicit operator choice: **slow
> but free beats fast but paid**. `deepseek`/`anthropic`/`openai` are now
> strictly the last 3 entries in `chat:fast`/`chat:smart`/`chat:code` —
> `test_strict_free_first` in `tests/test_chains.py` now covers all three
> (previously only `prefilter`/`structured`, which were already
> paid-last). `prefilter`/`structured`/`vision`/`transcription` were
> already free-first and untouched; `chat:edit`/`chat:deep` are
> deliberately narrow single-purpose chains, also untouched.
>
> **zai added to `JSON_UNRELIABLE_PROVIDERS` (2026-07-05).** Confirmed via
> `litellm.get_supported_openai_params(model="glm-4.5-flash",
> custom_llm_provider="zai")`: `response_format` isn't in the supported
> list at all. `litellm.drop_params=True` (broker-wide) silently strips it
> on every call, so the model never receives an instruction to emit JSON —
> a 100%-guaranteed `InvalidJSON` on any JSON-format request, not just "a
> meaningful rate" like cerebras/cohere/openrouter. Confirmed live (request
> `#871336`): 200 OK, unparseable body, correctly fell through to the next
> provider per the JSON quality gate — now demoted behind the JSON-reliable
> providers on any JSON-format request instead of being tried first.

## Scopes & the reserved lane

A key's `scopes` (JSONB array) gate which capabilities it can serve; the selector
filters `scopes ? :scope`. The project's `allowed_scopes` gate which capabilities
it may call. Both are checked per request against `scope_for(capability)`.

This gives a **reserved lane** without per-project key ownership (keys stay a
shared pool):

- A gemini key scoped only to `["llm:edit"]` serves **only** `chat:edit` — bot
  `llm:chat` traffic can't select it.
- Mark it `is_reserve = TRUE` and it's picked **last** within its group (see
  selector ordering). Shared edit keys (`["llm:chat","llm:edit"]`) serve first;
  the reserve is the safety net, fresh because nothing else touches it.

So Stepan's Coach always finds a working gemini key for JSON, even when the bot
has driven every shared gemini key into cooldown — and we never pinned a key to
a project. Set it up in the dashboard: edit the key, scopes = `llm:edit`, tick
**reserve**.

## Cooldown resolution — provider-signal first (2026-06-29)

When a call rate-limits, `cooldown_until(key_id, provider, error_msg)` picks
the parking duration most-authoritative-first:

1. **Provider retry-after hint** — if the error carries "Please retry in
   24.5s" / "retry after 30s" / "retryDelay: 24s" (Gemini, OpenAI, Google),
   honour it exactly. The provider knows its own window.
2. **Monthly account/plan cap** (2026-07-03) — `is_monthly_quota_error`
   detects "trial key" / "api calls / month" (Cohere trial: "You are using a
   Trial key, which is limited to 1000 API calls / month") → `next_utc_month_start`
   parks until the **next UTC calendar month**. Not a rate limit that clears
   in minutes/hours — the account's monthly allowance is gone until the
   provider's billing cycle rolls over.
3. **Daily-quota exhaustion** — if the error says "tokens per day" /
   "per day" / "daily limit" (Cerebras "Tokens per day limit exceeded")
   and gave no hint, park the key until **UTC midnight** when the daily
   quota resets.
4. **Per-hour request cap** (2026-07-01) — Cerebras "Requests per hour limit
   exceeded" → park to the top of the next UTC hour. Same anti-storm logic as
   the daily tier, one hour scale. See the full list under **Adaptive
   cooldown** below.
5. **Otherwise** — the adaptive per-provider backoff below.

Why: a daily-exhausted key used to get the flat 60 s adaptive cooldown,
recover, get picked again, fail again — a retry storm (~290 wasted calls
every 2 minutes on Cerebras) looping until midnight. Now it's parked once
until reset, so the selector skips it entirely and the storm is gone. The
per-hour tier fixes the same storm at hour scale.

**LiteLLM cohere exception-mapping bug (2026-07-03).** LiteLLM 1.89.3 maps
cohere's HTTP 429 quota response to `litellm.APIConnectionError` instead of
`RateLimitError` (confirmed live against a real exhausted key: exception
class `APIConnectionError`, `status_code=500` — both wrong; traces to
`litellm_core_utils/exception_mapping_utils.py`'s cohere handler losing the
real status code somewhere before the generic-fallback raise). Worse: cohere's
message body says "...higher **rate limits** at..." (with a space) — that
doesn't match `ratelimit`/`rate_limit` in `classify_provider_error`'s
`_RATE_LIMIT_SIGNS`, so it fell all the way through to generic `'error'`.
`_penalize` does **nothing** for `'error'` (no cooldown, no `mark_dead`) — an
exhausted key was retried on **every single pick with zero backoff**: 1447
wasted attempts in 17h before this was caught. Fixed by adding `"trial key"` /
`"api calls / month"` to `_RATE_LIMIT_SIGNS` (classification) and
`_MONTHLY_QUOTA_MARKERS` (cooldown duration) — both are message-body substring
matches, so they route around the wrong exception type entirely rather than
depending on a LiteLLM fix or version bump. Checked gemini/openrouter for the
same pattern: both already log correct `RateLimitError` — this was cohere-only,
not a general LiteLLM classification failure.

## Adaptive cooldown (2026-06-26)

The 429 cooldown is no longer a flat 5 min. `routing/cooldown.py` exposes:

| Provider | Base cooldown | Why |
|---|---|---|
| `gemini` | 60s | RPM window resets every 60s |
| `mistral` | 10s | 1 RPS, recovers instantly |
| `cohere` | 60s | per-minute trial limit |
| `cerebras`, `groq`, `voyage` | 60s | rolling RPM |
| `deepseek` | 30s | paid, fast quotas |
| `anthropic`, `openai` | 120s | paid, conservative |
| `openrouter` | 300s | `:free` overload can last minutes |
| _(unknown)_ | 300s | safe default |

Exponential backoff: each consecutive 429 on the same key within a 1h
window doubles the wait (60 → 120 → 240 → 480 …) capped at 30 min.
Counter resets when the key has gone 1h without a 429.

**Anti-herd jitter (2026-07-02).** A provider's whole key pool tends to 429
together, so without jitter they'd all recover on the same tick and re-storm in
lockstep. Adaptive waits are stretched by a random 0-25% (`_adaptive_jitter`);
day/hour boundary resets get a random 0-90s offset (`_boundary_jitter`). Jitter
only ever LENGTHENS a wait — never shortens it — and is skipped when the
provider gave an explicit retry-after (we honour that exactly).

`cooldown_until(api_key_id, provider, error_msg)` resolves the `until`
timestamp most-authoritative-first:

1. **retry-after hint** in the message → wait exactly that.
2. **per-day cap** (`tokens per day`, `rpd`, …) → until UTC midnight.
3. **per-hour cap** — `is_hourly_quota_error()` (cerebras `Requests per hour
   limit exceeded`, 2026-07-01) → `next_hour_boundary()`, the top of the next
   UTC hour. Parking 60s just re-hit the wall and climbed the adaptive backoff
   one 429 at a time; the hour boundary parks a meaningful amount on the first
   hit. Self-calibrating off the provider's own message — no hard-coded
   per-hour rate.
4. **otherwise** → adaptive per-provider backoff (table above).

`services/llm_service.py` calls it on every rate-limit error. Vending mode
honours the client's `retry_after_s` instead — the client knows its provider
best.

## Selector — fair, anti-fingerprint ordering

`src/aibroker/routing/selector.py:pick_and_reserve(provider, scope)`

```sql
UPDATE api_keys SET last_used_at = now()
WHERE id = (
    SELECT id FROM api_keys
    WHERE provider = :provider
      AND is_active AND is_alive
      AND scopes ? :scope
      AND (cooldown_until IS NULL OR cooldown_until < now())
      -- daily_cost_used_usd/daily_used are reset-aware (FRESH_DAILY_*_SQL,
      -- see **Cost guard**) — a stale (non-today) value reads as 0, not its
      -- raw stored total.
      AND (daily_cost_cap_usd IS NULL OR fresh(daily_cost_used_usd) < daily_cost_cap_usd)
      AND (daily_limit = 0 OR fresh(daily_used) < daily_limit)
    ORDER BY
      k.is_reserve,
      <saturation_case>,            -- over-quota keys pushed to back
      random()                       -- pure random rotation within bucket
    LIMIT 1
    FOR UPDATE OF k SKIP LOCKED
)
```

(Plus a `LEFT JOIN` against `toks_today` (today's per-key req + token sum) and a
`defaults` VALUES CTE built from `PROVIDER_QUOTAS` so the saturation check sees
the right quota even when auto-discover hasn't populated `discovered_*_limit`
yet.)

Why each column matters:
1. **`is_reserve`** — non-reserve keys first; reserved Coach safety net is last.
2. **saturation case** — a key whose today's tokens/requests ≥ 95% of its cap
   (per-key `discovered_*` or `PROVIDER_QUOTAS` default) gets a `1`; clean
   peers get `0`. Soft sort, not a hard filter: when **every** peer is
   saturated the picker still returns one rather than fail the request.
3. **random()** — true random rotation. Replaced the LRU+`daily_used`
   ordering after we caught one workload-class (Coach JSON-heavy edits)
   monopolising the same handful of cerebras keys to token saturation
   while peers sat idle. Random distributes the next pick uniformly across
   eligible peers.

Atomic, race-free across replicas, advances LRU in one go. Postgres-only
(JSONB `?` + SKIP LOCKED + `random()`); exercised by the Postgres CI job.

**Hot-path performance (2026-07-02).** `toks_today` filters `created_at >=
date_trunc('day', now() AT TIME ZONE 'UTC')` (half-open range), NOT
`created_at::date = today` — the cast was non-sargable and forced a full seq
scan of `usage_log` (450k+ rows, ~220ms) on **every** pick, i.e. every provider
attempt of every request. The bare-column range uses `ix_usage_created_at`
(migration 005). A vestigial `recent` (15-min per-key error count) CTE was
also dropped — it was computed and joined but never referenced in `ORDER BY`
(removed when the recent-error sort gave way to `random()`), so it was pure
scan cost.

`mark_cooldown` normalises tz-aware datetimes to naive UTC — `cooldown_until` is
a naive `TIMESTAMP` and asyncpg rejects offset-aware values.

## Cost guard

`src/aibroker/routing/cost_guard.py:reserve_cost(api_key, project, estimated_cost)`
+ `release_cost(api_key, estimated_cost)`.

Three independent daily caps: per-key (atomic, see below), per-project (live
SUM from `usage_log`), global (30s-cached SUM vs `GLOBAL_DAILY_CAP_USD`).
Free-tier keys with `cost <= 0` skip the check entirely.

**Real reservation pattern (2026-07-03).** `run_chat` estimates the
worst-case cost (`estimate_llm_cost(model, prompt_tokens, max_tokens)` —
assumes the full `max_tokens` budget is generated) and calls `reserve_cost`
**before** the provider call; `release_cost` undoes the reservation once the
attempt resolves (success or failure), and `record_usage` then books the REAL
final cost on top — so a key ends up debited by exactly the real cost, the
estimate only ever counting toward the cap for the few hundred ms the call is
actually in flight. This used to be advertised on the landing page
("Reservation pattern: estimate before, settle after") without actually being
implemented that way — `check_caps` was called with a hardcoded
`estimated_cost=0.0`, so the per-key check was really just "is the counter,
loaded earlier in the request, already over cap" — a plain Python comparison
against a possibly-stale object, race-prone under concurrent requests against
the same key. The per-key branch is now a single atomic
`UPDATE ... WHERE ... RETURNING`: Postgres row-locks the key for the
statement's duration, so two concurrent reservations against the same key
serialize correctly instead of both reading the same stale value and both
passing. Per-project/global checks are unchanged (live/cached SUM) — a
smaller, accepted residual race remains there, a secondary backstop behind the
now-atomic, tighter per-key cap.

**Daily counters that never reset (2026-07-03, found while fixing the above).**
`api_keys.daily_used`/`daily_cost_used_usd` were never actually reset day to
day — nothing wrote `daily_reset_at` forward. Confirmed on prod: a key created
six days earlier had `daily_used=51,921` (~8.6k/day) with `daily_reset_at`
still `NULL` — a "daily" cap was really a **lifetime** cap, permanently
locking a key out the first time it was ever crossed. Fixed with a lazy,
self-healing reset (no cron dependency): every read (`pick_and_reserve`'s
`WHERE`) and write (`record_usage`, `reserve_cost`) of these two columns
treats them as `0` if `daily_reset_at IS DISTINCT FROM CURRENT_DATE`, and
every write stamps `daily_reset_at = CURRENT_DATE`. The exact SQL fragment is
shared (not duplicated) as `routing.selector.FRESH_DAILY_USED_SQL` /
`FRESH_DAILY_COST_SQL` so the read-side check, the atomic reservation, and the
final-cost write can never disagree on what "today" means.

**Cost source** (`providers/litellm_adapter.py:estimate_llm_cost`): LiteLLM's
`cost_per_token(model, prompt_tokens, completion_tokens)` pricing map, summed.
Unpriced models return 0 and are logged **once per model** — the guard must
never silently zero costs. (It did: `completion_cost(prompt_tokens=…)` lost that
kwarg in a LiteLLM bump on 2026-06-27, so every cost read 0 and all $-caps went
blind until 2026-07-01. The one-shot warning + a "known model costs > 0" test
now guard the regression.)

**Cache-aware pricing** (2026-07-02): `estimate_llm_cost` also takes
`cache_read_tokens`/`cache_write_tokens`, forwarded to LiteLLM's
`cache_read_input_tokens`/`cache_creation_input_tokens` kwargs. A cache read
(anthropic, ~0.1x input rate) now prices correctly instead of at the flat
input rate — before this, every anthropic call that hit its prompt cache
(see **Prompt caching** in [providers.md](providers.md)) had its `cost_usd`
over-counted (safe direction for the cost guard, but not the real bill).
`usage_log.cache_read_tokens`/`cache_write_tokens` (migration 006) persist
the activity; the project drill-down shows it as a **Prompt cache** card.

**Free-tier keys always bill $0** (`services/llm_service.py:_billed_cost`).
`estimate_llm_cost` prices by MODEL — it has no idea whether the specific key
that served the call is on a free plan. A free cerebras/gemini/mistral key
calling e.g. `gemini-2.5-flash` gets the same nominal per-token price LiteLLM
would quote a paid caller, even though the free plan absorbs it at $0 real
cost. `_billed_cost(key, meta)` zeroes `meta["cost_usd"]` whenever
`key.tier == "free"`, applied once right after each provider call
(`run_chat`/`run_embed`/`run_transcribe`) so every downstream use — `usage_log`,
the dashboard's per-key/per-project spend, `daily_cost_used_usd` — sees the
real (zero) cost. (Regression: restoring real pricing in the fix above made
free-tier keys show non-zero "spend" for the first time — $5.26 accrued
across 51 free keys in a few hours before this landed. One-time prod cleanup:
zeroed `usage_log.cost_usd` and reset `daily_cost_used_usd` /
`monthly_cost_used_usd` / `total_cost_usd` for tier='free' keys.)

> **Voyage is the one exception to "free tier bills $0" (2026-07-07).**
> Confirmed live via Voyage's own dashboard (Usage → Free Token tab): the
> whole `voyage-3` family (`voyage-3`, `voyage-3-large`, `voyage-3-lite`,
> `voyage-3.5`, `voyage-3.5-lite`, `rerank-2`, `rerank-2-lite`) shows **0
> used / 0 remaining** free tokens on our accounts — only the newer models
> (`voyage-context-3`, `voyage-4` family, `voyage-multimodal-3(.5)`) get the
> 200M free-token allocation. A `tier="free"` label on a voyage key does NOT
> mean $0 real cost: a real $0.51 invoice arrived for July while
> `usage_log` showed $0.00 for every single call, because `_billed_cost`
> zeroed it out unconditionally regardless of provider. Originally fixed by
> special-casing `key.provider == "voyage"` in `_billed_cost` to always bill
> the real LiteLLM-estimated cost — **but that carve-out was reverted the same
> day** once we switched the default model to `voyage-4` (below): voyage-4
> gets 200M free tokens/month (genuinely $0 under our run-rate), so a voyage
> free-tier key is now correctly $0 like any other free key, `tier` is the
> single source of truth again, and there's no voyage exception. If a voyage
> account ever burns its 200M monthly free allocation, flip that specific key
> to `tier='paid'` and it bills real cost from then on. (The carve-out had
> also become a latent landmine: LiteLLM currently has *no* price for
> `voyage-4`, so it books $0 anyway — but the moment LiteLLM ships a
> voyage-4 price, the old carve-out would have started billing the 200M free
> tokens as phantom spend.)
>
> **Switched to `voyage-4` (2026-07-07).** Live audit of real usage: both
> callers (Vera, Stepan2) run ~61M input tokens/month combined against
> voyage-3's $0-free ceiling — real ~$3.7/mo, invisible until the fix above.
> `voyage-4` gets 200M free tokens/month, comfortably covering that run-rate
> at $0. Confirmed live that `voyage-4` outputs the SAME 1024 dims as
> voyage-3 (no storage schema change needed) but is a genuinely different
> vector space — an old voyage-3 row compared against a new voyage-4 query
> vector produces a same-length, silently-wrong cosine score (both Vera's
> `brain_search/app.py:_cosine` and Stepan2's `rag.py:retrieve` only guard
> against differing LENGTH, not differing space). Every existing embedding
> in both projects must be re-embedded before/soon after this switch — see
> the one-off backfill scripts in each repo, run right after this deploy.
> `voyage-context-3` was also tested live and rejected for now: it 400s with
> `"requires enable_auto_chunking=True or input_type='query'"` — a different
> request shape our generic embed path doesn't send; worth a dedicated
> integration later, not blocking this fix.

**Peak/valley surcharge** (`providers/peak_pricing.py:peak_multiplier`): DeepSeek
charges 2x during peak UTC hours (01:00–04:00 and 06:00–10:00) from mid-July
2026. `estimate_llm_cost` multiplies the base price by that factor, so the
recorded cost and every $-cap reflect the real peak bill — the same daily budget
buys half as many peak deepseek tokens, and per-key/global caps trip 2x faster in
peak, throttling paid deepseek use without any routing change. Dormant until
`DEEPSEEK_PEAK_FROM` (set the confirmed date when DeepSeek announces it). If peak
deepseek volume ever grows material, the next lever is off-peak backfill
(client-side) or a peak-hours chain demotion — not built yet.

## Selection policy — the whole picture (refactored 2026-06-28)

Choosing which key serves a request is **deterministic, self-calibrating
rules** — no ML, no LLM-judge, and no hardcoded number as a *sole* source
of truth. Every provider-specific constant is a **seed** that real
observations override.

### Resolution tiers (highest wins)

| Signal | manual (operator) | learned (observed) | seed (code) |
|---|---|---|---|
| daily req/tok/in/out quota | `manual_*_limit` | `discovered_*` (response headers) | `PROVIDER_QUOTAS` |
| single-request size ceiling | — | `provider_observations` (from 413s) | `SEED_MAX_REQUEST_TOKENS` |
| cooldown duration | — | adaptive exponential backoff | `COOLDOWN_BASE_S` |

A seed that's overridden by reality is a legitimate bootstrap, not a
crutch: it's used only until the provider teaches us the real value.

**Paid keys drop the seed tier entirely** (`quota_for_key`): `PROVIDER_QUOTAS`
are FREE-tier limits, so a `tier='paid'` key keeps only its explicit
manual/discovered axes — a paid gemini key no longer reads as 212% of the 1,500
free RPD. Its dollar budget lives in the separate `daily_cost_cap_usd` column on
the dashboard, not the quota bar.

**Discovered daily quota must be confirmed daily (2026-07-03).** Same root
cause as the cerebras/mistral seed fixes below, but at the auto-discover
layer this time: `extract_quota_headers` used to trust ANY bare (non
`-day`/`-1d`-suffixed) `x-ratelimit-limit-{requests,tokens}` header as a daily
figure. Groq's bare headers are **not** daily — confirmed live:
`x-ratelimit-limit-tokens=8000` resets in **~547ms** (a rolling TPM bucket) and
`x-ratelimit-limit-requests=1000` resets in **~1h33m** — neither is a 24h
window. A groq key logging 90k-170k real tokens/day against a stored "8000
tokens/day" `discovered_tok_limit` read 1000%+ saturated and showed fully red
on the dashboard while perfectly healthy (0 real 429s that day).

Fixed at the source: `extract_quota_headers` now only trusts a bare header as
daily when the provider's own `x-ratelimit-reset-{requests,tokens}` duration
is within `_MIN_DAILY_RESET_S` (20h) of 24h — otherwise it returns `None` for
that axis rather than mis-storing a sub-day bucket as `discovered_*_limit`.
Existing bogus rows (4 groq keys, `discovered_tok_limit=8000` /
`discovered_req_limit=1000`) were cleared on prod; the seed
`PROVIDER_QUOTAS['groq']` (14,400 req / 500,000 tok) now applies until a
genuinely daily-scoped header is observed.

Don't confuse this with `SEED_MAX_REQUEST_TOKENS['groq'] = 8_000`
(`context_limits.py`) — same 8k figure, entirely different axis: that one is
correctly a **single-request** size ceiling (a lone request bigger than
groq's TPM burst always 413s), not a daily cumulative cap. The bug was
specifically applying the header's rolling-bucket number to the *daily* axis.

### Per request

1. **Size filter** — `run_chat` estimates prompt tokens (≈chars/4) and
   drops providers whose effective ceiling (`min(learned, seed)`) can't fit
   it. groq (free TPM ≈8k) won't be offered a 24k Coach prompt. The request
   still reaches a provider that CAN serve it, so **the answer and its
   quality are identical** — only the guaranteed-failing attempts are
   skipped. If every provider is too small, it falls back to the full chain
   (never starves).

2. **Availability filter** (selector SQL `WHERE`) — active, alive, scope
   match, not in cooldown, under hard cost caps.

3. **Saturation soft-skip** (selector `ORDER BY`) — keys ≥95% on any quota
   axis (req/tok/in/out, resolved manual>discovered>seed) sink to the back;
   used only if every peer is also full.

4. **Fair rotation** — `random()` among the healthy bucket so no key is
   burned first while peers idle.

### Self-learning the size ceiling

When a provider rejects a request as too large (`is_too_large_error`:
413 / context length / "request too large"), `run_chat` records the prompt
size into `provider_observations` (min of all rejections) and jumps to the
next provider — no wasted key retries. Next time, any prompt ≥ that size
skips the provider automatically. So if groq's tier changes, the broker
recalibrates from one rejection instead of waiting for someone to edit a
constant. Measured impact at rollout: ~108 guaranteed-failing groq calls/day
on chat:smart eliminated.

**Two guards against learning garbage** (added 2026-06-29 after a real
incident):

1. **Rate-limit ≠ size.** Groq's TPM 429 literally reads *"Request too
   large for model … tokens per minute (TPM)"*. `is_too_large_error`
   checks `_RATE_LIMIT_MARKERS` first and returns `False` if any match —
   a transient rate-limit must never teach a permanent size ceiling.
2. **Floor.** `record_too_large` refuses to store any ceiling below
   `MIN_LEARNABLE_CEILING` (4 000 tokens) — no real model rejects a
   200-token prompt for size. Without this, `LEAST()` had converged the
   learned ceilings of cerebras/groq/gemini down to ~210 tokens, so the
   broker skipped its three best free providers on essentially every
   prompt and dumped all traffic on mistral. Resetting the bogus rows and
   adding the floor restored free-first routing.

## Failure → next key → next provider

The orchestration lives in `services/llm_service.run_chat` (routes stay thin).
For each provider in the chain it tries **up to `_max_keys(provider)` keys**
before falling through — a direct client loops all of a provider's keys, and
free keys 429 constantly, so a single rate-limited key must not sink the
request. Default is 5; **gemini and cerebras are capped at 3** (2026-07-02):
their keys rate-limit in lockstep (gemini's ~20/day/model free cap, cerebras
rolling RPM), so a 4th/5th retry there rarely finds a healthy key and is pure
latency before the chain moves on. The selector hands a fresh LRU key each pick
and `_penalize` cools failed ones, so each retry gets a different key.

**Dynamic per-request attempt budget (2026-07-07, was a flat `12`).** The total
provider-call attempts for one request = `_attempt_budget(chain)` = the SUM of
every provider's key allowance across the actual chain ("try every key we have
before giving up"), bounded by an absolute runaway backstop `_MAX_ATTEMPTS_ABS`
(60). This guarantees the paid tail (deepseek/anthropic/openai) is reached
before a 503 — a saturated provider yields no key and costs 0 attempts, so the
chain falls through to the tail fast. The old flat `12` predated the chains
growing to 14 providers: during the 2026-07-07 incident (cerebras+groq daily
quota exhausted, overflow saturating the free head) the cap was consumed by
early providers and long dialogs 503'd without ever reaching the paid tail. A
per-provider-call `timeout` (`_CALL_TIMEOUT_S`, uniform across every
key/provider; chat:deep gets its own 19 min since nemotron legitimately runs
minutes as an async job) is the companion safeguard: without it a hung
upstream would block until the client's own read timeout (a hard 504) instead
of the broker cleanly failing over. Past the budget the request 503s.
>
> **`_CALL_TIMEOUT_S` raised 45s → 60s (2026-07-07, explicit ask).** Applies
> uniformly to every key/provider. Trade-off worth knowing: Stepan2's own
> client read timeout for `chat:fast` is ALSO 60s (`llm_read_timeout_s`) — a
> single hung attempt at the new ceiling can now consume that entire client
> budget, leaving no time for the chain to fail over to the next provider
> before the CLIENT gives up (a 504/abort instead of a clean 503).
> `chat:smart`'s 90s client budget still has headroom for one hang + a
> fallback attempt. Flagging this rather than silently tightening `chat:fast`
> back down, since the 60s was an explicit choice.
>
> **LiteLLM's `timeout` kwarg alone is NOT reliable — confirmed live the same
> day.** Right after the 60s raise, Stepan2 started seeing genuine 504s
> (nginx's `proxy_read_timeout` cutting the connection). Live logs showed WHY:
> a zai key was completing normally (a real response, just JSON-invalid) at
> **90-180 seconds wall time** on a `timeout=60` request — LiteLLM never
> raised a `TimeoutError` at all. Whatever LiteLLM/the zai plugin does
> internally with the `timeout` kwarg isn't a hard guarantee. Fixed by
> wrapping `litellm.acompletion(**kwargs)` in `asyncio.wait_for(..., timeout=)`
> in `call_llm` — an independent, broker-side enforcement that doesn't depend
> on LiteLLM's own behavior. Confirmed live with a mocked never-returning call:
> `asyncio.wait_for` cancels it and raises `TimeoutError` at the exact ceiling.
>
> **`TimeoutError` classifies as `rate_limit`, not generic `error`.** A bare
> `asyncio.TimeoutError`/`TimeoutError` carries no message, so none of the
> string-substring signs above can ever match it — it would have fallen to
> generic `error` (no cooldown), hitting the same overloaded key again
> immediately with zero backoff, the exact failure mode this classifier
> exists to prevent. `classify_provider_error` now special-cases
> `isinstance(exc, TimeoutError)` → `rate_limit` before any string matching: a
> provider/key that's currently too slow is transient overload, not a dead
> credential, and `cooldown_until` resolves it via the provider's normal
> adaptive backoff (empty exception message doesn't match any quota marker).

**Translate exact-match cache (2026-07-02).** `run_chat` first checks
`services/response_cache.py` for deterministic capabilities — `is_cacheable`
allow-lists `translate` only (same phrases recur verbatim, and a translation of
fixed input is stable). A hit returns immediately (`provider="cache"`, cost 0)
with no provider call; a success is stored (LRU + 24h TTL, per-replica,
in-process, keyed on the full request signature incl. model/params). chat/* is
never cacheable — non-deterministic.

**Skip size-filter on the small-prompt path (2026-07-02).** The learned-ceiling
size filter (`learned_ceilings()`, a DB round-trip) runs only when
`est_tokens >= MIN_LEARNABLE_CEILING`. Below the floor every provider fits by
definition, so chat:fast/translate (avg ~1k / ~370 tokens) skip the query
entirely — the highest-volume path no longer pays for a filter that can't change
the chain.

**JSON-reliable ordering (2026-07-02).** When `response_format` asks for JSON,
`run_chat` runs the chain through `deprioritize_for_json` first, pushing
`JSON_UNRELIABLE_PROVIDERS` (cerebras/cohere/openrouter — their gpt-oss /
command-r7b mangle JSON at volume) to the back so the reliable providers lead.
Cuts InvalidJSON at the source instead of after a wasted call. groq stays
reliable (grammar-constrained JSON at volume); nothing is dropped, so a JSON
request still reaches every provider.

Per key:

1. `pick_and_reserve` (None → no more keys for this provider → next provider).
2. `check_caps` (CostGuardError → audit → next provider; caps are project/global).
3. `call_llm`. Exceptions are classified by `classify_provider_error`:
   - rate-limit / 429 → `mark_cooldown` 5 min, **try next key**.
   - 401/403/auth → `mark_dead`, **try next key**.
   - other → log, **try next key**.
4. **JSON quality gate:** when the request asked for JSON (`response_format`
   type `json_object`/`json_schema`) and the body doesn't parse, the response is
   billed but treated as a failure → **next PROVIDER** (2026-07-02: was next
   key). Malformed JSON is a model property — sibling keys of the same provider
   re-mangle the same prompt, so retrying them just multiplied the wasted
   tokens ~5×. Deterministic, no LLM judge. Paired with gemini's
   `reasoning_effort=disable` in the adapter, which stops 2.5's thinking from
   eating the token budget and truncating the JSON.

Chain exhausted → HTTP 503. The first success returns text + meta, including the
chosen key's **label** (surfaced to clients for their cost/usage chip).

> **Two more real messages added to `classify_provider_error` (2026-07-05),
> both found by reading live logs, not guessed.** `_AUTH_SIGNS` now includes
> Anthropic's `"credit balance is too low"` — confirmed live, the `default`
> key had been failing ~2743 times/day, generic-`error`-classified (no
> `mark_dead`), so it kept getting picked and kept failing at zero cost to
> itself but real waste on every request whose chain reached anthropic.
> Billing exhaustion isn't transient, so it belongs in the same bucket as
> 401/403 — `mark_dead` stops real traffic from hitting it, and the
> monitor's own probe (independent of `is_alive`) keeps checking every
> `MONITOR_INTERVAL_S` and auto-revives it the moment credits are topped up.
>
> `_RATE_LIMIT_SIGNS` now includes DeepSeek's `"response_format type is
> unavailable"` — confirmed live, hit every single deepseek key identically
> (not one bad key, a provider-side feature outage), ~2510 wasted
> attempts/day. Not literally a rate limit, but the wanted behavior
> (throttle this key, don't `mark_dead` it — the credential is fine) is
> exactly rate_limit's.

> **A third message added, Voyage's "no payment method" (2026-07-07),
> confirmed live via `docker logs` (24h window).** Every voyage key
> (lev/verandapay/eatmeat/itstep/...) was hitting `VoyageException -
> "You have not yet added your payment method ... reduced rate limits of 3
> RPM and 10K TPM"` dozens of times/day, falling to generic `error` (no
> `429`/`401`/`403`/`auth` substring) — zero cooldown, hammered on every
> pick. The account isn't dead or unauthorized, just throttled to a lower
> ceiling, so `_RATE_LIMIT_SIGNS` now matches `"reduced rate limits"` →
> `mark_cooldown` using voyage's existing 60s `COOLDOWN_BASE_S` entry with
> adaptive backoff, instead of an instant zero-backoff retry storm.

> **`_AUTH_SIGNS` gains zai's "Invalid API parameter" (2026-07-07), found
> live during a real incident.** cerebras+groq hit their daily token quota
> unusually early (~10:38 UTC) and cooled down until UTC midnight — with
> our two highest-capacity free providers gone, overflow traffic from Vera's
> triage volume and Stepan2 hammered the remaining pool (72 keys: only 4
> alive-with-no-cooldown at the worst point) hard enough to surface a
> latent bug: zai key `eatmeat` failed 3141 of ~3189 attempts in 30 minutes
> (98.5%) with `ZaiException - "Invalid API parameter, please check the
> documentation."`, while every other zai key on the same account
> type/model succeeded normally in the same window — a persistent
> config problem isolated to that one key/account, not a shared zai outage
> or a rate limit. Was generic `error` (no `mark_dead`), so it got hammered
> with zero backoff on every pick that reached zai. `mark_dead` stops real
> traffic on it; the monitor's own probe keeps checking and auto-revives it
> once whatever's misconfigured on that account is fixed. The `zai/mbar`
> key in the same incident showed a normal `RateLimitError` — already
> correctly cooling down, no fix needed there, just genuine overload.

> **Narrow signatures moved to provider-scoped maps (2026-07-07).** Three of
> the fixes above were narrow, provider-specific strings — deepseek's
> `"response_format type is unavailable"`, voyage's `"reduced rate limits"`,
> and zai's `"invalid api parameter"`. Left in the GLOBAL
> `_RATE_LIMIT_SIGNS`/`_AUTH_SIGNS`, they risked mis-penalising an unrelated
> provider's healthy key on a superficially-similar message — most dangerously
> zai's, which is an `auth`/`mark_dead` verdict: a request WE built wrong
> eliciting "invalid api parameter" from some other provider would have killed
> that provider's key. They now live in `_PROVIDER_RATE_LIMIT_SIGNS` /
> `_PROVIDER_AUTH_SIGNS`, keyed by provider, and `classify_provider_error(exc,
> provider)` only applies them when the failing key matches (`_penalize`
> passes `key.provider`). The genuinely-generic signs (`429`, `quota`, `credit
> balance is too low`, …) stay global. Each fix is now surgical.

> **mistral's bare 401 is monthly quota, not a dead key (2026-07-10).** Live
> probe confirmed mistral returns `AuthenticationError - {"detail":
> "Unauthorized"}` with NOTHING in the text about quota — indistinguishable
> from a genuinely revoked key. But on our 7 accounts it's the monthly Vibe-
> plan call allowance being exhausted (confirmed via Mistral's admin console),
> and the key returns fine on the billing-cycle reset. It was classified
> `auth` → `mark_dead` (dashboard: "мёртв/auth failed") — technically true
> (401 = unauthorized *right now*) but misleading, and it recovered only via
> the monitor's periodic re-probe. Now handled **consistently across BOTH
> classification paths** (fixing a latent DRY gap — the request path and the
> monitor probe each classify errors independently and must agree):
> - request path: `_PROVIDER_RATE_LIMIT_SIGNS["mistral"] = ("unauthorized",)`
>   → `rate_limit`, and `cooldown.cooldown_until` has a provider-scoped
>   `_is_provider_monthly` rule → cools to `next_utc_month_start`, key stays
>   `is_alive`.
> - monitor: `health_probes` returns `("cooldown", 401, "monthly quota")` for
>   mistral (not `dead`), and `monitor.tick` parks it until next month on that
>   hint (not the token 5 min — else it re-cools every 5 min all month). The
>   monthly-vs-short decision is a pure `monitor._cooldown_end(hint)` helper
>   (unit-tested) so tick stays thin.
> Net: mistral shows the honest "monthly quota, resets DATE" cooled state, not
> "dead", and comes back automatically on reset. Assumption (documented):
> EVERY mistral 401 is treated as monthly — a genuinely revoked mistral key
> would stay cooled-and-retried-monthly rather than dead, which is harmless
> (still out of rotation). The durable answer is per-(provider, model) state
> (roadmap §3.1); this is the correct interim for the one provider it affects.

> **Model-gone (404) breaks to next provider WITHOUT penalizing the key
> (2026-07-10, roadmap Phase 0).** A vanished/unprovisioned MODEL is neither a
> dead key nor a rate limit: the key's OTHER models still work, and sibling
> keys of the same provider run the same dead model. Confirmed live: nvidia
> `kimi-k2.6` (chat:fast) started 404-ing "Function not found for account"
> ~30x/hr, and `deepseek-v4-pro` (chat:smart) began timing out at ~91s — both
> models silently vanished from our account's provisioning while nemotron
> (chat:deep) stayed alive. Two-part fix: (1) both dead models **removed from
> their chains** (nvidia stays in chat:deep only) — the real, immediate fix;
> (2) `_is_model_unavailable(exc)` (litellm `NotFoundError` type, or "not found
> for account"/"model_not_found"/"does not exist" in the body) makes `run_chat`
> `break` to the next provider and record the error but **skip `_penalize`** —
> so a future model that dies mid-chain doesn't wrongly cooldown/mark_dead a
> key whose other models are fine. This is the interim, model-level fix for the
> drift problem; the durable one is the per-(provider, model) handler with
> its own liveness/cooldown/quota/timeout — see `docs/roadmap.md` §3.1.

## Embedding: retry same-provider keys, never cross providers (2026-07-02)

`run_embed` used to be a stark outlier vs `run_chat`/`run_transcribe`: **one**
`pick_and_reserve` call, **zero** retry — any failure raised `EmbedFailed`
(HTTP 502) immediately. Real driver: `voyage APIConnectionError` was 100% of
7-day embedding failures (621 calls) — a transient network blip, not a dead
key or a dead provider. One flaky connection killed the whole request with a
live pool of 6 voyage keys sitting unused.

Fixed to mirror the chat retry loop — `for _ in range(_max_keys(provider))`,
picking a fresh key each attempt (`_penalize` cools the failed one first) —
**but scoped to the single `provider` the caller asked for**. Unlike
`chat_for(capability)`'s multi-provider chain, embedding deliberately does
**not** walk to a different provider on exhaustion: `voyage-3` and
`cohere embed-english-v3` are different vector spaces, so a silent
voyage→cohere fallback mid-batch would write incomparable vectors into the
same index — a correctness bug, not a resilience win. `provider` stays
exactly what the client specified; only the key rotates.

`EmbedFailed` (all keys exhausted) → HTTP 502, same as before — now it means
"the whole provider is actually down", not "one key blipped once".

**`run_embed` intentionally does NOT reserve against the cost guard.** Unlike
`run_chat` (which calls `reserve_cost`/`release_cost` around each attempt),
the embed path books real cost only *after* the fact via `record_usage`, with
no pre-call cap admission. This was harmless while embeddings were free
(voyage-3 was the only embed provider and, once on voyage-4, genuinely $0
under the 200M/mo free allocation — `_billed_cost` returns $0 for its
free-tier key). It IS a gap to remember if a paid embed provider is ever
added or a voyage key is flipped to `tier='paid'`: embed spend would then be
uncapped by the project/global $-guard. Documented here as a deliberate,
known limitation rather than an oversight.

**Prefer native structured output over the gate.** The JSON gate is a
post-hoc safety net. The *root-cause* fix is for the caller to send a full
`response_format={"type":"json_schema","json_schema":{…,"strict":true}}`
instead of a bare `json_object`: providers that support it (gemini, openai,
groq) then grammar-constrain generation, so the model *cannot* emit invalid
JSON and the gate never fires. The broker forwards the schema byte-for-byte
(`call_llm`) — it can't invent one, so this win depends on the client sending
it. cerebras/cohere don't grammar-constrain, which is why they're in
`JSON_UNRELIABLE_PROVIDERS` and deprioritized for JSON either way.

> **json_schema → json_object downgrade for deepseek (2026-07-07).** DeepSeek
> disabled the strict `{"type":"json_schema"}` sub-type server-side: a
> json_schema request 400s with `"This response_format type is unavailable
> now"` on every key (~2414 wasted triage calls in 6h), while plain
> `{"type":"json_object"}` returns valid JSON fine — confirmed live with all
> three shapes. This was quietly removing a whole paid-tail provider from
> service under load. The deepseek adapter (`providers/adapters.py`, see
> **Provider adapters** in [architecture.md](architecture.md)) downgrades
> json_schema → json_object in its `prepare()` before sending; the JSON intent
> survives and the post-hoc JSON gate + caller validation replace the lost
> server-side grammar enforcement. `litellm.supports_response_schema` is NOT
> usable as the gate — it reports deepseek supports json_schema
> (stale/optimistic), so the downgrade is broker-maintained and confirmed live.
> The provider-scoped `_PROVIDER_RATE_LIMIT_SIGNS["deepseek"]` cooldown stays
> as defence-in-depth. Remove deepseek from the set if it re-enables
> json_schema.

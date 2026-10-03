# Provider / model / chain choices — dated history

This file holds the incident history and measurements that used to sit in code
comments in `routing/chains.py`, `providers/litellm_adapter.py` (DEFAULT_MODEL,
MODEL_ROTATION), `providers/quotas.py`, `routing/cooldown.py`,
`providers/provider_errors.py` and `providers/health_probes.py`. The code keeps a
one-line rationale per entry; the evidence is here. Entries are grouped by subject.

## Chain order (routing/chains.py)

- **2026-07-05 strict free-first.** Paid (deepseek/anthropic/openai) moved to the
  tail of every `chat:*` chain, after ALL free providers. Was: deepseek ahead of
  openrouter/github/sambanova/zai "for backfill speed", which fired a paid call as
  soon as the first 5 free providers were saturated while 3+ live free providers
  were still untried. Explicit choice: slow-but-free beats fast-but-paid.
- **2026-07-17 deepseek at the head of chat:smart** (owner-approved exception, cap
  raised $0.50 -> $1): Stepan's money lane wants one strong model, a warm
  per-account prompt cache (cache-hit input $0.0028/M, measured 80-99% hit) and
  independence from free-pool storms. A reply cost ~$0.0003-0.0005.
- **2026-07-21 chat:smart pruned** to providers that give good smart answers:
  removed mistral (0 ok in 7d, keys in AuthError), cohere command-r7b (25
  InvalidJSON / 4 ok), openrouter gemma-4-31b (0 ok / 114 rate-limited), and
  gpt-oss-120b (cerebras/groq/cloudflare) by owner call. nvidia stays out (v4-pro
  91s timeouts). Tradeoff accepted: once deepseek's $1 cap and the free
  gemini/sambanova quota are spent, smart lands on the paid tail.
- **2026-07-21 chat:fast is FREE-ONLY**: the whole paid tail removed (owner: do
  not burn the scarce deepseek on the fast lane). Removing only deepseek would have
  shifted ~2969 calls/week to anthropic-haiku (~6x the price). All 9 free
  providers saturated -> fast returns None and the job retries.
- **2026-07-24 (owner)** chat:smart rotates ONLY gemini, anthropic and
  DeepSeek-family models; openai (gpt-5) removed (never earned its place as the
  last resort). sambanova stays: its smart model IS DeepSeek-V3.2 on a free tier
  (111 successes at $0 during DeepSeek's 2026-07-22 empty-body degradation).
- **2026-08-26 gemini AHEAD of deepseek in both money lanes**, measured on a real
  135k-char sales prompt (N=8, JSON mode, max_tokens=2000):

  | model | ok | invalid | truncated | out avg | median | p90 |
  |---|---|---|---|---|---|---|
  | deepseek-v4-flash | 6/8 | 2 | 2 (length) | 2035 | 18413ms | 26054 |
  | gemini-3.1-flash-lite | 8/8 | 0 | 0 | 113 | 1405ms | 1579 |
  | gemini-3.5-flash-lite | 8/8 | 0 | 0 | 84 | 1366ms | 1661 |
  | gemini-3.6-flash | 8/8 | 0 | 0 | 81 | 9676ms | 14632 |

  DeepSeek's thinking pass eats the 2000-token ceiling; 4000 was measured WORSE on
  2026-07-21. Gemini is 13x faster, never truncates and is free. deepseek stays the
  paid fallback.
- **2026-07-23 chat:sales** (smart-LLM sales mode): Anthropic Sonnet leads, billed
  on its own $5/day key; deepseek the cheap paid fallback; gemini/sambanova the free
  tail; openai deliberately not wired. It reuses scope `llm:chat` so no key/project
  re-scoping was needed.
- **2026-07-10 chat:code / chat:edit**: anthropic re-added after the balance was
  topped up. chat:edit (Coach editor) is JSON-reliable providers ONLY: mistral /
  cohere returned Bahasa-drifted, torn JSON on 2026-07-01 when gemini was cooling.
- **2026-07-04 chat:deep**: nvidia Nemotron 3 Ultra 550B-A55B, real 1M context,
  but ~27s for 5 tokens on the free oversubscribed pool. Own scope `llm:deep`,
  single-provider chain by design (a miss falls to 503).
- **2026-07-16 prefilter**: zai removed — prefilter is always JSON and zai has no
  response_format support (guaranteed billed-but-unusable InvalidJSON).
- **2026-10-02 prefilter/translate**: cerebras removed — its only model there,
  gemma-4-31b, was deleted by Cerebras on 2026-09-03 and all 14 keys are inactive
  (free tier became a 30-day $5 trial).
- **translate**: small fast NON-reasoning models first (gpt-oss "thinks" ~16s even
  on one phrase and starved the client's 15s timeout).
- **2026-07-01 structured**: cerebras dropped (gpt-oss returned HTTP-200 malformed
  JSON, ~4.6k/week InvalidJSON).
- **2026-09-12 mistral removed from every chain**: all 7 free keys answer 429 code
  1300 with `x-ratelimit-limit-req-minute: 0` — the free tier has ZERO RPM. 7d: 0 ok
  / 2174 errors. Spec kept ("known but not chained").
- **2026-10-02 cohere removed** from chat:fast, structured, prefilter, translate
  (kept in `embedding` as voyage's fallback). 7d chat:fast 1 ok / 66 err (trial
  keys at 1000 calls/month, InvalidJSON 17); structured 9 ok / 1136 err.
- **2026-10-02 openrouter removed** from chat:fast and structured: gemma-4-31b-it:free
  is capped at 50 req/day per ACCOUNT (1000 only after >= $10 lifetime purchases),
  so 7 keys never had the volume. 7d chat:fast 0 ok / 101 err, structured 0 / 1744.
- **2026-07-10 nvidia removed from chat:fast** (kimi-k2.6 -> 404 "Function not
  found for account"); stays in chat:deep.
- **2026-07-07 cloudflare in chat:fast** (gpt-oss-120b): live with the real strict
  triage json_schema, ~1.6s. 2026-07-10 chat:smart/chat:code added.
- **2026-07-01 vision: anthropic dropped** (400 "Unable to download the file" on
  image URLs anthropic's fetcher cannot reach); re-add when callers send base64.
  2026-07-04 cloudflare llava tried and removed same day (Workers AI wants raw
  byte arrays, litellm does not convert -> 400 "Unsupported image data").
- **2026-08-31 vision: `local` FIRST** (Qwen3-VL-4B via llama.cpp): vision ran an
  8% success rate over 14 days (1762 ok / ~20000 CapBlock+RateLimit). Local is
  ~69s/image and unmetered; the cloud tail takes the 00:00 UTC peak (162 images in
  one hour). 2026-09-12 sambanova (free gemma-4-31B-it, 9 keys x 20/day) added as a
  second free cloud pool and deepseek (~$0.0002/image off-peak) as the paid tail
  AHEAD of openai (openai has no vision key).
- **2026-09-12 vision is a FREE-WALK capability** (owner: slow but free): the paid
  tail is reachable only via the job queue's final-retry escalation.
- **transcription: 2026-07-18 local first, 2026-07-26 moved BEHIND groq.** Local:
  131-168 s per transcription, 35 timeouts vs 23 successes/24h; groq 753-1150 ms,
  ~150x faster and also free. Leading with local burned the 180 s ASR timeout before
  falling through, and those fall-throughs drained groq's daily quota.
- **2026-09-23 decision lane** (TypeSafe Jev): OpenRouter is the only host; served
  on PAID keys only so spend lands on the prepaid account. A $0 free-tier key is NOT
  refused (200 OK, cost booked), so this is a routing choice, not an error workaround.

## JSON reliability

- **unreliable** = cerebras, cohere, openrouter, groq: kept in chains as a last
  resort for JSON (a maybe-malformed retry beats a 503) but sunk behind reliable
  providers. cerebras gpt-oss ~4.6k/week InvalidJSON; cohere r7b; openrouter
  gpt-oss:free. **2026-09-07 groq added**: its grammar-constrained JSON mode 400s
  server-side ("Failed to validate JSON") — 7d on 4 keys: structured 553 ok / 383
  BadRequest, chat:fast 2203 ok / 258 BadRequest = 641 guaranteed-failed attempts/week.
- **incapable** = zai: no `response_format` in litellm's supported params;
  `drop_params=True` silently strips it (live: 200 OK, unparseable body, request
  #871336). 2026-07-16 deprioritising was not enough (44 InvalidJSON / 45 min) so it
  is EXCLUDED on JSON requests.

## Models (defaults / rotation)

- **cerebras 2026-07-10** added gemma-4-31b (fast non-reasoning) for translate/prefilter;
  deleted by Cerebras 2026-09-03, entries removed 2026-10-02. chat:* stay on
  gpt-oss-120b. zai-glm-4.7 skipped (reasoning, content=None at low max_tokens).
- **local**: self-hosted faster-whisper (asr-local) and Qwen3-VL-4B Q4_K_M via
  llama.cpp. The model string is a routing label only; the transports call the
  services directly.
- **gemini**: 2026-07-10 chat:smart 2.5-pro -> 2.5-flash (pro: ~100% 429 on free,
  4096 err / 0 ok in 3 days). 2026-07-18 prefilter/translate -> 2.5-flash-lite
  (quota is per model per key; flash-lite has a 1000 RPD/key bucket). **2026-08-16
  gemini-3.7-flash tried and REVERTED**: N=10 on a real sales prompt: 7/10 ok (3
  ServiceUnavailable), median 1495 ms vs 2.5-flash 10/10 at 878 ms; vision in prod
  2 ok + 1 timeout vs 77 ok @1998 ms. Re-measure at N>=10 on a realistic prompt
  before moving any lane. The `_GeminiAdapter` reasoning_effort fix stays.
- **gemini rotation** (2026-08-26, 2026-09-12): Google meters the free tier per model
  per key (live cap 20/day on our projects), so a single model used ~78% less than
  available (320 of ~1440 calls/day). Every rotated model was measured N=5 on a real
  JSON prompt: 3.5-flash-lite 5/5 median 906 ms; 3.6-flash 5/5 1807 ms; 3.1-flash-lite
  5/5 3455 ms. Excluded: `-latest` aliases (flash-lite-latest 0/5 — resolves to a
  3.7-class model rejecting reasoning_effort=disable; flash-latest 1/5), 3.7-flash 3/5,
  3-flash-preview 4/5 with 6.4 s median. Vision (3 real images): 3.5-flash-lite 3/3
  1967 ms, 3.5-flash 3/3 2009 ms, 3.1-flash-lite 3/3 3143 ms, 3.6-flash 2/3 (one 29 s
  call — excluded, would eat the 60 s vision timeout), 2.5-flash 0/3 (bucket
  exhausted). gemma-4-31b-it on the Gemini API 500'd on the image. 3.5-flash on a
  real 112k-char JSON: 5/5, median 2021 ms.
- **gemini transcription 2026-10-03**: gemini-3.5-transcribe is Google's dedicated
  ASR model. 15 real Russian voice notes: groq turbo median 312 ms; 3.5-transcribe
  (VERBATIM) 15/15, median 1208 ms, 0 rate-limit errors; 2.5-flash via chat 13/15
  (two 429), 1264 ms; local faster-whisper 18.5 s. It needs
  `generationConfig.audioTranscriptionConfig` and no text prompt (without it: HTTP 200
  with EMPTY output), so it cannot go through litellm. 2.5-flash is the in-transport
  fallback (a bad key fails both identically and is surfaced at once).
- **deepseek 2026-07-17 -> v4-flash**: the 2026-07-10 "regression" (truncated/empty
  JSON, ~49% InvalidJSON) was not the model — v4 defaults to THINKING mode and
  reasoning_content ate max_tokens. `thinking={"type":"disabled"}` is the right knob
  (set by `_DeepseekAdapter`); confirmed valid JSON at max_tokens=120 on a 17k-token
  prompt, cache 17280/17286. Prod: 482 calls, zero EmptyBody (vs 1590 on
  deepseek-chat). **2026-09-12 -> deepseek-flash** (V4.1-Flash): /models lists only
  deepseek-flash and deepseek-v4-pro; half the price (off-peak $0.15/$0.60 vs
  $0.22/$0.66), native vision, 1M context. On a real 112k-char JSON V4.1-Flash
  returns an all-whitespace body every time (29-438 tokens), as did v4-pro, while
  gemini-3.5-flash-lite is 5/5 — so deepseek stays the paid FALLBACK. 2026-10-02
  correction: deepseek-v4-pro is still a separate model at its own price.
- **openrouter 2026-07-16**: gpt-oss-120b:free DELISTED (404, 48 errs/75 min); all
  lanes moved to google/gemma-4-31b-it:free (verified on our keys, instruct,
  JSON-safe at low max_tokens, 262k ctx). The decision model is not a litellm route.
- **anthropic 2026-07-02** chat:smart/code/vision/edit sonnet-4-6 -> sonnet-5 (near-Opus
  at Sonnet price); chat:fast/structured stay haiku-4-5. 2026-07-23 chat:sales ->
  sonnet-5. sonnet-5 rejects non-default sampling params (drop_params strips them).
- **cohere**: 2026-06-26 command-r/r-plus retired (use command-a-03-2025 / r7b).
  2026-07-10 chat:smart/code command-a -> command-r7b (command-a was billing ~$2.4/day,
  mostly on FAILED calls: 2 ok/day, 96% error).
- **voyage 2026-07-07 voyage-3 -> voyage-4**: the whole voyage-3 family has ZERO free
  tokens on our accounts; voyage-4 gets 200M/month. Same 1024 dims but a DIFFERENT
  vector space — existing embeddings must be re-embedded (the callers' length guard
  does not catch a stale row). Absent from litellm's map -> registered at $0.06/M
  (2026-07-16) so a paid key's cap is not blind.
- **deepseek-flash price** absent from litellm: registered at the OFF-PEAK list
  price; `peak_pricing` doubles it in weekday peak windows. litellm's own
  deepseek-v4-flash entry carried the PEAK rate as base ($0.44/M), over-counting 2x.
- **sambanova 2026-07-04** free tier confirmed at 20 req/day/key. 2026-07-21
  chat:smart/code -> DeepSeek-V3.2 (free, deepseek quality, ~6-8 s). 2026-09-12
  chat:fast/prefilter/vision -> gemma-4-31B-it: 7d 0 ok / 2087 err on Llama-3.3-70B
  (every call 429 "high demand"; same for gpt-oss-120b, V3.2, MiniMax-M3); gemma was
  the only model that answered (1.5 s text, valid json_object AND json_schema, 3.1 s
  correct image description).
- **GitHub Models removed 2026-07-10**: ~150 req/day on one key, reset not aligned to
  UTC midnight; 155 attempts / 0 ok on the last full day.
- **nvidia 2026-07-04/05**: no litellm price (cost_usd always 0; `daily_limit` is the
  only guard), no card on file. 2026-07-10 chat:fast/smart models removed (kimi-k2.6
  404; v4-pro ~91 s timeout); nemotron (chat:deep) alive.
- **cloudflare 2026-07-04**: llava for vision; not wired for transcription (litellm's
  cloudflare provider is chat-only). 2026-07-07 chat:fast/prefilter, 2026-07-10
  chat:smart/code (same gpt-oss-120b; kept off `structured`).
- **zai 2026-07-05**: only glm-4.5-flash was free ("Insufficient balance" on the
  bigger models). 2026-08-16 -> glm-4.7-flash (thinking mode ate the whole budget —
  fixed in `_ZaiAdapter`; glm-4.6-flash does not exist).

## Quotas (ProviderSpec.quota)

- cerebras: free tier is enforced on TOKENS/day; a key logged 4,866 req against its
  2,400 req/day header without a 429, so the req axis is dropped (and auto-discover
  no longer ingests it).
- mistral: only PER-MINUTE headers (`x-ratelimit-limit-req-minute=50`, tokens-minute
  50000, live 2026-07-02); the old 86_400/500_000 daily seed was invented — real keys
  ran 1.3-1.7M tok/day at ~260% of it. Both axes dropped.
- voyage: 200M free tokens is per MONTH — no honest daily axis (throttle is RPM/TPM,
  handled as a cooldown via the "reduced rate limits" sign).
- sambanova: `x-ratelimit-limit-requests-day: 20` (live 2026-07-04): a real daily
  reset, hard 20/day per key.
- nvidia: NO rate-limit headers; 1,000 ONE-TIME credits that silently convert to
  pay-as-you-go. Only a manual daily_limit guards it.
- cloudflare: 10,000 neurons/day is a compute budget, not a request count.
- zai: no headers, no documented daily cap. local: throughput, not a quota.

## Error signs (ProviderSpec.*_signs)

- deepseek "response_format type is unavailable" (2026-07-05): hit every key (provider
  outage), ~2510 wasted attempts/day; rate_limit behaviour wanted (cool, don't kill).
- voyage "reduced rate limits" (2026-07-07): no payment method -> 3 RPM / 10K TPM.
- mistral bare 401 "Unauthorized": monthly plan exhaustion, not a revoked key (admin
  console 2026-07); scoped to mistral for both the rate-limit and monthly rules.
- cloudflare "daily free allocation of 10,000 neurons" (2026-07-12): litellm wraps it
  in APIConnectionError; resets at 00:00 UTC.
- zai "Invalid API parameter" (2026-07-07): one key hit it on 3141 of ~3189 attempts,
  every other key fine -> persistent per-account problem -> mark dead.

## Cooldown base (ProviderSpec.cooldown_base_s)

Chosen from each provider's published reset cadence; mistral 10 s (1 RPS),
openrouter 300 s (":free" overloads last minutes), sambanova 120 s (20 req/day),
nvidia 300 s (invisible quota), `local` 30 s: a timeout means the single-worker decode
lock was busy, not a dead credential (the old default 300 -> 600 on timeout bump parked
the free/private path ~10 minutes per slow decode and dumped all voice on the paid tail).

## Size ceiling

groq: free TPM ~8k, a single bigger request always 413/429 (`max_request_tokens=8000`);
the self-learned ceiling in `provider_observations` overrides every seed.

## Cache stickiness

deepseek and anthropic have a PER-KEY prompt cache AND a paid high-throughput API with
no tight RPM, so all of a project's traffic is pinned to one key (2026-07-20: deepseek
smart cache hit ~50% scattered vs ~80% pinned; a warm deepseek input token is ~50x
cheaper). Free/RPM-limited providers are excluded: concentrating them hits rate limits
and their cache gives no discount.

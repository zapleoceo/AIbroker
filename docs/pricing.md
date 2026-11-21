# Pricing: how every call is costed

All prices end up in `cost_usd` on `usage_log`, which feeds the per-key, per-project
and global daily caps. Free-tier keys are zeroed afterwards by `_billed_cost` (except paid OpenRouter override routes, see below); the
numbers here are what a PAID key would be charged. Code: `providers/pricing.py`
(helpers + override tables) and `providers/cost.py` (`estimate_llm_cost`,
`whisper_cost`, `register_price_overrides`; the override table itself is the registry, `providers/provider_specs/`).

## Rules, in order of precedence

| Path | Price source |
| --- | --- |
| OpenRouter chat | `usage.cost` from the response when present (the real bill), else the LiteLLM estimate. `_prefer_reported_cost` (a reported 0 on a paid `override` route is replaced by the estimate). |
| OpenRouter decisions (`/api/alpha/decisions`) | `usage.cost` when OpenRouter reports it (even `0`); otherwise the registry/LiteLLM estimate (`estimate_llm_cost`), never a silent `$0`. Same helper `reported_cost`. |
| All other chat/vision/structured | `litellm.cost_per_token` from LiteLLM's map (pinned `litellm==1.103.2`) x `peak_multiplier` (DeepSeek weekday peak). |
| Embeddings | Same map, input tokens only. |
| Whisper-style ASR | Per audio minute from `ModelSpec.usd_per_minute` (provider registry) x duration. Groq bills at least 10 s per request (`MIN_BILLED_AUDIO_S`). |
| Gemini chat transcription | Token price; the audio part of the prompt is billed at the model's audio rate (see below). |
| Gemini TTS (`speech`) | Token price: text tokens in at the text rate, audio tokens out at the audio output rate (`providers/gemini_tts.py` `tts_cost`). See Gemini audio. |
| Gemini Omni video (`video`) | Token price: input tokens at the text rate, video output tokens at the video rate (`providers/gemini_video.py` `video_cost`). See Gemini Omni video. |
| Gemini image (`image`) | Token price: prompt tokens at the text input rate, image output tokens (candidate + thought tokens) at the image output rate (`providers/gemini_image.py` `image_cost`). See Gemini image. |
| `local/*` (Qwen3-VL) | Free, no external bill. |

The usage write after a successful paid call runs as its own tracked task behind `asyncio.shield` (`services/attempt.py`). The provider has already billed, so the ledger row lands even if the client disconnects: the cancel is raised at once and does not wait for the write, which completes in the background. A failed background write is logged with `log.error` (key, project, model).

## Cached tokens

`_cache_tokens(usage)` returns (read, write) as the SUBSET of `prompt_tokens`:

* Anthropic: `cache_read_input_tokens` / `cache_creation_input_tokens`. Writes use the
  1-hour TTL, which LiteLLM cannot price, so `_extended_ttl_write_premium` adds
  `cache_creation_input_token_cost_above_1hr - cache_creation_input_token_cost`.
* OpenAI-shape providers (OpenAI, DeepSeek, Gemini, Groq, Mistral, ...):
  `prompt_tokens_details.cached_tokens` (read only; there is no write charge).
* litellm >= 1.103 prices a cache read/write at the plain input rate when the model has
  no cached rate; 1.92 dropped those tokens (under-count). Verified 2026-10-03 across
  all routable models: non-cached prices are identical between 1.92.0 and 1.103.2.

## Gemini audio

* `gemini/gemini-3.5-transcribe` (raw `generateContent`, per minute): $0.003/min audio
  in + $0.002/min text out = $0.005/min (https://ai.google.dev/gemini-api/docs/pricing,
  checked 2026-10-03; LiteLLM's token view is $2/M in, $12/M out).
* Chat transcription (`_transcribe_via_chat`, e.g. the gemini-2.5-flash fallback):
  `prompt_tokens_details.audio_tokens` (all prompt tokens if absent) are billed at
  `input_cost_per_audio_token` from LiteLLM's map, else `AUDIO_INPUT_USD_PER_M`:
  2.5-flash $1.00/M, 2.5-flash-lite $0.30/M, 3.1-flash-lite $0.50/M. The pre-call
  reservation (`estimate_transcription_cost`) assumes 32 audio tokens/s.

* Gemini TTS (`gemini/gemini-3.8-flash-tts`, `speech`, raw `generateContent` with `responseModalities: ["AUDIO"]`, 2026-11-11): $0.50/M text tokens in + $9.00/M audio tokens out. Audio is 25 tokens per second, about $0.0135 per minute of speech. Google list price through 2026-12-31; it doubles from 2027-01-01 to $1.00 in / $18.00 out per M. `gemini/gemini-3.8-flash-lite-tts` is $0.50 / $6.00 per M (about $0.009 per minute). The pre-call reservation (`estimate_speech_cost`) is estimated from the text length; the booked cost comes from the reported token counts.

* Gemini Live (`gemini/gemini-3.8-live`, `live`, WebSocket BidiGenerateContent, 2026-11-13): $0.75/M text tokens in, $4.50/M text tokens out, $3.00/M audio tokens in, $12.00/M audio tokens out. Audio rates are ~$0.005/min of input speech heard (~30 input tokens/s), ~$0.018/min of output speech spoken (~25 output tokens/s). Google list price (2026-11-13). The pre-call reservation (`estimate_live_cost`) assumes the session runs at full capacity (both sides speaking continuously) for the session ceiling (`LIVE_MAX_SESSION_S`=540 s); the booked cost (`live_cost`) is computed from the actual usage reported by Google, split by modality (`LiveUsage.text_in`, `audio_in`, `text_out`, `audio_out`). See `providers/gemini_live.py`.

* Gemini Omni video (`gemini/gemini-omni-1.2-flash`, `video`, Interactions API, 2026-11-14): $1.50/M input tokens, $12.00/M video output tokens. Google bills 1,931 output tokens per second of video at 360p and 5,792 at 720p, so about $0.023/s at 360p and $0.07/s at 720p: a 5 s 360p clip is about $0.12, a 5 s 720p clip about $0.35. No free tier. The pre-call reservation (`estimate_video_cost`) is estimated from the prompt length (plus 1,500 tokens for a reference image) and the table's output tokens (`video_output_tokens`). The booked cost (`video_cost`) uses the `usage.total_input_tokens` / `total_output_tokens` the response reports, else the tokens-per-second table.

## Gemini image (`image`, 2026-11-21)

* `gemini/gemini-3.1-flash-image` (Nano Banana 2, default): $0.50/M text input, $60/M image output. A 1K image is 1120 output tokens (about $0.067), 2K 1680 (about $0.10), 4K 2520 (about $0.15).
* `gemini/gemini-3-pro-image` (Nano Banana Pro): $2.00/M text input, $120/M image output. 1K and 2K are 1120 tokens (about $0.134), 4K 2000 tokens (about $0.24).
* `gemini/gemini-3.1-flash-lite-image` (Nano Banana 2 Lite): $0.25/M text input, $30/M image output, not tuned for several references.

Standard-tier list prices, Google pricing page 2026-11. Reference images are input tokens. Cost is prompt tokens x input price + (candidate + thought tokens) x image output price, an upper bound from the reported usage. The pre-call reservation (`estimate_image_cost`) is only an estimate; the booked cost is `image_cost`. A live 1K call with two PNG references (544 prompt and 1429 candidate tokens) cost about $0.086. Paid keys only.

## Gemini pinned text models (2026-11-21)

* `gemini/gemini-4-neon`: $3.00/M in, $15.00/M out, $0.15/M cache read. Paid keys only.
* `gemini/gemini-4-carbon`: $2.00/M in, $10.00/M out, $0.10/M cache read through 2026-11-30; $4.00 / $20.00 / $0.20 from 2026-12-01. Paid keys only.
* `gemini/gemini-4-hydrogen`: $1.50/M in, $7.50/M out, $0.075/M cache read. GA, free tier exists.
* `gemini/gemini-4-argon`: $2.00/M in, $10.00/M out, $0.10/M cache read through 2026-11-30; $4.00 / $20.00 / $0.20 from 2026-12-01. Paid keys only.
* `gemini/gemini-3.8-flash`: $0.75/M in, $3.75/M out, $0.075/M cache read through 2026-12-31; $1.50 / $7.50 / $0.15 from 2027-01-01. Free tier exists.

Standard-tier list prices, Google pricing page 2026-11-21, up to 200k context. Registered as override prices (`_PINNED_TEXT_MODELS` in `providers/provider_specs/gemini.py`), not from LiteLLM's map.

## Pin-only models (catalog refresh 2026-11-21)

Prices per 1M tokens (input / output), cache read where listed. Pin-only models are never in a walk; see [providers.md](providers.md), "Catalog refresh 2026-11-21", for the ids. Official-docs prices are for providers with no active broker key (anthropic, openai, cerebras, sambanova).

* **anthropic** (Anthropic pricing, 2026-11-21): fable-5-2 $8 / $40 (cache $0.20); opus-5-5 $4 / $20 (cache $0.20); sonnet-5-5 $2 / $10 (cache $0.10); haiku-5-5 $0.10 / $0.50 up to 100k prompt tokens, $0.50 / $2.50 above.
* **openai** (OpenAI pricing, standard tier up to 272k, 2026-11-21): gpt-6.2-astra $8 / $40 (cached $0.40); gpt-6.2-sol $2 / $10 (cached $0.10); gpt-6.1-terra $1 / $5 (cached $0.05); gpt-6-luna $0.10 / $0.50 (cached $0.01).
* **cerebras**: qwen-3.8-27b $0.99 / $1.49 (override).
* **sambanova** (plans pricing page, 2026-11-21): GLM-5.3 $2.80 / $8.80; MiniMax-M3.1-Flash $0.30 / $1.20.
* **groq**: openai/gpt-oss-20b $0.075 / $0.30; qwen/qwen3.8-27b $0.80 / $4.00 (LiteLLM map).
* **mistral**: mistral-medium-latest $1.50 / $7.50 (the chat:smart default); ministral-14b-latest and ministral-3b-latest from LiteLLM's map.
* **cohere**: command-a-plus-05-2026 $0.30 / $1.50 (cache $0.15, OpenRouter passthrough rate); command-a-03-2025 $2.50 / $10 (LiteLLM map).
* **deepseek**: deepseek-v4-pro $0.66 miss / $0.022 hit / $1.98 out, off-peak; peak is 2x (`peak_multiplier`).
* **zai**: glm-5.3-flash is booked as $0 on our free-tier key; Z.ai's list price is $0.15 / $0.50.
* **nvidia_nim**: the six pin-only models are $0 (credits-based free tier, no LiteLLM price).
* **cloudflare** (Workers AI model catalog, 2026-11-21): gemma-4-26b-a4b-it $0.10 / $0.30 (cache $0.05); gpt-oss-20b $0.20 / $0.30; inkling-2-small $0.30 / $1.00 (cache $0.10); qwen3.8-27b $0.45 / $3.20 (cache $0.05); glm-5.3 $1.40 / $4.40 (cache $0.26); deepseek-v4.1-flash-1017 $0.30 / $1.20 (cache $0.006). The last three need the Workers Paid plan.
* **openrouter** (public `/api/v1/models` fields, checked 2026-11-21): claude-fable-5.2 $8 / $40; claude-sonnet-5.5 $2 / $10; claude-haiku-5.5 $0.10 / $0.50 (above 100k prompt tokens $0.50 / $2.50); gpt-6.2-sol $2 / $10 (above 272k $4 / $15); gemini-4-hydrogen $1.50 / $7.50; qwen4-max $1.425 / $4.275; deepseek-v4.1-flash-1017 $0.15 / $0.60; kimi-k3.1-pro $0.253 / $8.80; glm-5.3 $0.0699 / $1.40; mistral-large-4-0 $0.70 / $2.10. The `:free` models (ling-3.2-flash, particle-1, nemotron-3.5-super-vl) are $0.
* **openrouter/moonshotai/kimi-k3** price corrected to $0.192 in / $16 out (was $0.45 / $15).

## Models we register ourselves (`litellm.register_model`)

| Model | Price | Source / date |
| --- | --- | --- |
| `voyage/voyage-4` | $0.06/M in | Voyage list price, 2026-07-16 |
| `deepseek/deepseek-flash` | $0.15 miss / $0.003 hit / $0.60 out per M (off-peak) | DeepSeek list, 2026-09-12 |
| `nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b` | $0 | build.nvidia.com hosted API is free-tier; 2026-10-03 |
| `cloudflare/@cf/google/gemma-4-26b-a4b-it` | $0.10 in / $0.30 out per M (LiteLLM map; cache read $0.05) | Replaces the llava-1.5-7b-hf row (2026-10-03), which was removed from the Workers AI catalog 2026-11-21 |

`cloudflare/@cf/openai/gpt-oss-120b` ($0.35 in / $0.75 out per M) comes from LiteLLM's
map and matches developers.cloudflare.com/workers-ai/platform/pricing (2026-10-03).
Anthropic `claude-sonnet-5` is permanently $2/$10 per M.

## OpenRouter override routes (2026-10-15)

For OpenRouter models registered with `pricing="override"` (`providers/provider_specs/openrouter.py`), the registry's `input_usd_per_mtok` / `output_usd_per_mtok` is the price floor:

* No LiteLLM price (or a LiteLLM exception): cost = registry rate x tokens x `peak_multiplier`. The cost-cap reservation is not zeroed for a registry-priced route.
* LiteLLM price present: cost = max(LiteLLM, registry), then x `peak_multiplier`.
* Reported `usage.cost` of 0 with a positive estimate: the estimate is used.
* Free-tier keys: not zeroed by `_billed_cost` when the rate is positive. Zero-rate `:free` overrides still bill $0.

## Adding a model

`tests/test_pricing.py` fails for any model reachable from a
`ProviderSpec` (`defaults` / `rotation`) that LiteLLM (or a `register_model` entry, or the ASR table) does not
price. Free models are marked `pricing="free"` in `providers/provider_specs/` (registered at 0.0 by `providers/cost.register_price_overrides`) with a `note` naming the source.

## Code map (`providers/pricing.py`)

- `reported_cost(usage)` — the provider's own `usage.cost` (OpenRouter); preferred over the price table when present.
- `audio_input_rate(model)` — USD per audio input token (LiteLLM map, else `AUDIO_INPUT_USD_PER_M`).
- `audio_surcharge(model, audio_tokens)` — extra cost of audio tokens over the text rate.
- `is_priced(model)` — whether LiteLLM's map (incl. our `register_model` entries) knows the model; used by the pricing regression test.

## 2026-10-10 — OpenRouter decision / free models re-confirmed

LiteLLM's remote price map dropped these models, so their prices are now registered from
OpenRouter's official per-model endpoint API (not the `/models` list, where jev-1.13 is absent):

| model | price per 1M (in / out) | source |
|---|---|---|
| `openrouter/typesafe/jev-1.13` | $0.042 / $0 | https://openrouter.ai/api/v1/models/typesafe/jev-1.13/endpoints (provider TypeSafe) |
| `openrouter/inception/mercury-decide:free` | $0 / $0 | https://openrouter.ai/api/v1/models/inception/mercury-decide:free/endpoints |
| `openrouter/google/gemma-4-31b-it:free` | $0 / $0 | OpenRouter `/api/v1/models` catalog |

jev-1.13 is registered in LiteLLM's map (`register` on) so the pricing checks see the confirmed
price; the decisions path still books the cost OpenRouter reports in `usage.cost`.
`typesafe/jev-router` is a different model (a router over Jev with variable price), not a rename.

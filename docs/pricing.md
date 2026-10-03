# Pricing: how every call is costed

All prices end up in `cost_usd` on `usage_log`, which feeds the per-key, per-project
and global daily caps. Free-tier keys are zeroed afterwards by `_billed_cost`; the
numbers here are what a PAID key would be charged. Code: `providers/pricing.py`
(helpers + override tables) and `providers/litellm_adapter.py` (`estimate_llm_cost`,
`whisper_cost`, `register_model` block).

## Rules, in order of precedence

| Path | Price source |
| --- | --- |
| OpenRouter chat | `usage.cost` from the response when present (the real bill), else the LiteLLM estimate. `_prefer_reported_cost`. |
| OpenRouter decisions (`/api/alpha/decisions`) | `usage.cost` only (not in LiteLLM's map). Same helper `reported_cost`. |
| All other chat/vision/structured | `litellm.cost_per_token` from LiteLLM's map (pinned `litellm==1.103.2`) x `peak_multiplier` (DeepSeek weekday peak). |
| Embeddings | Same map, input tokens only. |
| Whisper-style ASR | Per audio minute from `_WHISPER_USD_PER_MIN` x duration. Groq bills at least 10 s per request (`MIN_BILLED_AUDIO_S`). |
| Gemini chat transcription | Token price; the audio part of the prompt is billed at the model's audio rate (see below). |
| `local/*` (asr-local, Qwen3-VL) | Free, no external bill. |

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

## Models we register ourselves (`litellm.register_model`)

| Model | Price | Source / date |
| --- | --- | --- |
| `voyage/voyage-4` | $0.06/M in | Voyage list price, 2026-07-16 |
| `deepseek/deepseek-flash` | $0.15 miss / $0.003 hit / $0.60 out per M (off-peak) | DeepSeek list, 2026-09-12 |
| `nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b` | $0 | build.nvidia.com hosted API is free-tier; 2026-10-03 |
| `cloudflare/@cf/llava-hf/llava-1.5-7b-hf` | $0 | Beta, absent from Cloudflare's pricing page; 2026-10-03 |

`cloudflare/@cf/openai/gpt-oss-120b` ($0.35 in / $0.75 out per M) comes from LiteLLM's
map and matches developers.cloudflare.com/workers-ai/platform/pricing (2026-10-03).
Anthropic `claude-sonnet-5` is permanently $2/$10 per M.

## Adding a model

`tests/test_pricing.py` fails for any model reachable from `DEFAULT_MODEL` /
`MODEL_ROTATION` that LiteLLM (or a `register_model` entry, or the ASR table) does not
price. Free models must be registered at 0.0 with a comment naming the source.

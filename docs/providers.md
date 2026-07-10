# Providers

LiteLLM SDK does the per-provider HTTP. Our config maps capabilities to
default models.

Source: `src/aibroker/providers/litellm_adapter.py:DEFAULT_MODEL`.

`litellm.drop_params = True` is set at adapter import: the broker sends every
provider the same kwargs (`temperature`, `response_format`, …) and LiteLLM
strips the ones a given provider doesn't support instead of 400-ing. Fixes
cohere, which rejected `response_format`/`temperature` with
`UnsupportedParamsError` on every structured/chat call. On `structured`, cohere
then free-forms (no schema enforcement) → the broker's JSON validation falls it
through; cohere's real value is `chat`, which now works.

## Anthropic on Sonnet 5 (2026-07-02)

`chat:smart`, `chat:code`, `vision`, and **`chat:edit`** on anthropic moved
sonnet-4-6 → `claude-sonnet-5` (`DEFAULT_MODEL["anthropic"]`). Same $3/$15
sticker ($2/$10 intro through 2026-08-31); near-Opus coding/agentic quality.
Verified live: the key reaches `claude-sonnet-5`, and `litellm.drop_params`
(above) strips the broker's `temperature=0.7` that Sonnet 5 otherwise rejects.
`chat:fast`/`structured` stay on `claude-haiku-4-5` (fast tier, untouched).

`chat:edit` is Stepan's and Stepan2's **Coach** fallback
(`chains.CAPABILITY_CHAINS["chat:edit"] = [gemini, deepseek, anthropic]`) —
fires only after gemini and deepseek both fail. Both projects already reach it
with zero code change: `stepan` runs `llm_backend=broker` (its
`stepan_shared.llm.broker_client.BrokerLLMClient` posts
`/v1/jobs?capability=chat:edit` to this broker) and already carries the
`llm:edit` scope; `stepan2`'s `coach_service.py` does the same via its own
`BrokerLLM` adapter — its project was missing `llm:edit` until 2026-07-02
(added via the same `dash_edit_project` code path, audit-logged). Stepan's own
local routing policy (`stepan_shared/llm/routing.py`) is a *separate*,
provider-direct fallback used only when `llm_backend=local` — it does not
route through this broker and does not see anthropic.

## Prompt caching (2026-07-01, wired end-to-end 2026-07-02)

`apply_prompt_cache(model, messages)` marks the first system message with
`cache_control: {ephemeral}` for providers with **explicit** prompt caching
(currently anthropic). A byte-stable system prefix is then billed as a cache
read (~0.1× input cost) after the first write. The marker is harmless when the
prefix varies or is under the provider's minimum cacheable size (silently not
cached). **deepseek** caches automatically server-side (no param); **gemini**
needs its own context-cache lifecycle — neither is marked here. Caching only
helps when the caller (Vera/Stepan) sends a stable system prompt — a
timestamp or per-request ID in the prefix defeats it.

`call_llm`'s `meta` carries `cache_read_tokens` / `cache_write_tokens`
(parsed by `_cache_tokens`, handling both the anthropic and OpenAI usage
shapes) — and now (2026-07-02) it's wired all the way through, not just
computed and discarded:

- `estimate_llm_cost(..., cache_read_tokens=, cache_write_tokens=)` passes
  them to `litellm.cost_per_token`'s `cache_read_input_tokens` /
  `cache_creation_input_tokens` kwargs, so a cache read prices at ~0.1× and a
  cache write at its real (higher) creation rate — before this, every prompt
  token priced flat, over-counting cached calls (safe direction, but not the
  real bill).
- `usage_log.cache_read_tokens` / `cache_write_tokens` (migration 006)
  persist every call's cache activity.
- `run_chat` → `ChatOutcome.cache_read_tokens/cache_write_tokens` → the chat
  `JobResponse` — `/v1/jobs` callers can see their own cache hit rate.
- `/dashboard/projects/{id}` shows a **Prompt cache** KPI card (read/write
  token totals + reuse ratio) for the selected range — hidden entirely when a
  project never touches caching (most calls don't route through anthropic).

| Provider | chat:fast | chat:smart | chat:code | vision | embedding |
|---|---|---|---|---|---|
| **cerebras** | gpt-oss-120b | gpt-oss-120b | gpt-oss-120b | — | — |
| **groq** | openai/gpt-oss-120b | openai/gpt-oss-120b | — | — | — |
| **gemini** | gemini-2.5-flash | gemini-2.5-pro | — | gemini-2.5-flash | — |
| **deepseek** | deepseek-chat | — | deepseek-coder | — | — |
| **openrouter** | openai/gpt-oss-120b:free | openai/gpt-oss-120b:free | — | — | — |
| **anthropic** | claude-haiku-4-5 | claude-sonnet-5 | claude-sonnet-5 | claude-sonnet-5 | — |
| **openai** | gpt-5-mini | gpt-5 | — | — | — |
| **voyage** | — | — | — | — | voyage-3 |

## Adding a new provider

1. Verify LiteLLM supports it (`pip install litellm` then
   `litellm.providers.list_providers()`).
2. Add a row to `DEFAULT_MODEL` with the capabilities you want.
3. Add the provider to `routing.chains.CAPABILITY_CHAINS` where it fits.
4. Add a health probe in `providers/health_probes.py` (smallest possible
   call — usually `max_tokens=1`).
5. Update [routing.md](./routing.md) with the new chain.
6. POST `/admin/keys` with the new provider + label + token.

## Health probes

Run every 10 min by the monitor container. Verdicts:

| Verdict | Trigger | Action |
|---|---|---|
| `alive` | 2xx | `is_alive=true`, `error_count=0`, clear Telegram alert |
| `cooldown` | 429 | `cooldown_until = now + 5min` |
| `dead` | 401/403, "insufficient balance", "payment required" | `is_alive=false`, alert TG |
| `neterr` | TCP/TLS failure | no-op, retried next tick |

When `is_alive` flips true → false, the monitor sends a Telegram alert via
`@aibzapleo_bot`. When it flips back → false → true, a recovery message
goes out. Throttle: state files in `/var/lib/aibroker/`, alerts skipped
within 30 min of the last for the same key.

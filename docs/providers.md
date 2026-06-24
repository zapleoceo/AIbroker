# Providers

LiteLLM SDK does the per-provider HTTP. Our config maps capabilities to
default models.

Source: `src/aibroker/providers/litellm_adapter.py:DEFAULT_MODEL`.

| Provider | chat:fast | chat:smart | chat:code | vision | embedding |
|---|---|---|---|---|---|
| **cerebras** | gpt-oss-120b | gpt-oss-120b | gpt-oss-120b | — | — |
| **groq** | openai/gpt-oss-120b | openai/gpt-oss-120b | — | — | — |
| **gemini** | gemini-2.5-flash | gemini-2.5-pro | — | gemini-2.5-flash | — |
| **deepseek** | deepseek-chat | — | deepseek-coder | — | — |
| **openrouter** | openai/gpt-oss-120b:free | openai/gpt-oss-120b:free | — | — | — |
| **anthropic** | claude-haiku-4-5 | claude-sonnet-4-6 | claude-sonnet-4-6 | claude-sonnet-4-6 | — |
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

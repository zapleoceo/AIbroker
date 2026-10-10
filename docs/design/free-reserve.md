# Design note: optional free-reserve pool (proposal, not implemented)

Status: proposal (2026-10-11, owner idea via dot #1112). No code yet. Does not touch
agent runs (C2/C3).

Question: when the Claude/Codex subscriptions are exhausted, can the broker pick a
working free model on the fly ("ping them before the request")?

## Rules

1. **No separate ping.** Read quota from free metadata when a provider really exposes
   it (rate-limit headers, a free status/list endpoint). Where it does not, the useful
   request itself is the probe. Never spend a generation call just to test capacity.
2. **Unambiguous quota rejection → cooldown → next model.** A clear quota answer (429
   with a per-day/per-minute quota id, provider "quota exhausted") puts that
   (key, model) into cooldown (existing `api_key_model_cooldowns`) and the request moves
   to the next allowed free model. Anything ambiguous is not treated as quota.
3. **Never duplicate a hotfix.** If the previous attempt's outcome is unknown or still
   running, check its status first (job id / `X-Request-Id`, idempotency key); retry only
   after a definite failure.
4. **Explicit pin is sacred.** A request with an explicit `model` is never silently
   substituted. The auto pool is a separate mode the caller enables explicitly
   (e.g. `model="auto:free-reserve"`).
5. **No multi-account rotation** to get around rejections.
6. **Own allowlist, not `chat:code`.** The `chat:code` chain contains paid DeepSeek, so
   the reserve uses an explicit free-only allowlist (today's candidates:
   gemini 3.5/3.1 flash-lite, 3.6/3.5 flash; groq and cloudflare gpt-oss-120b).
   Each entry needs a proven billing mode: `$0` in `usage_log` is not proof, and the
   billing state of the Google projects is UNKNOWN until the owner checks the console.
   Unproven → not in the allowlist.
7. **Privacy.** The owner accepts that relevant code goes to free Google/OpenRouter
   tiers (which may use it for training). Secrets and third-party personal data must be
   stripped before sending; the reserve refuses payloads that fail the secret scan.
8. **Access from ChatGPT.** Via an official custom MCP server
   (https://developers.openai.com/api/docs/guides/custom-mcp-server) with OAuth
   (https://developers.openai.com/plugins/build/auth). No static broker key is handed to
   ChatGPT. Work still goes through `deep_jobs`; provider keys never leave the server.

## To prove before enabling

- Billing mode of every allowlisted key's project (owner, in the provider console).
- Which providers expose usable free quota metadata (per provider, documented).
- Quota-rejection signatures per allowlisted model (from real 429 bodies already logged).
- Secret-scan coverage on code payloads.
- MCP + OAuth flow end to end with a non-production project.

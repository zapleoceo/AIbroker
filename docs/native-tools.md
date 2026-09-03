# Native tools through the broker

`POST /v1/jobs?capability=chat:sales` accepts optional OpenAI-style `tools`
(1–32 function definitions) and `tool_choice` (`auto`, `none`, `required`, or a
named function object). Existing text/JSON clients are unchanged. Do not combine
tools with `response_format`: argument schemas belong in each function's
`parameters`. Schemas must describe objects; only local references are allowed.

Assistant history supports `content: null` with `tool_calls`; tool replies use
`role: tool`, `tool_call_id`, and content. Send the assistant call and its tool
result in the next job's messages to continue the conversation. The broker
NEVER executes tools: the authenticated application must authorize and execute
each operation, independently of model output.

Successful poll responses add `tool_calls`, `finish_reason`, and `refusal`.
A valid tool-only response has `text: ""`, not an empty-response error. Each call
contains `id`, `type: function`, and `function: {name, arguments}`; arguments
remain a JSON string. Clients must not execute calls before job status is done.

The gateway validates names, unique call IDs, strict JSON (including duplicate
keys and nonfinite constants), and argument schemas before accepting a result.
Truncation, refusal, unknown finish reasons, unknown functions, missing required
calls, and invalid arguments fail closed and try the next eligible provider.
Usage telemetry records a distinct failure label and actual billed tokens/cost.
Malformed responses are never returned as successful executable instructions.
This prevents unsafe acceptance; it does NOT guarantee every provider request
will succeed or eliminate provider-side generation errors.

OpenAI, Anthropic, Gemini, and Mistral are initial candidate provider routes,
not a claim of live-verified compatibility for every model. Tools disable
LiteLLM parameter dropping: unsupported model/parameter combinations fail over
instead of silently becoming plain text calls. No tools request uses the
text-only response cache. Existing queue deadlines/retry budgets still apply.
An explicit native-tool `model` must be provider-qualified (for example,
`openai/gpt-4o-mini`) and pins selection to that provider within the capability's
existing chain. Unsupported or unqualified overrides are rejected before any
provider call; exhausted pinned capacity cannot fall back to another provider's
keys. Omit `model` to allow normal eligible-provider failover. Legacy no-tools
model override behavior is unchanged. Context/cost estimates include serialized
assistant call names, IDs and arguments as well as tool-result IDs and content.
Calls must finish with `tool_calls`; final text must finish with `stop`. Provider
models reporting a different terminal convention are not compatible yet.
Validation is covered offline with mocked provider responses; live model
conformance and latency remain rollout gates before enabling production agents.

No database migration is needed: tool contracts participate in the existing
JSONB request payload and dedup hash; results use existing result metadata.

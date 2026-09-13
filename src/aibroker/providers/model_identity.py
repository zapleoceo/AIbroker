"""Which model ACTUALLY answered — the exact one, for the logs and the client.

`usage_log.model` and the client's `model` field carry the name the ROUTER
asked for (`deepseek/deepseek-flash`, `local/qwen3vl`). That is the right key
for pricing and for aggregating history, but it is not always the model that
ran, and the owner cannot tell which from the log (2026-09-13):

  - `deepseek-flash` is a family name. DeepSeek serves it with
    DeepSeek-V4.1-Flash today; the previous names `deepseek-v4-flash` and
    `deepseek-v4-flash-vision-exp` are retired aliases that DeepSeek now also
    answers with V4.1-Flash (changelog 2026-09-10), and `deepseek-v4-pro`
    joins them on 2026-09-14. A log row saying "deepseek-v4-pro" after that
    date names a model that did not run.
  - `local/qwen3vl` is OUR label, invented for routing; it never leaves the
    broker. The real model is whatever gguf llama-server has loaded.

So this module answers one question — "what exactly served this call?" — from
two sources, in order of authority:

  1. a curated identity for a routing name whose exact release we verified
     (small on purpose; an entry is added only with dated evidence, never to
     guess, because a stale mapping here would state a falsehood in the log);
  2. the name the PROVIDER reported in its own response, when it carries more
     than the routing name did (llama-server returns the gguf path; most cloud
     providers just echo the requested name, measured 2026-09-13, and are
     already exact).

Unknown → None, and every caller falls back to the routing name, i.e. exactly
what was shown before this module existed. It can add detail; it cannot lie by
omission.
"""
from __future__ import annotations

import posixpath

# Routing name → the exact model that serves it. Dated evidence per entry.
#
# deepseek-flash: DeepSeek-V4.1-Flash, released 2026-09-10 (552B MoE, native
#   vision) — api-docs.deepseek.com/news/news260910, verified live on our key
#   the same day.
# deepseek-v4-flash: the pre-V4.1 name. DeepSeek's changelog: the old names
#   "are still accepted, but requests are served by the DeepSeek-V4.1-Flash
#   model". Kept here so the 68 history rows under that name read truthfully.
#
# deepseek-v4-pro is deliberately ABSENT: it was a genuinely different model
# until 2026-09-14 04:00 UTC and V4.1-Flash after it, so a single entry would
# mislabel one side. It is no longer routed to (see DEFAULT_MODEL), and its
# history rows keep showing the routing name.
MODEL_IDENTITY: dict[str, str] = {
    "deepseek/deepseek-flash": "DeepSeek-V4.1-Flash",
    "deepseek/deepseek-v4-flash": "DeepSeek-V4.1-Flash",
}


def _normalise_reported(reported: str) -> str:
    """A provider's own model string, reduced to a bare model name.

    llama-server reports the file it loaded ("/models/qwen3-vl-4b-Q4_K_M.gguf");
    OpenAI-compatible providers report a plain id. Strips any directory and a
    single model-file extension, and nothing else — an unfamiliar shape is
    returned as-is rather than mangled.
    """
    name = posixpath.basename(reported.strip().replace("\\", "/"))
    for ext in (".gguf", ".bin", ".safetensors"):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return name


def served_model(routed: str, reported: str | None = None) -> str | None:
    """The exact model that answered, or None when we know nothing beyond the
    routing name (then the caller keeps showing `routed`, as before).

    `reported` is the provider's own `model` field from the response.
    """
    curated = MODEL_IDENTITY.get(routed)
    if curated:
        return curated
    if not reported:
        return None
    name = _normalise_reported(reported)
    if not name:
        return None
    # An echo of what we asked for adds nothing — most cloud providers do this,
    # and their routing name is already the exact model id.
    tail = routed.split("/", 1)[-1]
    if name.lower() in {routed.lower(), tail.lower(), posixpath.basename(tail).lower()}:
        return None
    return name

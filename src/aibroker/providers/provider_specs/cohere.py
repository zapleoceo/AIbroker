"""Definition for the cohere provider."""
from __future__ import annotations

from aibroker.providers.provider_specs.common import CHAT_USER_PING
from aibroker.providers.registry import ProbeSpec, Quota, build_provider

SPEC = build_provider(
    "cohere", rank=90, cooldown_base_s=60, json_reliability="unreliable",
    quota=Quota(req_per_day=1_000, doc="https://docs.cohere.com/v2/docs/rate-limits"),
    probe=ProbeSpec("https://api.cohere.com/v1/models?page_size=1"),
    billing_probe=ProbeSpec("https://api.cohere.com/v2/chat",
                            {"model": "command-r7b-12-2024", "max_tokens": 1,
                             "messages": CHAT_USER_PING}),
    defaults={"chat:fast": "cohere/command-r7b-12-2024",
              "chat:smart": "cohere/command-r7b-12-2024",
              "chat:code": "cohere/command-r7b-12-2024",
              "prefilter": "cohere/command-r7b-12-2024",
              "structured": "cohere/command-r7b-12-2024",
              "translate": "cohere/command-r7b-12-2024",
              "embedding": "cohere/embed-english-v3.0"},
)

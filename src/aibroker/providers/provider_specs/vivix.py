"""Vivix transport foundation; video admission and workspace access are unverified."""
from aibroker.providers.registry import Quota, build_provider

SPEC = build_provider(
    "vivix", rank=230, paid=True,
    auth_signs=("vivix upstream failure (10001)", "vivix upstream failure (10003)"),
    rate_limit_signs=("vivix upstream failure (10008)",),
    quota=Quota(doc="https://docs.vivix.ai/overview/models"),
    # No automatic key probe: discovery needs envelope validation, not just HTTP 200.
    # No routable models until video admission, reservations and billing are implemented.
    transport_factories={"vivix_video": "aibroker.providers.vivix:VivixVideoTransport"},
)

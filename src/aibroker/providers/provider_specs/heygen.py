"""HeyGen's raw asynchronous video API, opt-in per Broker project."""
from aibroker.providers.registry import Quota, build_provider

HEYGEN_MODEL = "heygen/heygen-video-1"
HEYGEN_SCOPE = "llm:video:heygen"

SPEC = build_provider(
    "heygen", rank=240, paid=True,
    defaults={"video": HEYGEN_MODEL},
    quota=Quota(doc="https://developers.heygen.com/docs/models/heygen-video"),
    transport_factories={"heygen_video": "aibroker.providers.heygen_video:HeyGenVideoTransport"},
    model_meta={HEYGEN_MODEL: {
        # Override represents the standard 768p T2V rate with synthetic millisecond
        # usage units. The raw video transport bills the applicable mode/rate.
        "transport": "heygen_video", "pricing": "override", "register": False,
        "input_usd_per_mtok": 0.0, "output_usd_per_mtok": 30.0,
        "note": "HeyGen Video 1; explicit provider selection, native scene audio",
    }},
)

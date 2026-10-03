"""providers.model_identity — which model ACTUALLY answered.

Owner request 2026-09-13: the logs page and the client response showed only
the routing name (`deepseek`, `deepseek/deepseek-flash`, `local/qwen3vl`),
which does not say which model ran.
"""
from __future__ import annotations

from aibroker.providers.model_identity import MODEL_IDENTITY, served_model


def test_curated_identity_expands_a_family_name():
    """`deepseek-flash` is a family name DeepSeek serves with V4.1-Flash
    (changelog 2026-09-10, verified live). The retired `deepseek-v4-flash`
    alias is answered by the same model, so its history rows read truthfully
    instead of naming a model that no longer runs."""
    assert served_model("deepseek/deepseek-flash") == "DeepSeek-V4.1-Flash"
    assert served_model("deepseek/deepseek-v4-flash") == "DeepSeek-V4.1-Flash"


def test_deepseek_v4_pro_is_deliberately_not_mapped():
    """It was a genuinely different model until 2026-09-14 and V4.1-Flash
    after — one entry would mislabel one side of that date, so history keeps
    showing the routing name."""
    assert "deepseek/deepseek-v4-pro" not in MODEL_IDENTITY
    assert served_model("deepseek/deepseek-v4-pro") is None


def test_local_model_comes_from_the_file_llama_server_loaded():
    """`local/qwen3vl` is OUR label and never leaves the broker. llama-server
    reports the gguf it loaded — the only place the real local model is
    knowable, and it follows a model swap with no code change."""
    assert served_model(
        "local/qwen3vl", "/models/qwen3-vl-4b-Q4_K_M.gguf") == "qwen3-vl-4b-Q4_K_M"
    assert served_model(
        "local/whisper", "/models/faster-whisper-small.bin") == "faster-whisper-small"


def test_an_echo_of_the_requested_name_adds_nothing():
    """Measured 2026-09-13: cloud providers echo the model we asked for, and
    their routing name is already the exact id — returning it again would only
    duplicate the `model` column."""
    assert served_model("gemini/gemini-2.5-flash", "gemini-2.5-flash") is None
    assert served_model("groq/openai/gpt-oss-120b", "openai/gpt-oss-120b") is None
    assert served_model("zai/glm-4.7-flash", "zai/glm-4.7-flash") is None
    assert served_model("voyage/voyage-4", "voyage-4") is None


def test_unknown_and_missing_report_fall_back_to_the_routing_name():
    """Never guess: with nothing better to say, every caller keeps showing the
    routing name — exactly what it showed before this module existed."""
    assert served_model("gemini/gemini-3.5-flash") is None
    assert served_model("openrouter/google/gemma-4-31b-it:free", None) is None
    assert served_model("cloudflare/@cf/openai/gpt-oss-120b", "") is None


def test_a_genuinely_different_answer_is_reported():
    """The case this exists for: the provider says it served something else."""
    assert served_model("deepseek/some-alias", "DeepSeek-V5") == "DeepSeek-V5"

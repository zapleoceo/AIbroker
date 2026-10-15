"""Artificial Analysis Intelligence Index for the Broker's catalog models.

STATIC data, copied by hand from the Orchestra repo snapshot
`infra/benchmarks/aa_snapshot_2026-10-03.yaml` (index v4.3). Nothing is fetched at runtime and
nothing is guessed: a catalog model gets a score ONLY when it appears below under its exact
canonical name (`providers.catalog.canonical_name`, lower-cased, version included). Another
version (`claude-sonnet-5.5` vs `claude-sonnet-5`), an alias, a "closest" row or a model AA has
no row for is `None`. Where AA publishes several effort/reasoning variants of one model, only
the row whose AA slug equals the model name is used (the variant is recorded in `aa_name`); a
model whose base row forks into reasoning / non-reasoning (gemini-2.5-flash, deepseek-v3.2,
glm-4.7-flash) is left unscored rather than picking a side.

Attribution is mandatory wherever a score is shown: ATTRIBUTION.
"""
from __future__ import annotations

from typing import NamedTuple

SOURCE = "https://artificialanalysis.ai/leaderboards/models"
SNAPSHOT = "Orchestra infra/benchmarks/aa_snapshot_2026-10-03.yaml (Intelligence Index v4.3)"
CAPTURED_ON = "2026-10-03"
ATTRIBUTION = "Benchmarks: Artificial Analysis (https://artificialanalysis.ai/)"


class Score(NamedTuple):
    value: float
    aa_slug: str
    aa_name: str


# Broker canonical model name -> AA row (exact name + version).
SCORES: dict[str, Score] = {
    "claude-sonnet-5": Score(38.16, "claude-sonnet-5",
                             "Claude Sonnet 5 (Adaptive Reasoning, Max Effort)"),
    "gpt-5": Score(22.98, "gpt-5", "GPT-5 (High)"),
    "gpt-5-mini": Score(16.77, "gpt-5-mini", "GPT-5 mini (High)"),
    "gpt-oss-120b": Score(11.6, "gpt-oss-120b", "gpt-oss-120b (High)"),
    "gemini-3.5-flash": Score(32.6, "gemini-3-5-flash", "Gemini 3.5 Flash (High)"),
    "gemini-3.5-flash-lite": Score(22.17, "gemini-3-5-flash-lite", "Gemini 3.5 Flash-Lite"),
    "gemini-3.6-flash": Score(33.98, "gemini-3-6-flash", "Gemini 3.6 Flash (High)"),
    "gemini-3.1-flash-lite": Score(15.55, "gemini-3-1-flash-lite-preview",
                                   "Gemini 3.1 Flash-Lite"),
    "nemotron-3-ultra-550b-a55b": Score(22.93, "nvidia-nemotron-3-ultra-550b-a55b",
                                        "Nemotron 3 Ultra 550B A55B (Reasoning)"),
}


def intelligence_index(canonical_name: str, table: dict[str, Score] | None = None) -> float | None:
    """Score for EXACTLY this canonical name, else None. No normalisation beyond the
    case the catalog already applies, no prefix/fuzzy/nearest-version matching."""
    row = (SCORES if table is None else table).get(canonical_name)
    return None if row is None else row.value

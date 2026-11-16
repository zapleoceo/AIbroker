"""Trusted integration boundary. No configured runtime or activation switch is shipped."""
from dataclasses import dataclass, field
from typing import Any, Protocol

from aibroker.db.models import ProjectRow
from aibroker.providers.vivix import VivixVideoTransport
from aibroker.providers.vivix_admission import VivixQuote
from aibroker.providers.vivix_media import MediaEvidence
from aibroker.services.vivix_store import Snapshot, VivixLimits


@dataclass(frozen=True)
class Authorization:
    api_key_id: int
    account_id: str
    allowed_models: frozenset[str]
    quote: VivixQuote
    limits: VivixLimits
    materials_verified: bool = False
    media: dict[str, MediaEvidence] = field(default_factory=dict)


@dataclass(frozen=True)
class Receipt:
    actual_usd: str
    evidence: str


class VivixRuntime(Protocol):
    transport: VivixVideoTransport

    async def authorize(self, project: ProjectRow, model: str,
                        inputs: dict[str, Any]) -> Authorization: ...

    async def receipt(self, job: Snapshot) -> Receipt | None: ...


def get_runtime() -> VivixRuntime | None:
    # Implement only after verified account, media, quote and receipt sources + release gates.
    return None

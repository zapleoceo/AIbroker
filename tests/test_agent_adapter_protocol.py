"""AgentCLI is a structural Protocol: its method bodies are stubs that must never be mistaken for
a working default (no class inherits it; real adapters implement every method)."""
from __future__ import annotations

import pytest

from aibroker.agent.adapter import AgentCLI


def test_protocol_stubs_refuse_to_run():
    with pytest.raises(NotImplementedError):
        AgentCLI.export_artifacts(None, None, 0.0)  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        AgentCLI.start(None, None)  # type: ignore[arg-type]

"""Exercise PostgreSQL test oracles locally; this does not validate PostgreSQL."""
from dataclasses import replace

import pytest

from aibroker.providers.gemini_video import VideoRejected
from tests import test_vivix_postgres as pg
from tests.test_vivix_billing import binding as _binding_fixture
from tests.test_vivix_billing import prepared
from tests.test_vivix_service import ledger as ledger

# Register shared fixtures without shadowing imported names in test arguments.
binding = _binding_fixture


@pytest.mark.parametrize("case", ["admission", "settlement"])
@pytest.mark.parametrize("error", [RuntimeError("database failure"), TimeoutError("timeout")])
async def test_concurrency_oracles_reject_unexpected_errors(binding, monkeypatch, case, error):
    snapshot = await prepared(binding)

    async def fake_gather(*coroutines, **kwargs):
        for coroutine in coroutines:
            coroutine.close()
        if case == "admission":
            return [VideoRejected("Vivix project daily budget exhausted"), error]
        return [replace(snapshot, body={**snapshot.body, "state": "settled"}), error]

    async def no_terminal(service):
        pass

    monkeypatch.setattr(pg.asyncio, "gather", fake_gather)
    monkeypatch.setattr(pg, "terminal", no_terminal)
    target = (pg.test_postgres_concurrent_project_admission if case == "admission"
              else pg.test_postgres_concurrent_settlement_books_once)
    with pytest.raises(AssertionError):
        await target(binding)

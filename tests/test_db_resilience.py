"""retry_terminal_write — billed provider responses must survive a DB blip."""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from aibroker.db.resilience import retry_terminal_write


def _op_error() -> OperationalError:
    return OperationalError("INSERT …", {}, ConnectionResetError("gone"))


async def test_retries_transient_then_succeeds(monkeypatch):
    monkeypatch.setattr("aibroker.db.resilience._BASE_DELAY_S", 0.001)
    calls = {"n": 0}

    @retry_terminal_write
    async def write() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _op_error()
        return "ok"

    assert await write() == "ok"
    assert calls["n"] == 3


async def test_gives_up_after_attempts_and_reraises(monkeypatch):
    monkeypatch.setattr("aibroker.db.resilience._BASE_DELAY_S", 0.001)

    @retry_terminal_write
    async def write() -> None:
        raise _op_error()

    with pytest.raises(OperationalError):
        await write()


async def test_non_transient_error_not_retried():
    calls = {"n": 0}

    @retry_terminal_write
    async def write() -> None:
        calls["n"] += 1
        raise IntegrityError("INSERT …", {}, Exception("duplicate key"))

    with pytest.raises(IntegrityError):
        await write()
    assert calls["n"] == 1  # a real bug must surface immediately, not retry

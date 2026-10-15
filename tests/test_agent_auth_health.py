"""Synthetic catalog health checks; no CLI, account, or model calls."""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy.exc import SQLAlchemyError

from aibroker.agent import auth_health


@pytest.mark.parametrize("value", [True, False])
async def test_cached_auth_health_does_not_query(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(auth_health, "_cached_value", value)
    monkeypatch.setattr(auth_health, "_cached_until", float("inf"))

    def unexpected_session():
        raise AssertionError("cached health must not query the database")

    monkeypatch.setattr(auth_health, "get_session", unexpected_session)
    assert await auth_health.codex_auth_available() is value


@pytest.mark.parametrize(("rows", "expected"), [
    ([("run.completed", None, None)], True),
    ([("run.failed", "cli_error", "workspace routing discovery unauthorized (401)")], False),
    ([("run.failed", "cli_error", "unrelated failure")], True),
    ([("run.failed", "other", "workspace routing discovery unauthorized (401)")], True),
    ([], True),
])
async def test_latest_terminal_run_sets_auth_health(monkeypatch, rows, expected: bool) -> None:
    class Result:
        def all(self):
            return rows

    class Session:
        async def execute(self, _query):
            return Result()

    @asynccontextmanager
    async def session():
        yield Session()

    monkeypatch.setattr(auth_health, "get_session", session)
    monkeypatch.setattr(auth_health, "_cached_until", 0.0)
    assert await auth_health.codex_auth_available() is expected
    assert await auth_health.codex_auth_available() is expected


@pytest.mark.parametrize("failure", [SQLAlchemyError("db unavailable"), OSError("socket closed")])
async def test_auth_health_query_failure_is_unavailable(monkeypatch, failure: Exception) -> None:
    @asynccontextmanager
    async def session():
        raise failure
        yield

    monkeypatch.setattr(auth_health, "get_session", session)
    monkeypatch.setattr(auth_health, "_cached_until", 0.0)
    assert await auth_health.codex_auth_available() is False

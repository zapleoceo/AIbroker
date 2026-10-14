"""Every process entry point configures logging: the agent workers used to skip it, so INFO from
the aibroker loggers (run start, gate decisions, exec event key names) never reached the container log."""
from __future__ import annotations

import logging

import pytest

from aibroker import logging_setup
from aibroker.agent import run_worker as rw


async def test_run_worker_configures_logging_first(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(rw, "configure_logging", lambda: calls.append("logging"))

    def not_dumpable() -> bool:
        calls.append("dumpable")
        return False                       # stop the harness right after the first step

    monkeypatch.setattr(rw, "set_non_dumpable", not_dumpable)
    with pytest.raises(RuntimeError, match="non-dumpable"):
        await rw.run_worker()
    assert calls == ["logging", "dumpable"]


@pytest.mark.parametrize(("env", "expected"), [("INFO", logging.INFO), ("debug", logging.DEBUG),
                                               ("WARNING", logging.WARNING),
                                               ("no-such-level", logging.INFO)])
def test_configure_logging_uses_log_level(monkeypatch, env, expected):
    seen: dict = {}
    monkeypatch.setattr(logging_setup.get_settings(), "LOG_LEVEL", env)
    monkeypatch.setattr(logging_setup.logging, "basicConfig", lambda **kw: seen.update(kw))
    logging_setup.configure_logging()
    assert seen == {"level": expected, "format": logging_setup.LOG_FORMAT}

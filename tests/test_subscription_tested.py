"""Catalog `tested` evidence is separate from selectability and never breaks the catalog."""
from __future__ import annotations

import logging

from aibroker.config import get_settings
from aibroker.providers.catalog import listing

EVIDENCE = ('{"codex-sub/gpt-6.1-sol": {"text": 552530, "tools": 552530, '
            '"vision": "attachment:552738"}}')


def _rows():
    return {r["id"]: r for r in listing()}


def test_tested_true_with_evidence_and_false_for_others(monkeypatch):
    before = _rows()
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_TESTED_MODELS", EVIDENCE)
    rows = _rows()
    sol = rows["codex-sub/gpt-6.1-sol"]
    assert sol["tested"] is True
    assert sol["evidence"] == {"text": 552530, "tools": 552530, "vision": "attachment:552738"}
    other = rows["claude-sub/claude-sonnet-5-5"]
    assert other["tested"] is False and other["evidence"] is None
    for mid, row in before.items():  # additive: existing keys unchanged
        assert {k: v for k, v in rows[mid].items() if k not in ("tested", "evidence")} ==             {k: v for k, v in row.items() if k not in ("tested", "evidence")}


def test_default_and_empty_value_mean_nobody_tested(monkeypatch):
    for value in ("{}", ""):
        monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_TESTED_MODELS", value)
        assert all(r["tested"] is False for r in _rows().values() if "tested" in r)


def test_malformed_value_is_empty_and_catalog_survives(monkeypatch, caplog):
    ids = set(_rows())
    for bad in ("not json", "[1]", '{"codex-sub/gpt-6.1-sol": "x"}'):
        monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_TESTED_MODELS", bad)
        with caplog.at_level(logging.WARNING):
            rows = _rows()
        assert set(rows) == ids
        assert rows["codex-sub/gpt-6.1-sol"]["tested"] is False
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_unknown_id_ignored_with_warning(monkeypatch, caplog):
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_TESTED_MODELS",
                        '{"codex-sub/nope": {"text": 1}, "claude-sub/claude-sonnet-5-5": {"text": 2}}')
    with caplog.at_level(logging.WARNING):
        rows = _rows()
    assert rows["claude-sub/claude-sonnet-5-5"]["evidence"] == {"text": 2}
    assert "codex-sub/nope" not in rows
    assert any("codex-sub/nope" in r.getMessage() for r in caplog.records)


def test_non_subscription_rows_omit_the_fields():
    rows = [r for r in listing() if r["billing_type"] != "subscription"]
    assert rows and all("tested" not in r and "evidence" not in r for r in rows)


def test_oversized_entry_rejected_with_warning(monkeypatch, caplog):
    import json
    big = {"codex-sub/gpt-6.1-sol": {"text": "x" * 201},
           "claude-sub/claude-sonnet-5-5": {f"k{i}": i for i in range(17)}}
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_TESTED_MODELS", json.dumps(big))
    with caplog.at_level(logging.WARNING):
        rows = _rows()
    assert rows["codex-sub/gpt-6.1-sol"]["tested"] is False
    assert rows["claude-sub/claude-sonnet-5-5"]["tested"] is False
    assert sum(r.levelno == logging.WARNING for r in caplog.records) >= 2
    ok = {"codex-sub/gpt-6.1-sol": {"text": "x" * 200}}
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_TESTED_MODELS", json.dumps(ok))
    assert _rows()["codex-sub/gpt-6.1-sol"]["tested"] is True


def test_malformed_value_errors_once_per_distinct_value(monkeypatch, caplog):
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_TESTED_MODELS", "bad-once-xyz")
    with caplog.at_level(logging.ERROR):
        _rows()
        _rows()
        n1 = sum(r.levelno == logging.ERROR for r in caplog.records)
        monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_TESTED_MODELS", "bad-twice-xyz")
        _rows()
        n2 = sum(r.levelno == logging.ERROR for r in caplog.records)
    assert n1 == 1 and n2 == 2

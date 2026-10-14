"""Log lines carry ids, counts and fingerprints only: client trace ids, label values and the App
Server peer's method / id are never echoed (the token rules admit secret-looking strings)."""
from __future__ import annotations

import logging

import pytest

from aibroker.agent import codex_app_server as cas
from aibroker.services.job_meta import check_labels, check_token, log_tag

SENTINEL = "AIzaSYNTHETICSECRET0123456789"


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
def test_job_log_tag_never_echoes_trace_or_label_values(caplog, level):
    trace = check_token(SENTINEL, "trace_id")                  # accepted by the validator
    labels = check_labels({"exam_run_id": SENTINEL, SENTINEL: "x"})
    log = logging.getLogger("aibroker.agent.executor")
    with caplog.at_level(level, logger="aibroker"):
        log.info("agent run %d starting: project=%s %s%s", 7, 12, "agent:codex", log_tag(trace, labels))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert SENTINEL not in text and "SYNTHETICSECRET" not in text
    assert "trace#" in text and "labels=2" in text


@pytest.mark.parametrize("message", [
    {"method": SENTINEL, "id": SENTINEL},
    {"method": f"item/{SENTINEL}", "id": 10**30},
    {"id": SENTINEL, "result": {"token": SENTINEL}},
])
def test_app_server_debug_log_never_echoes_peer_method_or_id(caplog, message):
    with caplog.at_level(logging.DEBUG, logger="aibroker.agent.codex_app_server"):
        cas._log_message(message)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert SENTINEL not in text and "SYNTHETICSECRET" not in text
    assert "method=" in text and "id=other" in text


def test_app_server_debug_log_keeps_known_methods_and_small_ids(caplog):
    with caplog.at_level(logging.DEBUG, logger="aibroker.agent.codex_app_server"):
        cas._log_message({"method": "turn/completed", "id": 3})
        cas._log_message({"method": "something/new"})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "method=turn/completed id=3" in text and "method=other id=-" in text

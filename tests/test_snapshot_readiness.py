"""Serving-process access snapshot evidence, without policy or credential payloads."""
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from aibroker.agent import project_access as pa


@pytest.fixture
def evidence(monkeypatch):
    monkeypatch.setattr(pa, "_snapshot", None)
    monkeypatch.setattr(pa, "_evidence_pid", None)
    monkeypatch.setattr(pa, "_evidence_instance", None)
    monkeypatch.setattr(pa, "_evidence_generation", 0)
    monkeypatch.setattr(pa, "_evidence_last_success", None)
    state = SimpleNamespace(pid=17, now=datetime(2026, 11, 7, tzinfo=UTC),
                            rows=[], error=None, events=[], levels=[], serial=0)
    monkeypatch.setattr(pa.os, "getpid", lambda: state.pid)

    def new_id():
        state.serial += 1
        return f"process-{state.serial}"

    monkeypatch.setattr(pa.uuid, "uuid4", new_id)
    monkeypatch.setattr(pa, "datetime", SimpleNamespace(now=lambda zone: state.now))

    async def execute(query):
        if state.error is not None:
            raise state.error
        return SimpleNamespace(all=lambda: state.rows)

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(pa, "get_session", session)

    def log(level, fmt, message):
        import json
        state.events.append((json.loads(message), pa._snapshot))
        state.levels.append(level)

    monkeypatch.setattr(pa.log, "log", log)
    return state


async def test_empty_success_published_before_evidence(evidence):
    await pa.refresh_access()
    event, snapshot = evidence.events[-1]
    assert snapshot == {} and pa.snapshot_loaded()
    assert evidence.levels == [logging.INFO]
    assert event == {
        "event": "agent_access_snapshot", "schema": 1,
        "policy_revision": "accounts-v1", "pid": 17, "process_id": "process-1",
        "observed_at": "2026-11-07T00:00:00+00:00",
        "last_success_at": "2026-11-07T00:00:00+00:00", "generation": 1,
        "snapshot_loaded": True, "last_refresh_ok": True,
    }


@pytest.mark.parametrize("normalization", [False, True])
async def test_initial_failure_is_unloaded_and_preserves_exception(evidence, monkeypatch,
                                                                  normalization):
    error = RuntimeError("secret database detail")
    if normalization:
        evidence.rows = [(871234, True, ["secret-model"], ["secret-account"])]

        def fail(values):
            raise error

        monkeypatch.setattr(pa, "_strings", fail)
    else:
        evidence.error = error
    with pytest.raises(RuntimeError) as caught:
        await pa.refresh_access()
    assert caught.value is error
    event, snapshot = evidence.events[-1]
    assert snapshot is None and not event["snapshot_loaded"]
    assert evidence.levels == [logging.WARNING]
    assert not event["last_refresh_ok"]
    assert event["generation"] == 0 and event["last_success_at"] is None
    assert "secret" not in str(event) and "871234" not in str(event)


async def test_failed_refresh_retains_snapshot_then_recovery(evidence):
    evidence.rows = [(871234, True, ["secret-model"], ["secret-account"])]
    await pa.refresh_access()
    first, original = evidence.events[-1]
    evidence.now = datetime(2026, 11, 7, 0, 0, 5, tzinfo=UTC)
    evidence.error = RuntimeError("secret failure")
    with pytest.raises(RuntimeError):
        await pa.refresh_access()
    failed, retained = evidence.events[-1]
    assert retained is original
    assert failed["snapshot_loaded"] and not failed["last_refresh_ok"]
    assert failed["last_success_at"] == first["last_success_at"]
    assert failed["observed_at"] != first["observed_at"]
    assert failed["generation"] == 1
    evidence.error = None
    evidence.rows = []
    await pa.refresh_access()
    recovered, snapshot = evidence.events[-1]
    assert snapshot == {} and recovered["generation"] == 2
    assert recovered["last_refresh_ok"]
    assert recovered["last_success_at"] == failed["observed_at"]
    assert {e["process_id"] for e, _ in evidence.events} == {"process-1"}
    assert "secret" not in str([e for e, _ in evidence.events])
    assert "871234" not in str([e for e, _ in evidence.events])


async def test_fork_resets_identity_and_process_success_history(evidence):
    await pa.refresh_access()
    evidence.pid = 18
    evidence.error = RuntimeError("failed after fork")
    with pytest.raises(RuntimeError):
        await pa.refresh_access()
    event, _ = evidence.events[-1]
    assert event["pid"] == 18 and event["process_id"] == "process-2"
    assert event["generation"] == 0 and event["last_success_at"] is None
    # Inherited snapshot is not a successful load by the new process.
    assert event["snapshot_loaded"] and not event["last_refresh_ok"]
    evidence.error = None
    await pa.refresh_access()
    assert evidence.events[-1][0]["generation"] == 1
    assert evidence.events[-1][0]["process_id"] == "process-2"


@pytest.mark.parametrize("source", ["logger", "clock", "identity"])
async def test_telemetry_errors_do_not_change_refresh_outcome(evidence, monkeypatch, source):
    def fail(*args, **kwargs):
        raise RuntimeError("telemetry broken")

    if source == "logger":
        monkeypatch.setattr(pa.log, "log", fail)
    elif source == "clock":
        monkeypatch.setattr(pa, "datetime", SimpleNamespace(now=fail))
    else:
        monkeypatch.setattr(pa.uuid, "uuid4", fail)
    await pa.refresh_access()
    assert pa._snapshot == {}
    original = pa._snapshot
    error = ValueError("original read failure")
    evidence.error = error
    with pytest.raises(ValueError) as caught:
        await pa.refresh_access()
    assert caught.value is error and pa._snapshot is original


async def test_normalization_failure_never_publishes_partial_snapshot(evidence, monkeypatch):
    await pa.refresh_access()
    original = pa._snapshot
    evidence.rows = [(1, True, [], []), (2, True, [], [])]
    calls = 0

    def normalize(values):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise ValueError("bad second row")
        return ()

    monkeypatch.setattr(pa, "_strings", normalize)
    with pytest.raises(ValueError):
        await pa.refresh_access()
    assert pa._snapshot is original
    event, snapshot = evidence.events[-1]
    assert snapshot is original and event["generation"] == 1
    assert event["snapshot_loaded"] and not event["last_refresh_ok"]


async def test_identity_generation_retries_after_telemetry_failure(evidence, monkeypatch):
    original = pa.uuid.uuid4

    def fail():
        raise RuntimeError("random source unavailable")

    monkeypatch.setattr(pa.uuid, "uuid4", fail)
    await pa.refresh_access()
    assert not evidence.events
    monkeypatch.setattr(pa.uuid, "uuid4", original)
    await pa.refresh_access()
    assert evidence.events[-1][0]["process_id"] == "process-1"
    assert evidence.events[-1][0]["generation"] == 1


async def test_refresh_loop_failure_and_recovery_without_sleep(evidence):
    evidence.error = RuntimeError("first refresh fails")

    class ControlledStop:
        waits = 0

        def is_set(self):
            return self.waits >= 2

        async def wait(self):
            self.waits += 1
            evidence.error = None
            evidence.now = datetime(2026, 11, 7, 0, 0, 5, tzinfo=UTC)
            if self.waits == 1:
                raise TimeoutError

    await pa.access_refresh_loop(ControlledStop())
    failed, recovered = (event for event, _ in evidence.events)
    assert not failed["snapshot_loaded"] and not failed["last_refresh_ok"]
    assert failed["generation"] == 0 and failed["last_success_at"] is None
    assert recovered["snapshot_loaded"] and recovered["last_refresh_ok"]
    assert recovered["generation"] == 1
    assert recovered["last_success_at"] == "2026-11-07T00:00:05+00:00"
    assert failed["process_id"] == recovered["process_id"]
    assert evidence.levels == [logging.WARNING, logging.INFO]

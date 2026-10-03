"""Typed decisions (TypeSafe Jev via OpenRouter /api/alpha/decisions).

Covers the adapter (question validation, error text the classifier can read),
the service (paid-only key choice, honest cost reservation, rotation, caps)
and the route (scope, 422, 503, happy path). No real provider is called.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

import aibroker.providers.decisions as dec
import aibroker.services.attempt as att
import aibroker.services.llm_service as svc
from aibroker.main import app
from aibroker.providers.provider_errors import classify_provider_error
from aibroker.routing.chains import CAPABILITY_CHAINS, scope_for
from aibroker.routing.cost_guard import CostGuardError
from tests.test_routes_proxy import _make_project

client = TestClient(app)

NOUL = {"type": "noul", "instructions": "Urgent?",
        "criteria": {"true": "time-sensitive", "false": "not urgent"}}
SCORE = {"type": "score", "instructions": "How important?",
         "criteria": ["noise", "low", "high"]}
CHOICE = {"type": "choice", "instructions": "Which project?",
          "criteria": {"itstep": "academy", "veranda": "bar"}}


# ─── wiring ──────────────────────────────────────────────────────────────


def test_decision_is_its_own_paid_lane():
    """Its own scope, so holding llm:chat never silently grants a paid lane."""
    assert CAPABILITY_CHAINS["decision"] == ["openrouter"]
    assert scope_for("decision") == "llm:decision"


# ─── validate_questions ──────────────────────────────────────────────────


def test_valid_questions_pass():
    dec.validate_questions({"a": NOUL, "b": SCORE, "c": CHOICE})


@pytest.mark.parametrize("questions, why", [
    ({}, "empty"),
    ({"a": {"type": "freeform"}}, "type must be"),
    ({"a": {**SCORE, "criteria": ["only one"]}}, "2–10 levels"),
    ({"a": {**SCORE, "criteria": [str(i) for i in range(11)]}}, "2–10 levels"),
    ({"a": {**CHOICE, "criteria": {}}}, "1–255 options"),
    ({"a": {**NOUL, "criteria": {"yes": "y", "no": "n"}}}, "'true' and 'false'"),
])
def test_invalid_questions_are_rejected_before_any_key_is_used(questions, why):
    with pytest.raises(dec.DecisionRequestInvalid, match=why):
        dec.validate_questions(questions)


# ─── adapter error text ──────────────────────────────────────────────────


def _mock_client(status: int, body: dict | str):
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, dict):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=body)
    real = httpx.AsyncClient
    return lambda **kw: real(transport=httpx.MockTransport(handler), **kw)


async def test_decide_returns_answers_and_real_cost(monkeypatch):
    monkeypatch.setattr(dec.httpx, "AsyncClient", _mock_client(200, {
        "model": "typesafe/jev-1.13-20260917",
        "answers": {"u": {"type": "noul", "noul": 0.95}},
        "usage": {"input_tokens": 307, "output_tokens": 23, "cost": 1.29e-05},
    }))
    answers, meta = await dec.decide(model="openrouter/typesafe/jev-1.13",
                                     state="Help!", questions={"u": NOUL}, api_key="k")
    assert answers["u"]["noul"] == 0.95
    assert meta["tokens_in"] == 307 and meta["tokens_out"] == 23
    assert meta["cost_usd"] == pytest.approx(1.29e-05)
    assert meta["model_served"] == "typesafe/jev-1.13-20260917"


async def test_decide_strips_the_litellm_prefix(monkeypatch):
    """The decisions endpoint takes a bare OpenRouter id; `openrouter/` is
    LiteLLM routing syntax and would be an unknown model there."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"answers": {}, "usage": {}})
    real = httpx.AsyncClient
    monkeypatch.setattr(dec.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    await dec.decide(model="openrouter/typesafe/jev-1.13", state="s",
                     questions={"u": NOUL}, api_key="k")
    assert seen["model"] == "typesafe/jev-1.13"


async def test_402_classifies_as_depleted_not_as_a_transient_error(monkeypatch):
    """An empty prepaid balance must cool the key as out-of-money. The body
    wording is not relied on — HTTP 402 is enough."""
    monkeypatch.setattr(dec.httpx, "AsyncClient",
                        _mock_client(402, {"error": {"message": "whatever they say"}}))
    with pytest.raises(dec.DecisionHTTPError) as exc:
        await dec.decide(model="m", state="s", questions={"u": NOUL}, api_key="k")
    assert classify_provider_error(exc.value, "openrouter") == "auth"


async def test_http_error_keeps_the_provider_body(monkeypatch):
    """A bare httpx.HTTPStatusError drops the provider's reason — the
    classifier works on the message, so the body must survive."""
    monkeypatch.setattr(dec.httpx, "AsyncClient",
                        _mock_client(429, {"error": {"message": "Rate limit exceeded"}}))
    with pytest.raises(dec.DecisionHTTPError, match="Rate limit exceeded"):
        await dec.decide(model="m", state="s", questions={"u": NOUL}, api_key="k")


# ─── run_decision ────────────────────────────────────────────────────────


def _project_cap():
    return CostGuardError("project", 1.0, 1.0, 0.1)


def _paid_key(kid=1):
    return SimpleNamespace(id=kid, label=f"paid{kid}", tier="paid",
                           provider="openrouter", token_encrypted="x")


def _wire(monkeypatch, *, picks, decide, record=None, reserve=None):
    calls = {"pick": []}

    async def fake_pick(provider, scope, **kw):
        calls["pick"].append({"provider": provider, "scope": scope, **kw})
        i = len(calls["pick"]) - 1
        return picks[i] if i < len(picks) else None

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "decide", decide)
    monkeypatch.setattr("aibroker.services.attempt.decrypt", lambda t: "plain")
    monkeypatch.setattr("aibroker.services.attempt.reserve_cost", reserve or AsyncMock(return_value=None))
    monkeypatch.setattr("aibroker.services.attempt.release_cost", AsyncMock(return_value=None))
    monkeypatch.setattr("aibroker.services.attempt._penalize", AsyncMock(return_value="error"))
    monkeypatch.setattr("aibroker.services.attempt._record_error", AsyncMock(return_value=None))
    monkeypatch.setattr("aibroker.services.attempt.affinity.note_success", AsyncMock(return_value=None))
    monkeypatch.setattr("aibroker.services.attempt.record_usage", record or AsyncMock(return_value=77))
    return calls


async def test_only_paid_keys_are_asked_for(monkeypatch):
    """Billed traffic must land on the account with the prepaid credit and
    its limit — never spread over free accounts where no limit is set."""
    ok = AsyncMock(return_value=({"u": {"noul": 0.9}},
                                 {"tokens_in": 10, "tokens_out": 2, "cost_usd": 4e-7,
                                  "latency_ms": 300, "model_served": "jev"}))
    calls = _wire(monkeypatch, picks=[_paid_key()], decide=ok)
    await svc.run_decision(project=SimpleNamespace(id=1, name="vera"),
                           state="s", questions={"u": NOUL}, model=None, workflow="triage")
    assert calls["pick"][0]["require_tier"] == "paid"
    assert calls["pick"][0]["scope"] == "llm:decision"
    assert calls["pick"][0]["provider"] == "openrouter"


async def test_success_books_the_real_cost_and_both_token_counts(monkeypatch):
    record = AsyncMock(return_value=77)
    ok = AsyncMock(return_value=({"u": {"noul": 0.9}},
                                 {"tokens_in": 307, "tokens_out": 23, "cost_usd": 1.29e-05,
                                  "latency_ms": 350, "model_served": "jev-1.13"}))
    _wire(monkeypatch, picks=[_paid_key()], decide=ok, record=record)
    out = await svc.run_decision(project=SimpleNamespace(id=1, name="vera"),
                                 state="s", questions={"u": NOUL}, model=None,
                                 workflow="triage")
    assert out.answers == {"u": {"noul": 0.9}} and out.request_id == 77
    kw = record.await_args.kwargs
    assert (kw["capability"], kw["status"], kw["tokens_in"], kw["tokens_out"]) == \
        ("decision", "ok", 307, 23)
    assert kw["cost_usd"] == pytest.approx(1.29e-05)


async def test_reservation_is_not_zero(monkeypatch):
    """LiteLLM has no price for this model and would reserve $0 — the project
    daily cap would never see the spend. The estimate uses our own price."""
    reserve = AsyncMock(return_value=None)
    ok = AsyncMock(return_value=({}, {"tokens_in": 1, "tokens_out": 0, "cost_usd": 0.0,
                                      "latency_ms": 1, "model_served": None}))
    _wire(monkeypatch, picks=[_paid_key()], decide=ok, reserve=reserve)
    await svc.run_decision(project=SimpleNamespace(id=1, name="vera"),
                           state="x" * 4000, questions={"u": NOUL}, model=None, workflow=None)
    assert reserve.await_args.kwargs["estimated_cost"] > 0


async def test_rotates_to_the_next_paid_key_on_failure(monkeypatch):
    n = {"i": 0}

    async def flaky(**kw):
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("APIConnectionError: connection reset")
        return {"u": {"noul": 0.1}}, {"tokens_in": 5, "tokens_out": 1, "cost_usd": 2e-7,
                                     "latency_ms": 200, "model_served": None}
    _wire(monkeypatch, picks=[_paid_key(1), _paid_key(2)], decide=flaky)
    # Pinned: since 2026-10-03 an unpinned failure is answered by the free
    # fallback on the SAME key first; this test is about rotating the primary.
    out = await svc.run_decision(project=SimpleNamespace(id=1, name="vera"),
                                 state="s", questions={"u": NOUL}, model=_JEV, workflow=None)
    assert out.key_label == "paid2"


async def test_every_key_failing_raises_decision_failed(monkeypatch):
    async def dead(**kw):
        raise RuntimeError("boom")
    _wire(monkeypatch, picks=[_paid_key(1), _paid_key(2)], decide=dead)
    with pytest.raises(svc.DecisionFailed, match="boom"):
        await svc.run_decision(project=SimpleNamespace(id=1, name="vera"),
                               state="s", questions={"u": NOUL}, model=None, workflow=None)


async def test_no_paid_key_returns_none(monkeypatch):
    _wire(monkeypatch, picks=[], decide=AsyncMock())
    assert await svc.run_decision(project=SimpleNamespace(id=1, name="vera"),
                                  state="s", questions={"u": NOUL},
                                  model=None, workflow=None) is None


async def test_spent_project_cap_stops_before_the_provider_is_called(monkeypatch):
    provider = AsyncMock()

    async def blocked(**kw):
        raise _project_cap()
    _wire(monkeypatch, picks=[_paid_key()], decide=provider, reserve=blocked)
    with pytest.raises(svc.DecisionFailed, match="budget cap"):
        await svc.run_decision(project=SimpleNamespace(id=1, name="vera"),
                               state="s", questions={"u": NOUL}, model="openrouter/typesafe/jev-1.13", workflow=None)
    provider.assert_not_awaited()


# ─── fallback model ──────────────────────────────────────────────────────

_JEV = "openrouter/typesafe/jev-1.13"
_FB = dec.DECISION_FALLBACK_MODEL
_ANS = {"u": {"noul": 0.7}}


def _meta(cost=0.0, served=None):
    return {"tokens_in": 10, "tokens_out": 2, "cost_usd": cost,
            "latency_ms": 100, "model_served": served}


def _decide_by_model(jev_exc=None, fb_exc=None):
    seen: list[str] = []

    async def fake(*, model, **kw):
        seen.append(model)
        if model == _FB:
            if fb_exc:
                raise fb_exc
            return _ANS, _meta(0.0, "inception/mercury-decide-20260930")
        if jev_exc:
            raise jev_exc
        return _ANS, _meta(1e-6, "jev")
    return fake, seen


_PROJ = SimpleNamespace(id=1, name="vera")


async def test_fallback_not_called_when_jev_succeeds(monkeypatch):
    fake, seen = _decide_by_model()
    _wire(monkeypatch, picks=[_paid_key()], decide=fake)
    out = await svc.run_decision(project=_PROJ, state="s", questions={"u": NOUL},
                                 model=None, workflow=None)
    assert seen == [_JEV] and out.model == _JEV


async def test_jev_failure_falls_back_on_the_same_key(monkeypatch):
    fake, seen = _decide_by_model(jev_exc=dec.DecisionHTTPError("HTTP 402: out of credits"))
    record = AsyncMock(return_value=77)
    calls = _wire(monkeypatch, picks=[_paid_key(1), _paid_key(2)], decide=fake, record=record)
    out = await svc.run_decision(project=_PROJ, state="s", questions={"u": NOUL},
                                 model=None, workflow=None)
    assert seen == [_JEV, _FB]
    assert len(calls["pick"]) == 1  # the fallback reuses the held key
    assert out.model == _FB and out.cost_usd == 0.0 and out.key_label == "paid1"
    assert out.model_served == "inception/mercury-decide-20260930"
    # one error row for Jev, one ok row for the fallback
    assert att._record_error.await_count == 1
    assert att._record_error.await_args.kwargs["model"] == _JEV
    assert record.await_args.kwargs["model"] == _FB
    assert record.await_args.kwargs["status"] == "ok"
    assert att._penalize.await_count == 1  # Jev's failure only


async def test_fatal_cap_block_still_gets_a_free_answer(monkeypatch):
    fake, seen = _decide_by_model()
    reserve = AsyncMock(side_effect=_project_cap())
    _wire(monkeypatch, picks=[_paid_key()], decide=fake, reserve=reserve)
    out = await svc.run_decision(project=_PROJ, state="s", questions={"u": NOUL},
                                 model=None, workflow=None)
    assert seen == [_FB] and out.model == _FB
    assert reserve.await_count == 1  # Jev only; the $0 fallback is never reserved
    assert reserve.await_args.kwargs["estimated_cost"] > 0


async def test_jev_and_fallback_both_failing_raises_decision_failed(monkeypatch):
    fake, seen = _decide_by_model(jev_exc=RuntimeError("jev boom"),
                                  fb_exc=RuntimeError("mercury boom"))
    _wire(monkeypatch, picks=[_paid_key(1), _paid_key(2)], decide=fake)
    with pytest.raises(svc.DecisionFailed, match="mercury boom"):
        await svc.run_decision(project=_PROJ, state="s", questions={"u": NOUL},
                               model=None, workflow=None)
    assert seen == [_JEV, _FB, _JEV, _FB]
    # Jev row + fallback row per key; the key is penalised once per key, not twice
    assert att._record_error.await_count == 4
    assert att._penalize.await_count == 2


async def test_fallback_failure_never_penalises_the_decision_key(monkeypatch):
    """Jev cap-blocked, then the free fallback hits ITS own limit (20 RPM).
    The key must not be cooled for that: it is the only decision key, and its
    paid quota for Jev has nothing to do with Mercury's free one. Only a usage
    row is booked; the fatal cap still surfaces as DecisionFailed."""
    fake, seen = _decide_by_model(fb_exc=RuntimeError("429 rate limit"))
    reserve = AsyncMock(side_effect=_project_cap())
    _wire(monkeypatch, picks=[_paid_key()], decide=fake, reserve=reserve)
    with pytest.raises(svc.DecisionFailed):
        await svc.run_decision(project=_PROJ, state="s", questions={"u": NOUL},
                               model=None, workflow=None)
    assert seen == [_FB]
    assert att._penalize.await_count == 0
    assert att._record_error.await_count == 1
    assert att._record_error.await_args.kwargs["model"] == _FB


async def test_pinned_model_never_falls_back(monkeypatch):
    fake, seen = _decide_by_model(jev_exc=RuntimeError("boom"))
    _wire(monkeypatch, picks=[_paid_key()], decide=fake)
    with pytest.raises(svc.DecisionFailed, match="boom"):
        await svc.run_decision(project=_PROJ, state="s", questions={"u": NOUL},
                               model=_JEV, workflow=None)
    assert seen == [_JEV]


# ─── route ───────────────────────────────────────────────────────────────


async def test_route_requires_the_decision_scope():
    plain, _ = await _make_project(["llm:chat"])
    r = client.post("/v1/decisions", headers={"X-Project-Key": plain},
                    json={"state": "s", "questions": {"u": NOUL}})
    assert r.status_code == 403


async def test_route_rejects_malformed_questions_with_422():
    plain, _ = await _make_project(["llm:decision"])
    with patch("aibroker.routes.proxy.run_decision", AsyncMock()) as run:
        r = client.post("/v1/decisions", headers={"X-Project-Key": plain},
                        json={"state": "s", "questions": {"u": {"type": "freeform"}}})
    assert r.status_code == 422
    run.assert_not_awaited()


async def test_route_503_without_a_paid_key():
    plain, _ = await _make_project(["llm:decision"])
    with patch("aibroker.routes.proxy.run_decision", AsyncMock(return_value=None)):
        r = client.post("/v1/decisions", headers={"X-Project-Key": plain},
                        json={"state": "s", "questions": {"u": NOUL}})
    assert r.status_code == 503


async def test_route_happy_path():
    plain, _ = await _make_project(["llm:decision"])
    outcome = svc.DecisionOutcome(
        answers={"u": {"type": "noul", "noul": 0.95}}, provider="openrouter",
        model="openrouter/typesafe/jev-1.13", tokens_in=307, tokens_out=23,
        cost_usd=1.29e-05, latency_ms=350, key_label="paid1", request_id=77,
        model_served="typesafe/jev-1.13-20260917")
    with patch("aibroker.routes.proxy.run_decision", AsyncMock(return_value=outcome)):
        r = client.post("/v1/decisions", headers={"X-Project-Key": plain},
                        json={"state": "Help!", "questions": {"u": NOUL},
                              "workflow": "triage"})
    assert r.status_code == 200
    data = r.json()
    assert data["answers"]["u"]["noul"] == 0.95
    assert data["request_id"] == 77 and data["tokens_out"] == 23

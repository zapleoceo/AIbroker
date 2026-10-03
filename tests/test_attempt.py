"""services/attempt.run_attempt — the ONE key-attempt template every capability
shares: reserve -> decrypt -> call -> release -> gate -> record -> affinity, with a
single error path. These tests pin the verdict (Flow) and the bookkeeping of each
branch, independently of any capability runner."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aibroker.routing.cost_guard import CostGuardError
from aibroker.services import attempt as att
from aibroker.services.attempt import Attempt, Flow, Rejection, run_attempt
from aibroker.telemetry.request_context import request_scope

_META = {"model": "groq/m", "tokens_in": 10, "tokens_out": 5, "cost_usd": 0.25,
         "latency_ms": 7, "cache_read_tokens": 3, "cache_write_tokens": 1,
         "model_served": "exact-m"}


def _key(tier: str = "free"):
    return SimpleNamespace(id=4, label="k4", tier=tier, provider="groq", token_encrypted="x")


class _Rig:
    """Patches every collaborator of run_attempt and records what it saw."""

    def __init__(self, monkeypatch):
        self.record = AsyncMock(return_value=99)
        self.reserve = AsyncMock(return_value=None)
        self.release = AsyncMock(return_value=None)
        self.penalize = AsyncMock(return_value="error")
        self.audit = AsyncMock(return_value=None)
        self.too_large = AsyncMock(return_value=None)
        self.note = AsyncMock(return_value=None)
        for name, mock in (("record_usage", self.record), ("reserve_cost", self.reserve),
                           ("release_cost", self.release), ("_penalize", self.penalize),
                           ("audit", self.audit), ("record_too_large", self.too_large)):
            monkeypatch.setattr(att, name, mock)
        monkeypatch.setattr(att, "decrypt", lambda t: "plain")
        monkeypatch.setattr(att.affinity, "note_success", self.note)

    @staticmethod
    def attempt(call=None, **kw) -> Attempt:
        async def ok(plain):
            assert plain == "plain"
            return "text", dict(_META)

        defaults = {"key": _key(), "project": SimpleNamespace(id=1, name="p"),
                    "provider": "groq", "model": "groq/m", "capability": "chat:fast",
                    "workflow": "w", "call": call or ok}
        defaults.update(kw)
        return Attempt(**defaults)


@pytest.fixture
def rig(monkeypatch):
    return _Rig(monkeypatch)


# --- success ------------------------------------------------------------------


async def test_success_records_the_row_releases_and_repins(rig):
    res = await run_attempt(rig.attempt(key=_key("paid"), estimated_cost=0.5,
                                        pinned_model="gpt-oss-120b"))
    assert res.flow is Flow.SUCCESS and res.payload == "text" and res.usage_id == 99
    rig.reserve.assert_awaited_once()
    rig.release.assert_awaited_once()
    assert rig.release.await_args.kwargs["estimated_cost"] == 0.5
    kw = rig.record.await_args.kwargs
    assert (kw["status"], kw["http_status"], kw["error_kind"]) == ("ok", 200, None)
    assert (kw["tokens_in"], kw["tokens_out"], kw["cost_usd"]) == (10, 5, 0.25)
    assert (kw["cache_read_tokens"], kw["cache_write_tokens"]) == (3, 1)
    assert kw["model_served"] == "exact-m"
    rig.note.assert_awaited_once_with(1, "w", "chat:fast", "gpt-oss-120b", "groq", "groq/m", 4)


async def test_free_key_books_zero_and_never_releases(rig):
    res = await run_attempt(rig.attempt(key=_key("free")))
    assert res.flow is Flow.SUCCESS and rig.record.await_args.kwargs["cost_usd"] == 0.0
    rig.release.assert_not_awaited()      # nothing was reserved


async def test_meta_without_token_fields_is_tolerated(rig):
    async def call(plain):
        return "t", {"cost_usd": 0.0, "latency_ms": 3}      # transcription-shaped meta

    res = await run_attempt(rig.attempt(call=call))
    assert res.flow is Flow.SUCCESS
    assert rig.record.await_args.kwargs["tokens_in"] == 0


async def test_every_row_carries_the_request_id(rig):
    with request_scope("trace-123456"):
        await run_attempt(rig.attempt())
        await run_attempt(rig.attempt(call=AsyncMock(side_effect=RuntimeError("boom"))))
    assert [c.kwargs["request_id"] for c in rig.record.await_args_list] == ["trace-123456"] * 2
    await run_attempt(rig.attempt())       # outside a request scope: NULL, never a stale id
    assert rig.record.await_args.kwargs["request_id"] is None


# --- cap blocks ---------------------------------------------------------------


@pytest.mark.parametrize("kind", ["project", "global"])
async def test_project_or_global_cap_aborts_the_walk_without_calling(rig, kind):
    called = AsyncMock()
    rig.reserve.side_effect = CostGuardError(kind, 1.0, 1.0, 0.1)
    res = await run_attempt(rig.attempt(key=_key("paid"), estimated_cost=0.1, call=called))
    assert res.flow is Flow.BUDGET_EXHAUSTED and isinstance(res.error, CostGuardError)
    called.assert_not_awaited()
    kw = rig.record.await_args.kwargs
    assert (kw["error_kind"], kw["http_status"], kw["cost_usd"]) == ("CapBlock", 402, 0.0)
    rig.audit.assert_awaited_once()
    assert rig.audit.await_args.kwargs["action"] == "cap_block"


async def test_per_key_cap_uses_the_runners_verdict(rig):
    rig.reserve.side_effect = CostGuardError("key", 1.0, 1.0, 0.1)
    assert (await run_attempt(rig.attempt())).flow is Flow.NEXT_PROVIDER
    assert (await run_attempt(rig.attempt(cap_flow=Flow.NEXT_KEY))).flow is Flow.NEXT_KEY


async def test_reserve_false_skips_the_cap_entirely(rig):
    rig.reserve.side_effect = CostGuardError("project", 1.0, 1.0, 0.1)
    res = await run_attempt(rig.attempt(reserve=False, estimated_cost=0.5))
    assert res.flow is Flow.SUCCESS
    rig.reserve.assert_not_awaited()
    rig.release.assert_not_awaited()


# --- provider errors ----------------------------------------------------------


async def test_generic_error_penalizes_books_and_tries_the_next_key(rig):
    rig.penalize.return_value = "error"
    res = await run_attempt(rig.attempt(
        key=_key("paid"), estimated_cost=0.5, call=AsyncMock(side_effect=RuntimeError("boom"))))
    assert res.flow is Flow.NEXT_KEY and str(res.error) == "boom"
    rig.release.assert_awaited_once()       # an answerless call consumes no admission budget
    rig.penalize.assert_awaited_once()
    kw = rig.record.await_args.kwargs
    assert (kw["status"], kw["error_kind"], kw["cost_usd"], kw["tokens_in"]) == (
        "error", "RuntimeError", 0.0, 0)
    assert kw["http_status"] is None


async def test_rate_limit_error_books_429_for_the_adaptive_backoff(rig):
    await run_attempt(rig.attempt(call=AsyncMock(side_effect=RuntimeError("429 Too Many Requests"))))
    assert rig.record.await_args.kwargs["http_status"] == 429


async def test_auth_error_books_401(rig):
    await run_attempt(rig.attempt(call=AsyncMock(side_effect=RuntimeError("401 Unauthorized"))))
    assert rig.record.await_args.kwargs["http_status"] == 401


async def test_model_unavailable_is_a_model_problem_not_a_key_problem(rig):
    res = await run_attempt(rig.attempt(call=AsyncMock(
        side_effect=RuntimeError("404 - Function 'x': Not found for account 'y'"))))
    assert res.flow is Flow.NEXT_PROVIDER
    rig.penalize.assert_not_awaited()       # the key's other models still work
    assert rig.record.await_args.kwargs["status"] == "error"


async def test_too_large_prompt_teaches_the_ceiling_and_skips_the_provider(rig):
    res = await run_attempt(rig.attempt(est_tokens=9000, call=AsyncMock(
        side_effect=RuntimeError("context length exceeded, request too large"))))
    assert res.flow is Flow.NEXT_PROVIDER
    rig.too_large.assert_awaited_once_with("groq", 9000)
    rig.record.assert_awaited_once()        # every attempt is booked (and so traceable)


async def test_too_large_is_not_learned_without_a_size_estimate(rig):
    res = await run_attempt(rig.attempt(est_tokens=0, call=AsyncMock(
        side_effect=RuntimeError("context length exceeded"))))
    assert res.flow is Flow.NEXT_KEY
    rig.too_large.assert_not_awaited()


async def test_penalize_false_books_the_error_but_never_cools_the_key(rig):
    res = await run_attempt(rig.attempt(penalize=False, call=AsyncMock(
        side_effect=RuntimeError("429 rate limit"))))
    assert res.flow is Flow.NEXT_KEY
    rig.penalize.assert_not_awaited()
    assert rig.record.await_args.kwargs["http_status"] == 429


# --- quality gate -------------------------------------------------------------


async def test_rejection_books_a_billed_error_and_returns_its_flow(rig):
    seen = []
    rej = Rejection("InvalidJSON", Flow.NEXT_PROVIDER, note=lambda: seen.append("noted"))
    res = await run_attempt(rig.attempt(key=_key("paid"), check=lambda payload, meta: rej))
    assert res.flow is Flow.NEXT_PROVIDER and res.payload == "text"
    kw = rig.record.await_args.kwargs
    assert (kw["status"], kw["error_kind"], kw["http_status"]) == ("error", "InvalidJSON", 200)
    assert (kw["tokens_in"], kw["cost_usd"], kw["cache_read_tokens"]) == (10, 0.25, 3)
    assert seen == ["noted"]
    rig.note.assert_not_awaited()           # a rejected answer must not pin affinity


async def test_unbilled_rejection_books_zeros(rig):
    rej = Rejection("EmptyBody", Flow.NEXT_PROVIDER, http_status=502, bill=False)
    await run_attempt(rig.attempt(check=lambda payload, meta: rej))
    kw = rig.record.await_args.kwargs
    assert (kw["tokens_in"], kw["tokens_out"], kw["cost_usd"], kw["http_status"]) == (0, 0, 0.0, 502)


async def test_passing_gate_is_a_success(rig):
    res = await run_attempt(rig.attempt(check=lambda payload, meta: None))
    assert res.flow is Flow.SUCCESS


async def test_note_affinity_false_skips_the_repin(rig):
    await run_attempt(rig.attempt(note_affinity=False))
    rig.note.assert_not_awaited()


# --- helpers ------------------------------------------------------------------


def test_scrub_secrets_masks_credentials():
    raw = "401 for sk-abcdefghijklmnop and key=AIzaSyAbcdefghijklmnopqrstuvw and Bearer abcdefghijklmnopqrst"
    out = att._scrub_secrets(raw)
    assert "sk-abcdef" not in out and "AIzaSy" not in out and "abcdefghijklmnopqrst" not in out
    assert "[redacted]" in out


def test_billed_cost_free_tier_is_always_zero():
    assert att._billed_cost(_key("free"), {"cost_usd": 3.0}) == 0.0
    assert att._billed_cost(_key("paid"), {"cost_usd": 3.0}) == 3.0

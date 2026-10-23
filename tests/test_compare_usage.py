"""/ask2many: max_tokens on submit, truncated (finish_reason=length) results, per-model usage and
cost, request totals, subscription pricing and unknown-vs-zero. Reuses the fakes of
test_compare_page (Broker's job path is replaced at services.compare.submit_model/poll_job)."""
# ruff: noqa: F811, I001  (fixtures imported from test_compare_page are used by parameter name)
from __future__ import annotations

from types import SimpleNamespace

from aibroker.providers.job_contract import DEFAULT_MAX_TOKENS, JOB_MAX_TOKENS_MAX
from aibroker.scripts.provision_compare import provision
from aibroker.services import compare as svc
from tests.test_compare_page import (  # noqa: F401  (fixtures are used by name)
    _no_background,
    fakes,
    job,
    make_client,
    subscriptions,
    submit,
    two_models,
)


async def test_submit_model_sends_the_max_token_limit_except_for_subscription_routes(monkeypatch):
    from aibroker.routes import proxy
    seen = []

    async def fake_submit(body, response, capability, key, trace, ctx):
        seen.append(body)
        return SimpleNamespace(job_id=9)

    monkeypatch.setattr(proxy, "jobs_submit", fake_submit)
    project = SimpleNamespace(name=svc.COMPARE_PROJECT, id=1, allowed_scopes=[])
    await svc.submit_model(project, "anthropic/claude-sonnet-5", "chat:smart", "q")
    await svc.submit_model(project, "claude-sub/claude-sonnet-5-5", "chat:smart", "q")
    assert seen[0].max_tokens == JOB_MAX_TOKENS_MAX == 16384       # the constant, not a literal
    assert seen[1].max_tokens == DEFAULT_MAX_TOKENS                # subscription routes demand it


async def test_length_finish_is_truncated_not_done(fakes):
    await provision()
    ids = await two_models()
    async with make_client() as c:
        rid = (await submit(c, "q", ids)).json()["id"]
        await svc.fan_out(rid, await svc.get_project(), "q")
        fakes.jobs[101] = job("done", text="обрезанный текст", finish="length", tin=5, tout=1024)
        fakes.jobs[102] = job("done", text="", finish="length", tin=5, tout=1024)
        p = (await c.get(f"/ask2many/api/requests/{rid}")).json()
    first, second = p["results"]
    assert first["status"] == second["status"] == "truncated" and p["done"] is True
    assert first["answer"] == "обрезанный текст" and first["error"] == svc.TRUNCATED_NOTE
    assert svc.TRUNCATED_NOTE in second["error"] and svc.TRUNCATED_EMPTY_NOTE in second["error"]
    assert first["finish_reason"] == "length"


async def test_usage_cost_and_totals_are_exposed_with_unknown_not_zero(fakes):
    await provision()
    ids = [m["id"] for m in svc.page_models() if not m["subscription"]][:2]   # priced models
    async with make_client() as c:
        rid = (await submit(c, "q", ids)).json()["id"]
        await svc.fan_out(rid, await svc.get_project(), "q")
        fakes.jobs[101] = job("done", text="a", finish="stop", tin=10, tout=20, cost=0.0123)
        fakes.jobs[102] = job("done", text="b", finish="stop")        # Broker reported no usage
        p = (await c.get(f"/ask2many/api/requests/{rid}")).json()
    known, unknown = p["results"]
    assert (known["tokens_in"], known["tokens_out"], known["cost_usd"]) == (10, 20, 0.0123)
    assert (unknown["tokens_in"], unknown["tokens_out"], unknown["cost_usd"]) == (None, None, None)
    assert p["usage"] == {"tokens_in": 10, "tokens_out": 20, "cost_usd": 0.0123,
                          "tokens_unknown": 1, "cost_unknown": 1, "subscription": 0}


async def test_a_real_zero_cost_stays_zero(fakes):
    await provision()
    ids = [m["id"] for m in svc.page_models() if not m["subscription"]][:1]
    async with make_client() as c:
        rid = (await submit(c, "q", ids)).json()["id"]
        await svc.fan_out(rid, await svc.get_project(), "q")
        fakes.jobs[101] = job("done", text="a", tin=1, tout=2, cost=0.0)
        r = (await c.get(f"/ask2many/api/requests/{rid}")).json()["results"][0]
    assert r["cost_usd"] == 0.0 and r["cost_usd"] is not None


def test_subscription_results_are_never_priced_in_the_totals():
    rows = [{"status": "done", "subscription": True, "tokens_in": 3, "tokens_out": 4,
             "cost_usd": None},
            {"status": "done", "subscription": False, "tokens_in": 1, "tokens_out": 2,
             "cost_usd": 0.5},
            {"status": "error", "subscription": False, "tokens_in": None, "tokens_out": None,
             "cost_usd": None}]
    assert svc.usage_totals(rows) == {"tokens_in": 4, "tokens_out": 6, "cost_usd": 0.5,
                                      "tokens_unknown": 0, "cost_unknown": 0, "subscription": 1}


async def test_subscription_model_result_says_subscription(subscriptions, fakes):
    await provision()
    async with make_client() as c:
        rid = (await submit(c, "q", ["claude-sub/claude-sonnet-5-5"])).json()["id"]
        await svc.fan_out(rid, await svc.get_project(), "q")
        fakes.jobs[101] = job("done", text="a", tin=3, tout=4, billing="subscription")
        r = (await c.get(f"/ask2many/api/requests/{rid}")).json()["results"][0]
    assert r["subscription"] is True and r["cost_usd"] is None


async def test_page_shows_score_only_in_the_table_and_explains_it():
    async with make_client() as c:
        page = (await c.get("/ask2many")).text
    assert "сводный балл интеллекта по бенчмаркам, чем выше, тем умнее" in page
    assert 'nameOf(id)+" — "' not in page and "Балл: " not in page    # chips/cards: no score
    for needle in ("по подписке", "неизвестно", "обрезано", "Итого: токены", "ⓘ"):
        assert needle in page, needle

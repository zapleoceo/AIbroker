"""Public /ask2many page: shell, model catalog + AA scores, fan-out, cap-0 refusal, history,
XSS, validation and the one-question-at-a-time guard. Broker's job machinery is replaced by
fakes at two seams (services.compare.submit_model / poll_job); one test drives the real
submit path to prove the job lands under the dedicated project."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, update

from aibroker.auth import generate_project_key, hash_project_key
from aibroker.db import get_session
from aibroker.db.models import CompareRequestRow, CompareResultRow, DeepJobRow, ProjectRow
from aibroker.main import app
from aibroker.providers import catalog
from aibroker.providers.model_scores import SCORES, Score, intelligence_index
from aibroker.scripts.provision_compare import provision
from aibroker.services import compare as svc
from aibroker.services.llm_service import cap_message

BASE = "http://testserver"


def make_client(ip: str = "10.0.0.1") -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE,
                             headers={"x-forwarded-for": ip})


@pytest.fixture(autouse=True)
def _no_background(monkeypatch):
    """The tracker is fire-and-forget; tests call fan_out/sync_request themselves."""
    monkeypatch.setattr(svc, "spawn_tracker", lambda *a, **k: None)


@pytest.fixture
def fakes(monkeypatch):
    """submit_model hands out sequential job ids; poll_job answers from `jobs[id]`."""
    state = SimpleNamespace(jobs={}, refuse=set(), next=100, submitted=[], cancelled=[])

    async def cancel(project, job_id):
        state.cancelled.append(job_id)

    monkeypatch.setattr(svc, "cancel_model_job", cancel)

    async def submit(project, model_id, capability, prompt):
        if model_id in state.refuse:
            raise svc.Refused("отказ: тест")
        state.next += 1
        state.submitted.append((project.name, model_id, capability, prompt))
        return state.next

    async def poll(project, job_id):
        return state.jobs.get(job_id)

    monkeypatch.setattr(svc, "submit_model", submit)
    monkeypatch.setattr(svc, "poll_job", poll)
    return state


def job(status, **kw):
    return SimpleNamespace(status=status, text=kw.get("text"), error=kw.get("error"),
                           model_served=kw.get("served"), latency_ms=kw.get("latency"))


async def two_models() -> list[str]:
    ids = [m["id"] for m in svc.page_models()]
    assert len(ids) >= 2
    return ids[:2]


# ─── page + catalog ─────────────────────────────────────────────────────────


async def test_page_renders_with_attribution_and_sets_httponly_cookie():
    async with make_client() as c:
        r = await c.get("/ask2many")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "Benchmarks: Artificial Analysis" in r.text
    assert 'name="viewport"' in r.text
    cookie = r.headers["set-cookie"]
    assert svc.COOKIE_NAME in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie


async def test_page_shell_never_writes_markup_from_data():
    async with make_client() as c:
        r = await c.get("/ask2many")
    assert "innerHTML" not in r.text and "insertAdjacentHTML" not in r.text


async def test_models_endpoint_lists_live_chat_models_with_scores():
    async with make_client() as c:
        r = await c.get("/ask2many/api/models")
    body = r.json()
    assert r.status_code == 200
    assert body["attribution"] == "Benchmarks: Artificial Analysis (https://artificialanalysis.ai/)"
    assert body["limits"] == {"max_prompt_chars": 4000, "max_models": 12}
    rows = {m["id"]: m for m in body["data"]}
    live = {row["id"]: row for row in catalog.listing()}
    assert rows and set(rows) <= set(live)
    for mid in rows:        # chat-capable and routable (subscription routes: marked available)
        if live[mid]["billing_type"] == "subscription":
            assert live[mid].get("available") is not False
        else:
            assert svc.lane_for(live[mid]["capabilities"], live[mid]["owned_by"]) is not None
    scores = [m["intelligence_index"] for m in body["data"]]
    scored = [s for s in scores if s is not None]
    assert scored == sorted(scored, reverse=True)                  # best first
    assert scores[:len(scored)] == scored and not any(s is not None for s in scores[len(scored):])
    assert all(set(m) == {"id", "name", "provider", "intelligence_index", "subscription"}
               for m in rows.values())
    names = [m["name"] for m in body["data"]]
    assert len(names) == len(set(names)) and len(names) <= svc.TOP_MODELS   # deduped, top 20
    assert [m["id"] for m in body["data"]] == [m["id"] for m in svc.page_models()]
    assert "whisper" not in " ".join(rows)                                # not a chat model


def row(mid, name, score, *, billing="api", caps=("chat:fast",), owner="groq", **extra):
    return {"id": mid, "name": name, "owned_by": owner, "capabilities": list(caps),
            "billing_type": billing, "intelligence_index": score, **extra}


def fake_rows(monkeypatch, rows):
    monkeypatch.setattr(svc.catalog, "listing", lambda: rows)


def test_page_models_dedupes_same_names_with_subscription_then_free_then_id(monkeypatch):
    fake_rows(monkeypatch, [
        row("groq/x", "gpt-oss-120b", 11.6), row("cerebras/gpt-oss-120b", "gpt-oss-120b", 11.6,
                                                     owner="cerebras"),
        row("anthropic/dup", "dup", 5.0, owner="anthropic", caps=("chat:smart",)),
        row("claude-sub/dup", "dup", 5.0, billing="subscription", caps=("chat:smart",),
            owner="claude-sub", available=True),
        row("anthropic/solo", "solo", 1.0, owner="anthropic", caps=("chat:smart",)),
    ])
    by_name = {m["name"]: m["id"] for m in svc.page_models()}
    assert by_name["dup"] == "claude-sub/dup"                    # subscription route wins
    assert by_name["gpt-oss-120b"] == "cerebras/gpt-oss-120b"    # both free: first by id
    assert len(svc.page_models()) == 3                            # one entry per name


def test_page_models_paid_provider_loses_to_free_with_the_same_name(monkeypatch):
    fake_rows(monkeypatch, [row("anthropic/m", "m", 3.0, owner="anthropic", caps=("chat:smart",)),
                            row("groq/m", "m", 3.0, owner="groq")])
    assert [m["id"] for m in svc.page_models()] == ["groq/m"]


def test_page_models_keeps_only_the_top_20_best_first_unscored_last(monkeypatch):
    rows = [row(f"groq/m{i}", f"m{i:02d}", float(i)) for i in range(1, 26)]
    rows += [row("groq/zz", "zz", None), row("groq/aa", "aa", None)]
    fake_rows(monkeypatch, rows)
    got = svc.page_models()
    assert len(got) == svc.TOP_MODELS == 20
    assert [m["intelligence_index"] for m in got] == [float(i) for i in range(25, 5, -1)]
    # unscored models only fill the tail when there are fewer than 20 scored ones
    fake_rows(monkeypatch, rows[:3] + rows[-2:])
    assert [m["name"] for m in svc.page_models()] == ["m03", "m02", "m01", "aa", "zz"]


async def test_model_outside_the_top_20_is_422_on_submit_and_not_listed(monkeypatch, fakes):
    await provision()
    rows = [row(f"groq/m{i}", f"m{i:02d}", float(i)) for i in range(1, 26)]
    fake_rows(monkeypatch, rows)
    async with make_client() as c:
        listed = (await c.get("/ask2many/api/models")).json()["data"]
        assert len(listed) == 20 and "groq/m01" not in {m["id"] for m in listed}
        low = await c.post("/ask2many/api/requests", json={"prompt": "x", "models": ["groq/m01"]})
        top = await c.post("/ask2many/api/requests", json={"prompt": "x", "models": ["groq/m25"]})
    assert low.status_code == 422 and "groq/m01" in low.json()["detail"]
    assert top.status_code == 202


def test_models_sorted_by_score_desc_unscored_last_then_by_name(monkeypatch):
    rows = [row("groq/b", "b", 10.0), row("groq/zz", "zz", None), row("groq/a", "a", 10.0),
            row("groq/top", "top", 40.5), row("groq/aa", "aa", None), row("groq/mid", "mid", 22.0)]
    monkeypatch.setattr(svc.catalog, "listing", lambda: rows)
    assert [m["name"] for m in svc.chat_models()] == ["top", "mid", "a", "b", "aa", "zz"]


@pytest.fixture
def subscriptions(monkeypatch):
    """Claude + Codex subscription routes verified and enabled, as in test_subscription_jobs."""
    from aibroker.config import get_settings
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_MODELS", "default")
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_VERIFIED_MODELS",
                        '{"claude-sub":["claude-sonnet-5-5"],"codex-sub":["gpt-6.1-sol"]}')
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_CODEX_ENABLED", True)


async def test_subscription_models_are_listed_with_the_smart_lane(subscriptions):
    by_id = {m["id"]: m for m in svc.chat_models()}
    assert by_id["claude-sub/claude-sonnet-5-5"]["capability"] == "chat:smart"
    assert by_id["claude-sub/claude-sonnet-5-5"]["subscription"] is True
    async with make_client() as c:
        ids = {m["id"] for m in (await c.get("/ask2many/api/models")).json()["data"]}
    assert {"claude-sub/claude-sonnet-5-5", "codex-sub/gpt-6.1-sol"} <= ids
    assert by_id["codex-sub/gpt-6.1-sol"]["capability"] == "chat:smart"
    assert by_id["codex-sub/gpt-6.1-sol"]["subscription"] is True


async def test_submit_endpoint_accepts_claude_and_codex_subscription_models(subscriptions, fakes):
    await provision()
    async with make_client() as c:
        r = await c.post("/ask2many/api/requests", json={
            "prompt": "x", "models": ["claude-sub/claude-sonnet-5-5", "codex-sub/gpt-6.1-sol"]})
    assert r.status_code == 202
    async with get_session() as s:
        models = (await s.execute(select(CompareResultRow.model_id))).scalars().all()
    assert sorted(models) == ["claude-sub/claude-sonnet-5-5", "codex-sub/gpt-6.1-sol"]


async def test_page_busy_text_is_the_servers_text():
    async with make_client() as c:
        page = (await c.get("/ask2many")).text
    assert json.dumps(svc.BUSY_MESSAGE, ensure_ascii=False) in page


def test_unverified_subscription_models_are_not_offered(monkeypatch):
    from aibroker.config import get_settings
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_VERIFIED_MODELS", "{}")
    assert not [m for m in svc.chat_models() if m["id"].startswith(("claude-sub/", "codex-sub/"))]


async def test_subscription_submit_enqueues_an_agent_job_under_the_compare_project(
        subscriptions, monkeypatch):
    from unittest.mock import AsyncMock

    from aibroker.routes import proxy
    await provision()
    project = await svc.get_project()
    create = AsyncMock(return_value=(77, True))
    monkeypatch.setattr(proxy, "create_run", create)
    monkeypatch.setattr(proxy, "admit_request", AsyncMock())
    monkeypatch.setattr(proxy, "wake_worker", AsyncMock())
    monkeypatch.setattr(proxy, "project_allowed", lambda _id: True)
    monkeypatch.setattr(proxy, "policy_violation", lambda *_: None)
    for model, agent, cli in (("codex-sub/gpt-6.1-sol", "agent:codex", "gpt-6.1-sol"),
                              ("claude-sub/claude-sonnet-5-5", "agent:claude",
                               "claude-sonnet-5-5")):
        job_id = await svc.submit_model(project, model, "chat:smart", "привет")
        kw = create.await_args.kwargs
        assert job_id == 77 and kw["project"].name == svc.COMPARE_PROJECT
        assert kw["capability"] == agent and kw["model"] == cli
        assert kw["client_job"]["capability"] == "chat:smart" and kw["client_job"]["model"] == model
        assert kw["allowed_tools"] == [] and not kw["client_job"]["tools"]   # text only


async def test_subscription_refusals_are_shown_as_refusals_not_crashes(subscriptions):
    await provision()
    project = await svc.get_project()
    # AGENT_ALLOWED_PROJECT_IDS is empty by default: fail-closed -> readable refusal
    with pytest.raises(svc.Refused, match="agent_project_not_allowed"):
        await svc.submit_model(project, "claude-sub/claude-sonnet-5-5", "chat:smart", "x")


# ─── intelligence_index in the catalog ──────────────────────────────────────


def test_exact_name_gets_the_score_and_other_versions_get_none():
    assert intelligence_index("claude-sonnet-5") == SCORES["claude-sonnet-5"].value == 38.16
    for other in ("claude-sonnet-5.5", "gpt-6-sol", "gpt-5.6-luna", "claude-sonnet-4", "claude-sonnet",
                  "claude-sonnet-5-latest", "gpt-5-nano", "gemini-2.5-flash"):
        assert intelligence_index(other) is None


def test_neighbouring_versions_do_not_share_a_score():
    table = {"claude-sonnet-5": Score(38.16, "claude-sonnet-5", "Sonnet 5"),
             "claude-sonnet-5.5": Score(56.0, "claude-sonnet-5-5", "Sonnet 5.5")}
    assert intelligence_index("claude-sonnet-5", table) == 38.16
    assert intelligence_index("claude-sonnet-5.5", table) == 56.0
    assert intelligence_index("claude-sonnet-5.6", table) is None


async def test_v1_models_exposes_intelligence_index_field():
    key = generate_project_key()
    async with get_session() as s:
        s.add(ProjectRow(name="t", project_key_hash=hash_project_key(key),
                         project_key_prefix=key[:12], allowed_scopes=["llm:chat"]))
    async with make_client() as c:
        r = await c.get("/v1/models", headers={"X-Project-Key": key})
    assert r.status_code == 200
    rows = {m["id"]: m for m in r.json()["data"]}
    assert all("intelligence_index" in m for m in rows.values())
    assert rows["anthropic/claude-sonnet-5"]["intelligence_index"] == 38.16
    assert rows["groq/openai/gpt-oss-120b"]["intelligence_index"] == 11.6
    assert rows["anthropic/claude-haiku-4-5"]["intelligence_index"] is None


# ─── provisioning ───────────────────────────────────────────────────────────


async def test_provision_creates_cap_zero_project_idempotently_without_printing_a_key(capsys):
    assert await provision() == "created"
    assert await provision() == "updated"
    async with get_session() as s:
        rows = (await s.execute(select(ProjectRow).where(
            ProjectRow.name == svc.COMPARE_PROJECT))).scalars().all()
    assert len(rows) == 1
    p = rows[0]
    assert p.daily_cost_cap_usd == 0.0 and p.monthly_cost_cap_usd == 0.0 and p.is_active
    assert "llm:chat" in p.allowed_scopes and "admin:read" not in p.allowed_scopes
    assert "llm:agent" in p.allowed_scopes          # subscription (agent) models
    async with get_session() as s:                  # re-run re-asserts scopes, caps stay 0
        await s.execute(update(ProjectRow).where(ProjectRow.id == p.id)
                        .values(allowed_scopes=["llm:chat"], daily_cost_cap_usd=5.0))
    assert await provision() == "updated"
    async with get_session() as s:
        p = (await s.execute(select(ProjectRow).where(ProjectRow.id == p.id))).scalar_one()
    assert "llm:agent" in p.allowed_scopes
    assert p.daily_cost_cap_usd == 0.0 and p.monthly_cost_cap_usd == 0.0
    out = capsys.readouterr()
    assert "aib_prj_" not in out.out + out.err


async def test_real_submit_path_enqueues_job_under_the_compare_project():
    await provision()
    project = await svc.get_project()
    entry = next(m for m in svc.chat_models() if m["id"] == "anthropic/claude-sonnet-5")
    job_id = await svc.submit_model(project, entry["id"], entry["capability"], "привет")
    async with get_session() as s:
        row = (await s.execute(select(DeepJobRow).where(DeepJobRow.id == job_id))).scalar_one()
    assert row.project_id == project.id and row.status == "pending"
    assert row.request["model"] == "anthropic/claude-sonnet-5"   # paid model: no page-side check
    with pytest.raises(svc.Refused, match="отказ"):
        await svc.submit_model(project, "no/such-model", "chat:smart", "x")


# ─── validation ─────────────────────────────────────────────────────────────


async def test_submit_validation(fakes):
    await provision()
    ids = await two_models()
    async with make_client() as c:
        async def post(prompt, models):
            return await c.post("/ask2many/api/requests", json={"prompt": prompt, "models": models})

        assert (await post("x" * 4001, ids)).status_code == 422
        assert (await post("", ids)).status_code == 422
        assert (await post("   ", ids)).status_code == 422
        assert (await post("ok", [])).status_code == 422
        assert (await post("ok", [f"m{i}" for i in range(13)])).status_code == 422
        r = await post("ok", ["no/such-model"])
        assert r.status_code == 422 and "no/such-model" in r.json()["detail"]
        assert (await post("x" * 4000, ids)).status_code == 202


async def test_submit_without_provisioned_project_is_503(fakes):
    ids = await two_models()
    async with make_client() as c:
        r = await c.post("/ask2many/api/requests", json={"prompt": "ok", "models": ids})
    assert r.status_code == 503


# ─── fan-out ────────────────────────────────────────────────────────────────


async def submit(c, prompt, ids):
    return await c.post("/ask2many/api/requests", json={"prompt": prompt, "models": ids})


async def test_fan_out_creates_one_job_per_model_and_fills_answers(fakes):
    await provision()
    ids = await two_models()
    async with make_client() as c:
        r = await submit(c, "какая лучшая частная школа полтавы", ids)
        assert r.status_code == 202
        rid = r.json()["id"]
        await svc.fan_out(rid, await svc.get_project(), "какая лучшая частная школа полтавы")
        assert [s[1] for s in fakes.submitted] == ids
        assert {s[0] for s in fakes.submitted} == {svc.COMPARE_PROJECT}
        assert all(s[3] == "какая лучшая частная школа полтавы" for s in fakes.submitted)

        p = (await c.get(f"/ask2many/api/requests/{rid}")).json()
        assert p["done"] is False and [x["status"] for x in p["results"]] == ["pending"] * 2

        fakes.jobs[101] = job("done", text="Ответ A", served="m-a", latency=120)
        fakes.jobs[102] = job("pending")
        p = (await c.get(f"/ask2many/api/requests/{rid}")).json()
        by = {x["model_id"]: x for x in p["results"]}
        assert by[ids[0]]["status"] == "done" and by[ids[0]]["answer"] == "Ответ A"
        assert by[ids[0]]["model_served"] == "m-a" and by[ids[0]]["latency_ms"] == 120
        assert by[ids[1]]["status"] == "pending" and p["done"] is False

        fakes.jobs[102] = job("done", text="Ответ B")
        p = (await c.get(f"/ask2many/api/requests/{rid}")).json()
        assert p["done"] is True
        assert {x["model_served"] for x in p["results"]} == {"m-a", "unknown"}


async def test_cap_zero_refusal_is_shown_as_the_models_result(fakes):
    await provision()
    ids = await two_models()
    async with make_client() as c:
        rid = (await submit(c, "q", ids)).json()["id"]
        await svc.fan_out(rid, await svc.get_project(), "q")
        fakes.jobs[101] = job("error", error=cap_message("daily"))   # what the dispatcher writes
        fakes.jobs[102] = job("error", error="all providers failed")
        p = (await c.get(f"/ask2many/api/requests/{rid}")).json()
    first, second = p["results"]
    assert first["status"] == "refused" and first["error"].startswith("отказ: лимит проекта")
    assert second["status"] == "error" and second["error"] == "all providers failed"
    assert p["done"] is True


async def test_submit_time_refusal_is_stored_per_model(fakes):
    await provision()
    ids = await two_models()
    fakes.refuse.add(ids[0])
    async with make_client() as c:
        rid = (await submit(c, "q", ids)).json()["id"]
        await svc.fan_out(rid, await svc.get_project(), "q")
        p = (await c.get(f"/ask2many/api/requests/{rid}")).json()
    assert p["results"][0]["status"] == "refused" and p["results"][0]["error"] == "отказ: тест"
    assert p["results"][1]["status"] == "pending"


# ─── one question at a time ─────────────────────────────────────────────────


async def finish(rid: int, fakes) -> None:
    async with get_session() as s:
        await s.execute(update(CompareResultRow).where(CompareResultRow.request_id == rid)
                        .values(status="done", answer="ok"))


async def test_second_submit_while_first_is_running_is_429_with_polite_text(fakes):
    await provision()
    ids = await two_models()
    async with make_client() as c:
        assert (await submit(c, "первый", ids)).status_code == 202
        r = await submit(c, "второй", ids)
    assert r.status_code == 429
    assert r.json()["detail"] == svc.BUSY_MESSAGE
    assert r.json()["detail"].startswith("Извините, пока идёт ваш предыдущий вопрос")
    async with get_session() as s:
        n = (await s.execute(select(CompareRequestRow))).scalars().all()
    assert len(n) == 1


async def test_allowed_again_after_every_model_reached_a_terminal_status(fakes):
    await provision()
    ids = await two_models()
    async with make_client() as c:
        rid = (await submit(c, "первый", ids)).json()["id"]
        async with get_session() as s:      # one model finished, the other still pending
            await s.execute(update(CompareResultRow).where(
                CompareResultRow.request_id == rid, CompareResultRow.model_id == ids[0])
                .values(status="done"))
        assert (await submit(c, "второй", ids)).status_code == 429
        async with get_session() as s:      # a refusal / error is terminal too
            await s.execute(update(CompareResultRow).where(
                CompareResultRow.request_id == rid, CompareResultRow.model_id == ids[1])
                .values(status="refused"))
        assert (await submit(c, "второй", ids)).status_code == 202


async def test_concurrent_submits_admit_exactly_one(fakes):
    await provision()
    ids = await two_models()
    async with make_client() as c:
        rs = await asyncio.gather(*(submit(c, f"q{i}", ids) for i in range(4)))
    assert sorted(r.status_code for r in rs) == [202, 429, 429, 429]
    async with get_session() as s:
        assert len((await s.execute(select(CompareRequestRow))).scalars().all()) == 1


def test_cap_error_matches_only_the_dispatchers_exact_message():
    assert svc.is_cap_error(cap_message("daily")) and svc.is_cap_error(cap_message("monthly"))
    assert not svc.is_cap_error("provider said: cap exceeded, budget reached")


async def test_page_has_the_key_elements_and_no_external_assets():
    async with make_client() as c:
        page = (await c.get("/ask2many")).text
    assert "<h1>Ask2many — один вопрос, много моделей</h1>" in page
    assert "Вопросы и ответы видны всем в общей истории." in page
    for needle in ('id="prompt"', 'id="go"', "Спросить", "Ждём ответы", 'id="models"',
                   'id="all"', 'id="clear"', "выбрать все", "снять все", 'id="out"',
                   'id="q"', 'id="hist"', "подписка", "prefers-color-scheme:dark",
                   "Benchmarks: Artificial Analysis",
                   # two-column layout, model table with filter, selected chips, counter
                   "grid-template-columns:3fr 2fr", 'id="mf"', "Фильтр моделей", "Модель", "Балл",
                   "position:sticky", 'id="selected"', "Выбранные модели", 'id="sel"',
                   "из __MAX_MODELS__".replace("__MAX_MODELS__", "12"), 'setAttribute("role","button")',
                   "aria-pressed", "Ctrl+Enter", "Показать ещё", 'id="more"',
                   "нет оценки", "Ничего не найдено", "min-height:44px", "@media (max-width:767px)"):
        assert needle in page, needle
    assert "__MAX_" not in page and "__ATTRIBUTION__" not in page   # placeholders filled
    for external in ("http://cdn", "https://cdn", "googleapis", "<link rel=\"stylesheet\"",
                     "<script src"):
        assert external not in page
    assert "textContent" in page and "innerHTML" not in page


async def test_another_browser_and_ip_is_allowed_but_same_ip_new_cookie_is_not(fakes):
    await provision()
    ids = await two_models()
    async with make_client("10.0.0.1") as a, make_client("10.0.0.2") as b, \
            make_client("10.0.0.1") as same_ip:
        assert (await submit(a, "a", ids)).status_code == 202
        assert (await submit(b, "b", ids)).status_code == 202          # other browser + IP
        assert (await submit(same_ip, "c", ids)).status_code == 429    # cookie cleared, same IP


async def test_same_cookie_from_another_ip_is_still_blocked(fakes):
    await provision()
    ids = await two_models()
    async with make_client("10.0.0.1") as a:
        assert (await submit(a, "a", ids)).status_code == 202
        a.headers["x-forwarded-for"] = "10.9.9.9"
        assert (await submit(a, "b", ids)).status_code == 429


async def test_timeout_safety_net_frees_a_stuck_user_and_closes_the_request(fakes):
    await provision()
    ids = await two_models()
    async with make_client() as c:
        rid = (await submit(c, "завис", ids)).json()["id"]
        await svc.fan_out(rid, await svc.get_project(), "завис")
        fakes.jobs[101] = job("pending")
        fakes.jobs[102] = job("pending")
        assert (await submit(c, "ещё", ids)).status_code == 429
        old = svc._now() - timedelta(seconds=svc.ACTIVE_TIMEOUT_S + 5)
        async with get_session() as s:
            await s.execute(update(CompareRequestRow).where(CompareRequestRow.id == rid)
                            .values(created_at=old))
        assert (await submit(c, "ещё", ids)).status_code == 202   # guard no longer holds
        p = (await c.get(f"/ask2many/api/requests/{rid}")).json()  # and the stuck one is closed
    assert p["done"] is True
    assert {x["status"] for x in p["results"]} == {"error"}
    assert all(x["error"] == svc.TIMEOUT_MESSAGE for x in p["results"])
    assert sorted(fakes.cancelled) == [101, 102]       # unfinished Broker jobs were cancelled


# ─── history ────────────────────────────────────────────────────────────────


async def seed_history(prompts: list[str]) -> None:
    now = svc._now()
    async with get_session() as s:
        for i, p in enumerate(prompts):
            req = CompareRequestRow(prompt=p, browser_id="seed", client_ip="",
                                    created_at=now + timedelta(seconds=i))
            s.add(req)
            await s.flush()
            s.add(CompareResultRow(request_id=req.id, model_id="m/x", capability="chat:fast",
                                   status="done", answer=f"ответ {i}", created_at=now))


async def test_history_is_saved_newest_first_and_searchable_case_insensitively(fakes):
    await provision()
    ids = await two_models()
    async with make_client() as c:
        rid = (await submit(c, "Лучшая приватная школа Полтавы, Best PRIVATE school", ids)).json()["id"]
        h = (await c.get("/ask2many/api/history")).json()
        assert h["total"] == 1 and h["items"][0]["id"] == rid
        assert [r["model_id"] for r in h["items"][0]["results"]] == ids
        assert (await c.get("/ask2many/api/history", params={"q": "приватная"})).json()["total"] == 1
        # ASCII case-folding; Cyrillic folding is Postgres ILIKE behaviour (SQLite folds ASCII only)
        assert (await c.get("/ask2many/api/history", params={"q": "private"})).json()["total"] == 1
        assert (await c.get("/ask2many/api/history", params={"q": "нет такого"})).json()["total"] == 0


async def test_history_search_treats_percent_and_underscore_literally_and_paginates():
    await seed_history([f"вопрос {i}" for i in range(25)] + ["100% уверен", "a_b", "axb"])
    async with make_client() as c:
        first = (await c.get("/ask2many/api/history")).json()
        assert first["total"] == 28 and len(first["items"]) == svc.PAGE_SIZE and first["has_more"]
        assert first["items"][0]["prompt"] == "axb"                       # newest first
        second = (await c.get("/ask2many/api/history", params={"page": 2})).json()
        assert len(second["items"]) == 8 and not second["has_more"]
        pct = (await c.get("/ask2many/api/history", params={"q": "%"})).json()
        assert [i["prompt"] for i in pct["items"]] == ["100% уверен"]
        und = (await c.get("/ask2many/api/history", params={"q": "a_b"})).json()
        assert [i["prompt"] for i in und["items"]] == ["a_b"]


# ─── XSS ────────────────────────────────────────────────────────────────────


async def test_script_in_prompt_and_answer_stays_inert_data(fakes):
    await provision()
    ids = await two_models()
    evil = "<script>alert(1)</script><img src=x onerror=alert(2)>"
    async with make_client() as c:
        rid = (await submit(c, evil, ids)).json()["id"]
        await svc.fan_out(rid, await svc.get_project(), evil)
        fakes.jobs[101] = job("done", text=evil, served=evil)
        fakes.jobs[102] = job("pending")
        r = await c.get(f"/ask2many/api/requests/{rid}")
        h = await c.get("/ask2many/api/history", params={"q": "script"})
        page = await c.get("/ask2many")
    for resp in (r, h):
        assert resp.headers["content-type"].startswith("application/json")  # never rendered as HTML
    assert r.json()["prompt"] == evil and r.json()["results"][0]["answer"] == evil
    assert evil not in page.text and "<script>alert" not in page.text    # shell has no user text

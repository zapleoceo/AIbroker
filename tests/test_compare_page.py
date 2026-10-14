"""Public /ask2many page: shell, model catalog + AA scores, fan-out, cap-0 refusal, history,
XSS, validation and the one-question-at-a-time guard. Broker's job machinery is replaced by
fakes at two seams (services.compare.submit_model / poll_job); one test drives the real
submit path to prove the job lands under the dedicated project."""
from __future__ import annotations

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
    state = SimpleNamespace(jobs={}, refuse=set(), next=100, submitted=[])

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
    ids = [m["id"] for m in svc.chat_models()]
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
    for mid in rows:        # chat-capable, API-billed, and routable through some chat chain
        assert live[mid]["billing_type"] != "subscription"
        assert svc.lane_for(live[mid]["capabilities"], live[mid]["owned_by"]) is not None
    assert all(set(m) == {"id", "name", "provider", "intelligence_index"} for m in rows.values())
    assert rows["anthropic/claude-sonnet-5"]["intelligence_index"] == 38.16
    assert rows["deepseek/deepseek-flash"]["intelligence_index"] is None   # no AA version match
    assert "whisper" not in " ".join(rows)                                  # not a chat model


# ─── intelligence_index in the catalog ──────────────────────────────────────


def test_exact_name_gets_the_score_and_other_versions_get_none():
    assert intelligence_index("claude-sonnet-5") == SCORES["claude-sonnet-5"].value == 38.16
    for other in ("claude-sonnet-5.5", "claude-sonnet-5-5", "claude-sonnet-4", "claude-sonnet",
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
    assert row.request["model"] == "anthropic/claude-sonnet-5"
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
        fakes.jobs[101] = job("error", error="daily budget cap reached — retry after 00:00 UTC")
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

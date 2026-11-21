"""Image lane: providers/gemini_image.py, services/image_service.py,
POST /v1/images/jobs and the poll shape. No real provider is called."""
from __future__ import annotations

import base64
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

import aibroker.providers.gemini_image as gi
import aibroker.services.image_service as svc
from aibroker.main import app
from aibroker.providers import catalog
from aibroker.providers import transport as t
from aibroker.providers.provider_errors import classify_provider_error
from aibroker.providers.registry import model_for, model_spec
from aibroker.routes import images, proxy
from aibroker.routing.chains import CAPABILITY_CHAINS, scope_for
from aibroker.routing.cost_guard import CostGuardError
from aibroker.services.llm_service import _BudgetExhausted
from tests.test_routes_proxy import _make_project

client = TestClient(app)
PROJECT = SimpleNamespace(id=1, name="sim")
MODEL = "gemini/gemini-3.1-flash-image"
JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


def _png(color=(200, 10, 10), fmt="PNG") -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, fmt)
    return buf.getvalue()


REQ = gi.ImageRequest(prompt="Марина в кафе", references=((_png(), "image/png"),),
                      aspect_ratio="9:16", image_size="1K")


def _reply(parts=None, usage=None, **extra) -> dict:
    parts = parts if parts is not None else [
        {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(JPEG).decode()}}]
    return {"candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}],
            "usageMetadata": usage or {"promptTokenCount": 544, "candidatesTokenCount": 1429},
            "modelVersion": "gemini-3.1-flash-image", **extra}


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(gi, "get_settings", lambda: SimpleNamespace(GEMINI_IMAGE_TIMEOUT_S=5.0))


def _post_returns(monkeypatch, status, payload=None, exc=None):
    calls = []

    async def fake(url, api_key, body, timeout):
        calls.append({"url": url, "key": api_key, "body": body})
        if exc is not None:
            raise exc
        req = httpx.Request("POST", url)
        if isinstance(payload, dict):
            return httpx.Response(status, json=payload, request=req)
        return httpx.Response(status, text=payload or "", request=req)
    monkeypatch.setattr(gi, "_post_gemini_image", fake)
    return calls


# ─── registry / catalog ──────────────────────────────────────────────────


def test_image_is_its_own_paid_gemini_lane():
    assert CAPABILITY_CHAINS["image"] == ["gemini"]
    assert scope_for("image") == "llm:image"
    assert model_for("gemini", "image") == MODEL
    for mid in (MODEL, "gemini/gemini-3-pro-image", "gemini/gemini-3.1-flash-lite-image"):
        spec = model_spec(mid)
        assert spec.transport == "gemini_image" and "image" in spec.capabilities
        assert spec.input_usd_per_mtok and spec.output_usd_per_mtok


def test_catalog_lists_image_models_with_image_output():
    rows = {r["id"]: r for r in catalog.listing()}
    row = rows["gemini/gemini-3-pro-image"]
    assert row["output_modalities"] == ["image"] and row["modalities"] == ["text", "image"]
    assert catalog.resolve_pin("gemini-3-pro-image", "image", ["gemini"])[0].model == \
        "gemini/gemini-3-pro-image"
    with pytest.raises(catalog.UnknownModel):
        catalog.resolve_pin("gemini-3.5-flash", "image", ["gemini"])


def test_transport_is_registered():
    assert isinstance(t.transport_for(MODEL), gi.GeminiImageTransport)
    assert isinstance(t.transport_for(MODEL), t.ImageTransport)


# ─── transport ───────────────────────────────────────────────────────────


def test_body_puts_references_first_and_asks_for_an_image():
    body = gi.image_body(REQ)
    parts = body["contents"][0]["parts"]
    assert parts[0]["inline_data"]["mime_type"] == "image/png"
    assert parts[-1] == {"text": "Марина в кафе"}
    assert body["generationConfig"] == {"responseModalities": ["IMAGE"],
                                        "imageConfig": {"imageSize": "1K",
                                                        "aspectRatio": "9:16"}}
    bare = gi.image_body(gi.ImageRequest(prompt="p"))
    assert bare["generationConfig"]["imageConfig"] == {"imageSize": "1K"}


def test_cost_and_reservation():
    assert gi.image_cost(MODEL, 544, 1429) == pytest.approx((544 * 0.5 + 1429 * 60) / 1e6)
    assert gi.image_cost("no/such", 1, 1) == 0.0
    one = gi.estimate_image_cost(MODEL, gi.ImageRequest(prompt="p"))
    two = gi.estimate_image_cost(MODEL, REQ)
    big = gi.estimate_image_cost(MODEL, gi.ImageRequest(prompt="p", image_size="4K"))
    assert one < two and one < big
    assert one >= gi.image_cost(MODEL, 0, 1120)


async def test_success_decodes_the_image_and_prices_it(monkeypatch):
    calls = _post_returns(monkeypatch, 200, _reply())
    image, meta = await gi.generate_via_gemini(model=MODEL, request=REQ, api_key="k")
    assert image == JPEG
    assert calls[0]["url"].endswith("/models/gemini-3.1-flash-image:generateContent")
    assert (meta["mime_type"], meta["tokens_in"], meta["tokens_out"]) == ("image/jpeg", 544, 1429)
    assert meta["cost_usd"] == pytest.approx(gi.image_cost(MODEL, 544, 1429))


async def test_missing_usage_falls_back_to_the_size_table(monkeypatch):
    _post_returns(monkeypatch, 200, _reply(usage={"promptTokenCount": 10}))
    _, meta = await gi.generate_via_gemini(model=MODEL, request=REQ, api_key="k")
    assert meta["tokens_out"] == 1120


@pytest.mark.parametrize(("payload", "needle"), [
    ({"promptFeedback": {"blockReason": "SAFETY"}}, "SAFETY"),
    (_reply(parts=[{"text": "I can't make that"}]), "can't make that"),
    ({"candidates": []}, "no candidates"),
])
async def test_a_reply_without_an_image_is_a_rejection(monkeypatch, payload, needle):
    _post_returns(monkeypatch, 200, payload)
    with pytest.raises(gi.ImageRejected, match=needle):
        await gi.generate_via_gemini(model=MODEL, request=REQ, api_key="k")


async def test_http_errors(monkeypatch):
    _post_returns(monkeypatch, 400, "bad aspect")
    with pytest.raises(gi.ImageRejected):
        await gi.generate_via_gemini(model=MODEL, request=REQ, api_key="k")
    _post_returns(monkeypatch, 429, "quota")
    with pytest.raises(RuntimeError) as e:
        await gi.generate_via_gemini(model=MODEL, request=REQ, api_key="k")
    assert classify_provider_error(e.value, "gemini") == "rate_limit"
    _post_returns(monkeypatch, 0, exc=httpx.ReadTimeout("slow"))
    with pytest.raises(RuntimeError, match="gave up") as e:
        await gi.generate_via_gemini(model=MODEL, request=REQ, api_key="k")
    assert not isinstance(e.value, TimeoutError)
    _post_returns(monkeypatch, 0, exc=httpx.ConnectError("down"))
    with pytest.raises(RuntimeError, match="unreachable"):
        await gi.generate_via_gemini(model=MODEL, request=REQ, api_key="k")


async def test_transport_class_delegates(monkeypatch):
    _post_returns(monkeypatch, 200, _reply())
    image, _ = await gi.GeminiImageTransport().generate_image(model=MODEL, request=REQ,
                                                              api_key="k")
    assert image == JPEG


# ─── service ─────────────────────────────────────────────────────────────


def _paid_key(kid=1):
    return SimpleNamespace(id=kid, label=f"paid{kid}", tier="paid", provider="gemini",
                           token_encrypted="x")


def _meta(cost=0.086):
    return {"model": MODEL, "model_served": None, "mime_type": "image/jpeg",
            "tokens_in": 544, "tokens_out": 1429, "cost_usd": cost, "latency_ms": 8400}


def _wire(monkeypatch, *, picks, gen, reserve=None):
    calls = {"pick": []}

    async def fake_pick(provider, scope, **kw):
        calls["pick"].append({"provider": provider, "scope": scope, **kw})
        i = len(calls["pick"]) - 1
        return picks[i] if i < len(picks) else None

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "generate_image", gen)
    monkeypatch.setattr("aibroker.services.attempt.decrypt", lambda tok: "plain")
    monkeypatch.setattr("aibroker.services.attempt.reserve_cost",
                        reserve or AsyncMock(return_value=None))
    monkeypatch.setattr("aibroker.services.attempt.release_cost", AsyncMock(return_value=None))
    monkeypatch.setattr("aibroker.services.attempt._penalize", AsyncMock(return_value="error"))
    monkeypatch.setattr("aibroker.services.attempt._record_error", AsyncMock(return_value=None))
    monkeypatch.setattr("aibroker.services.attempt.record_usage", AsyncMock(return_value=77))
    return calls


async def _run(**kw):
    return await svc.run_image(**({"project": PROJECT, "request": REQ, "model": None,
                                   "workflow": "sim-photo"} | kw))


async def test_only_paid_keys_with_the_image_scope_are_asked_for(monkeypatch):
    gen = AsyncMock(return_value=(JPEG, _meta()))
    calls = _wire(monkeypatch, picks=[_paid_key()], gen=gen)
    out = await _run()
    pick = calls["pick"][0]
    assert (pick["provider"], pick["scope"], pick["require_tier"]) == ("gemini", "llm:image", "paid")
    assert pick["models"] == [MODEL]
    assert base64.b64decode(out.text) == JPEG
    assert (out.mime_type, out.cost_usd, out.request_id) == ("image/jpeg", 0.086, 77)


async def test_pinned_model_is_resolved_through_the_catalog(monkeypatch):
    gen = AsyncMock(return_value=(JPEG, _meta()))
    calls = _wire(monkeypatch, picks=[_paid_key()], gen=gen)
    out = await _run(model="gemini-3-pro-image")
    assert out.model == "gemini/gemini-3-pro-image"
    assert calls["pick"][0]["models"] == ["gemini/gemini-3-pro-image"]


async def test_rotation_failure_rejection_and_no_key(monkeypatch):
    gen = AsyncMock(side_effect=[RuntimeError("gemini-image HTTP 500: x"), (JPEG, _meta())])
    _wire(monkeypatch, picks=[_paid_key(1), _paid_key(2)], gen=gen)
    assert (await _run()).key_label == "paid2"

    _wire(monkeypatch, picks=[_paid_key(1)], gen=AsyncMock(side_effect=RuntimeError("HTTP 500")))
    with pytest.raises(svc.ImageFailed):
        await _run()

    refused = AsyncMock(side_effect=gi.ImageRejected("blocked"))
    _wire(monkeypatch, picks=[_paid_key(1), _paid_key(2)], gen=refused)
    with pytest.raises(gi.ImageRejected):
        await _run()
    assert refused.await_count == 1

    _wire(monkeypatch, picks=[], gen=AsyncMock())
    assert await _run() is None


async def test_spent_cap_is_reported_without_calling_the_provider(monkeypatch):
    provider = AsyncMock()

    async def blocked(**kw):
        raise CostGuardError("project", 1.0, 1.0, 0.1)
    _wire(monkeypatch, picks=[_paid_key()], gen=provider, reserve=blocked)
    assert isinstance(await _run(), _BudgetExhausted)
    provider.assert_not_awaited()


def test_job_payload_round_trips():
    assert svc.image_request_from_job(svc.image_job_payload(REQ)) == REQ
    bare = gi.ImageRequest(prompt="p")
    assert svc.image_request_from_job(svc.image_job_payload(bare)) == bare
    with pytest.raises(KeyError):
        svc.image_request_from_job({"references": []})


def test_an_image_job_is_retried_far_less_than_a_chat_job():
    from aibroker.services import job_queue
    assert job_queue._max_retries_for("image") == svc.IMAGE_MAX_RETRIES < job_queue._MAX_RETRIES
    assert job_queue._max_retries_for("chat:fast") is None


async def test_job_runner_rebuilds_the_request(monkeypatch):
    from aibroker.services import job_queue
    run = AsyncMock(return_value="outcome")
    monkeypatch.setattr(job_queue, "run_image", run)
    req = {"image": svc.image_job_payload(REQ), "model": "gemini-3-pro-image", "workflow": "w"}
    assert await job_queue._run_image_job(PROJECT, req) == "outcome"
    kw = run.await_args.kwargs
    assert (kw["request"], kw["model"], kw["workflow"]) == (REQ, "gemini-3-pro-image", "w")


# ─── route ───────────────────────────────────────────────────────────────


def _post(plain, body, **headers):
    return client.post("/v1/images/jobs", headers={"X-Project-Key": plain, **headers}, json=body)


def _ref(raw=None, **kw):
    return {"image_base64": base64.b64encode(raw or _png()).decode(), **kw}


async def test_route_requires_the_image_scope():
    plain, _ = await _make_project(["llm:vision", "llm:video"])
    with patch("aibroker.routes.images.submit_job_ex", AsyncMock()) as submit:
        r = _post(plain, {"prompt": "hi"})
    assert r.status_code == 403 and "llm:image" in r.json()["detail"]
    submit.assert_not_awaited()


@pytest.mark.parametrize("body", [
    {"prompt": ""}, {"prompt": "  "}, {"prompt": "x" * 8001},
    {"prompt": "hi", "aspect_ratio": "7:3"}, {"prompt": "hi", "image_size": "8K"},
    {"prompt": "hi", "references": [{"image_base64": "aGk="}] * 9},
    {"prompt": "hi", "references": [{"image_base64": "aGk=", "mime": "image/gif"}]},
    {"prompt": "hi", "labels": {"bad key!": "v"}},
])
async def test_route_rejects_invalid_requests_with_422(body):
    plain, _ = await _make_project(["llm:image"])
    with patch("aibroker.routes.images.submit_job_ex", AsyncMock()) as submit:
        r = _post(plain, body)
    assert r.status_code == 422, r.text
    submit.assert_not_awaited()


@pytest.mark.parametrize(("body", "needle"), [
    ({"prompt": "hi", "model": "gemini-3.5-flash"}, "does not serve image"),
    ({"prompt": "hi", "references": [{"image_base64": "not base64 !!"}]}, "not valid base64"),
    ({"prompt": "hi", "references": [_ref(b"plain text")]}, "not a decodable image"),
    ({"prompt": "hi", "references": [_ref(_png(fmt="GIF"))]}, "accepted: JPEG, PNG, WebP"),
    ({"prompt": "hi", "references": [_ref(b"x" * (images.IMAGE_MAX_REFERENCE_BYTES + 10))]},
     "exceeds 7 MB"),
    ({"prompt": "hi", "references": [_ref(_png() + b"x" * (6 * 1024 * 1024))] * 4}, "in total"),
])
async def test_route_rejects_a_wrong_model_or_reference_with_400(body, needle):
    plain, _ = await _make_project(["llm:image"])
    with patch("aibroker.routes.images.submit_job_ex", AsyncMock()) as submit:
        r = _post(plain, body)
    assert r.status_code == 400, r.text
    assert needle in r.json()["detail"]
    submit.assert_not_awaited()


async def test_route_enqueues_an_image_job_and_returns_its_poll_url():
    plain, _ = await _make_project(["llm:image"])
    a, b = _png(), _png((0, 0, 200), "WEBP")
    outcome = SimpleNamespace(job_id=612, replayed=False)
    with patch("aibroker.routes.images.submit_job_ex", AsyncMock(return_value=outcome)) as submit:
        r = _post(plain, {"prompt": "Марина у окна", "aspect_ratio": "3:4",
                          "references": [_ref(a), {"image_base64": "data:image/webp;base64,"
                                                   + base64.b64encode(b).decode()}],
                          "workflow": "sim", "labels": {"chat": "42"},
                          "priority": "interactive"},
                  **{"Idempotency-Key": "photo-42-1"})
    assert r.status_code == 202, r.text
    assert r.json() == {"job_id": 612, "status": "pending", "poll_url": "/v1/jobs/612",
                        "poll_after_s": 5, "trace_id": None, "labels": {"chat": "42"}}
    kw = submit.await_args.kwargs
    assert (kw["capability"], kw["model"], kw["workflow"]) == ("image", None, "sim")
    assert kw["idempotency_key"] == "photo-42-1"
    payload = kw["extra"]["image"]
    assert [r["mime"] for r in payload["references"]] == ["image/png", "image/webp"]
    assert (payload["aspect_ratio"], payload["image_size"]) == ("3:4", "1K")


async def test_route_replays_an_idempotent_submit():
    plain, _ = await _make_project(["llm:image"])
    outcome = SimpleNamespace(job_id=613, replayed=True)
    replay = AsyncMock(return_value=proxy.JSONResponse({"job_id": 613}))
    with patch("aibroker.routes.images.submit_job_ex", AsyncMock(return_value=outcome)), \
            patch("aibroker.routes.images._replay", replay):
        r = _post(plain, {"prompt": "hi"}, **{"Idempotency-Key": "k1"})
    assert r.status_code == 200 and r.json() == {"job_id": 613}


async def test_route_rejects_a_bad_idempotency_key():
    plain, _ = await _make_project(["llm:image"])
    r = _post(plain, {"prompt": "hi"}, **{"Idempotency-Key": "bad key!"})
    assert r.status_code == 422


def _row(capability: str, text: str):
    from datetime import datetime
    now = datetime(2026, 11, 21, 12, 0, 0)
    return SimpleNamespace(
        id=9, status="done", capability=capability, request={}, result_text=text,
        result_meta={"provider": "gemini", "model": MODEL, "cost_usd": 0.086,
                     "mime_type": "image/jpeg"},
        error_message=None, trace_id=None, labels=None, priority=None,
        created_at=now, started_at=now, completed_at=now, run_after=None)


def test_poll_returns_a_finished_image_as_image_base64_not_text():
    img = base64.b64encode(JPEG).decode()
    done = proxy._job_response(_row("image", img))
    assert done.status == "done" and done.text is None and done.video_base64 is None
    assert (done.image_base64, done.mime_type) == (img, "image/jpeg")
    assert done.cost_usd == pytest.approx(0.086)

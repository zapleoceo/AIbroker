"""GET /v1/models (the pin-able catalog), 400-on-unknown-pin at the submit routes,
and the X-Request-Id trace header."""
from __future__ import annotations

from fastapi.testclient import TestClient

from aibroker.main import app
from tests.test_routes_proxy import _make_project

client = TestClient(app)
_MSGS = [{"role": "user", "content": "hi"}]


async def test_models_requires_a_project_key():
    assert client.get("/v1/models").status_code in (401, 403)
    assert client.get("/v1/models", headers={"X-Project-Key": "nope"}).status_code in (401, 403)


async def test_models_lists_every_pinnable_model_with_provider_and_price():
    plain, _ = await _make_project(["llm:chat"])     # any scope: it is a catalog
    r = client.get("/v1/models", headers={"X-Project-Key": plain})
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    by_id = {m["id"]: m for m in body["data"]}
    flash = by_id["gemini/gemini-2.5-flash"]
    assert flash["object"] == "model" and flash["owned_by"] == "gemini"
    assert "chat:fast" in flash["capabilities"] and "vision" in flash["capabilities"]
    assert flash["price"]["kind"] in ("litellm", "unknown")
    assert by_id["voyage/voyage-4"]["price"]["input_usd_per_mtok"] == 0.06
    assert by_id["local/qwen3vl"]["price"]["kind"] == "local"
    assert by_id["local/qwen3vl"]["modalities"] == ["text", "image"]
    assert by_id["local/qwen3vl"]["supports_tools"] is False
    assert flash["modalities"] == ["text", "image"]
    assert by_id["cerebras/gpt-oss-120b"]["supports_tools"] is False
    assert by_id["groq/whisper-large-v3-turbo"]["modalities"] == ["audio"]
    assert by_id["groq/whisper-large-v3-turbo"]["price"]["usd_per_minute"] > 0
    oss = by_id["cerebras/gpt-oss-120b"]
    assert {"groq/openai/gpt-oss-120b", "cloudflare/@cf/openai/gpt-oss-120b"} <= set(oss["also_served_by"])
    assert "groq/whisper-large-v3" not in by_id        # price-only entry, nothing to pin


def test_missing_api_price_is_unknown_not_zero(monkeypatch):
    import litellm

    from aibroker.providers.catalog import price_info
    from aibroker.providers.registry import model_spec

    def missing_price(_model):
        raise LookupError("price unavailable")

    monkeypatch.setattr(litellm, "get_model_info", missing_price)
    model = model_spec("gemini/gemini-2.5-flash")
    assert model is not None
    assert price_info(model) == {"kind": "unknown"}


def test_tool_metadata_requires_broker_adapter_and_verified_model_map(monkeypatch):
    import litellm

    from aibroker.providers.catalog import supports_tools
    from aibroker.providers.registry import model_spec

    gemini = model_spec("gemini/gemini-2.5-flash")
    assert gemini is not None
    monkeypatch.setattr(litellm, "get_model_info", lambda _mid: {"supports_function_calling": True})
    assert supports_tools(gemini) is True
    monkeypatch.setattr(litellm, "get_model_info", lambda _mid: {})
    assert supports_tools(gemini) is None
    monkeypatch.setattr(litellm, "get_model_info", lambda _mid: {"supports_function_calling": False})
    assert supports_tools(gemini) is False


async def test_unknown_pinned_model_is_a_400_with_suggestions_before_anything_is_queued():
    plain, _ = await _make_project(["llm:chat"])
    r = client.post("/v1/jobs?capability=chat:fast", headers={"X-Project-Key": plain},
                    json={"messages": _MSGS, "model": "gemini/gemini-2.5-flsh"})
    assert r.status_code == 400
    assert "gemini/gemini-2.5-flash" in r.json()["detail"]
    assert "/v1/models" in r.json()["detail"]


async def test_model_that_does_not_serve_the_lane_is_a_400():
    plain, _ = await _make_project(["llm:chat"])
    r = client.post("/v1/jobs?capability=chat:fast", headers={"X-Project-Key": plain},
                    json={"messages": _MSGS, "model": "voyage/voyage-4"})
    assert r.status_code == 400 and "does not serve" in r.json()["detail"]


async def test_unknown_embed_pin_is_a_400():
    plain, _ = await _make_project(["llm:embed"])
    r = client.post("/v1/embed?provider=voyage", headers={"X-Project-Key": plain},
                    json={"input": ["x"], "model": "voyage/voyage-99"})
    assert r.status_code == 400


async def test_every_response_carries_an_x_request_id():
    plain, _ = await _make_project(["llm:chat"])
    r1 = client.get("/v1/models", headers={"X-Project-Key": plain})
    r2 = client.get("/v1/models", headers={"X-Project-Key": plain})
    assert len(r1.headers["x-request-id"]) == 32
    assert r1.headers["x-request-id"] != r2.headers["x-request-id"]
    assert client.get("/healthz").headers.get("x-request-id")        # not only /v1


async def test_a_sane_client_supplied_request_id_is_honoured_and_junk_is_replaced():
    plain, _ = await _make_project(["llm:chat"])
    ok = client.get("/v1/models", headers={"X-Project-Key": plain, "X-Request-Id": "client-req-0001"})
    assert ok.headers["x-request-id"] == "client-req-0001"
    junk = client.get("/v1/models", headers={"X-Project-Key": plain, "X-Request-Id": "a b;drop"})
    assert junk.headers["x-request-id"] != "a b;drop" and len(junk.headers["x-request-id"]) == 32


def test_job_request_id_is_deterministic_per_job():
    from aibroker.telemetry.request_context import job_request_id
    assert job_request_id(42) == "job-42"

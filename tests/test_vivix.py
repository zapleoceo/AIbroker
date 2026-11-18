"""Vivix wire contract tests; all HTTP is intercepted in memory."""
import json

import httpx
import pytest

from aibroker.providers.catalog import UnknownModel, resolve_pin
from aibroker.providers.gemini_video import VideoRejected, VideoRequest
from aibroker.providers.provider_errors import classify_provider_error
from aibroker.providers.registry import REGISTRY
from aibroker.providers.transport import VideoTransport, _load
from aibroker.providers.vivix import VivixVideoTransport
from aibroker.providers.vivix_contract import VivixError, envelope, model_ids, video_result


def response(data, *, code=0, status=200):
    return httpx.Response(status, json={"code": code, "message": "SECRET", "data": data})


def test_registry_disabled_pricing_and_pin():
    spec = REGISTRY["vivix"]
    assert spec.paid and not spec.models and not spec.defaults
    assert spec.probe is None and spec.billing_probe is None
    assert isinstance(_load()["vivix_video"], VideoTransport)
    for model in ("vivix-a1", "vivix/vivix-w1", "vivix-w1-stream"):
        with pytest.raises(UnknownModel):
            resolve_pin(model, "chat:fast", ["vivix"])


def test_exact_model_permissions():
    assert model_ids({"models": [{"name": "vivix-w1", "tag": "latest"}]}) == {"vivix-w1:latest"}
    assert model_ids({"models": [{"name": "vivix-w1"}, {"name": "vivix-w1", "tag": "v1"}]}) == {
        "vivix-w1", "vivix-w1:v1"}
    assert model_ids({"models": []}) == set()


@pytest.mark.parametrize("payload", [{}, {"code": False}, {"code": 0, "data": []},
                                    {"code": 0, "data": {"error_code": "0"}}])
def test_malformed_envelope(payload):
    with pytest.raises(ValueError):
        envelope(payload)


@pytest.mark.parametrize("code,kind", [(10001, "auth"), (10003, "auth"), (10008, "rate_limit"),
                                     (30004, "error")])
def test_outer_and_business_errors(code, kind):
    for payload in ({"code": code, "message": "SECRET"},
                    {"code": 0, "data": {"error_code": code, "message": "SECRET"}}):
        with pytest.raises(VivixError) as exc:
            envelope(payload)
        assert classify_provider_error(exc.value, "vivix") == kind
        assert "SECRET" not in str(exc.value)


@pytest.mark.parametrize("status,kind", [(401, "auth"), (403, "auth"), (429, "rate_limit"),
                                       (400, "error"), (404, "error"), (500, "error")])
async def test_http_errors(status, kind):
    transport = VivixVideoTransport(http_transport=httpx.MockTransport(
        lambda req: response({}, status=status)))
    with pytest.raises(VivixError) as exc:
        await transport.list_models(api_key="FAKE")
    assert exc.value.status_code == status
    assert classify_provider_error(exc.value, "vivix") == kind
    assert "SECRET" not in str(exc.value)


async def test_default_disabled_without_network():
    def forbidden(req):
        pytest.fail("disabled model attempted HTTP")
    transport = VivixVideoTransport(http_transport=httpx.MockTransport(forbidden))
    with pytest.raises(ValueError, match="disabled"):
        await transport.submit_video(model="vivix-w1", inputs={}, api_key="FAKE")
    with pytest.raises(ValueError, match="Unsupported"):
        await transport.submit_video(model="vivix-w1-stream", inputs={}, api_key="FAKE")
    request = VideoRequest(prompt="mock", duration_s=5, aspect_ratio="16:9", resolution="720p")
    with pytest.raises(VideoRejected, match="disabled"):
        await transport.generate_video(model="vivix/vivix-w1", request=request, api_key="FAKE")


@pytest.mark.parametrize("model,world", [("vivix-a1:latest", False), ("vivix-w1", True)])
async def test_submit_then_poll(model, world):
    requests = []
    inputs = {"jobs": [{"prompt": "mock", "duration": 5}]} if world else {
        "prompt": "mock", "first_frame_image": "https://example.test/image.png"}
    path = "/v1/videos/jobs" if world else "/v1/videos/generations"
    id_field, url_field = ("job_id", "video_url") if world else ("video_id", "playback_url")

    def handler(req):
        requests.append(req)
        assert req.headers["Authorization"] == "Bearer FAKE"
        if req.url.path == "/v1/models":
            entry = {"name": model.split(":")[0]}
            if ":" in model:
                entry["tag"] = model.split(":")[1]
            return response({"models": [entry]})
        if req.method == "POST":
            assert req.url.path == path
            assert json.loads(req.content) == {**inputs, "model": model}
            item = {id_field: "task1", "status": "queued"}
            if not world:
                item["error_code"] = 0
            return response({"results": [item]} if world else item)
        assert req.url.path == path + "/task1"
        item = {id_field: "task1", "status": "succeeded", url_field: "https://example.test/v.mp4"}
        return response(item if world else {"items": [item]})

    transport = VivixVideoTransport(http_transport=httpx.MockTransport(handler),
                                    enabled_models=frozenset({model}))
    result = await transport.submit_video(model=model, inputs=inputs, api_key="FAKE")
    assert result.id == "task1" and not result.terminal
    result = await transport.poll_video(model=model, video_id=result.id, api_key="FAKE")
    assert result.terminal and result.url == "https://example.test/v.mp4"
    assert [r.method for r in requests] == ["GET", "POST", "GET"]


async def test_workspace_permission_does_not_drop_latest():
    def handler(req):
        assert req.method == "GET"
        return response({"models": [{"name": "vivix-w1", "tag": "latest"}]})
    transport = VivixVideoTransport(http_transport=httpx.MockTransport(handler),
                                    enabled_models=frozenset({"vivix-w1"}))
    with pytest.raises(ValueError, match="not enabled"):
        await transport.submit_video(model="vivix-w1", inputs={"jobs": [{}]}, api_key="FAKE")


@pytest.mark.parametrize("world", [True, False])
@pytest.mark.parametrize("status", ["queued", "processing", "succeeded", "failed"])
def test_lifecycle(world, status):
    item = {"job_id" if world else "video_id": "task1", "status": status,
            "video_url" if world else "playback_url": "https://example.test/v.mp4"}
    result = video_result(item if world else {"items": [item]}, video_id="task1", world=world)
    assert result.terminal == (status in {"succeeded", "failed"})
    assert bool(result.url) == (status == "succeeded")


def test_avatar_pending_ready_timeout_and_missing_url():
    assert not video_result({"items": []}, video_id="x", world=False).terminal
    for status in ("ready", "timeout"):
        result = video_result({"items": [{"video_id": "x", "status": status}]}, video_id="x", world=False)
        assert result.terminal == (status == "timeout")
    for world in (False, True):
        item = {"job_id" if world else "video_id": "x", "status": "succeeded"}
        with pytest.raises(ValueError, match="playable"):
            video_result(item if world else {"items": [item]}, video_id="x", world=world)


async def test_transport_failure_is_not_retried():
    calls = []
    def handler(req):
        calls.append(req)
        if req.method == "GET":
            return response({"models": [{"name": "vivix-w1"}]})
        raise httpx.ReadTimeout("SECRET")
    transport = VivixVideoTransport(http_transport=httpx.MockTransport(handler),
                                    enabled_models=frozenset({"vivix-w1"}))
    with pytest.raises(TimeoutError, match="outcome may be unknown") as exc:
        await transport.submit_video(model="vivix-w1", inputs={"jobs": [{}]}, api_key="FAKE")
    assert "SECRET" not in str(exc.value)
    assert [r.method for r in calls] == ["GET", "POST"]


@pytest.mark.parametrize("data", [{}, {"models": None}, {"models": [None]},
                                 {"models": [{"name": "vivix-w1", "tag": ""}]}])
def test_malformed_model_list(data):
    with pytest.raises(ValueError):
        model_ids(data)


@pytest.mark.parametrize("world", [True, False])
def test_mismatched_task_and_unknown_status(world):
    id_field = "job_id" if world else "video_id"
    for item in ({id_field: "other", "status": "succeeded"},
                 {id_field: "task1", "status": "new-upstream-state"}):
        with pytest.raises(ValueError):
            video_result(item if world else {"items": [item]}, video_id="task1", world=world)


@pytest.mark.parametrize("bad", [httpx.Response(200, text="not JSON"),
                                httpx.Response(302, headers={"Location": "https://other.test/"})])
async def test_json_and_redirect_errors(bad):
    calls = []
    def handler(req):
        calls.append(req)
        return bad
    transport = VivixVideoTransport(http_transport=httpx.MockTransport(handler))
    with pytest.raises((ValueError, VivixError)):
        await transport.list_models(api_key="FAKE")
    assert len(calls) == 1


@pytest.mark.parametrize("jobs", [[], [{}, {}], None])
async def test_world_rejects_non_single_job_before_http(jobs):
    def forbidden(req):
        pytest.fail("invalid batch attempted HTTP")
    transport = VivixVideoTransport(http_transport=httpx.MockTransport(forbidden),
                                    enabled_models=frozenset({"vivix-w1"}))
    with pytest.raises(ValueError, match="exactly one job"):
        await transport.submit_video(model="vivix-w1", inputs={"jobs": jobs}, api_key="FAKE")

"""Inline video / PDF on the vision lane (2026-11-21, SIM v2).

Block shapes, submit-time validation, the Gemini-only walk, the litellm block
rewrite, catalog modalities and the /v1/jobs gate. No real provider is called.
"""
from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import aibroker.services.llm_service as svc
from aibroker.main import app
from aibroker.providers import catalog
from aibroker.providers.adapters import adapter_for
from aibroker.providers.context_limits import content_chars, estimate_prompt_tokens
from aibroker.providers.media_inputs import (
    block_kind,
    gemini_media_messages,
    media_kinds,
)
from aibroker.providers.registry import model_spec, reads_media
from aibroker.services.vision_payload import (
    MAX_MEDIA_BYTES,
    inline_image_problem,
    inline_media_problem,
)
from tests.test_routes_proxy import _make_project

client = TestClient(app)
MP4 = b"\x00\x00\x00 ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * 64
WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 64
PDF = b"%PDF-1.4\n%fake\n" + b"0" * 64


def _url(mime: str, raw: bytes) -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


def _video(raw: bytes = MP4, mime: str = "video/mp4") -> dict:
    return {"type": "video_url", "video_url": {"url": _url(mime, raw)}}


def _file(raw: bytes = PDF, mime: str = "application/pdf") -> dict:
    return {"type": "file", "file": {"file_data": _url(mime, raw), "filename": "scan.pdf"}}


def _msgs(*blocks: dict) -> list[dict]:
    return [{"role": "user", "content": [{"type": "text", "text": "что здесь?"}, *blocks]}]


# ─── block shapes ────────────────────────────────────────────────────────


def test_block_kinds():
    assert block_kind(_video()) == "video"
    assert block_kind(_file(MP4, "video/mp4")) == "video"
    assert block_kind(_file()) == "pdf"
    assert block_kind(_video(PDF, "application/pdf")) == "unsupported"   # PDF only as a file
    assert block_kind(_file(b"x", "application/msword")) == "unsupported"
    assert block_kind({"type": "video_url", "video_url": "data:video/webm;base64,AA"}) == "video"
    assert block_kind({"type": "image_url", "image_url": {"url": "x"}}) is None
    assert block_kind("text") is None
    assert media_kinds(_msgs(_video(), _file())) == {"video", "pdf"}
    assert media_kinds([{"role": "user", "content": "plain"}]) == frozenset()


def test_valid_video_and_pdf_pass():
    assert inline_media_problem(_msgs(_video(), _file())) is None
    assert inline_media_problem(_msgs(_video(WEBM, "video/webm"))) is None
    assert inline_media_problem(_msgs(_video(MP4, "video/quicktime"))) is None
    assert inline_media_problem([{"role": "user", "content": "no blocks"}]) is None


@pytest.mark.parametrize(("blocks", "needle"), [
    ([{"type": "video_url", "video_url": {"url": "https://x/clip.mp4"}}], "data URL"),
    ([_file(b"x", "application/msword")], "not supported"),
    ([{"type": "file", "file": {"file_data": "data:application/pdf;charset=x,%PDF"}}], "base64"),
    ([{"type": "file", "file": {"file_data": "data:application/pdf;base64,!!"}}], "invalid base64"),
    ([{"type": "file", "file": {"file_data": "data:application/pdf;base64,"}}], "empty"),
    ([_video(PDF)], "not video/mp4"),
    ([_file(MP4)], "not application/pdf"),
    ([_video(b"\x00" * (MAX_MEDIA_BYTES + 10))], "exceeds 20 MB"),
    ([_video(MP4 + b"\x00" * (MAX_MEDIA_BYTES // 2)), _file(PDF + b"0" * (MAX_MEDIA_BYTES // 2))],
     "in total"),
])
def test_bad_media_is_refused_with_a_reason(blocks, needle):
    problem = inline_media_problem(_msgs(*blocks))
    assert problem is not None and needle in problem


def test_a_video_sent_as_image_points_at_the_video_block():
    problem = inline_image_problem([{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": _url("image/jpeg", MP4)}}]}])
    assert "video_url" in problem


# ─── registry, catalog, estimates ────────────────────────────────────────


def test_only_gemini_vision_models_read_inline_media():
    for model in ("gemini/gemini-2.5-flash", "gemini/gemini-3.5-flash-lite",
                  "gemini/gemini-3.5-flash", "gemini/gemini-3.1-flash-lite"):
        assert reads_media(model, frozenset({"video", "pdf"})), model
    assert not reads_media("local/qwen3vl", frozenset({"video"}))
    assert not reads_media("no/such-model", frozenset({"pdf"}))
    assert not reads_media("gemini/gemini-3.5-flash", frozenset({"unsupported"}))


def test_catalog_lists_video_and_pdf_modalities_for_gemini_vision_models():
    rows = {r["id"]: r for r in catalog.listing()}
    assert rows["gemini/gemini-3.5-flash-lite"]["modalities"] == ["text", "image", "video", "pdf"]
    others = [r for r in rows.values() if r["owned_by"] != "gemini"
              and "vision" in r["capabilities"]]
    assert others and all("video" not in r["modalities"] for r in others)


def test_token_estimate_grows_with_the_attachment():
    small = content_chars([_video()])
    big = content_chars([_video(MP4 + b"\x00" * 8_000_000)])
    assert small == 4000 and big > small * 5
    assert estimate_prompt_tokens(_msgs(_file())) >= 1000


# ─── provider call ───────────────────────────────────────────────────────


def test_gemini_adapter_rewrites_video_url_as_a_file_block_without_mutating():
    msgs = _msgs(_video(), _file())
    kwargs = {"messages": msgs}
    adapter_for("gemini").prepare("gemini/gemini-3.5-flash", kwargs, "vision")
    blocks = kwargs["messages"][0]["content"]
    assert blocks[1] == {"type": "file", "file": {"file_data": _url("video/mp4", MP4),
                                                  "format": "video/mp4"}}
    assert blocks[2] == _file()
    assert msgs[0]["content"][1]["type"] == "video_url"        # stored request untouched
    plain = [{"role": "user", "content": "hi"}]
    assert gemini_media_messages(plain) is plain


def test_litellm_gemini_transformation_sends_inline_video_and_pdf():
    """Drive litellm's real Gemini request transformation offline."""
    from litellm.llms.vertex_ai.gemini.transformation import (
        _gemini_convert_messages_with_history,
    )
    msgs = gemini_media_messages(_msgs(_video(), _file()))
    contents = _gemini_convert_messages_with_history(messages=msgs,
                                                     model="gemini-3.5-flash")
    parts = contents[0]["parts"]
    mimes = [p["inline_data"]["mime_type"] for p in parts if "inline_data" in p]
    assert mimes == ["video/mp4", "application/pdf"]


# ─── the walk ────────────────────────────────────────────────────────────


def _key(provider: str):
    return SimpleNamespace(id=7, label=f"{provider}-k", tier="free", provider=provider,
                           token_encrypted="x", account_id=None)


@pytest.fixture
def walk(monkeypatch):
    picks: list[tuple[str, list[str] | None]] = []

    async def fake_pick(provider, scope, **kw):
        picks.append((provider, kw.get("models")))
        return _key(provider)

    async def fake_attempt(attempt):
        return SimpleNamespace(flow=svc.Flow.SUCCESS, payload="ok", usage_id=1, rejection=None,
                               meta={"model": attempt.model, "tokens_in": 1, "tokens_out": 1,
                                     "cost_usd": 0.0, "latency_ms": 1})

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "run_attempt", fake_attempt)
    monkeypatch.setattr(svc.affinity, "lookup_route", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "learned_ceilings", AsyncMock(return_value={}))
    return picks


async def _vision(messages, model=None):
    return await svc.run_chat(project=SimpleNamespace(id=1, name="sim"), capability="vision",
                              messages=messages, model=model, max_tokens=100,
                              temperature=0.2, response_format=None, workflow=None)


async def test_a_video_request_walks_only_models_that_read_it(walk):
    out = await _vision(_msgs(_video()))
    assert out.provider == "gemini"
    provider, pool = walk[0]
    assert provider == "gemini" and pool
    assert all(model_spec(m).media_inputs >= {"video"} for m in pool)


async def test_an_image_only_request_still_starts_at_local(walk):
    out = await _vision([{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]}])
    assert out.provider == walk[0][0] == "local"


async def test_a_pinned_model_without_media_support_is_an_unknown_model(walk):
    with pytest.raises(catalog.UnknownModel, match="does not read inline pdf"):
        await _vision(_msgs(_file()), model="local/qwen3vl")
    out = await _vision(_msgs(_file()), model="gemini-3.5-flash")
    assert out.model == "gemini/gemini-3.5-flash"


# ─── /v1/jobs gate ───────────────────────────────────────────────────────


def _post(plain, body, capability="vision"):
    return client.post(f"/v1/jobs?capability={capability}",
                       headers={"X-Project-Key": plain}, json=body)


async def test_jobs_submit_enqueues_a_vision_job_with_video_and_pdf():
    plain, _ = await _make_project(["llm:vision"])
    outcome = SimpleNamespace(job_id=41, replayed=False)
    with patch("aibroker.routes.proxy.submit_job_ex", AsyncMock(return_value=outcome)) as sub:
        r = _post(plain, {"messages": _msgs(_video(), _file()), "model": "gemini-3.5-flash"})
    assert r.status_code == 202, r.text
    kw = sub.await_args.kwargs
    assert kw["capability"] == "vision"
    assert kw["messages"][0]["content"][1]["type"] == "video_url"


@pytest.mark.parametrize(("scopes", "capability", "body", "status", "needle"), [
    (["llm:chat"], "chat:fast", {"messages": _msgs(_video())}, 403, "llm:vision"),
    (["llm:chat", "llm:vision"], "chat:fast", {"messages": _msgs(_file())}, 400,
     "capability=vision"),
    (["llm:vision"], "vision", {"messages": _msgs(_video(PDF))}, 400, "not video/mp4"),
    (["llm:vision"], "vision", {"messages": _msgs(_file()), "model": "qwen3vl"}, 400,
     "does not read inline pdf"),
])
async def test_jobs_submit_refuses_media_it_cannot_route(scopes, capability, body, status, needle):
    plain, _ = await _make_project(scopes)
    with patch("aibroker.routes.proxy.submit_job_ex", AsyncMock()) as sub:
        r = _post(plain, body, capability)
    assert r.status_code == status, r.text
    assert needle in r.json()["detail"]
    sub.assert_not_awaited()

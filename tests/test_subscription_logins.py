"""Subscription login health, one-shot Telegram alerts and the dashboard re-login
(docs/subscription-logins.md)."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aibroker.agent import login_alerts, login_flow, login_probe, login_worker
from aibroker.agent import login_sessions as ls
from aibroker.agent import login_status as st
from aibroker.agent.login_identity import (
    LoginReport,
    codex_callback,
    display_name,
    parse_claude_status,
    parse_codex_account,
    parse_login_url,
)
from aibroker.agent.login_seal import new_key, public_pem, seal, unseal
from aibroker.db import get_session
from aibroker.db.models import (
    AgentLoginSessionRow,
    AgentLoginStatusRow,
    AgentRunEventRow,
    AgentRunRow,
)
from aibroker.main import app
from aibroker.telemetry import notifier
from tests import dash_seed as ds

# asyncio is patched module-wide, so every fake must wrap the ORIGINAL launcher.
_REAL_EXEC = asyncio.create_subprocess_exec
client = TestClient(app)
EMAIL = "zaporozec_d@itstep.org"


# ─── identity parsing ────────────────────────────────────────────────────────


def test_display_name_uses_mailbox_letters_and_app_prefix() -> None:
    assert display_name("claude-sub", EMAIL, "owner") == "CLD_ZAPO"
    assert display_name("codex-sub", EMAIL, "owner") == "GPT_ZAPO"
    assert display_name("claude-sub", "de.mo-niwwwe@gmail.com", "claude-2") == "CLD_DEMO"
    assert display_name("claude-sub", None, "claude-2") == "CLD_claude-2"


def test_parse_claude_status_logged_in_and_out() -> None:
    ok = parse_claude_status(json.dumps({
        "loggedIn": True, "authMethod": "claude.ai", "email": EMAIL,
        "subscriptionType": "team", "orgName": "ItStep Claude Team"}))
    assert ok == LoginReport(True, EMAIL, "team · ItStep Claude Team", None)
    out = parse_claude_status('noise {"loggedIn": false, "authMethod": "none"}')
    assert not out.logged_in and "authMethod=none" in (out.detail or "")
    assert parse_claude_status("not json").detail == "unreadable `claude auth status` output"
    assert parse_claude_status("[1]").logged_in is False


def test_parse_claude_status_api_key_login_is_not_a_subscription() -> None:
    report = parse_claude_status('{"loggedIn": true, "authMethod": "api_key"}')
    assert report.logged_in is False


def test_parse_codex_account() -> None:
    ok = parse_codex_account({"account": {"type": "chatgpt", "email": EMAIL, "planType": "pro"}})
    assert ok == LoginReport(True, EMAIL, "pro", None)
    assert parse_codex_account({"account": None}).logged_in is False
    assert parse_codex_account({"account": {"type": "apiKey"}}).logged_in is False
    assert parse_codex_account(None).logged_in is False


def test_login_url_parsing() -> None:
    claude = "If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&x=1\n"
    assert parse_login_url(claude) == "https://claude.com/cai/oauth/authorize?code=true&x=1"
    codex = ("Starting local login server on http://localhost:1455.\n\n"
             "\x1b[94mhttps://auth.openai.com/oauth/authorize?response_type=code&x=1\x1b[0m\n")
    assert parse_login_url(codex) == "https://auth.openai.com/oauth/authorize?response_type=code&x=1"
    assert parse_login_url("visit https://evil.example/claude.com") is None


def test_codex_callback_accepts_only_the_local_listener() -> None:
    ok = "http://127.0.0.1:1455/auth/callback?code=ac_1&state=s"
    assert codex_callback(ok) == ok
    assert codex_callback(" http://localhost:1455/auth/callback?code=ac_1&state=s ") == ok
    for bad in ("http://evil.com:1455/auth/callback?code=1",
                "http://127.0.0.1@evil.com:1455/auth/callback?code=1",
                "https://127.0.0.1:1455/auth/callback?code=1",
                "http://127.0.0.1:1456/auth/callback?code=1",
                "http://127.0.0.1:1455/other?code=1",
                "http://127.0.0.1:1455/auth/callback?state=1",
                "http://[::1:1455/auth/callback?code=1", "code=1"):
        assert codex_callback(bad) is None, bad


# ─── sealing ─────────────────────────────────────────────────────────────────


def test_seal_roundtrip_and_limits() -> None:
    key = new_key()
    sealed = seal(public_pem(key), "  code#abc-123  ")
    assert "code" not in sealed and unseal(key, sealed) == "code#abc-123"
    long_line = "http://127.0.0.1:1455/auth/callback?code=" + "c" * 900
    assert unseal(key, seal(public_pem(key), long_line)) == long_line
    for bad in ("", "   ", "x" * 4097):
        with pytest.raises(ValueError):
            seal(public_pem(key), bad)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    ec_pem = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    with pytest.raises(ValueError):
        seal(ec_pem, "code")


# ─── verdict + stored status ─────────────────────────────────────────────────


def _row(logged_in: bool, *, checked: int = 1, changed: int = 60, detail: str | None = None,
         email: str | None = EMAIL) -> AgentLoginStatusRow:
    now = ds.now()
    return AgentLoginStatusRow(provider="claude-sub", account_id="owner", logged_in=logged_in,
                               email=email, detail=detail,
                               checked_at=now - timedelta(minutes=checked),
                               changed_at=now - timedelta(minutes=changed))


def test_verdict_states() -> None:
    now = ds.now()
    assert st.verdict(None, None, None, now)[0] == st.UNKNOWN
    assert st.verdict(_row(True, checked=30), None, None, now)[0] == st.STALE
    assert st.verdict(_row(False, detail="nope"), None, None, now) == (st.LOGGED_OUT, "nope")
    assert st.verdict(_row(True), None, None, now) == (st.OK, "")
    down = now - timedelta(minutes=10)
    assert st.verdict(_row(True), down, None, now)[0] == st.BROKEN
    # a later successful run clears it
    assert st.verdict(_row(True), down, now - timedelta(minutes=5), now)[0] == st.OK
    # a fresh sign-in (changed after the failure) clears it too
    assert st.verdict(_row(True, changed=2), down, None, now)[0] == st.OK


def test_record_status_keeps_mailbox_and_moves_changed_at_only_on_flip() -> None:
    t0 = ds.now() - timedelta(hours=1)
    ds.run(st.record_status("claude-sub", "owner", LoginReport(True, EMAIL, "team"), t0))
    ds.run(st.record_status("claude-sub", "owner", LoginReport(True, EMAIL, "team"),
                            t0 + timedelta(minutes=5)))
    ds.run(st.record_status("claude-sub", "owner", LoginReport(False, detail="out"),
                            t0 + timedelta(minutes=10)))

    async def read() -> AgentLoginStatusRow | None:
        async with get_session() as s:
            return await s.get(AgentLoginStatusRow, ("claude-sub", "owner"))

    row = ds.run(read())
    assert row is not None and row.logged_in is False
    assert row.email == EMAIL and row.plan == "team" and row.detail == "out"
    assert row.changed_at == t0 + timedelta(minutes=10)
    assert row.checked_at == t0 + timedelta(minutes=10)
    assert ds.run(st.known_email("claude-sub", "owner")) == EMAIL
    assert ds.run(st.known_email("claude-sub", "nobody")) is None


def _seed_run(run_id: int, account: str, kind: str, minutes_ago: int,
              payload: dict[str, Any]) -> None:
    ds.seed(ds.project(1, "p"))
    ds.seed(ds.job(run_id, "error" if kind == "run.failed" else "done", 1, minutes_ago,
                   capability="agent:claude"))
    ds.seed(AgentRunRow(run_id=run_id, project_id=1, capability="agent:claude",
                        task_id=f"t{run_id}", account_id=account, billing_mode="subscription"))
    ds.seed(AgentRunEventRow(run_id=run_id, seq=1, ts=ds.now() - timedelta(minutes=minutes_ago),
                             type=kind, payload=payload))


def test_account_logins_lists_every_slot_and_detects_broken_login() -> None:
    now = ds.now()
    ds.run(st.record_status("claude-sub", "owner", LoginReport(True, EMAIL, "team"),
                            now - timedelta(minutes=1)))
    async def backdate() -> None:
        async with get_session() as s:
            row = await s.get(AgentLoginStatusRow, ("claude-sub", "owner"))
            assert row is not None
            row.changed_at = now - timedelta(hours=2)
    ds.run(backdate())
    _seed_run(901, "owner", "run.failed", 5,
              {"error_class": "cli_error", "message": "Failed to refresh OAuth token: 401"})
    logins = {a.key: a for a in ds.run(st.account_logins(now))}
    assert set(logins) == {"claude-sub/claude-2", "claude-sub/owner", "codex-sub/owner"}
    owner = logins["claude-sub/owner"]
    assert owner.state == st.BROKEN and owner.name == "CLD_ZAPO" and owner.app == "Claude Code"
    assert logins["claude-sub/claude-2"].state == st.UNKNOWN
    assert logins["claude-sub/claude-2"].name == "CLD_claude-2"


def test_account_logins_ignores_ordinary_failures() -> None:
    now = ds.now()
    ds.run(st.record_status("claude-sub", "owner", LoginReport(True, EMAIL), now))
    _seed_run(902, "owner", "run.failed", 5, {"error_class": "invalid_json", "message": "x"})
    logins = {a.key: a for a in ds.run(st.account_logins(now))}
    assert logins["claude-sub/owner"].state == st.OK


# ─── session state machine ───────────────────────────────────────────────────


def test_session_lifecycle_and_compare_and_swap() -> None:
    first = ds.run(ls.start_session("claude-sub", "claude-2"))
    second = ds.run(ls.start_session("claude-sub", "claude-2"))
    assert ds.run(ls.get_login(first)).state == ls.CANCELLED
    assert ds.run(ls.latest_session("claude-sub", "claude-2")).id == second
    assert ds.run(ls.submit_code(second, "abc")) is not None      # not awaiting yet
    claimed = ds.run(ls.claim_requested("claude-sub", "claude-2"))
    assert claimed is not None and claimed.id == second
    assert ds.run(ls.claim_requested("claude-sub", "claude-2")) is None
    key = new_key()
    assert ds.run(ls.advance(second, (ls.STARTING,), state=ls.AWAITING,
                             public_key=public_pem(key)))
    assert "empty" in (ds.run(ls.submit_code(second, "  ")) or "")
    assert ds.run(ls.submit_code(second, "the-code")) is None
    row = ds.run(ls.get_login(second))
    assert row.state == ls.CODE_SUBMITTED and row.code_sealed and "the-code" not in row.code_sealed
    sealed = ds.run(ls.take_code(second))
    assert sealed is not None and unseal(key, sealed) == "the-code"
    row = ds.run(ls.get_login(second))
    assert row.state == ls.VERIFYING and row.code_sealed is None
    assert ds.run(ls.take_code(second)) is None
    assert ds.run(ls.finish(second, ls.DONE, "signed in"))
    assert ds.run(ls.cancel_session(second)) is False
    assert ds.run(ls.get_login(second)).public_key is None


def test_expire_orphans_only_touches_running_sessions() -> None:
    waiting = ds.run(ls.start_session("codex-sub", "owner"))
    ds.run(ls.claim_requested("codex-sub", "owner"))
    assert ds.run(ls.expire_orphans("codex-sub", "owner")) == 1
    assert ds.run(ls.get_login(waiting)).state == ls.EXPIRED
    fresh = ds.run(ls.start_session("codex-sub", "owner"))
    assert ds.run(ls.expire_orphans("codex-sub", "owner")) == 0
    assert ds.run(ls.get_login(fresh)).state == ls.REQUESTED


# ─── one-shot alerts ─────────────────────────────────────────────────────────


def test_alerts_fire_once_per_transition(tmp_path, monkeypatch) -> None:
    sent: list[str] = []

    async def fake_post(text: str) -> None:
        sent.append(text)

    monkeypatch.setattr(notifier, "STATE_DIR", tmp_path)
    monkeypatch.setattr(notifier, "_post", fake_post)
    now = ds.now()
    ds.run(st.record_status("claude-sub", "claude-2",
                            LoginReport(True, "demoniwwwe@gmail.com", "max"), now))
    ds.run(st.record_status("claude-sub", "claude-2", LoginReport(False, detail="out"), now))
    ds.run(login_alerts.check_subscription_logins(now))
    ds.run(login_alerts.check_subscription_logins(now))
    assert len(sent) == 1
    assert "Подписка отвалилась: <b>CLD_DEMO</b>" in sent[0]
    assert "demoniwwwe@gmail.com" in sent[0] and "Claude Code" in sent[0]
    assert "/dashboard/subscriptions" in sent[0] and "не залогинена" in sent[0]
    ds.run(st.record_status("claude-sub", "claude-2", LoginReport(True, "demoniwwwe@gmail.com"),
                            now))
    ds.run(login_alerts.check_subscription_logins(now))
    ds.run(login_alerts.check_subscription_logins(now))
    assert len(sent) == 2 and "Подписка снова работает: <b>CLD_DEMO</b>" in sent[1]


def test_never_reported_login_sends_nothing(tmp_path, monkeypatch) -> None:
    sent: list[str] = []

    async def fake_post(text: str) -> None:
        sent.append(text)

    monkeypatch.setattr(notifier, "STATE_DIR", tmp_path)
    monkeypatch.setattr(notifier, "_post", fake_post)
    ds.run(login_alerts.check_subscription_logins())
    assert sent == []


# ─── dashboard ───────────────────────────────────────────────────────────────


def test_page_lists_accounts_and_starts_a_sign_in() -> None:
    ds.run(st.record_status("claude-sub", "owner", LoginReport(True, EMAIL, "team")))
    page = ds.get(client, "/dashboard/subscriptions")
    assert page.status_code == 200
    body = page.text
    assert "CLD_ZAPO" in body and "CLD_claude-2" in body and EMAIL in body
    r = client.post("/dashboard/subscriptions/claude-sub/claude-2/login", cookies=ds.cookies(),
                    follow_redirects=False)
    assert r.status_code == 303 and "#acc-claude-sub-claude-2" in r.headers["location"]
    session = ds.run(ls.latest_session("claude-sub", "claude-2"))
    assert session is not None and session.state == ls.REQUESTED
    panel = ds.get(client, "/dashboard/subscriptions/claude-sub/claude-2/login")
    assert panel.status_code == 200 and 'hx-trigger="every 2s"' in panel.text
    assert ds.get(client, "/dashboard/subscriptions/x/y/login").status_code == 404


def test_page_requires_owner_and_rejects_unknown_account() -> None:
    assert client.get("/dashboard/subscriptions", follow_redirects=False).status_code == 303
    r = client.post("/dashboard/subscriptions/claude-sub/ghost/login", cookies=ds.cookies(),
                    follow_redirects=False)
    assert "Unknown" in r.headers["location"]


def test_code_box_does_not_poll_and_code_is_sealed() -> None:
    sid = ds.run(ls.start_session("claude-sub", "claude-2"))
    ds.run(ls.claim_requested("claude-sub", "claude-2"))
    key = new_key()
    ds.run(ls.advance(sid, (ls.STARTING,), state=ls.AWAITING, public_key=public_pem(key),
                      verify_url="https://claude.com/cai/oauth/authorize?x=1"))
    panel = ds.get(client, "/dashboard/subscriptions/claude-sub/claude-2/login").text
    assert "hx-trigger" not in panel and 'name="code"' in panel
    r = client.post(f"/dashboard/subscriptions/login/{sid}/code", data={"code": "secret-1"},
                    cookies=ds.cookies(), follow_redirects=False)
    assert r.status_code == 303 and "flash=Code" in r.headers["location"]
    row = ds.run(ls.get_login(sid))
    assert row.state == ls.CODE_SUBMITTED and unseal(key, row.code_sealed) == "secret-1"
    again = client.post(f"/dashboard/subscriptions/login/{sid}/code", data={"code": "x"},
                        cookies=ds.cookies(), follow_redirects=False)
    assert "flash=%21" in again.headers["location"]
    c = client.post(f"/dashboard/subscriptions/login/{sid}/cancel", cookies=ds.cookies(),
                    follow_redirects=False)
    assert "cancelled" in c.headers["location"]
    assert ds.run(ls.get_login(sid)).state == ls.CANCELLED
    c2 = client.post(f"/dashboard/subscriptions/login/{sid}/cancel", cookies=ds.cookies(),
                     follow_redirects=False)
    assert "already" in c2.headers["location"]


def test_codex_paste_box_and_plain_placeholder() -> None:
    sid = ds.run(ls.start_session("codex-sub", "owner"))
    ds.run(ls.claim_requested("codex-sub", "owner"))
    ds.run(ls.advance(sid, (ls.STARTING,), state=ls.AWAITING, public_key=public_pem(new_key()),
                      verify_url="https://auth.openai.com/oauth/authorize?x=1"))
    panel = ds.get(client, "/dashboard/subscriptions/codex-sub/owner/login").text
    assert "hx-trigger" not in panel and 'name="code"' in panel and "127.0.0.1:1455" in panel
    # prod 2026-11-11: t() markup leaked into the attribute ("<span data-i18n ...")
    assert 'placeholder="<' not in panel and 'placeholder="&lt;' not in panel
    ds.run(ls.finish(sid, ls.FAILED, "sign-in did not complete"))
    done = ds.get(client, "/dashboard/subscriptions/codex-sub/owner/login").text
    assert "hx-trigger" not in done and "sign-in did not complete" in done


# ─── worker: probe and login flow against fake CLIs ──────────────────────────


def _fake_exec(monkeypatch: pytest.MonkeyPatch, module: Any, script: str) -> list[list[str]]:
    seen: list[list[str]] = []

    async def fake(*argv: str, **kw: Any) -> asyncio.subprocess.Process:
        seen.append(list(argv))
        kw.pop("start_new_session", None)
        kw["cwd"] = None
        return await _REAL_EXEC(sys.executable, "-c", script, *argv[1:], **kw)

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", fake)
    return seen


def test_probe_claude_and_codex_with_fake_cli(monkeypatch) -> None:
    _fake_exec(monkeypatch, login_probe, "import json;print(json.dumps({'loggedIn': True, "
               "'authMethod': 'claude.ai', 'email': 'a@b.c', 'subscriptionType': 'max'}))")
    assert ds.run(login_probe.probe("claude-sub")) == LoginReport(True, "a@b.c", "max", None)
    codex = ("import sys,json\n"
             "for line in sys.stdin:\n"
             "    m = json.loads(line)\n"
             "    if m.get('id') == 1: print(json.dumps({'id': 1, 'result': {}}), flush=True)\n"
             "    if m.get('id') == 2:\n"
             "        print(json.dumps({'method': 'note', 'params': {}}), flush=True)\n"
             "        print(json.dumps({'id': 2, 'result': {'account': {'type': 'chatgpt', "
             "'email': 'g@p.t', 'planType': 'pro'}}}), flush=True)\n")
    seen = _fake_exec(monkeypatch, login_probe, codex)
    assert ds.run(login_probe.probe("codex-sub")) == LoginReport(True, "g@p.t", "pro", None)
    assert seen[0][:2] == ["codex", "app-server"]
    assert 'cli_auth_credentials_store="file"' in seen[0]


def test_probe_codex_exits_without_answer(monkeypatch) -> None:
    _fake_exec(monkeypatch, login_probe, "import sys")
    assert "no account/read answer" in (ds.run(login_probe.probe("codex-sub")).detail or "")


def test_probes_time_out(monkeypatch) -> None:
    monkeypatch.setattr(login_probe, "PROBE_TIMEOUT_S", 1.0)
    _fake_exec(monkeypatch, login_probe, "import time;time.sleep(30)")
    codex = ds.run(login_probe.probe("codex-sub"))
    claude = ds.run(login_probe.probe("claude-sub"))
    assert codex.failed and "timed out" in (codex.detail or "")
    assert claude.failed and "timed out" in (claude.detail or "")


_CLAUDE_LOGIN = ("import sys\n"
                 "print('If the browser didn\\'t open, visit: https://claude.com/cai/oauth/"
                 "authorize?code=true', flush=True)\n"
                 "sys.stdout.write('Paste code here if prompted > '); sys.stdout.flush()\n"
                 "code = sys.stdin.readline().strip()\n"
                 "if code != 'good-code':\n"
                 "    print('Invalid code ' + code); sys.exit(1)\n"
                 "print('Login successful')\n")


async def _drive(provider: str, account: str, code: str | None,
                 report: LoginReport) -> tuple[LoginReport, AgentLoginSessionRow]:
    sid = await ls.start_session(provider, account)
    await ls.claim_requested(provider, account)
    task = asyncio.create_task(login_flow.run_login(provider, account, sid, "a@b.c"))
    for _ in range(200):
        row = await ls.get_login(sid)
        if row is not None and row.state == ls.AWAITING:
            break
        await asyncio.sleep(0.05)
    if code is not None:
        assert await ls.submit_code(sid, code) is None
    result = await asyncio.wait_for(task, 30)
    final = await ls.get_login(sid)
    assert final is not None
    return result, final


def test_claude_login_flow_success(monkeypatch) -> None:
    seen = _fake_exec(monkeypatch, login_flow, _CLAUDE_LOGIN)
    monkeypatch.setattr(login_flow, "POLL_S", 0.05)

    async def ok_probe(provider: str) -> LoginReport:
        return LoginReport(True, "a@b.c", "max")

    monkeypatch.setattr(login_flow, "probe", ok_probe)
    report, row = ds.run(_drive("claude-sub", "claude-2", "good-code", LoginReport(True)))
    assert report.logged_in and row.state == ls.DONE and "a@b.c" in (row.message or "")
    assert row.code_sealed is None and row.public_key is None
    assert seen[0] == ["claude", "auth", "login", "--claudeai", "--email", "a@b.c"]


def test_claude_login_flow_refused_code_is_scrubbed(monkeypatch) -> None:
    _fake_exec(monkeypatch, login_flow, _CLAUDE_LOGIN)
    monkeypatch.setattr(login_flow, "POLL_S", 0.05)

    async def out_probe(provider: str) -> LoginReport:
        return LoginReport(False, detail="not logged in")

    monkeypatch.setattr(login_flow, "probe", out_probe)
    _, row = ds.run(_drive("claude-sub", "claude-2", "bad-code-xyz", LoginReport(False)))
    assert row.state == ls.FAILED and "refused" in (row.message or "")
    assert "bad-code-xyz" not in (row.message or "") and "***" in (row.message or "")


# Fake `codex login`: prints the link, waits for the replayed callback (a marker file the fake
# replay writes), then writes auth.json into ITS CODEX_HOME and exits 0.
_CODEX_LOGIN = ("import os, sys, time, pathlib\n"
                "print('Starting local login server on http://localhost:1455.', flush=True)\n"
                "print('https://auth.openai.com/oauth/authorize?response_type=code&x=1', flush=True)\n"
                "home = pathlib.Path(os.environ['CODEX_HOME'])\n"
                "for _ in range(400):\n"
                "    if (home / 'callback-hit').exists():\n"
                "        (home / 'auth.json').write_text('{\"new\": true}')\n"
                "        print('Successfully logged in'); sys.exit(0)\n"
                "    time.sleep(0.05)\n"
                "sys.exit(3)\n")


def _codex_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, *, hit: bool = True) -> Any:
    live = tmp_path / "codex-state"
    live.mkdir()
    (live / "auth.json").write_text('{"old": true}')
    monkeypatch.setenv("CODEX_HOME", str(live))
    seen = _fake_exec(monkeypatch, login_flow, _CODEX_LOGIN)
    monkeypatch.setattr(login_flow, "POLL_S", 0.05)
    replayed: list[str] = []

    async def fake_replay(url: str) -> str | None:
        replayed.append(url)
        if hit:
            for staging in live.glob(".login-*"):
                (staging / "callback-hit").write_text("1")
        return None

    monkeypatch.setattr(login_flow, "_replay_callback", fake_replay)

    async def ok_probe(provider: str) -> LoginReport:
        return LoginReport(True, "g@p.t", "pro")

    monkeypatch.setattr(login_flow, "probe", ok_probe)
    return live, seen, replayed


def test_codex_login_installs_credentials_only_after_success(monkeypatch, tmp_path) -> None:
    live, seen, replayed = _codex_env(monkeypatch, tmp_path)
    report, row = ds.run(_drive("codex-sub", "owner",
                                "http://localhost:1455/auth/callback?code=ac_9&state=z",
                                LoginReport(True)))
    assert report.logged_in and row.state == ls.DONE
    assert seen[0][:2] == ["codex", "login"] and "--device-auth" not in seen[0]
    assert replayed == ["http://127.0.0.1:1455/auth/callback?code=ac_9&state=z"]
    assert (live / "auth.json").read_text() == '{"new": true}'
    assert not list(live.glob(".login-*"))          # staging home removed


def test_cancelled_codex_login_keeps_the_working_credentials(monkeypatch, tmp_path) -> None:
    """prod 2026-11-11 16:48: a cancelled re-login ran in the live CODEX_HOME and deleted the
    working auth.json. The staging home must make that impossible."""
    live, _, _ = _codex_env(monkeypatch, tmp_path)

    async def go() -> AgentLoginSessionRow:
        sid = await ls.start_session("codex-sub", "owner")
        await ls.claim_requested("codex-sub", "owner")
        task = asyncio.create_task(login_flow.run_login("codex-sub", "owner", sid, None))
        for _ in range(200):
            row = await ls.get_login(sid)
            if row is not None and row.state == ls.AWAITING:
                break
            await asyncio.sleep(0.05)
        await ls.cancel_session(sid)
        await asyncio.wait_for(task, 30)
        row = await ls.get_login(sid)
        assert row is not None
        return row

    assert ds.run(go()).state == ls.CANCELLED
    assert (live / "auth.json").read_text() == '{"old": true}'
    assert not list(live.glob(".login-*"))


def test_codex_login_rejects_a_foreign_address(monkeypatch, tmp_path) -> None:
    live, _, replayed = _codex_env(monkeypatch, tmp_path)

    async def out_probe(provider: str) -> LoginReport:
        return LoginReport(False, detail="no")

    monkeypatch.setattr(login_flow, "probe", out_probe)
    _, row = ds.run(_drive("codex-sub", "owner", "http://evil.com:1455/auth/callback?code=1",
                           LoginReport(False)))
    assert row.state == ls.FAILED and "127.0.0.1:1455" in (row.message or "")
    assert replayed == [] and (live / "auth.json").read_text() == '{"old": true}'


def test_codex_login_needs_codex_home(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert "CODEX_HOME" in (ds.run(login_flow._codex(1)) or "")


def test_replay_callback_reports_an_unreachable_listener(monkeypatch) -> None:
    monkeypatch.setattr(login_flow, "CALLBACK_TIMEOUT_S", 2.0)
    problem = ds.run(login_flow._replay_callback("http://127.0.0.1:9/auth/callback?code=1"))
    assert problem and "did not answer" in problem


def test_install_requires_written_credentials(tmp_path) -> None:
    staging, live = tmp_path / "s", tmp_path / "l"
    staging.mkdir()
    live.mkdir()
    assert "no credentials" in (login_flow._install(staging, live) or "")
    (staging / "auth.json").write_text("{}")
    assert login_flow._install(staging, live) is None and (live / "auth.json").exists()


def test_failed_probe_is_not_stored_as_logged_out() -> None:
    ds.run(st.record_status("codex-sub", "owner", LoginReport(True, "g@p.t", "pro")))
    ds.run(st.record_status("codex-sub", "owner",
                            LoginReport(False, detail="timed out", failed=True)))

    async def read() -> AgentLoginStatusRow | None:
        async with get_session() as s:
            return await s.get(AgentLoginStatusRow, ("codex-sub", "owner"))

    row = ds.run(read())
    assert row is not None and row.logged_in is True


def test_success_with_unconfirmed_probe_still_counts_as_signed_in(monkeypatch, tmp_path) -> None:
    _codex_env(monkeypatch, tmp_path)

    async def failed_probe(provider: str) -> LoginReport:
        return LoginReport(False, detail="timed out", failed=True)

    monkeypatch.setattr(login_flow, "probe", failed_probe)
    _, row = ds.run(_drive("codex-sub", "owner",
                           "http://127.0.0.1:1455/auth/callback?code=ac_1", LoginReport(False)))
    assert row.state == ls.DONE and "could not confirm" in (row.message or "")


def test_login_flow_without_link_fails_and_cancel_wins(monkeypatch) -> None:
    _fake_exec(monkeypatch, login_flow, "print('no link here')")
    monkeypatch.setattr(login_flow, "LINK_WAIT_S", 2.0)

    async def out_probe(provider: str) -> LoginReport:
        return LoginReport(False, detail="nope")

    monkeypatch.setattr(login_flow, "probe", out_probe)

    async def go() -> AgentLoginSessionRow:
        sid = await ls.start_session("claude-sub", "claude-2")
        await ls.claim_requested("claude-sub", "claude-2")
        await login_flow.run_login("claude-sub", "claude-2", sid, None)
        row = await ls.get_login(sid)
        assert row is not None
        return row

    row = ds.run(go())
    assert row.state == ls.FAILED and "no sign-in link" in (row.message or "")


def test_claude_login_cancelled_while_waiting_for_code(monkeypatch) -> None:
    _fake_exec(monkeypatch, login_flow, _CLAUDE_LOGIN)
    monkeypatch.setattr(login_flow, "POLL_S", 0.05)

    async def out_probe(provider: str) -> LoginReport:
        return LoginReport(False)

    monkeypatch.setattr(login_flow, "probe", out_probe)

    async def go() -> AgentLoginSessionRow:
        sid = await ls.start_session("claude-sub", "claude-2")
        await ls.claim_requested("claude-sub", "claude-2")
        task = asyncio.create_task(login_flow.run_login("claude-sub", "claude-2", sid, None))
        for _ in range(200):
            row = await ls.get_login(sid)
            if row is not None and row.state == ls.AWAITING:
                break
            await asyncio.sleep(0.05)
        await ls.cancel_session(sid)
        await asyncio.wait_for(task, 30)
        row = await ls.get_login(sid)
        assert row is not None
        return row

    assert ds.run(go()).state == ls.CANCELLED


def test_worker_identity_and_login_step(monkeypatch) -> None:
    monkeypatch.setattr(login_worker.get_settings(), "AGENT_ACCOUNT_ID", "")
    assert login_worker.worker_login_identity(("agent:codex",)) == ("codex-sub", "owner")
    assert login_worker.worker_login_identity(("agent:claude", "agent:codex")) is None
    calls: list[str] = []

    async def fake_probe(provider: str) -> LoginReport:
        calls.append("probe")
        return LoginReport(True, "g@p.t")

    async def fake_run(provider: str, account: str, sid: int, email: str | None) -> LoginReport:
        calls.append(f"login:{sid}:{email}")
        return LoginReport(True)

    monkeypatch.setattr(login_worker, "probe", fake_probe)
    monkeypatch.setattr(login_worker, "run_login", fake_run)
    assert ds.run(login_worker.login_step("codex-sub", "owner", False)) is False
    assert ds.run(login_worker.login_step("codex-sub", "owner", True)) is True
    sid = ds.run(ls.start_session("codex-sub", "owner"))
    assert ds.run(login_worker.login_step("codex-sub", "owner", False)) is True
    assert calls == ["probe", f"login:{sid}:g@p.t"]


def test_login_loop_probes_then_stops(monkeypatch) -> None:
    monkeypatch.setattr(login_worker.get_settings(), "AGENT_ACCOUNT_ID", "claude-2")
    seen: list[bool] = []

    async def fake_step(provider: str, account: str, due: bool) -> bool:
        seen.append(due)
        if len(seen) == 2:
            raise RuntimeError("boom")      # one bad pass must not end the loop
        return due

    monkeypatch.setattr(login_worker, "login_step", fake_step)
    monkeypatch.setattr(login_worker, "SESSION_POLL_S", 0.01)

    async def go() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(login_worker.login_loop(stop, ("agent:claude",)))
        for _ in range(100):
            if len(seen) >= 3:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 5)

    ds.run(go())
    assert seen[0] is True and len(seen) >= 3
    ds.run(login_worker.login_loop(asyncio.Event(), ("agent:claude", "agent:codex")))


async def _local_listener(redirect_to: str) -> tuple[asyncio.AbstractServer, int, list[str]]:
    """A tiny stand-in for the codex login server: /auth/callback -> 302, everything else 200."""
    paths: list[str] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = (await reader.readline()).decode()
        while (await reader.readline()) not in (b"\r\n", b""):
            pass
        path = line.split(" ")[1]
        paths.append(path.split("?")[0])
        if path.startswith("/auth/callback"):
            head = f"HTTP/1.1 302 Found\r\nLocation: {redirect_to}\r\nContent-Length: 0\r\n"
        else:
            head = "HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
        writer.write((head + "Connection: close\r\n\r\n" + ("" if "302" in head else "ok")).encode())
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], paths


def test_replay_follows_the_success_redirect_on_the_same_listener() -> None:
    async def go() -> list[str]:
        server, port, paths = await _local_listener("/success?ok=1")
        async with server:
            assert await login_flow._replay_callback(
                f"http://127.0.0.1:{port}/auth/callback?code=1") is None
        return paths

    assert ds.run(go()) == ["/auth/callback", "/success"]


def test_replay_never_follows_a_redirect_off_the_listener() -> None:
    async def go() -> list[str]:
        server, port, paths = await _local_listener("http://example.invalid/steal")
        async with server:
            assert await login_flow._replay_callback(
                f"http://127.0.0.1:{port}/auth/callback?code=1") is None
        return paths

    assert ds.run(go()) == ["/auth/callback"]


_CODEX_LOGIN_HANGS = _CODEX_LOGIN.replace(
    "        print('Successfully logged in'); sys.exit(0)\n",
    "        time.sleep(3600)\n")


def test_written_credentials_count_even_if_the_cli_never_exits(monkeypatch, tmp_path) -> None:
    """prod 2026-11-11 session 4: the CLI saved auth.json, answered 302 and waited for
    /success; the sign-in was then thrown away. Credentials on disk must win."""
    live, _, _ = _codex_env(monkeypatch, tmp_path)
    _fake_exec(monkeypatch, login_flow, _CODEX_LOGIN_HANGS)
    monkeypatch.setattr(login_flow, "SETTLE_S", 0.05)
    report, row = ds.run(_drive("codex-sub", "owner",
                                "http://127.0.0.1:1455/auth/callback?code=ac_1",
                                LoginReport(True)))
    assert row.state == ls.DONE
    assert (live / "auth.json").read_text() == '{"new": true}'
    assert not list(live.glob(".login-*"))


def test_cli_that_neither_exits_nor_writes_fails_with_a_clear_reason(monkeypatch, tmp_path) -> None:
    live, _, _ = _codex_env(monkeypatch, tmp_path, hit=False)
    monkeypatch.setattr(login_flow, "EXIT_WAIT_S", 0.5)

    async def out_probe(provider: str) -> LoginReport:
        return LoginReport(False, detail="no")

    monkeypatch.setattr(login_flow, "probe", out_probe)
    _, row = ds.run(_drive("codex-sub", "owner",
                           "http://127.0.0.1:1455/auth/callback?code=1", LoginReport(False)))
    assert row.state == ls.FAILED and "did not finish after the sign-in" in (row.message or "")
    assert (live / "auth.json").read_text() == '{"old": true}'
